"""Local paper execution service.

Owns the paper account, virtual orders, simulated fills, reservations, netting and
settlement. It performs NO network I/O and has no exchange client: it only consumes
validated market-data snapshots handed to it by the orchestrator. There is no code path
from here to any exchange order endpoint.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from ..db import tx
from ..marketdata.models import MarketInfo, OrderBook
from ..money import D, s
from ..schedule import Clock, iso, parse_ts
from ..settings_store import ensure_defaults, load_settings
from . import accounting as acc
from .fees import FeeSchedule
from .fill_model import Gates, OrderView, Trigger, step

log = logging.getLogger(__name__)


class PaperExecutionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        *,
        execution_delay: float,
        stale_after: float,
        market_status_stale: float,
        exchange_status_stale: float,
        pair_accounting: str,
        settlement_delay_warning: float = 3600.0,
    ):
        self.conn = conn
        self.clock = clock
        self.execution_delay = execution_delay
        self.stale_after = stale_after
        self.market_status_stale = market_status_stale
        self.exchange_status_stale = exchange_status_stale
        self.pair_accounting = pair_accounting
        self.settlement_delay_warning = settlement_delay_warning
        self.triggers: dict[str, Trigger] = {}
        self.order_gate: dict[str, str] = {}

    # ------------------------------------------------------------------ setup
    def ensure_initialized(self, starting_balance: Decimal) -> int:
        now = self.clock.now()
        ensure_defaults(self.conn, now, starting_balance)
        with tx(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO engine_state(id, trading_enabled, updated_at) VALUES(1, 0, ?)", (iso(now),)
            )
            settings = load_settings(self.conn)
            account_id = acc.ensure_account(self.conn, now, settings.starting_balance)
        return account_id

    @property
    def account_id(self) -> int:
        row = acc.current_account(self.conn)
        if row is None:
            raise RuntimeError("paper account not initialized")
        return int(row["id"])

    def engine_state(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM engine_state WHERE id=1").fetchone()

    def log_event(self, level: str, kind: str, message: str, window_id: str | None = None,
                  dedupe_key: str | None = None) -> None:
        now = self.clock.now()
        with tx(self.conn):
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events(ts, level, kind, message, window_id, dedupe_key) VALUES(?, ?, ?, ?, ?, ?)",
                (iso(now), level, kind, message, window_id, dedupe_key),
            )
        if cur.rowcount:
            getattr(log, "warning" if level in ("warn", "error") else "info")("[%s] %s", kind, message)

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        """Start or resume: new entries only from the next window that STARTS after now."""
        now = self.clock.now()
        with tx(self.conn):
            self.conn.execute(
                "UPDATE engine_state SET trading_enabled=1, armed_at=?, paused_at=NULL, updated_at=? WHERE id=1",
                (iso(now), iso(now)),
            )
        self.log_event("info", "control", "Trading started/resumed; waiting for the next eligible window to begin")

    def rearm_after_restart(self) -> None:
        """After a process restart, a running engine waits for the next eligible window."""
        now = self.clock.now()
        with tx(self.conn):
            row = self.engine_state()
            if row["trading_enabled"]:
                self.conn.execute("UPDATE engine_state SET armed_at=?, updated_at=? WHERE id=1", (iso(now), iso(now)))

    def pause(self, reason: str = "paused by user") -> int:
        """Cancel remaining virtual orders and block new entries; settlement tracking continues."""
        now = self.clock.now()
        canceled = 0
        with tx(self.conn):
            self.conn.execute(
                "UPDATE engine_state SET trading_enabled=0, paused_at=?, updated_at=? WHERE id=1", (iso(now), iso(now))
            )
            for w in self.conn.execute(
                "SELECT id FROM windows WHERE account_id=? AND status='active'", (self.account_id,)
            ).fetchall():
                canceled += acc.close_window_orders(self.conn, w["id"], now, "canceled", reason)
            acc.snapshot_equity(self.conn, self.account_id, now, "pause")
        self.triggers.clear()
        self.log_event("info", "control", f"Trading paused ({reason}); {canceled} working order(s) canceled")
        return canceled

    def reset_account(self) -> int:
        """Archive the current account and create a new one with the configured starting balance."""
        now = self.clock.now()
        with tx(self.conn):
            old = self.account_id
            for w in self.conn.execute("SELECT id FROM windows WHERE account_id=? AND status='active'", (old,)).fetchall():
                acc.close_window_orders(self.conn, w["id"], now, "canceled", "account reset")
            self.conn.execute(
                "UPDATE windows SET status='abandoned', settlement_note='account reset before settlement', updated_at=? "
                "WHERE account_id=? AND status='awaiting_settlement'",
                (iso(now), old),
            )
            self.conn.execute("UPDATE accounts SET status='archived', archived_at=? WHERE id=?", (iso(now), old))
            settings = load_settings(self.conn)
            new_id = acc.create_account(self.conn, now, settings.starting_balance)
            self.conn.execute(
                "UPDATE engine_state SET trading_enabled=0, armed_at=NULL, paused_at=NULL, updated_at=? WHERE id=1",
                (iso(now),),
            )
            acc.snapshot_equity(self.conn, new_id, now, "reset")
        self.triggers.clear()
        self.log_event("warn", "account", f"Paper account reset: account {old} archived, new account {new_id} "
                                          f"created with ${s(settings.starting_balance)}")
        return new_id

    # ------------------------------------------------------------------ windows
    def window_row(self, window_start: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM windows WHERE account_id=? AND window_start=?", (self.account_id, iso(window_start))
        ).fetchone()

    def active_windows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM windows WHERE account_id=? AND status='active' ORDER BY window_start", (self.account_id,)
        ).fetchall()

    def awaiting_windows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM windows WHERE account_id=? AND status='awaiting_settlement' ORDER BY window_start",
            (self.account_id,),
        ).fetchall()

    def skip(self, window_start: datetime, window_end: datetime, eligible: bool, reason: str,
             market_ticker: str | None = None) -> bool:
        now = self.clock.now()
        with tx(self.conn):
            created = acc.record_skip(self.conn, self.account_id, window_start, window_end, eligible, reason, now,
                                      market_ticker)
        if created:
            self.log_event("info" if not eligible else "warn", "skip",
                           f"Window {window_start:%H:%M}Z skipped: {reason}",
                           acc.window_id_for(self.account_id, window_start))
        return created

    def try_enter(self, plan: acc.EntryPlan) -> tuple[str, str]:
        """Create both virtual orders atomically. Returns (outcome, message)."""
        now = self.clock.now()
        try:
            with tx(self.conn):
                eng = self.engine_state()
                armed = parse_ts(eng["armed_at"])
                if not eng["trading_enabled"] or armed is None or armed > plan.window_start:
                    return "blocked", "trading not enabled for this window"
                if self.conn.execute(
                    "SELECT 1 FROM windows WHERE account_id=? AND window_start=?", (plan.account_id, iso(plan.window_start))
                ).fetchone():
                    return "exists", "window already recorded"
                acc.enter_window(self.conn, plan, now)
                acc.snapshot_equity(self.conn, plan.account_id, now, "entry")
        except acc.InsufficientFunds as exc:
            self.skip(plan.window_start, plan.window_end, True, str(exc), plan.market_ticker)
            return "insufficient", str(exc)
        except sqlite3.IntegrityError:
            return "exists", "duplicate entry prevented by database constraint"
        principal, fee = acc.required_reservation(plan)
        self.log_event(
            "info", "entry",
            f"Entered {plan.market_ticker}: BUY {s(plan.quantity)} UP (YES) and {s(plan.quantity)} DOWN (NO) "
            f"limit ${s(plan.limit_price)}; reserved ${s((principal + fee) * 2)} incl. est. fees",
            plan.window_id,
        )
        return "entered", "orders created"

    def orders_for_window(self, window_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders WHERE window_id=? ORDER BY side DESC", (window_id,)).fetchall()

    # ------------------------------------------------------------------ execution
    def confirmation_due_at(self) -> datetime | None:
        if not self.triggers:
            return None
        return min(t.book.requested_at for t in self.triggers.values()) + timedelta(seconds=self.execution_delay)

    def process_book(
        self,
        window: sqlite3.Row,
        book: OrderBook,
        book_problems: list[str],
        market_status: str | None,
        market_status_at: datetime | None,
        trading_active: bool | None,
        exchange_status_at: datetime | None,
        fee_waiver_until: datetime | None = None,
    ) -> list[dict]:
        now = self.clock.now()
        close_time = parse_ts(window["market_close_time"])
        assert close_time is not None
        fee_schedule = _fee_schedule_from_json(window["fee_schedule_json"])
        fee_mode = json.loads(window["settings_json"]).get("fee_mode", "taker")
        engine_trading = bool(self.engine_state()["trading_enabled"])
        fills: list[dict] = []
        events: list[tuple[str, str, str]] = []
        with tx(self.conn):
            if not book_problems:
                self._update_marks(window["id"], book, now)
            orders = self.conn.execute(
                "SELECT * FROM orders WHERE window_id=? AND status IN ('pending','partially_filled')", (window["id"],)
            ).fetchall()
            for o in orders:
                view = OrderView(
                    id=o["id"], side=o["side"], kalshi_side=o["kalshi_side"], limit_price=D(o["limit_price"]),
                    quantity=D(o["quantity"]), filled_qty=D(o["filled_qty"]), submitted_at=parse_ts(o["submitted_at"]),
                    status=o["status"],
                )
                gates = Gates(
                    now=now, close_time=close_time, book_problems=tuple(book_problems), market_status=market_status,
                    market_status_at=market_status_at, trading_active=trading_active,
                    exchange_status_at=exchange_status_at, stale_after=self.stale_after,
                    market_status_stale=self.market_status_stale, exchange_status_stale=self.exchange_status_stale,
                    engine_trading=engine_trading,
                )
                res = step(view, book, gates, acc.consumed_by_level(self.conn, o["id"]), self.triggers.get(o["id"]),
                           self.execution_delay)
                if res.trigger is not None:
                    self.triggers[o["id"]] = res.trigger
                else:
                    self.triggers.pop(o["id"], None)
                self.order_gate[o["id"]] = res.blocked or ("revalidating executable liquidity" if res.trigger else "working")
                if res.event:
                    events.append(("info", f"{o['side']} order: {res.event}", window["id"]))
                if res.legs:
                    obs_id = acc.store_observation(self.conn, book.to_json(), market_status)
                    trig_id = acc.store_observation(self.conn, res.trigger_book.to_json(), market_status) if res.trigger_book else None
                    for leg in res.legs:
                        rec = acc.apply_fill(self.conn, o["id"], leg, obs_id, trig_id, now, fee_schedule, fee_mode,
                                             fee_waiver_until, res.detail)
                        if rec:
                            fills.append(rec)
            if fills:
                acc.snapshot_equity(self.conn, self.account_id, now, "fill")
        for level, message, wid in events:
            self.log_event(level, "execution", message, wid)
        for f in fills:
            self.log_event("info", "fill", f"SIMULATED fill: {f['quantity']} {f['side']} @ ${f['price']} "
                                           f"(est. fee ${f['fee']})", window["id"])
        return fills

    def _update_marks(self, window_id: str, book: OrderBook, now: datetime) -> None:
        up = book.best_bid("yes")
        down = book.best_bid("no")
        self.conn.execute(
            "UPDATE windows SET mark_up=?, mark_down=?, mark_source='best bid (order book)', mark_ts=? WHERE id=?",
            (s(up.price) if up else "0", s(down.price) if down else "0", iso(now), window_id),
        )

    def close_window(self, window_id: str, status: str, reason: str) -> int:
        now = self.clock.now()
        with tx(self.conn):
            n = acc.close_window_orders(self.conn, window_id, now, status, reason)
            acc.snapshot_equity(self.conn, self.account_id, now, "orders closed")
        for oid in [k for k in self.triggers if k.startswith(window_id + ":")]:
            self.triggers.pop(oid, None)
        w = self.conn.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
        self.log_event("info", "close", f"Orders for {w['market_ticker']} closed ({reason}); {n} unfilled remainder(s) "
                                        f"{status}; outcome: {w['outcome_class']}", window_id)
        return n

    # ------------------------------------------------------------------ settlement
    def settle(self, window: sqlite3.Row, market: MarketInfo) -> dict | str:
        """Apply settlement once if Kalshi reports a final result; otherwise record why not."""
        now = self.clock.now()
        payouts = acc.settlement_payouts(market.status, market.result, market.settlement_value)
        if isinstance(payouts, str):
            note = payouts
            close = parse_ts(window["market_close_time"])
            if close and (now - close).total_seconds() > self.settlement_delay_warning:
                mins = int((now - close).total_seconds() // 60)
                note = f"SETTLEMENT DELAYED ({mins} min after close): {payouts}"
                self.log_event("warn", "settlement", f"{window['market_ticker']}: {note}", window["id"],
                               dedupe_key=f"delay:{window['id']}:{mins // 60}")
            with tx(self.conn):
                params: list = [market.status, market.result or None, note, iso(now)]
                sql = "UPDATE windows SET market_status=?, market_result=?, settlement_note=?, last_settlement_check=?"
                if market.status == "determined" and market.result in ("yes", "no"):
                    sql += ", mark_up=?, mark_down=?, mark_source='determined result (awaiting finalization)', mark_ts=?"
                    params += ["1" if market.result == "yes" else "0", "0" if market.result == "yes" else "1", iso(now)]
                self.conn.execute(sql + " WHERE id=?", (*params, window["id"]))
            return note
        yes_payout, no_payout = payouts
        with tx(self.conn):
            rec = acc.apply_settlement(self.conn, window["id"], market.status, market.result or "value", yes_payout,
                                       no_payout, market.settlement_ts, market.raw, now)
            if rec:
                acc.snapshot_equity(self.conn, self.account_id, now, "settlement")
        if rec:
            self.log_event("info", "settlement",
                           f"{window['market_ticker']} settled {market.result.upper() or 'VALUE'}: payout ${rec['payout']}, "
                           f"window net P&L ${rec['net_pnl']}", window["id"])
            return rec
        return "already settled"

    # ------------------------------------------------------------------ recovery
    def recover(self) -> None:
        """Close windows whose market closed while the engine was offline. No fills are invented."""
        now = self.clock.now()
        for w in self.active_windows():
            close = parse_ts(w["market_close_time"])
            if close is not None and now >= close:
                self.close_window(w["id"], "expired",
                                  "market closed while engine was offline; no fills simulated for the downtime")
        n_active = len(self.active_windows())
        n_wait = len(self.awaiting_windows())
        self.log_event("info", "recovery", f"Recovered state: {n_active} active window(s), {n_wait} awaiting settlement")

    def snapshot(self, reason: str) -> None:
        with tx(self.conn):
            acc.snapshot_equity(self.conn, self.account_id, self.clock.now(), reason)


def _fee_schedule_from_json(text: str | None) -> FeeSchedule:
    data = json.loads(text or "{}")
    return FeeSchedule(
        series_ticker=data.get("series_ticker", ""),
        fee_type=data.get("fee_type", ""),
        fee_multiplier=D(data.get("fee_multiplier", "1")),
        effective_from=data.get("effective_from"),
        source=data.get("source", ""),
        fetched_at=data.get("fetched_at", ""),
    )

"""HTTP API for the dashboard. All three UI layouts use these same endpoints.

Every response carries ``paper: true``; nothing here can reach an exchange order endpoint.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import __version__
from .db import tx
from .engine import Engine
from .marketdata.book import purchase_estimate
from .marketdata.discovery import market_url
from .marketdata.models import NO, YES, OrderBook
from .marketdata.pricecheck import Tick, compare_visible_price
from .money import ZERO, D, s
from .paper import accounting as acc
from .paper.fees import FeeSchedule, estimate_fee, max_fee_reservation, resolve_schedule
from .schedule import WINDOW, hour_slots, is_eligible_start, iso, next_eligible_starts, parse_ts, window_start_for
from .settings_store import SettingsError, load_settings, update_settings

router = APIRouter(prefix="/api")


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def _jsonable(d: dict) -> dict:
    return {k: (s(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def _local(dt: datetime | None, tz: ZoneInfo) -> str | None:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z") if dt else None


def _levels(levels) -> list[dict]:
    return [{"price": s(lv.price), "size": s(lv.size)} for lv in levels]


def _fee_schedule(eng: Engine, now: datetime) -> FeeSchedule | None:
    series = eng.md.series
    if series is None:
        return None
    base = FeeSchedule(series.ticker or eng.cfg.series_ticker, series.fee_type, series.fee_multiplier, None,
                       f"GET /series/{eng.cfg.series_ticker}", iso(eng.md.series_fetched_at) or "")
    return resolve_schedule(base, [(c.scheduled_ts, c.fee_type, c.fee_multiplier) for c in eng.md.fee_changes], now)


# ---------------------------------------------------------------------------- state


@router.get("/health")
async def health(request: Request) -> dict:
    eng = _engine(request)
    return {"ok": True, "paper": True, "mode": eng.cfg.mode, "version": __version__}


@router.get("/state")
async def state(request: Request) -> dict:
    eng = _engine(request)
    conn = eng.conn
    now = eng.clock.now()
    settings = load_settings(conn)
    tz = ZoneInfo(settings.display_timezone)
    account_id = eng.paper.account_id
    summary = acc.account_summary(conn, account_id)
    est = eng.paper.engine_state()
    trading_state = "running" if est["trading_enabled"] else ("paused" if est["paused_at"] else "stopped")

    ws = window_start_for(now)
    we = ws + WINDOW
    windows_by_start = {
        r["window_start"]: r for r in conn.execute(
            "SELECT * FROM windows WHERE account_id=? AND window_start >= ?", (account_id, iso(ws - timedelta(hours=1)))
        )
    }
    slots = []
    for sl in hour_slots(now, settings.eligible_minutes, settings.display_timezone):
        row = windows_by_start.get(iso(sl.start))
        if row is not None:
            status, note = row["status"], row["skip_reason"] or row["settlement_note"]
        elif sl.end <= now:
            status, note = "past", None
        elif sl.start <= now < sl.end:
            status, note = ("current" if sl.eligible else "skip"), None
        else:
            status, note = ("upcoming" if sl.eligible else "skip"), None
        slots.append({"start": iso(sl.start), "end": iso(sl.end), "label": sl.label, "eligible": sl.eligible,
                      "status": status, "note": note, "net_pnl": row["net_pnl"] if row is not None else None,
                      "is_current": sl.start <= now < sl.end})
    nxt = next_eligible_starts(now, settings.eligible_minutes, settings.display_timezone, count=3,
                               inclusive=False)
    cur_row = windows_by_start.get(iso(ws))

    # Focus market + live data
    focus = eng.focus_ticker
    st = eng.md.tickers.get(focus) if focus else None
    chk = eng.md.cached_discovery(ws)
    market_block = None
    m = st.summary.market if st and st.summary else (chk.market if chk and chk.market and chk.market.ticker == focus else None)
    if m is not None:
        market_block = {
            "ticker": m.ticker, "event_ticker": m.event_ticker, "title": m.title,
            "url": market_url(eng.cfg.series_ticker, m.event_ticker),
            "status": m.status, "result": m.result or None,
            "open_time": iso(m.open_time), "close_time": iso(m.close_time),
            "strike_type": m.strike_type, "target_price": s(m.floor_strike),
            "yes_sub_title": m.yes_sub_title, "rules_primary": m.rules_primary,
            "mapping": {"UP": "YES", "DOWN": "NO"},
            "mapping_basis": f"strike_type={m.strike_type}: YES pays when the settlement value is at/above the target",
            "contract_value": s(m.notional_value),
            "tick_size": s(m.min_tick()),
            "validation_ok": bool(chk.ok) if chk and chk.market and chk.market.ticker == m.ticker else None,
            "validation_checks": chk.checks if chk and chk.market and chk.market.ticker == m.ticker else [],
            "validation_warnings": chk.warnings if chk and chk.market and chk.market.ticker == m.ticker else [],
            "validation_error": chk.reason if chk and not chk.ok else None,
        }
    book_block = _book_block(st.book, st, now) if st and st.book else None
    summary_block = None
    if st and st.summary:
        sm = st.summary.market
        summary_block = {
            "label": "API market summary (GET /markets/{ticker}) — not the order book",
            "fetched_at": iso(st.summary.requested_at), "latency_ms": st.summary.latency_ms,
            "age_seconds": round((now - st.summary.requested_at).total_seconds(), 1),
            "yes_bid": s(sm.yes_bid), "yes_bid_size": s(sm.yes_bid_size), "yes_ask": s(sm.yes_ask),
            "yes_ask_size": s(sm.yes_ask_size), "no_bid": s(sm.no_bid), "no_ask": s(sm.no_ask),
            "last_price": s(sm.last_price), "status": sm.status,
        }
    fee_sched = _fee_schedule(eng, now)
    price_check = _price_check(eng, st, settings, fee_sched, cur_row, now) if st and st.book else None

    current = None
    if cur_row is not None:
        current = _window_detail(conn, cur_row, eng)
    open_windows = [
        _window_detail(conn, r, eng) for r in conn.execute(
            "SELECT * FROM windows WHERE account_id=? AND status IN ('active','awaiting_settlement') ORDER BY window_start DESC",
            (account_id,))
    ]
    fills = _rows(conn.execute(
        "SELECT id, order_id, window_id, market_ticker, side, price, quantity, principal, fee, liquidity, filled_at "
        "FROM fills WHERE account_id=? ORDER BY filled_at DESC LIMIT 25", (account_id,)))
    events = _rows(conn.execute("SELECT ts, level, kind, message, window_id FROM events ORDER BY id DESC LIMIT 40"))
    ex = eng.md.exchange
    return {
        "paper": True,
        "paper_label": "PAPER TRADING",
        "mode": eng.cfg.mode,
        "preview": eng.cfg.is_preview,
        "version": __version__,
        "server_time": iso(now),
        "server_time_local": _local(now, tz),
        "timezone": settings.display_timezone,
        "engine": {
            "state": trading_state,
            "trading_enabled": bool(est["trading_enabled"]),
            "armed_at": est["armed_at"], "paused_at": est["paused_at"],
            "started_at": iso(eng.started_at), "last_tick_at": iso(eng.last_tick_at),
            "last_tick_error": eng.last_tick_error,
            "entry_status": eng.entry_status,
            "pair_accounting": eng.cfg.pair_accounting,
            "execution_delay_seconds": eng.cfg.execution_delay_seconds,
            "stale_after_seconds": eng.cfg.stale_after_seconds,
            "entry_grace_seconds": eng.cfg.entry_grace_seconds,
        },
        "account": _jsonable(summary),
        "feed": {
            **eng.md.health.to_json(now),
            "source": eng.md.source.kind,
            "base_url": eng.md.source.base_url,
            "requests": eng.md.source.request_count,
            "exchange": {
                "exchange_active": ex.exchange_active, "trading_active": ex.trading_active,
                "fetched_at": iso(ex.fetched_at),
            } if ex else None,
        },
        "schedule": {
            "current": {
                "start": iso(ws), "end": iso(we),
                "eligible": any(sl["is_current"] and sl["eligible"] for sl in slots),
                "seconds_remaining": round((we - now).total_seconds(), 1),
                "status": cur_row["status"] if cur_row is not None else None,
                "skip_reason": cur_row["skip_reason"] if cur_row is not None else None,
            },
            "next_eligible": [{"start": iso(t), "end": iso(t + WINDOW), "seconds_until": round((t - now).total_seconds(), 1)} for t in nxt],
            "hour_slots": slots,
            "eligible_minutes": list(settings.eligible_minutes),
        },
        "market": market_block,
        "book": book_block,
        "summary_quotes": summary_block,
        "summary_comparison": st.comparison.to_json() if st and st.comparison else None,
        "price_check": price_check,
        "current_window": current,
        "open_windows": open_windows,
        "recent_fills": fills,
        "fee_model": ({**fee_sched.to_json(), "changes_error": eng.md.fee_changes_error,
                       "uncertain": fee_sched.uncertain or bool(eng.md.fee_changes_error)} if fee_sched else None),
        "clock": _clock_info(eng),
        "last_gap": eng.last_gap,
        "diagnosis": _diagnosis(eng, now, settings, est, trading_state, ws, cur_row, current, nxt, st, fee_sched, summary),
        "settings": settings.to_json(),
        "events": events,
    }


def _clock_info(eng: Engine) -> dict:
    measured = getattr(eng.clock, "measured_offset", None)
    applied = getattr(eng.clock, "applied_offset", 0.0)
    return {"measured_offset_seconds": round(measured, 1) if measured is not None else None,
            "applied_offset_seconds": applied, "source": "HTTP Date header from Kalshi",
            "clock_steps_detected": getattr(eng.clock, "discontinuities", 0),
            "tls": getattr(eng.md.source, "tls_mode", None)}


def _gate_for(eng: Engine, o: sqlite3.Row) -> dict:
    now = eng.clock.now()
    gate = eng.paper.order_gate.get(o["id"])
    if gate is None:
        err = eng.md.health.last_error if eng.md.health.state != "ok" else None
        return {"state": "no_book", "text": "Waiting for the first order-book observation for this order"
                + (f" ({err})" if err else "")}
    at = parse_ts(gate.get("book_requested_at"))
    if at is not None and (now - at).total_seconds() > max(2 * eng.cfg.stale_after_seconds, 10):
        err = eng.md.health.last_error if eng.md.health.state != "ok" else "no new order book received"
        return {**gate, "state": "stale", "text": f"No fresh order book for {(now - at).total_seconds():.0f}s — "
                                                  f"fills paused ({err})"}
    return gate


def _diagnosis(eng: Engine, now: datetime, settings, est, trading_state: str, ws: datetime, cur_row,
               current: dict | None, nxt: list, st, fee_sched, account: dict) -> dict:
    """One plain-language answer to "why isn't it buying?" plus the checklist behind it."""
    tz = ZoneInfo(settings.display_timezone)
    checks: list[dict] = []

    def add(key: str, status: str, title: str, detail: str = "") -> None:
        checks.append({"key": key, "status": status, "title": title, "detail": detail})

    mins_text = ", ".join(f":{m:02d}" for m in settings.eligible_minutes)
    limit_c = s(settings.limit_price * 100)
    nxt_text = (f"{nxt[0].astimezone(tz):%H:%M %Z} (in {max(0, int((nxt[0] - now).total_seconds() // 60))} min)"
                if nxt else "the next eligible window")

    # 1. engine loop (monotonic timing: clock corrections never look like a stall)
    mono = eng.clock.monotonic()
    started, done = eng.tick_started_mono, eng.last_tick_done_mono
    stall_limit = 30.0  # a tick can legitimately wait ~14 s on one slow request with host failover
    if eng.last_tick_error:
        add("engine", "block", "Engine error", eng.last_tick_error)
    elif started is None:
        add("engine", "wait", "Engine starting…")
    elif done is not None and done >= started and mono - done <= 10:
        add("engine", "ok", "Engine running", f"last cycle {mono - done:.1f}s ago")
    elif mono - started <= stall_limit:
        add("engine", "wait", "Waiting on a Kalshi request", f"{mono - started:.0f}s so far (slow network or failover)")
    else:
        since = mono - (done if done is not None else started)
        add("engine", "block", "Engine is not running its loop",
            f"No completed cycle for {since:.0f}s. If you clicked inside the start.ps1 window, press Esc there; "
            "otherwise restart start.ps1.")
    # 2. trading switch
    if trading_state == "stopped":
        add("trading", "block", "Trading has not been started", f"Press Start. Entries begin at the next window "
            f"that starts at {mins_text}.")
    elif trading_state == "paused":
        add("trading", "block", "Trading is paused", "Press Resume. Entries begin at the next eligible window.")
    else:
        add("trading", "ok", "Trading enabled")

    # 3. market data
    h = eng.md.health
    if eng.cfg.is_preview:
        add("preview", "info", "Preview mode", "Synthetic sample data, not live Kalshi prices.")
    if h.state in ("error", "rate_limited"):
        add("data", "block", "Cannot get live Kalshi data" if h.state == "error" else "Kalshi rate limit — waiting",
            h.last_error or "")
    elif h.state == "starting":
        add("data", "wait", "Connecting to Kalshi…")
    else:
        add("data", "ok", "Sample data OK (preview)" if eng.cfg.is_preview else "Live market data OK",
            f"source {eng.md.source.base_url}")
    tls = getattr(eng.md.source, "tls_mode", None)
    if tls == "bundled":
        add("tls", "info", "HTTPS uses Python's bundled certificate list",
            "Run setup.ps1 again to install 'truststore' so the Windows certificate store is used "
            "(needed if antivirus or a proxy scans HTTPS).")

    # 4. clock
    measured = getattr(eng.clock, "measured_offset", None)
    if measured is not None and abs(measured) > 5:
        direction = "behind" if measured > 0 else "ahead of"
        add("clock", "info", f"Your PC clock is {abs(measured):.0f}s {direction} Kalshi — corrected automatically",
            "Windows Settings > Time & language > Date & time > Sync now fixes it permanently.")

    # 5. exchange + fees
    ex = eng.md.exchange
    if ex is not None and not (ex.exchange_active and ex.trading_active):
        add("exchange", "block", "Kalshi reports trading is paused", "Maintenance or outside trading hours.")
    if eng.md.series is None and h.state == "ok":
        add("fees", "wait", "Fee schedule not loaded yet", "Entries wait until the series fee structure is known.")

    # 6. funds for the next window
    try:
        sched = fee_sched or FeeSchedule("", "quadratic", D("1"), None, "", "")
        per_order = settings.limit_price * settings.contracts_per_side + max_fee_reservation(
            sched, settings.contracts_per_side, settings.limit_price, settings.fee_mode)
        need = per_order * 2
        # Cash that will be free for the NEXT window: the current window's reservations are released at its close.
        avail_next = account["available"]
        if cur_row is not None and cur_row["status"] == "active":
            for o in (current or {}).get("orders", []):
                if o["status"] in acc.WORKING_STATUSES:
                    avail_next += D(o["reserved_remaining"])
        if avail_next < need:
            add("funds", "block", "Not enough paper cash for the next window",
                f"A window needs ${s(need)} reserved; ${s(avail_next)} will be available.")
    except Exception:  # noqa: BLE001 - diagnostics must never break the state endpoint
        pass

    # 7. this window
    eligible = is_eligible_start(ws, settings.eligible_minutes, settings.display_timezone)
    armed = parse_ts(est["armed_at"])
    if not eligible:
        add("window", "wait", f"This window ({ws.astimezone(tz):%H:%M}) is skipped by the schedule",
            f"Only windows starting at {mins_text} are traded. "
            f"Next entry: {nxt_text}.")
    elif cur_row is not None and cur_row["status"] == "skipped":
        category = _skip_category(cur_row["skip_reason"])
        if category in ("Started or resumed mid-window", "Scheduled skip (:45 window)"):
            add("window", "wait", "Waiting for the next window — started after this one began",
                f"{cur_row['skip_reason']}. Next entry: {nxt_text}.")
        else:
            add("window", "block", f"This window ({ws.astimezone(tz):%H:%M}) was skipped",
                f"{cur_row['skip_reason'] or ''} Next attempt: {nxt_text}.")
    elif trading_state == "running" and armed is not None and armed > ws and cur_row is None:
        add("window", "wait", "Started after this window began",
            f"Started at {armed.astimezone(tz):%H:%M:%S}; the first entry is at the next eligible window: {nxt_text}.")
    elif cur_row is None and trading_state == "running":
        add("window", "wait", "Entering this window…", eng.entry_status.get("message", ""))
    elif cur_row is not None and cur_row["status"] == "active":
        add("window", "ok", f"Orders placed for {cur_row['market_ticker']}")
    elif cur_row is not None:
        reasons = sorted({o.get("close_reason") or "" for o in (current or {}).get("orders", [])} - {""})
        add("window", "wait", f"This window's orders are closed ({cur_row['status'].replace('_', ' ')})",
            f"{'; '.join(reasons) + '. ' if reasons else ''}No more entries in this window. Next entry: {nxt_text}.")

    # 8. book + orders
    if st is not None and st.book is not None and not st.book_valid:
        add("book", "block", "Order book failed validation — fills paused", "; ".join(st.book_problems[:3]))
    waiting_bits = []
    filled_bits = []
    for o in (current or {}).get("orders", []) if cur_row is not None and cur_row["status"] == "active" else []:
        gate = o.get("gate") or {}
        if o["status"] == "filled":
            add(f"order-{o['side']}", "ok", f"{o['side']} filled {o['filled_qty']}/{o['quantity']}")
            filled_bits.append(f"{o['side']} filled {o['filled_qty']}/{o['quantity']}")
            continue
        state = gate.get("state", "")
        status = "block" if state in ("blocked", "stale") else ("ok" if state in ("revalidating", "filled") else "wait")
        add(f"order-{o['side']}", status, f"{o['side']} order: {o['filled_qty']}/{o['quantity']} filled",
            gate.get("text", ""))
        if state == "waiting_for_price" and gate.get("best_ask"):
            waiting_bits.append(f"{o['side']} best ask {s(D(gate['best_ask']) * 100)}c")
        elif state == "no_offers":
            waiting_bits.append(f"no {o['side']} sellers")

    blocks = [c for c in checks if c["status"] == "block"]
    waits = [c for c in checks if c["status"] == "wait"]
    if blocks:
        headline, status = f"Not buying: {blocks[0]['title']}", "blocked"
        detail = blocks[0]["detail"]
    elif waiting_bits:
        prefix = f"{'; '.join(filled_bits)}. " if filled_bits else ""
        headline, status = (f"{prefix}Orders are live, waiting for a seller at {limit_c}c or "
                            f"less ({', '.join(waiting_bits)})"), "waiting_for_price"
        detail = ("This is normal. Most 15-minute windows fill on at most one side: a fill needs BTC to move far "
                  f"enough that one side's ask drops to the limit. A bid or last trade at {limit_c}c does not count.")
    elif waits:
        headline, status, detail = f"Waiting: {waits[0]['title']}", "waiting", waits[0]["detail"]
    else:
        headline, status, detail = "Everything is working", "ok", ""
    return {"headline": headline, "status": status, "detail": detail, "checks": checks,
            "next_entry_at": iso(nxt[0]) if nxt else None}


def _book_block(book: OrderBook, st, now: datetime) -> dict:
    return {
        "ticker": book.ticker,
        "label": "Order book (GET /markets/{ticker}/orderbook): bids reported, asks derived as 1 - opposite bid",
        "source": book.source,
        "format": book.format,
        "requested_at": iso(book.requested_at),
        "received_at": iso(book.received_at),
        "server_date": book.server_date,
        "latency_ms": book.latency_ms,
        "age_seconds": round((now - book.requested_at).total_seconds(), 1),
        "valid": st.book_valid,
        "problems": st.book_problems,
        "yes_bids": _levels(book.yes_bids),
        "no_bids": _levels(book.no_bids),
        "yes_asks": _levels(book.asks(YES)),
        "no_asks": _levels(book.asks(NO)),
        "rejected_out_of_order": st.rejected_out_of_order,
    }


def _price_check(eng: Engine, st, settings, fee_sched: FeeSchedule | None, cur_row, now: datetime) -> dict:
    book: OrderBook = st.book
    out: dict[str, Any] = {
        "ticker": book.ticker,
        "limit_price": s(settings.limit_price),
        "quantity": s(settings.contracts_per_side),
        "fee_mode": settings.fee_mode,
        "fee_estimate_uncertain": fee_sched.uncertain if fee_sched else True,
        "observation": {
            "source": book.source, "requested_at": iso(book.requested_at), "received_at": iso(book.received_at),
            "latency_ms": book.latency_ms, "age_seconds": round((now - book.requested_at).total_seconds(), 1),
            "valid": st.book_valid, "problems": st.book_problems,
        },
        "sides": {},
    }
    sm = st.summary.market if st.summary else None
    orders = {}
    if cur_row is not None and cur_row["market_ticker"] == book.ticker:
        orders = {o["side"]: o for o in eng.conn.execute("SELECT * FROM orders WHERE window_id=?", (cur_row["id"],))}

    def fee_fn(legs):
        if fee_sched is None:
            return ZERO
        return estimate_fee(fee_sched, settings.fee_mode, legs)

    for side, k in (("UP", YES), ("DOWN", NO)):
        bid = book.best_bid(k)
        ask = book.best_ask(k)
        estimate = purchase_estimate(book, k, settings.contracts_per_side, settings.limit_price, None, fee_fn)
        order_est = None
        o = orders.get(side)
        if o is not None and o["status"] in acc.WORKING_STATUSES:
            remaining = D(o["quantity"]) - D(o["filled_qty"])
            order_est = purchase_estimate(book, k, remaining, D(o["limit_price"]),
                                          acc.consumed_by_level(eng.conn, o["id"]), fee_fn).to_json()
        out["sides"][side] = {
            "kalshi_side": k.upper(),
            "best_bid": {"price": s(bid.price), "size": s(bid.size)} if bid else None,
            "executable_ask": {"price": s(ask.price), "size": s(ask.size),
                               "derived_from": f"1 - best {'NO' if k == YES else 'YES'} bid"} if ask else None,
            "estimate": estimate.to_json(),
            "current_order_estimate": order_est,
            "summary": {
                "bid": s(sm.yes_bid if k == YES else sm.no_bid) if sm else None,
                "ask": s(sm.yes_ask if k == YES else sm.no_ask) if sm else None,
                "last_trade": (s(sm.last_price) if k == YES else (s(1 - sm.last_price) if sm.last_price is not None else None)) if sm else None,
                "last_trade_note": None if k == YES else "derived as 1 - YES last trade",
            },
        }
    return out


def _window_detail(conn: sqlite3.Connection, w: sqlite3.Row, eng: Engine) -> dict:
    d = dict(w)
    d["orders"] = []
    for o in conn.execute("SELECT * FROM orders WHERE window_id=? ORDER BY side DESC", (w["id"],)):
        od = dict(o)
        od["remaining_qty"] = s(D(o["quantity"]) - D(o["filled_qty"]))
        od["avg_fill_price"] = s((D(o["principal_cost"]) / D(o["filled_qty"])).quantize(Decimal("0.0001"))) if D(o["filled_qty"]) > 0 else None
        od["display_status"] = "settled" if o["settled"] else o["status"]
        od["gate"] = _gate_for(eng, o) if o["status"] in acc.WORKING_STATUSES else None
        d["orders"].append(od)
    up, down = D(w["up_qty"]), D(w["down_qty"])
    pairs = min(up, down)
    d["matched_pairs"] = s(pairs)
    d["unmatched_side"] = "UP" if up > pairs else ("DOWN" if down > pairs else None)
    d["unmatched_qty"] = s(max(up, down) - pairs)
    d["position_value"] = s(acc.window_position_value(w))
    d["settings"] = json.loads(w["settings_json"]) if w["settings_json"] else None
    d["fee_schedule"] = json.loads(w["fee_schedule_json"]) if w["fee_schedule_json"] else None
    return d


# ---------------------------------------------------------------------------- history


def _search_clause(search: str | None, cols: list[str]) -> tuple[str, list]:
    if not search:
        return "", []
    like = f"%{search.strip()}%"
    return " AND (" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")", [like] * len(cols)


@router.get("/windows")
async def windows(request: Request, search: str | None = None, limit: int = 200, status: str | None = None) -> dict:
    eng = _engine(request)
    clause, params = _search_clause(search, ["market_ticker", "status", "skip_reason", "outcome_class", "market_result"])
    if status:
        clause += " AND status=?"
        params.append(status)
    rows = eng.conn.execute(
        f"SELECT * FROM windows WHERE account_id=?{clause} ORDER BY window_start DESC LIMIT ?",
        (eng.paper.account_id, *params, min(limit, 2000)),
    ).fetchall()
    return {"paper": True, "windows": [_window_detail(eng.conn, r, eng) for r in rows]}


@router.get("/orders")
async def orders(request: Request, search: str | None = None, limit: int = 300) -> dict:
    eng = _engine(request)
    clause, params = _search_clause(search, ["market_ticker", "side", "status", "close_reason", "id"])
    rows = _rows(eng.conn.execute(
        f"SELECT * FROM orders WHERE account_id=?{clause} ORDER BY submitted_at DESC, side DESC LIMIT ?",
        (eng.paper.account_id, *params, min(limit, 5000))))
    return {"paper": True, "orders": rows}


@router.get("/fills")
async def fills(request: Request, search: str | None = None, limit: int = 300) -> dict:
    eng = _engine(request)
    clause, params = _search_clause(search, ["market_ticker", "side", "id"])
    rows = _rows(eng.conn.execute(
        f"SELECT id, order_id, window_id, market_ticker, side, level_price, price, quantity, principal, fee, liquidity, "
        f"observation_id, trigger_observation_id, filled_at FROM fills WHERE account_id=?{clause} "
        f"ORDER BY filled_at DESC LIMIT ?", (eng.paper.account_id, *params, min(limit, 5000))))
    return {"paper": True, "fills": rows, "note": "Simulated fills estimated from displayed order-book asks; not exchange executions."}


@router.get("/fills/{fill_id:path}")
async def fill_detail(request: Request, fill_id: str) -> dict:
    eng = _engine(request)
    f = eng.conn.execute("SELECT * FROM fills WHERE id=?", (fill_id,)).fetchone()
    if f is None:
        raise HTTPException(404, "fill not found")
    d = dict(f)
    d["calc"] = json.loads(f["calc_json"])
    obs = eng.conn.execute("SELECT * FROM observations WHERE id=?", (f["observation_id"],)).fetchone()
    d["observation"] = {**dict(obs), "book": json.loads(obs["book_json"])} if obs else None
    if f["trigger_observation_id"]:
        t = eng.conn.execute("SELECT * FROM observations WHERE id=?", (f["trigger_observation_id"],)).fetchone()
        d["trigger_observation"] = {**dict(t), "book": json.loads(t["book_json"])} if t else None
    return {"paper": True, "fill": d}


@router.get("/settlements")
async def settlements(request: Request, search: str | None = None, limit: int = 300) -> dict:
    eng = _engine(request)
    clause, params = _search_clause(search, ["market_ticker", "result", "market_status"])
    rows = _rows(eng.conn.execute(
        f"SELECT window_id, market_ticker, market_status, result, yes_payout, no_payout, kalshi_settled_ts, up_qty, "
        f"down_qty, pairs_credited, payout, principal, fees, net_pnl, applied_at FROM settlements "
        f"WHERE account_id=?{clause} ORDER BY applied_at DESC LIMIT ?", (eng.paper.account_id, *params, min(limit, 5000))))
    return {"paper": True, "settlements": rows}


@router.get("/equity")
async def equity(request: Request, limit: int = 2000) -> dict:
    eng = _engine(request)
    rows = _rows(eng.conn.execute(
        "SELECT ts, cash, reserved, position_value, equity, realized_pnl, unrealized_pnl, reason FROM equity_snapshots "
        "WHERE account_id=? ORDER BY id DESC LIMIT ?", (eng.paper.account_id, min(limit, 20000))))
    rows.reverse()
    return {"paper": True, "equity": rows}


@router.get("/prices")
async def prices(request: Request, ticker: str | None = None, minutes: int = 20) -> dict:
    eng = _engine(request)
    ticker = ticker or eng.focus_ticker
    if not ticker:
        return {"paper": True, "ticker": None, "ticks": []}
    since = iso(eng.clock.now() - timedelta(minutes=min(max(minutes, 1), 24 * 60)))
    rows = _rows(eng.conn.execute(
        "SELECT ts, kind, yes_bid, yes_bid_size, yes_ask, yes_ask_size, no_bid, no_bid_size, no_ask, no_ask_size, "
        "last_price, latency_ms FROM price_ticks WHERE market_ticker=? AND ts >= ? ORDER BY ts", (ticker, since)))
    return {"paper": True, "ticker": ticker, "ticks": rows,
            "series_labels": {
                "yes_ask": "UP/YES executable ask (order book, 1 - best NO bid)",
                "yes_bid": "UP/YES best bid (order book)",
                "no_ask": "DOWN/NO executable ask (order book, 1 - best YES bid)",
                "no_bid": "DOWN/NO best bid (order book)",
                "last_price": "Last trade, YES (API market summary)",
            }}


@router.get("/events")
async def events(request: Request, limit: int = 200) -> dict:
    eng = _engine(request)
    return {"paper": True, "events": _rows(eng.conn.execute(
        "SELECT ts, level, kind, message, window_id FROM events ORDER BY id DESC LIMIT ?", (min(limit, 2000),)))}


@router.get("/analytics")
async def analytics(request: Request) -> dict:
    eng = _engine(request)
    conn = eng.conn
    account_id = eng.paper.account_id
    rows = conn.execute("SELECT * FROM windows WHERE account_id=? ORDER BY window_start", (account_id,)).fetchall()
    entered = [r for r in rows if r["status"] not in ("skipped",)]
    settled = [r for r in rows if r["status"] == "settled"]
    classes = ["both_filled", "both_partial", "up_only", "down_only", "no_fill"]
    by_class: dict[str, dict] = {c: {"windows": 0, "settled": 0, "net_pnl": ZERO} for c in classes}
    partial_windows = 0
    for r in entered:
        c = r["outcome_class"]
        if c in by_class:
            by_class[c]["windows"] += 1
            if r["status"] == "settled":
                by_class[c]["settled"] += 1
                by_class[c]["net_pnl"] += D(r["net_pnl"])
        orders = conn.execute("SELECT quantity, filled_qty FROM orders WHERE window_id=?", (r["id"],)).fetchall()
        if any(ZERO < D(o["filled_qty"]) < D(o["quantity"]) for o in orders):
            partial_windows += 1
    pnl = [D(r["net_pnl"]) for r in settled]
    total = sum(pnl, ZERO)
    wins = sum(1 for p in pnl if p > 0)
    losses = sum(1 for p in pnl if p < 0)
    flats = len(pnl) - wins - losses
    fill_rows = conn.execute("SELECT side, price, quantity, fee FROM fills WHERE account_id=?", (account_id,)).fetchall()
    side_stats = {}
    for side in ("UP", "DOWN"):
        fs = [f for f in fill_rows if f["side"] == side]
        qty = sum((D(f["quantity"]) for f in fs), ZERO)
        cost = sum((D(f["quantity"]) * D(f["price"]) for f in fs), ZERO)
        intended = sum((D(o["quantity"]) for o in conn.execute(
            "SELECT quantity FROM orders WHERE account_id=? AND side=?", (account_id, side))), ZERO)
        side_stats[side] = {"fills": len(fs), "contracts": s(qty), "intended_contracts": s(intended),
                            "fill_rate": s((qty / intended).quantize(Decimal("0.0001"))) if intended else None,
                            "avg_price": s((cost / qty).quantize(Decimal("0.0001"))) if qty else None}
    improvement = ZERO
    for f in conn.execute("SELECT f.price, f.quantity, o.limit_price FROM fills f JOIN orders o ON o.id=f.order_id "
                          "WHERE f.account_id=?", (account_id,)):
        improvement += (D(f["limit_price"]) - D(f["price"])) * D(f["quantity"])
    fees_total = sum((D(f["fee"]) for f in fill_rows), ZERO)
    skipped = [r for r in rows if r["status"] == "skipped"]
    skip_reasons: dict[str, int] = {}
    for r in skipped:
        reason = _skip_category(r["skip_reason"])
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    series = [{"window_start": r["window_start"], "market_ticker": r["market_ticker"], "net_pnl": r["net_pnl"],
               "outcome_class": r["outcome_class"], "fees": r["fees"], "result": r["market_result"]} for r in settled]
    cum = ZERO
    for item in series:
        cum += D(item["net_pnl"])
        item["cumulative_pnl"] = s(cum)
    return {
        "paper": True,
        "caveat": ("Performance is measured per complete market window (both legs, fees and settlement together). "
                   "A high share of winning individual legs is NOT evidence that this two-sided strategy is profitable."),
        "windows": {
            "recorded": len(rows), "entered": len(entered), "settled": len(settled),
            "awaiting_settlement": sum(1 for r in rows if r["status"] == "awaiting_settlement"),
            "active": sum(1 for r in rows if r["status"] == "active"),
            "skipped": len(skipped), "skip_reasons": skip_reasons,
            "partial_fill_windows": partial_windows,
        },
        "outcomes": {c: {**v, "net_pnl": s(v["net_pnl"])} for c, v in by_class.items()},
        "pnl": {
            "total_net": s(total),
            "average_per_settled_window": s((total / len(pnl)).quantize(Decimal("0.0001"))) if pnl else None,
            "profitable_windows": wins, "losing_windows": losses, "flat_windows": flats,
            "best_window": s(max(pnl)) if pnl else None, "worst_window": s(min(pnl)) if pnl else None,
        },
        "fills": {"by_side": side_stats, "total_fills": len(fill_rows), "price_improvement_total": s(improvement)},
        "fees": {"total": s(fees_total),
                 "average_per_entered_window": s((fees_total / len(entered)).quantize(Decimal("0.0001"))) if entered else None},
        "per_window": series,
    }


def _skip_category(reason: str | None) -> str:
    text = (reason or "").lower()
    for needle, label in (
        ("scheduled skip", "Scheduled skip (:45 window)"),
        ("started/resumed", "Started or resumed mid-window"),
        ("not armed", "Started or resumed mid-window"),
        ("insufficient paper funds", "Insufficient paper funds"),
        ("grace period", "Entry grace period expired"),
        ("validation", "Market validation failed"),
        ("limit price not valid", "Limit price not on market tick"),
    ):
        if needle in text:
            return label
    return "Other"


# ---------------------------------------------------------------------------- price check comparison


class CompareRequest(BaseModel):
    side: str
    quote_type: str
    price: str
    price_unit: str = "cents"
    observed_at: str | None = None
    ticker: str | None = None


@router.post("/price-check/compare")
async def compare(request: Request, body: CompareRequest) -> dict:
    eng = _engine(request)
    now = eng.clock.now()
    ticker = body.ticker or eng.focus_ticker
    if not ticker:
        raise HTTPException(400, "no market is currently tracked")
    try:
        price = D(body.price)
    except ValueError as exc:
        raise HTTPException(400, f"invalid price: {exc}") from exc
    if body.price_unit == "cents":
        price = price / Decimal(100)
    elif body.price_unit != "dollars":
        raise HTTPException(400, "price_unit must be 'cents' or 'dollars'")
    observed = parse_ts(body.observed_at) if body.observed_at else now
    if observed is None:
        raise HTTPException(400, "invalid observed_at")
    window = (observed - timedelta(seconds=60), observed + timedelta(seconds=60))
    ticks = []
    for r in eng.conn.execute(
        "SELECT * FROM price_ticks WHERE market_ticker=? AND ts BETWEEN ? AND ? ORDER BY ts",
        (ticker, iso(window[0]), iso(window[1])),
    ):
        ticks.append(Tick(parse_ts(r["ts"]), r["kind"], _d(r["yes_bid"]), _d(r["yes_ask"]), _d(r["no_bid"]),
                          _d(r["no_ask"]), _d(r["last_price"])))
    fee_sched = _fee_schedule(eng, now)
    try:
        result = compare_visible_price(price, body.side.upper(), body.quote_type.lower(), observed, ticks,
                                       fee_multiplier=fee_sched.fee_multiplier if fee_sched else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = {"ticker": ticker, "side": body.side.upper(), "quote_type": body.quote_type.lower(),
               "observed_at": iso(observed), **result.to_json()}
    with tx(eng.conn):
        eng.conn.execute(
            "INSERT INTO price_comparisons(created_at, market_ticker, side, quote_type, visible_price, observed_at, verdict, "
            "result_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(now), ticker, payload["side"], payload["quote_type"], s(price), iso(observed), result.verdict,
             json.dumps(payload)),
        )
    return {"paper": True, "comparison": payload}


@router.get("/price-check/comparisons")
async def comparisons(request: Request, limit: int = 50) -> dict:
    eng = _engine(request)
    rows = eng.conn.execute("SELECT result_json FROM price_comparisons ORDER BY id DESC LIMIT ?", (min(limit, 500),))
    return {"paper": True, "comparisons": [json.loads(r["result_json"]) for r in rows]}


@router.post("/price-check/refresh")
async def refresh_comparison(request: Request) -> dict:
    """Fetch book and summary back-to-back now and compare them (read-only)."""
    eng = _engine(request)
    ticker = eng.focus_ticker
    if not ticker:
        raise HTTPException(400, "no market is currently tracked")
    st = eng.md.state(ticker)
    market = st.summary.market if st.summary else None
    cmp = await eng.md.fresh_comparison(ticker, market)
    eng._record_summary_tick(st.summary)
    eng._record_book_tick(st.book, st.book_valid)
    return {"paper": True, "comparison": cmp.to_json() if cmp else None, "feed": eng.md.health.to_json(eng.clock.now())}


def _d(v: str | None) -> Decimal | None:
    return D(v) if v not in (None, "") else None


# ---------------------------------------------------------------------------- settings & control


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    eng = _engine(request)
    return {"paper": True, "settings": load_settings(eng.conn).to_json(),
            "note": "Changes apply from the next new window; existing orders are never modified."}


@router.put("/settings")
async def put_settings(request: Request, body: dict) -> dict:
    eng = _engine(request)
    try:
        updated = update_settings(eng.conn, body, eng.clock.now())
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    eng.paper.log_event("info", "settings", f"Settings updated (effective next new window): {json.dumps(body)}")
    return {"paper": True, "settings": updated.to_json()}


@router.post("/control/start")
async def start(request: Request) -> dict:
    eng = _engine(request)
    eng.start_trading()
    return {"paper": True, "state": "running", "message": "Trading enabled; entries begin at the next eligible window start."}


@router.post("/control/resume")
async def resume(request: Request) -> dict:
    return await start(request)


@router.post("/control/pause")
async def pause(request: Request) -> dict:
    eng = _engine(request)
    n = eng.pause_trading()
    return {"paper": True, "state": "paused", "canceled_orders": n,
            "message": "Remaining virtual orders canceled; settlement tracking continues."}


class ResetRequest(BaseModel):
    confirm: str


@router.post("/account/reset")
async def reset(request: Request, body: ResetRequest) -> dict:
    eng = _engine(request)
    if body.confirm != "RESET":
        raise HTTPException(400, "type RESET to confirm")
    new_id = eng.reset_account()
    return {"paper": True, "account_id": new_id, "message": "Paper account reset. Trading is stopped until you press Start."}


@router.post("/admin/shutdown")
async def shutdown(request: Request) -> dict:
    """Graceful stop for stop.ps1. Accepted only from this computer."""
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "shutdown is only accepted from localhost")
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(409, "not running under the bundled launcher; stop it with Ctrl+C")
    _engine(request).paper.log_event("info", "control", "Shutdown requested via stop.ps1")
    server.should_exit = True
    return {"paper": True, "message": "Shutting down gracefully"}


# ---------------------------------------------------------------------------- CSV export

EXPORTS = {
    "orders": ("SELECT * FROM orders WHERE account_id=? ORDER BY submitted_at, side", True),
    "fills": ("SELECT id, order_id, window_id, market_ticker, side, level_price, price, quantity, principal, fee, liquidity, "
              "observation_id, trigger_observation_id, filled_at, calc_json FROM fills WHERE account_id=? ORDER BY filled_at", True),
    "settlements": ("SELECT * FROM settlements WHERE account_id=? ORDER BY applied_at", True),
    "windows": ("SELECT * FROM windows WHERE account_id=? ORDER BY window_start", True),
    "ledger": ("SELECT * FROM ledger WHERE account_id=? ORDER BY id", True),
    "equity": ("SELECT * FROM equity_snapshots WHERE account_id=? ORDER BY id", True),
    "comparisons": ("SELECT * FROM price_comparisons ORDER BY id", False),
}


@router.get("/export/{kind}.csv")
async def export(request: Request, kind: str) -> StreamingResponse:
    eng = _engine(request)
    if kind not in EXPORTS:
        raise HTTPException(404, f"unknown export {kind!r}; choose from {sorted(EXPORTS)}")
    sql, scoped = EXPORTS[kind]
    cur = eng.conn.execute(sql, (eng.paper.account_id,) if scoped else ())
    buf = io.StringIO()
    writer = csv.writer(buf)
    buf.write(f"# PAPER TRADING export ({eng.cfg.mode} mode) generated {iso(eng.clock.now())} — simulated, not exchange records\n")
    writer.writerow([c[0] for c in cur.description])
    for r in cur:
        writer.writerow(list(r))
    data = buf.getvalue()
    stamp = eng.clock.now().strftime("%Y%m%dT%H%M%SZ")
    prefix = "PREVIEW-" if eng.cfg.is_preview else ""
    return StreamingResponse(
        iter([data]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{prefix}paper-{kind}-{stamp}.csv"'},
    )

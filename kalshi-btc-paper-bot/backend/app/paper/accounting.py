"""Paper account ledger: reservations, fills, pair netting, order closing and settlement.

Every function here must be called inside ``db.tx(conn)``. Cash only moves through
``_post_cash`` which writes a ledger row with a UNIQUE ``ref``; a repeated call with the
same ref raises ``sqlite3.IntegrityError`` and rolls the whole transaction back, so money
can never be credited or debited twice.

Opposing positions: Kalshi keeps a single signed position per market (``position_fp``:
positive = YES, negative = NO), so buying NO while long YES offsets the YES position, and
each offsetting YES+NO pair is worth exactly $1.00 whatever the outcome. Both order legs
are preserved for analysis. In the default ``net_on_fill`` mode the $1.00 per pair is
credited once, when the pair forms (ledger ref ``pair:<window>:<cumulative pairs>``), and
settlement pays only the unmatched remainder; in ``hold_to_settlement`` mode nothing is
credited early and settlement pays every winning contract. In both modes:

    settlement payout = (up_qty - pairs_credited) * yes_payout + (down_qty - pairs_credited) * no_payout

so a pair is paid exactly once.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .fees import FeeSchedule, fee_for_fill, max_fee_reservation
from .fill_model import FillLeg
from ..money import ONE, ZERO, D, money, s
from ..schedule import iso

WORKING_STATUSES = ("pending", "partially_filled")


class InsufficientFunds(Exception):
    def __init__(self, required: Decimal, available: Decimal):
        self.required = required
        self.available = available
        super().__init__(f"Insufficient paper funds: need ${money(required)}, available ${money(available)}")


# --------------------------------------------------------------------------- account


def current_account(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM accounts WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()


def ensure_account(conn: sqlite3.Connection, now: datetime, starting_balance: Decimal) -> int:
    """Create the paper account exactly once (persisted across restarts)."""
    row = current_account(conn)
    if row is not None:
        return int(row["id"])
    return create_account(conn, now, starting_balance)


def create_account(conn: sqlite3.Connection, now: datetime, starting_balance: Decimal) -> int:
    cur = conn.execute(
        "INSERT INTO accounts(created_at, starting_balance, cash_balance, status) VALUES(?, ?, '0', 'active')",
        (iso(now), s(starting_balance)),
    )
    account_id = int(cur.lastrowid)
    _post_cash(conn, account_id, now, "deposit", starting_balance, f"deposit:{account_id}", memo="Initial paper balance")
    return account_id


def _post_cash(
    conn: sqlite3.Connection,
    account_id: int,
    now: datetime,
    kind: str,
    amount: Decimal,
    ref: str,
    window_id: str | None = None,
    order_id: str | None = None,
    memo: str | None = None,
) -> Decimal:
    row = conn.execute("SELECT cash_balance FROM accounts WHERE id=?", (account_id,)).fetchone()
    cash_after = D(row["cash_balance"]) + amount
    conn.execute(
        "INSERT INTO ledger(account_id, ts, kind, amount, cash_after, ref, window_id, order_id, memo) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, iso(now), kind, s(amount), s(cash_after), ref, window_id, order_id, memo),
    )
    conn.execute("UPDATE accounts SET cash_balance=? WHERE id=?", (s(cash_after), account_id))
    return cash_after


def reserved_cash(conn: sqlite3.Connection, account_id: int) -> Decimal:
    total = ZERO
    for r in conn.execute(
        "SELECT reserved_remaining FROM orders WHERE account_id=? AND status IN ('pending','partially_filled')",
        (account_id,),
    ):
        total += D(r["reserved_remaining"])
    return total


def available_cash(conn: sqlite3.Connection, account_id: int) -> Decimal:
    acct = conn.execute("SELECT cash_balance FROM accounts WHERE id=?", (account_id,)).fetchone()
    return D(acct["cash_balance"]) - reserved_cash(conn, account_id)


# --------------------------------------------------------------------------- entry


@dataclass(frozen=True)
class EntryPlan:
    window_id: str
    account_id: int
    window_start: datetime
    window_end: datetime
    market_ticker: str
    event_ticker: str
    market_title: str
    market_url: str
    strike_type: str | None
    floor_strike: Decimal | None
    rules_primary: str
    market_open_time: datetime | None
    market_close_time: datetime
    limit_price: Decimal
    quantity: Decimal
    fee_mode: str
    fee_schedule: FeeSchedule
    settings_json: dict
    pair_accounting: str


def window_id_for(account_id: int, window_start: datetime) -> str:
    return f"A{account_id}-{window_start.strftime('%Y%m%dT%H%MZ')}"


def required_reservation(plan: EntryPlan) -> tuple[Decimal, Decimal]:
    """(per-order principal reservation, per-order fee reservation)."""
    principal = plan.limit_price * plan.quantity
    fee = max_fee_reservation(plan.fee_schedule, plan.quantity, plan.limit_price, plan.fee_mode)
    return principal, fee


def enter_window(conn: sqlite3.Connection, plan: EntryPlan, now: datetime) -> None:
    """Create the window and BOTH virtual orders atomically, reserving cash for both.

    Raises InsufficientFunds (nothing written) or sqlite3.IntegrityError if the window or
    its orders already exist (duplicate entry after restart is impossible).
    """
    principal, fee_res = required_reservation(plan)
    per_order = principal + fee_res
    need = per_order * 2
    avail = available_cash(conn, plan.account_id)
    if avail < need:
        raise InsufficientFunds(need, avail)
    conn.execute(
        """INSERT INTO windows(id, account_id, window_start, window_end, eligible, status, market_ticker,
               event_ticker, market_title, market_url, strike_type, floor_strike, rules_primary, market_open_time,
               market_close_time, settings_json, fee_schedule_json, pair_accounting, market_status, created_at, updated_at)
           VALUES(?, ?, ?, ?, 1, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            plan.window_id, plan.account_id, iso(plan.window_start), iso(plan.window_end), plan.market_ticker,
            plan.event_ticker, plan.market_title, plan.market_url, plan.strike_type, s(plan.floor_strike),
            plan.rules_primary, iso(plan.market_open_time), iso(plan.market_close_time),
            json.dumps(plan.settings_json), json.dumps(plan.fee_schedule.to_json()), plan.pair_accounting,
            iso(now), iso(now),
        ),
    )
    for side, kalshi_side in (("UP", "yes"), ("DOWN", "no")):
        conn.execute(
            """INSERT INTO orders(id, account_id, window_id, market_ticker, side, kalshi_side, limit_price, quantity,
                   fee_reserved, reserved_remaining, status, submitted_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                f"{plan.window_id}:{side}", plan.account_id, plan.window_id, plan.market_ticker, side, kalshi_side,
                s(plan.limit_price), s(plan.quantity), s(fee_res), s(per_order), iso(now), iso(now),
            ),
        )


def record_skip(
    conn: sqlite3.Connection,
    account_id: int,
    window_start: datetime,
    window_end: datetime,
    eligible: bool,
    reason: str,
    now: datetime,
    market_ticker: str | None = None,
) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO windows(id, account_id, window_start, window_end, eligible, status, skip_reason,
               market_ticker, created_at, updated_at)
           VALUES(?, ?, ?, ?, ?, 'skipped', ?, ?, ?, ?)""",
        (window_id_for(account_id, window_start), account_id, iso(window_start), iso(window_end),
         1 if eligible else 0, reason, market_ticker, iso(now), iso(now)),
    )
    return cur.rowcount > 0


# --------------------------------------------------------------------------- fills


def consumed_by_level(conn: sqlite3.Connection, order_id: str) -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    for r in conn.execute("SELECT level_price, quantity FROM fills WHERE order_id=?", (order_id,)):
        price = D(r["level_price"])
        out[price] = out.get(price, ZERO) + D(r["quantity"])
    return out


def store_observation(conn: sqlite3.Connection, book_json: dict[str, Any], market_status: str | None) -> int:
    key = f"{book_json['ticker']}|{book_json['requested_at']}|{book_json['source']}"
    conn.execute(
        """INSERT OR IGNORE INTO observations(obs_key, market_ticker, requested_at, received_at, server_date, source,
               market_status, book_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
        (key, book_json["ticker"], book_json["requested_at"], book_json["received_at"], book_json.get("server_date"),
         book_json["source"], market_status, json.dumps(book_json)),
    )
    return int(conn.execute("SELECT id FROM observations WHERE obs_key=?", (key,)).fetchone()["id"])


def apply_fill(
    conn: sqlite3.Connection,
    order_id: str,
    leg: FillLeg,
    observation_id: int,
    trigger_observation_id: int | None,
    fill_time: datetime,
    fee_schedule: FeeSchedule,
    fee_mode: str,
    fee_waiver_until: datetime | None,
    calc_detail: dict,
) -> dict | None:
    """Apply one simulated fill leg. Returns the fill record, or None if it was a duplicate."""
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if order is None or order["status"] not in WORKING_STATUSES:
        return None
    fill_id = f"{order_id}:obs{observation_id}:{s(leg.price)}"
    if conn.execute("SELECT 1 FROM fills WHERE id=?", (fill_id,)).fetchone():
        return None  # same observation, same level: never fill twice
    quantity_total = D(order["quantity"])
    filled_before = D(order["filled_qty"])
    qty = min(leg.quantity, quantity_total - filled_before)
    if qty <= ZERO:
        return None
    price = leg.price
    limit = D(order["limit_price"])
    if price > limit:
        raise AssertionError("fill above limit price")  # defensive: the fill model never does this
    principal = price * qty
    fee, fee_exact_total, fee_record = fee_for_fill(
        fee_schedule, fee_mode, qty, price, D(order["fee_exact_total"]), D(order["fees_paid"]), fill_time,
        fee_waiver_until,
    )
    account_id = int(order["account_id"])
    window_id = order["window_id"]
    calc = {
        **calc_detail,
        "leg": leg.to_json(),
        "quantity_filled": s(qty),
        "principal": s(principal),
        "fee": fee_record,
        "model": "SIMULATED fill estimated from displayed order-book asks; not an exchange-confirmed execution",
    }
    conn.execute(
        """INSERT INTO fills(id, account_id, order_id, window_id, market_ticker, side, level_price, price, quantity,
               principal, fee, liquidity, observation_id, trigger_observation_id, filled_at, calc_json)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'taker', ?, ?, ?, ?)""",
        (fill_id, account_id, order_id, window_id, order["market_ticker"], order["side"], s(leg.price), s(price),
         s(qty), s(principal), s(fee), observation_id, trigger_observation_id, iso(fill_time), json.dumps(calc)),
    )
    _post_cash(conn, account_id, fill_time, "buy", -principal, f"fill:{fill_id}", window_id, order_id,
               f"Bought {s(qty)} {order['side']} @ {s(price)}")
    if fee > ZERO:
        _post_cash(conn, account_id, fill_time, "fee", -fee, f"fee:{fill_id}", window_id, order_id,
                   "Estimated Kalshi taker fee")

    filled_after = filled_before + qty
    status = "filled" if filled_after >= quantity_total else "partially_filled"
    fee_reserved = D(order["fee_reserved"])
    fees_paid = D(order["fees_paid"]) + fee
    reserved_remaining = limit * (quantity_total - filled_after) + max(fee_reserved - fees_paid, ZERO)
    if status == "filled":
        reserved_remaining = ZERO
    conn.execute(
        """UPDATE orders SET filled_qty=?, principal_cost=?, fees_paid=?, fee_exact_total=?, reserved_remaining=?,
               status=?, first_fill_at=COALESCE(first_fill_at, ?), last_fill_at=?, updated_at=?,
               closed_at=CASE WHEN ?='filled' THEN ? ELSE closed_at END,
               close_reason=CASE WHEN ?='filled' THEN 'filled' ELSE close_reason END
           WHERE id=?""",
        (s(filled_after), s(D(order["principal_cost"]) + principal), s(fees_paid), s(fee_exact_total),
         s(reserved_remaining), status, iso(fill_time), iso(fill_time), iso(fill_time),
         status, iso(fill_time), status, order_id),
    )
    _update_window_after_fill(conn, window_id, order["side"], qty, principal, fee, fill_time)
    return {"id": fill_id, "order_id": order_id, "side": order["side"], "price": s(price), "quantity": s(qty),
            "fee": s(fee)}


def _update_window_after_fill(
    conn: sqlite3.Connection, window_id: str, side: str, qty: Decimal, principal: Decimal, fee: Decimal, now: datetime
) -> None:
    w = conn.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
    up_qty, up_cost = D(w["up_qty"]), D(w["up_cost"])
    down_qty, down_cost = D(w["down_qty"]), D(w["down_cost"])
    if side == "UP":
        up_qty, up_cost = up_qty + qty, up_cost + principal
    else:
        down_qty, down_cost = down_qty + qty, down_cost + principal
    fees = D(w["fees"]) + fee
    credited = D(w["pairs_credited"])
    credit_amount = D(w["pair_credit_amount"])
    if w["pair_accounting"] == "net_on_fill":
        pairs = min(up_qty, down_qty)
        new_pairs = pairs - credited
        if new_pairs > ZERO:
            amount = new_pairs * ONE
            _post_cash(conn, int(w["account_id"]), now, "pair_netting", amount, f"pair:{window_id}:{s(pairs)}",
                       window_id, None, f"{s(new_pairs)} UP+DOWN pair(s) offset; $1.00 per pair returned")
            credited = pairs
            credit_amount += amount
    conn.execute(
        """UPDATE windows SET up_qty=?, up_cost=?, down_qty=?, down_cost=?, fees=?, pairs_credited=?,
               pair_credit_amount=?, updated_at=? WHERE id=?""",
        (s(up_qty), s(up_cost), s(down_qty), s(down_cost), s(fees), s(credited), s(credit_amount), iso(now), window_id),
    )


# --------------------------------------------------------------------------- close / cancel


def close_window_orders(conn: sqlite3.Connection, window_id: str, now: datetime, status: str, reason: str) -> int:
    """Cancel unfilled remainders (status 'expired' at close, 'canceled' on pause) and release reservations."""
    assert status in ("expired", "canceled")
    cur = conn.execute(
        """UPDATE orders SET status=?, close_reason=?, closed_at=?, reserved_remaining='0', updated_at=?
           WHERE window_id=? AND status IN ('pending','partially_filled')""",
        (status, reason, iso(now), iso(now), window_id),
    )
    w = conn.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
    if w is not None and w["status"] == "active":
        conn.execute(
            "UPDATE windows SET status='awaiting_settlement', orders_closed_at=?, outcome_class=?, updated_at=? WHERE id=?",
            (iso(now), classify_window(conn, window_id), iso(now), window_id),
        )
    return cur.rowcount


def classify_window(conn: sqlite3.Connection, window_id: str) -> str:
    orders = {r["side"]: r for r in conn.execute("SELECT * FROM orders WHERE window_id=?", (window_id,))}
    up = orders.get("UP")
    down = orders.get("DOWN")
    up_f = D(up["filled_qty"]) if up else ZERO
    down_f = D(down["filled_qty"]) if down else ZERO
    up_full = up is not None and up_f >= D(up["quantity"])
    down_full = down is not None and down_f >= D(down["quantity"])
    if up_f > ZERO and down_f > ZERO:
        return "both_filled" if (up_full and down_full) else "both_partial"
    if up_f > ZERO:
        return "up_only"
    if down_f > ZERO:
        return "down_only"
    return "no_fill"


# --------------------------------------------------------------------------- settlement


FINAL_STATUSES = ("finalized", "settled")


def settlement_payouts(status: str, result: str, settlement_value: Decimal | None) -> tuple[Decimal, Decimal] | str:
    """Return (yes_payout, no_payout) per contract for an officially settled market, or a
    human-readable reason why settlement cannot be applied yet."""
    if status not in FINAL_STATUSES:
        return {
            "initialized": "market not open yet",
            "inactive": "market paused/inactive",
            "active": "market still active",
            "closed": "trading closed; awaiting Kalshi determination",
            "determined": "result determined; awaiting Kalshi finalization (settlement timer)",
            "disputed": "result disputed; waiting for Kalshi resolution",
            "amended": "result amended; awaiting finalization",
        }.get(status, f"unrecognized market status {status!r}; waiting")
    if result == "yes":
        if settlement_value is not None and settlement_value != ONE:
            return f"inconsistent settlement: result yes but settlement_value {s(settlement_value)}"
        return ONE, ZERO
    if result == "no":
        if settlement_value is not None and settlement_value != ZERO:
            return f"inconsistent settlement: result no but settlement_value {s(settlement_value)}"
        return ZERO, ONE
    if settlement_value is not None and ZERO <= settlement_value <= ONE:
        return settlement_value, ONE - settlement_value  # unusual (e.g. scalar/void) settlement value
    return f"finalized without a usable result (result={result!r}); waiting"


def apply_settlement(
    conn: sqlite3.Connection,
    window_id: str,
    market_status: str,
    result: str,
    yes_payout: Decimal,
    no_payout: Decimal,
    kalshi_settled_ts: datetime | None,
    raw: dict,
    now: datetime,
) -> dict | None:
    """Apply settlement exactly once. Returns the settlement record, or None if already applied."""
    w = conn.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
    if w is None or w["status"] != "awaiting_settlement":
        return None
    if conn.execute("SELECT 1 FROM settlements WHERE window_id=?", (window_id,)).fetchone():
        return None
    if conn.execute(
        "SELECT 1 FROM orders WHERE window_id=? AND status IN ('pending','partially_filled')", (window_id,)
    ).fetchone():
        raise AssertionError("cannot settle a window with working orders")
    account_id = int(w["account_id"])
    up_qty, down_qty = D(w["up_qty"]), D(w["down_qty"])
    credited = D(w["pairs_credited"])
    payout = (up_qty - credited) * yes_payout + (down_qty - credited) * no_payout
    principal = D(w["up_cost"]) + D(w["down_cost"])
    fees = D(w["fees"])
    net = D(w["pair_credit_amount"]) + payout - principal - fees
    conn.execute(
        """INSERT INTO settlements(window_id, account_id, market_ticker, market_status, result, yes_payout, no_payout,
               kalshi_settled_ts, up_qty, down_qty, pairs_credited, payout, principal, fees, net_pnl, applied_at, raw_json)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (window_id, account_id, w["market_ticker"], market_status, result, s(yes_payout), s(no_payout),
         iso(kalshi_settled_ts), s(up_qty), s(down_qty), s(credited), s(payout), s(principal), s(fees), s(net),
         iso(now), json.dumps(raw, default=str)),
    )
    if payout > ZERO:
        _post_cash(conn, account_id, now, "settlement", payout, f"settle:{window_id}", window_id, None,
                   f"Settlement {w['market_ticker']} result={result}")
    yes_mark, no_mark = yes_payout, no_payout
    conn.execute(
        """UPDATE windows SET status='settled', market_status=?, market_result=?, settlement_value=?, payout=?,
               net_pnl=?, settled_at=?, mark_up=?, mark_down=?, mark_source='settlement', mark_ts=?,
               settlement_note=NULL, updated_at=? WHERE id=?""",
        (market_status, result, s(yes_payout), s(payout), s(net), iso(now), s(yes_mark), s(no_mark), iso(now),
         iso(now), window_id),
    )
    conn.execute("UPDATE orders SET settled=1, updated_at=? WHERE window_id=?", (iso(now), window_id))
    return {"window_id": window_id, "payout": s(payout), "net_pnl": s(net), "result": result}


# --------------------------------------------------------------------------- summary


def window_position_value(w: sqlite3.Row) -> Decimal:
    """Estimated value of a window's open (unsettled) position.

    Uncredited UP/DOWN pairs are worth exactly $1.00; unmatched contracts are marked at
    the latest fresh best bid for that side (a conservative liquidation estimate), or at
    the determined payout once Kalshi has determined the result.
    """
    if w["status"] in ("settled", "skipped", "abandoned"):
        return ZERO
    up, down = D(w["up_qty"]), D(w["down_qty"])
    pairs = min(up, down)
    uncredited_pairs = pairs - D(w["pairs_credited"])
    mark_up = D(w["mark_up"], default=ZERO)
    mark_down = D(w["mark_down"], default=ZERO)
    return uncredited_pairs * ONE + (up - pairs) * mark_up + (down - pairs) * mark_down


def account_summary(conn: sqlite3.Connection, account_id: int) -> dict:
    acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    cash = D(acct["cash_balance"])
    starting = D(acct["starting_balance"])
    reserved = reserved_cash(conn, account_id)
    position_value = ZERO
    open_cost = ZERO
    open_up = open_down = ZERO
    unmatched_cost = ZERO
    for w in conn.execute(
        "SELECT * FROM windows WHERE account_id=? AND status IN ('active','awaiting_settlement')", (account_id,)
    ):
        position_value += window_position_value(w)
        open_cost += D(w["up_cost"]) + D(w["down_cost"])
        up, down = D(w["up_qty"]), D(w["down_qty"])
        open_up += up
        open_down += down
        pairs = min(up, down)
        if up > pairs and up > ZERO:
            unmatched_cost += D(w["up_cost"]) * (up - pairs) / up
        if down > pairs and down > ZERO:
            unmatched_cost += D(w["down_cost"]) * (down - pairs) / down
    realized = ZERO
    for r in conn.execute("SELECT net_pnl FROM settlements WHERE account_id=?", (account_id,)):
        realized += D(r["net_pnl"])
    fees_total = ZERO
    for r in conn.execute("SELECT amount FROM ledger WHERE account_id=? AND kind='fee'", (account_id,)):
        fees_total -= D(r["amount"])
    equity = cash + position_value
    unrealized = equity - starting - realized
    return {
        "account_id": account_id,
        "created_at": acct["created_at"],
        "starting_balance": starting,
        "cash": cash,
        "reserved": reserved,
        "available": cash - reserved,
        "position_value": position_value,
        "open_cost": open_cost,
        "open_up_qty": open_up,
        "open_down_qty": open_down,
        "unmatched_cost": unmatched_cost,
        "equity": equity,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": equity - starting,
        "fees_total": fees_total,
    }


def snapshot_equity(conn: sqlite3.Connection, account_id: int, now: datetime, reason: str) -> dict:
    summ = account_summary(conn, account_id)
    conn.execute(
        """INSERT INTO equity_snapshots(account_id, ts, cash, reserved, position_value, equity, realized_pnl,
               unrealized_pnl, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, iso(now), s(summ["cash"]), s(summ["reserved"]), s(summ["position_value"]), s(summ["equity"]),
         s(summ["realized_pnl"]), s(summ["unrealized_pnl"]), reason),
    )
    return summ

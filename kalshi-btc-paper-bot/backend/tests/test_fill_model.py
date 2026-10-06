"""Pure tests of the executable-ask fill model (no database, no network)."""

from datetime import timedelta
from decimal import Decimal as Dec

from app.marketdata.models import OrderBook
from app.paper.fill_model import Gates, OrderView, check_gates, step

from conftest import T, book_payload

SUBMIT = T(15, 0, 1)
CLOSE = T(15, 15)


def order(side="UP", filled="0", status="pending", qty="20", limit="0.37"):
    return OrderView(id=f"W:{side}", side=side, kalshi_side="yes" if side == "UP" else "no",
                     limit_price=Dec(limit), quantity=Dec(qty), filled_qty=Dec(filled), submitted_at=SUBMIT,
                     status=status)


def book(at, yes=(), no=()):
    return OrderBook.from_api("M", book_payload(list(yes), list(no)), at, at + timedelta(milliseconds=50))


def gates(now, **over):
    g = dict(now=now, close_time=CLOSE, book_problems=(), market_status="active", market_status_at=now,
             trading_active=True, exchange_status_at=now, stale_after=6.0, market_status_stale=20.0,
             exchange_status_stale=120.0, engine_trading=True)
    g.update(over)
    return Gates(**g)


def run_two_step(o, first, second, consumed=None, delay=2.0):
    """Trigger on ``first`` then confirm on ``second``; returns the confirmation result."""
    consumed = consumed or {}
    r1 = step(o, first, gates(first.requested_at), consumed, None, delay)
    assert r1.trigger is not None, r1
    assert not r1.legs  # nothing fills on the trigger observation itself
    return step(o, second, gates(second.requested_at), consumed, r1.trigger, delay)


def test_ask_40_with_bid_and_last_at_37_never_fills():
    # A YES bid sitting at 0.37 (and a last trade at 0.37) is NOT an executable buy at 0.37.
    b = book(T(15, 1), yes=[("0.37", "100.00")], no=[("0.60", "100.00")])  # YES ask = 0.40
    r = step(order("UP"), b, gates(T(15, 1)), {}, None, 2.0)
    assert r.trigger is None and not r.legs
    assert r.detail["best_ask"] == "0.4"


def test_eight_at_37_next_at_39_fills_at_most_eight():
    b1 = book(T(15, 1), no=[("0.63", "8.00"), ("0.61", "50.00")])
    b2 = book(T(15, 1, 2), no=[("0.63", "8.00"), ("0.61", "50.00")])
    r = run_two_step(order("UP"), b1, b2)
    assert [(l.price, l.quantity) for l in r.legs] == [(Dec("0.37"), Dec("8.00"))]


def test_price_improvement_and_multi_level_walk():
    b1 = book(T(15, 1), no=[("0.67", "3.00"), ("0.64", "4.00"), ("0.63", "100.00")])
    b2 = book(T(15, 1, 3), no=[("0.67", "3.00"), ("0.64", "4.00"), ("0.63", "100.00")])
    r = run_two_step(order("UP"), b1, b2)
    assert [(l.price, l.quantity) for l in r.legs] == [
        (Dec("0.33"), Dec("3.00")), (Dec("0.36"), Dec("4.00")), (Dec("0.37"), Dec("13.00"))]
    assert all(l.price <= Dec("0.37") for l in r.legs)


def test_revalidation_after_delay_uses_the_fresh_book():
    b1 = book(T(15, 1), no=[("0.63", "20.00")])
    gone = book(T(15, 1, 2), no=[("0.60", "20.00")])  # ask moved to 0.40 during the delay
    r = run_two_step(order("UP"), b1, gone)
    assert not r.legs and "no longer executable" in r.event
    shrunk = book(T(15, 1, 2), no=[("0.63", "5.00")])
    r2 = run_two_step(order("UP"), b1, shrunk)
    assert [(l.price, l.quantity) for l in r2.legs] == [(Dec("0.37"), Dec("5.00"))]


def test_confirmation_must_wait_for_execution_delay():
    o = order("UP")
    b1 = book(T(15, 1), no=[("0.63", "20.00")])
    r1 = step(o, b1, gates(T(15, 1)), {}, None, 2.0)
    early = book(T(15, 1, 1), no=[("0.63", "20.00")])
    r2 = step(o, early, gates(T(15, 1, 1)), {}, r1.trigger, 2.0)
    assert r2.trigger is r1.trigger and not r2.legs


def test_consumed_liquidity_is_not_reused_and_only_growth_is_available():
    o = order("UP", filled="8", status="partially_filled")
    consumed = {Dec("0.37"): Dec("8.00")}
    same = book(T(15, 2), no=[("0.63", "8.00")])
    r = step(o, same, gates(T(15, 2)), consumed, None, 2.0)
    assert r.trigger is None and not r.legs  # unchanged snapshot: nothing new
    grown1 = book(T(15, 3), no=[("0.63", "12.50")])
    grown2 = book(T(15, 3, 2), no=[("0.63", "12.50")])
    r2 = run_two_step(o, grown1, grown2, consumed)
    assert [(l.price, l.quantity) for l in r2.legs] == [(Dec("0.37"), Dec("4.50"))]  # fractional, not truncated


def test_never_exceeds_remaining_quantity():
    o = order("UP", filled="18", status="partially_filled")
    b1 = book(T(15, 2), no=[("0.63", "500.00")])
    b2 = book(T(15, 2, 2), no=[("0.63", "500.00")])
    r = run_two_step(o, b1, b2)
    assert sum(l.quantity for l in r.legs) == Dec("2")


def test_up_and_down_use_opposite_bid_ladders_independently():
    b1 = book(T(15, 1), yes=[("0.64", "6.00")], no=[("0.20", "9.00")])  # NO ask 0.36; YES ask 0.80
    b2 = book(T(15, 1, 2), yes=[("0.64", "6.00")], no=[("0.20", "9.00")])
    assert step(order("UP"), b1, gates(T(15, 1)), {}, None, 2.0).trigger is None
    r = run_two_step(order("DOWN"), b1, b2)
    assert [(l.price, l.quantity) for l in r.legs] == [(Dec("0.36"), Dec("6.00"))]


def test_gates_block_stale_closed_inactive_paused_invalid():
    o = order("UP")
    b = book(T(15, 5), no=[("0.63", "20.00")])
    assert "stale" in check_gates(o, b, gates(T(15, 5, 10)))
    assert "closed" in check_gates(o, b, gates(T(15, 15, 0)))
    late = book(T(15, 15, 0), no=[("0.63", "20.00")])
    assert check_gates(o, late, gates(T(15, 14, 59), close_time=T(15, 15))) is not None
    assert "not active" in check_gates(o, b, gates(T(15, 5), market_status="inactive"))
    assert "trading paused" in check_gates(o, b, gates(T(15, 5), trading_active=False))
    assert "market status stale" in check_gates(o, b, gates(T(15, 5), market_status_at=T(15, 4)))
    assert "invalid order book" in check_gates(o, b, gates(T(15, 5), book_problems=("crossed book",)))
    assert "engine paused" in check_gates(o, b, gates(T(15, 5), engine_trading=False))
    early = book(T(15, 0, 0), no=[("0.63", "20.00")])
    assert "predates" in check_gates(o, early, gates(T(15, 0, 0)))


def test_pending_trigger_is_dropped_when_data_goes_stale():
    o = order("UP")
    b1 = book(T(15, 1), no=[("0.63", "20.00")])
    r1 = step(o, b1, gates(T(15, 1)), {}, None, 2.0)
    stale = book(T(15, 1, 3), no=[("0.63", "20.00")])
    r2 = step(o, stale, gates(T(15, 1, 30)), {}, r1.trigger, 2.0)
    assert r2.trigger is None and not r2.legs and "stale" in r2.blocked

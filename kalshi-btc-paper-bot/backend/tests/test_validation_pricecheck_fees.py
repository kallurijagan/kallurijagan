"""Market/side validation, visible-price comparison, fees and the single-engine guard."""

from datetime import timedelta
from decimal import Decimal as Dec

import pytest

from app.db import connect, init_schema
from app.instance_lock import EngineAlreadyRunning, FileLock, acquire_lease, release_lease
from app.marketdata.discovery import market_url, validate_window_market
from app.marketdata.models import MarketInfo
from app.marketdata.pricecheck import Tick, compare_visible_price
from app.paper.fees import FeeSchedule, fee_for_fill, max_fee_reservation, resolve_schedule

from conftest import T, market_payload

START = T(15, 0)
END = T(15, 15)


def mk(**over):
    return MarketInfo.from_api(market_payload(START, **over))


# ------------------------------------------------------------------ market & side validation


def test_valid_15_minute_market_maps_up_to_yes():
    chk = validate_window_market([mk()], "KXBTC15M", START, END)
    assert chk.ok, chk.reason
    assert any("YES = UP, NO = DOWN" in c for c in chk.checks)
    assert market_url("KXBTC15M", chk.market.event_ticker) == \
        "https://kalshi.com/markets/kxbtc15m/bitcoin-price-up-down/kxbtc15m-26oct061115"


def test_rejects_hourly_market_with_same_close():
    hourly = mk(open_time=(START - timedelta(minutes=45)).isoformat())
    chk = validate_window_market([hourly], "KXBTC15M", START, END)
    assert not chk.ok and not chk.retryable and "not a 15-minute window market" in chk.reason
    late = mk(open_time=(START + timedelta(minutes=5)).isoformat())
    assert "after the window start" in validate_window_market([late], "KXBTC15M", START, END).reason


def test_market_listed_a_few_minutes_early_is_accepted_with_warning():
    early = mk(open_time=(START - timedelta(minutes=3)).isoformat())
    chk = validate_window_market([early], "KXBTC15M", START, END)
    assert chk.ok and any("opened early" in w for w in chk.warnings)


def test_not_yet_filled_fields_are_retryable_but_structural_mismatches_are_not():
    for over, text in (({"floor_strike": None}, "not set yet"), ({"strike_type": None}, "not set yet"),
                       ({"rules_primary": ""}, "not published yet")):
        chk = validate_window_market([mk(**over)], "KXBTC15M", START, END)
        assert not chk.ok and chk.retryable and text in chk.reason, over
    for over in ({"strike_type": "less"}, {"cap_strike": 63000}, {"market_type": "scalar"},
                 {"notional_value_dollars": "10.0000"}):
        chk = validate_window_market([mk(**over)], "KXBTC15M", START, END)
        assert not chk.ok and not chk.retryable, over


def test_event_ticker_in_repeated_dst_hour_matches_either_instant():
    from datetime import datetime, timezone
    # 2026-11-01 01:30 New York happens twice: 05:30Z (EDT) and 06:30Z (EST).
    start = datetime(2026, 11, 1, 6, 15, tzinfo=timezone.utc)
    m = MarketInfo.from_api(market_payload(start, event_ticker="KXBTC15M-26NOV010130",
                                           ticker="KXBTC15M-26NOV010130-30"))
    chk = validate_window_market([m], "KXBTC15M", start, start + timedelta(minutes=15))
    assert chk.ok, chk.reason


def test_rejects_other_series_and_wrong_close():
    other = MarketInfo.from_api(market_payload(START, series="KXBTCD"))
    chk = validate_window_market([other], "KXBTC15M", START, END)
    assert not chk.ok and chk.retryable
    wrong_close = mk(close_time=(END + timedelta(minutes=15)).isoformat())
    assert not validate_window_market([wrong_close], "KXBTC15M", START, END).ok


def test_rejects_unverifiable_up_down_mapping():
    assert "cannot verify" in validate_window_market([mk(strike_type="less")], "KXBTC15M", START, END).reason
    assert "floor_strike" in validate_window_market([mk(floor_strike=None)], "KXBTC15M", START, END).reason
    assert "range market" in validate_window_market([mk(cap_strike=63000)], "KXBTC15M", START, END).reason
    assert "Bitcoin" in validate_window_market([mk(title="ETH up?", rules_primary="ETH rules", yes_sub_title="x")],
                                               "KXBTC15M", START, END).reason
    assert "binary" in validate_window_market([mk(market_type="scalar")], "KXBTC15M", START, END).reason
    assert "contract value" in validate_window_market([mk(notional_value_dollars="10.0000")], "KXBTC15M", START, END).reason


def test_rejects_ambiguous_and_ticker_time_mismatch():
    a = mk()
    b = mk(ticker=a.ticker + "B")
    amb = validate_window_market([a, b], "KXBTC15M", START, END)
    assert "Ambiguous" in amb.reason and amb.retryable
    bad = mk(event_ticker="KXBTC15M-26OCT061100", ticker="KXBTC15M-26OCT061100-00")
    assert "encodes close" in validate_window_market([bad], "KXBTC15M", START, END).reason


# ------------------------------------------------------------------ visible price comparison


def ticks(at):
    return [
        Tick(at, "book", yes_bid=Dec("0.36"), yes_ask=Dec("0.40"), no_bid=Dec("0.60"), no_ask=Dec("0.64")),
        Tick(at + timedelta(seconds=1), "summary", yes_bid=Dec("0.36"), yes_ask=Dec("0.40"), no_bid=Dec("0.60"),
             no_ask=Dec("0.64"), last_price=Dec("0.37")),
    ]


def test_buy_quote_matching_executable_ask_is_consistent():
    r = compare_visible_price(Dec("0.40"), "UP", "buy", T(15, 5), ticks(T(15, 5)))
    assert r.verdict == "consistent" and r.measure == "order-book executable ask"


def test_visible_37_vs_ask_40_reports_last_trade_evidence_without_altering_book():
    r = compare_visible_price(Dec("0.37"), "UP", "buy", T(15, 5), ticks(T(15, 5)))
    assert r.verdict == "discrepancy" and r.api_value == Dec("0.40")
    assert any("last trade" in n for n in r.notes)


def test_unknown_quote_type_and_distant_time_are_inconclusive():
    assert compare_visible_price(Dec("0.40"), "UP", "unknown", T(15, 5), ticks(T(15, 5))).verdict == "inconclusive"
    far = compare_visible_price(Dec("0.40"), "UP", "buy", T(15, 6), ticks(T(15, 5)), max_skew_seconds=10)
    assert far.verdict == "inconclusive" and "Timing" in far.notes[0]


def test_display_rounding_and_no_side_derivations():
    t = [Tick(T(15, 5), "book", Dec("0.364"), Dec("0.366"), Dec("0.63"), Dec("0.636"))]
    assert compare_visible_price(Dec("0.37"), "UP", "buy", T(15, 5), t).verdict == "consistent"
    r = compare_visible_price(Dec("0.63"), "DOWN", "last", T(15, 5), ticks(T(15, 5)))
    assert r.verdict == "consistent" and "derived" in r.measure
    sell = compare_visible_price(Dec("0.60"), "DOWN", "sell", T(15, 5), ticks(T(15, 5)))
    assert sell.verdict == "consistent" and sell.measure == "order-book best bid"


# ------------------------------------------------------------------ fees


SCHED = FeeSchedule("KXBTC15M", "quadratic", Dec("1"), None, "test", "")


def test_taker_fee_20_at_37_and_reservation_buffer():
    fee, exact, rec = fee_for_fill(SCHED, "taker", Dec("20"), Dec("0.37"), Dec(0), Dec(0), T(15, 1), None)
    assert fee == Dec("0.33") and exact == Dec("0.32634") and rec["liquidity"] == "taker"
    assert max_fee_reservation(SCHED, Dec("20"), Dec("0.37"), "taker") == Dec("0.52")  # 0.33 + 19 x $0.01 buffer
    assert max_fee_reservation(SCHED, Dec("20"), Dec("0.37"), "none") == 0


def test_fee_multiplier_waiver_and_none_mode():
    double = FeeSchedule("KXBTC15M", "quadratic", Dec("2"), None, "test", "")
    assert fee_for_fill(double, "taker", Dec("20"), Dec("0.37"), Dec(0), Dec(0), T(15, 1), None)[0] == Dec("0.66")
    assert fee_for_fill(SCHED, "taker", Dec("20"), Dec("0.37"), Dec(0), Dec(0), T(15, 1), T(16, 0))[0] == 0
    assert fee_for_fill(SCHED, "none", Dec("20"), Dec("0.37"), Dec(0), Dec(0), T(15, 1), None)[0] == 0


def test_scheduled_fee_change_applies_by_effective_time_and_uncertain_types_flagged():
    changed = resolve_schedule(SCHED, [(T(15, 0), "quadratic", Dec("1.5"))], T(15, 1))
    assert changed.fee_multiplier == Dec("1.5")
    assert resolve_schedule(SCHED, [(T(16, 0), "quadratic", Dec("1.5"))], T(15, 1)).fee_multiplier == Dec("1")
    assert FeeSchedule("X", "flat", Dec("1"), None, "t", "").uncertain
    assert not SCHED.uncertain


# ------------------------------------------------------------------ single-engine guard


def test_file_lock_prevents_second_engine(tmp_path):
    a = FileLock(tmp_path / "paper.engine.lock")
    a.acquire()
    with pytest.raises(EngineAlreadyRunning):
        FileLock(tmp_path / "paper.engine.lock").acquire()
    a.release()
    b = FileLock(tmp_path / "paper.engine.lock")
    b.acquire()
    b.release()


def test_db_lease_blocks_other_owner_until_stale(tmp_path):
    conn = connect(tmp_path / "x.sqlite3")
    init_schema(conn)
    acquire_lease(conn, "engine-A", T(15, 0), 20)
    with pytest.raises(EngineAlreadyRunning):
        acquire_lease(conn, "engine-B", T(15, 0, 10), 20)
    acquire_lease(conn, "engine-B", T(15, 0, 30), 20)  # A stopped heart-beating
    release_lease(conn, "engine-B")
    acquire_lease(conn, "engine-C", T(15, 0, 31), 20)

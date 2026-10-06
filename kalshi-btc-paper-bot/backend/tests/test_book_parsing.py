"""Fixed-point parsing, complementary bid->ask conversion, depth and validation."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal as Dec

from app.marketdata.book import compare_summary_to_book, purchase_estimate, validate_book
from app.marketdata.models import NO, YES, MarketInfo, OrderBook
from app.paper.fees import FeeSchedule, estimate_fee

from conftest import T, book_payload, market_payload

NOW = datetime(2026, 10, 6, 15, 5, tzinfo=timezone.utc)


def ob(yes, no, ticker="KXBTC15M-26OCT061115-15", at=NOW):
    return OrderBook.from_api(ticker, book_payload(yes, no), at, at + timedelta(milliseconds=80))


def test_parses_orderbook_fp_dollars_and_fp_counts():
    # API returns ascending lists; best bid is the LAST element
    payload = {"orderbook_fp": {"yes_dollars": [["0.3500", "10.00"], ["0.3600", "2.50"]],
                                "no_dollars": [["0.5000", "1.00"], ["0.6300", "7.25"]]}}
    b = OrderBook.from_api("X", payload, NOW, NOW)
    assert b.best_bid(YES).price == Dec("0.36") and b.best_bid(YES).size == Dec("2.50")
    assert b.best_bid(NO).price == Dec("0.63") and b.best_bid(NO).size == Dec("7.25")  # fractional kept
    assert not b.parse_problems


def test_no_bid_63_is_yes_ask_37_with_same_quantity():
    b = ob(yes=[], no=[("0.6300", "8.00")])
    ask = b.best_ask(YES)
    assert ask.price == Dec("0.37")
    assert ask.size == Dec("8.00")


def test_full_ladder_derived_and_sorted_cheapest_first():
    b = ob(yes=[("0.30", "4.00")], no=[("0.55", "3.00"), ("0.63", "8.00"), ("0.60", "5.00")])
    assert [(a.price, a.size) for a in b.asks(YES)] == [
        (Dec("0.37"), Dec("8.00")), (Dec("0.40"), Dec("5.00")), (Dec("0.45"), Dec("3.00"))]
    assert [(a.price, a.size) for a in b.asks(NO)] == [(Dec("0.70"), Dec("4.00"))]


def test_no_buy_price_is_one_minus_yes_bid_not_one_minus_yes_ask():
    # YES bid 0.40, NO bid 0.55 -> YES ask 0.45 (spread 0.05)
    b = ob(yes=[("0.40", "10.00")], no=[("0.55", "10.00")])
    assert b.best_ask(YES).price == Dec("0.45")
    assert b.best_ask(NO).price == Dec("0.60")          # 1 - YES bid
    assert b.best_ask(NO).price != 1 - b.best_ask(YES).price  # 1 - YES ask would be 0.55 (wrong)
    assert b.best_ask(YES).price + b.best_ask(NO).price == Dec("1.05")  # buy prices need not sum to $1


def test_empty_side_means_no_quote_not_zero_price():
    b = ob(yes=[("0.40", "10.00")], no=[])
    assert b.best_ask(YES) is None
    est = purchase_estimate(b, YES, Dec("20"), Dec("0.37"))
    assert est.fill_qty == 0 and est.vwap is None and est.depth_at_or_below_limit == 0


def test_legacy_integer_cents_are_converted_but_dollars_are_not():
    legacy = {"orderbook": {"yes": [[36, 5]], "no": [[63, 8]]}}
    b = OrderBook.from_api("X", legacy, NOW, NOW)
    assert b.best_bid(YES).price == Dec("0.36") and b.best_ask(YES).price == Dec("0.37")
    dollars = OrderBook.from_api("X", book_payload([("0.36", "5.00")], []), NOW, NOW)
    assert dollars.best_bid(YES).price == Dec("0.36")


def test_validation_flags_malformed_levels():
    payload = {"orderbook_fp": {"yes_dollars": [["0.3600", "5.00"], ["0.3600", "1.00"], ["1.2000", "1.00"],
                                                ["0.2000", "1.005"], ["0.1000", "-1.00"]],
                                "no_dollars": [["abc", "1.00"]]}}
    b = OrderBook.from_api("X", payload, NOW, NOW)
    text = " | ".join(b.parse_problems)
    assert "duplicate price level" in text
    assert "outside (0, 1)" in text
    assert "finer than 0.01" in text
    assert "non-positive size" in text
    assert "non-numeric" in text


def test_missing_side_key_is_incomplete():
    b = OrderBook.from_api("X", {"orderbook_fp": {"yes_dollars": []}}, NOW, NOW)
    assert any("incomplete" in p for p in b.parse_problems)


def test_tick_size_validation_against_market_price_ranges():
    m = MarketInfo.from_api(market_payload(T(15, 0)))
    good = ob([("0.36", "1.00")], [("0.60", "1.00")], ticker=m.ticker)
    assert validate_book(good, m) == []
    off_tick = ob([("0.365", "1.00")], [], ticker=m.ticker)
    assert any("not on a valid tick" in p for p in validate_book(off_tick, m))
    sub = MarketInfo.from_api(market_payload(T(15, 0), price_ranges=[{"start": "0", "end": "1", "step": "0.001"}]))
    assert validate_book(ob([("0.365", "1.00")], [], ticker=sub.ticker), sub) == []


def test_crossed_and_locked_books_are_invalid():
    m = MarketInfo.from_api(market_payload(T(15, 0)))
    crossed = ob([("0.40", "1.00")], [("0.62", "1.00")], ticker=m.ticker)
    assert any("crossed" in p for p in validate_book(crossed, m))
    locked = ob([("0.40", "1.00")], [("0.60", "1.00")], ticker=m.ticker)
    assert any("locked" in p for p in validate_book(locked, m))


def test_contract_value_must_be_one_dollar():
    m = MarketInfo.from_api(market_payload(T(15, 0), notional_value_dollars="10.0000"))
    assert any("contract value" in p for p in validate_book(ob([], [], ticker=m.ticker), m))


def test_market_summary_fields_parsed_as_dollars_and_fp():
    m = MarketInfo.from_api(market_payload(T(15, 0), yes_bid_dollars="0.3600", yes_bid_size_fp="12.50",
                                           yes_ask_dollars="0.3700", last_price_dollars="0.3700"))
    assert m.yes_bid == Dec("0.36") and m.yes_bid_size == Dec("12.50") and m.last_price == Dec("0.37")


def test_depth_eight_at_37_then_39():
    b = ob(yes=[], no=[("0.63", "8.00"), ("0.61", "50.00")])
    est = purchase_estimate(b, YES, Dec("20"), Dec("0.37"))
    assert est.depth_at_or_below_limit == Dec("8.00")
    assert est.fill_qty == Dec("8.00") and est.shortfall == Dec("12")
    assert est.next_level_above_limit == {"price": "0.39", "size": "50"}
    assert est.vwap == Dec("0.37") and est.worst_price == Dec("0.37") and est.principal == Dec("2.96")


def test_depth_weighted_average_worst_price_principal_and_fee():
    b = ob(yes=[], no=[("0.65", "5.00"), ("0.64", "10.00"), ("0.63", "10.00")])
    sched = FeeSchedule("KXBTC15M", "quadratic", Dec("1"), None, "test", "")
    est = purchase_estimate(b, YES, Dec("20"), Dec("0.37"), fee_fn=lambda legs: estimate_fee(sched, "taker", legs))
    assert est.fill_qty == Dec("20")
    assert est.principal == Dec("7.20")  # 5*0.35 + 10*0.36 + 5*0.37
    assert est.vwap == Dec("0.36") and est.best_price == Dec("0.35") and est.worst_price == Dec("0.37")
    # exact = 0.07*(5*.35*.65 + 10*.36*.64 + 5*.37*.63) = 0.32249 -> $0.33
    assert est.est_fee == Dec("0.33")


def test_summary_and_book_from_different_windows_rejected():
    m = MarketInfo.from_api(market_payload(T(15, 0), yes_ask_dollars="0.3700"))
    b = ob([], [("0.63", "5.00")], ticker=m.ticker, at=T(15, 14, 59))
    cmp = compare_summary_to_book(m, T(15, 15, 1), b)  # summary observed in the NEXT window
    assert cmp.status == "rejected" and "different 15-minute windows" in cmp.reason


def test_summary_and_book_too_far_apart_rejected_and_discrepancy_detected():
    m = MarketInfo.from_api(market_payload(T(15, 0), yes_bid_dollars="0.3000", yes_ask_dollars="0.3700",
                                           no_bid_dollars="0.6300", no_ask_dollars="0.7000"))
    b = ob([("0.30", "5.00")], [("0.63", "5.00")], ticker=m.ticker, at=T(15, 5, 0))
    assert compare_summary_to_book(m, T(15, 5, 30), b).status == "rejected"
    assert compare_summary_to_book(m, T(15, 5, 1), b).status == "consistent"
    b2 = ob([("0.30", "5.00")], [("0.60", "5.00")], ticker=m.ticker, at=T(15, 5, 0))
    cmp = compare_summary_to_book(m, T(15, 5, 1), b2)
    assert cmp.status == "discrepancy" and "YES ask" in cmp.reason

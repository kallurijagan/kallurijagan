from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.schedule import (
    hour_slots,
    is_eligible_start,
    next_eligible_starts,
    parse_event_ticker_close,
    window_start_for,
)

UTC = timezone.utc
CHI = "America/Chicago"
MINUTES = (0, 15, 30)


def t(h, m=0, s=0, day=6, month=10):
    return datetime(2026, month, day, h, m, s, tzinfo=UTC)


def test_first_three_windows_eligible_and_45_skipped():
    assert is_eligible_start(t(15, 0), MINUTES, CHI)
    assert is_eligible_start(t(15, 15), MINUTES, CHI)
    assert is_eligible_start(t(15, 30), MINUTES, CHI)
    assert not is_eligible_start(t(15, 45), MINUTES, CHI)
    # not a window boundary at all
    assert not is_eligible_start(t(15, 7), MINUTES, CHI)


def test_window_start_floor():
    assert window_start_for(t(15, 14, 59)) == t(15, 0)
    assert window_start_for(t(15, 15, 0)) == t(15, 15)
    assert window_start_for(t(15, 59, 59)) == t(15, 45)


def test_next_eligible_skips_45_and_uses_window_start():
    # 15:44:59 -> next eligible start is 16:00, not 15:45
    assert next_eligible_starts(t(15, 44, 59), MINUTES, CHI, count=1) == [t(16, 0)]
    assert next_eligible_starts(t(15, 30, 0), MINUTES, CHI, count=4) == [t(16, 0), t(16, 15), t(16, 30), t(17, 0)]
    # mid-window start: next window, never the current one
    assert next_eligible_starts(t(15, 7, 30), MINUTES, CHI, count=1) == [t(15, 15)]


def test_hour_slots_display_chicago_and_mark_45_skipped():
    slots = hour_slots(t(15, 20), MINUTES, CHI)  # 10:20 CDT
    assert [s.label for s in slots] == ["10:00–10:15", "10:15–10:30", "10:30–10:45", "10:45–11:00"]
    assert [s.eligible for s in slots] == [True, True, True, False]
    assert slots[0].start == t(15, 0)  # stored/compared in UTC


def test_dst_fall_back_chicago():
    # 2026-11-01: CDT (UTC-5) -> CST (UTC-6) at 07:00 UTC. The 01:00 local hour occurs twice.
    first = hour_slots(datetime(2026, 11, 1, 6, 20, tzinfo=UTC), MINUTES, CHI)
    second = hour_slots(datetime(2026, 11, 1, 7, 20, tzinfo=UTC), MINUTES, CHI)
    assert first[0].start == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert second[0].start == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    assert first[0].label == second[0].label == "01:00–01:15"
    assert first[0].start.astimezone(ZoneInfo(CHI)).tzname() == "CDT"
    assert second[0].start.astimezone(ZoneInfo(CHI)).tzname() == "CST"
    assert [s.eligible for s in second] == [True, True, True, False]


def test_dst_spring_forward_eligibility_unchanged():
    # 2026-03-08 08:00 UTC = 03:00 CDT (02:00 CST skipped)
    assert is_eligible_start(datetime(2026, 3, 8, 8, 0, tzinfo=UTC), MINUTES, CHI)
    assert not is_eligible_start(datetime(2026, 3, 8, 8, 45, tzinfo=UTC), MINUTES, CHI)


def test_event_ticker_encodes_close_time_in_new_york():
    # Observed real ticker format: KXBTC15M-26OCT021330 closes 13:30 EDT = 17:30 UTC
    assert parse_event_ticker_close("KXBTC15M-26OCT021330", "KXBTC15M") == datetime(2026, 10, 2, 17, 30, tzinfo=UTC)
    # winter (EST, UTC-5)
    assert parse_event_ticker_close("KXBTC15M-26DEC011330", "KXBTC15M") == datetime(2026, 12, 1, 18, 30, tzinfo=UTC)
    assert parse_event_ticker_close("KXBTCD-26OCT0213", "KXBTC15M") is None

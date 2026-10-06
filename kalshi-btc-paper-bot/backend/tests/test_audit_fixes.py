"""Regression tests for the 'why is it not buying?' audit findings."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.marketdata.client import KalshiNotFound, KalshiReadOnlyClient
from app.schedule import Clock, ManualClock, SkewCorrectedClock

from conftest import T, FakeSource, book_payload, make_engine, run_for, tickers_for


def arun(coro):
    return asyncio.run(coro)


def windows(eng):
    return {r["window_start"]: r for r in eng.conn.execute("SELECT * FROM windows ORDER BY window_start")}


def test_prelisted_market_without_target_price_still_enters_at_open(tmp_path, clock):
    """Blocker: the next market is listed before its window with floor_strike/strike_type still null."""
    def mf(start, now):
        if now < start:
            return {"status": "initialized", "floor_strike": None, "strike_type": None}
        return {}
    src = FakeSource(clock, market_fn=mf)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "active", w["skip_reason"]


def test_target_price_filled_a_few_seconds_after_open_still_enters(tmp_path, clock):
    def mf(start, now):
        if now < start + timedelta(seconds=4):
            return {"status": "initialized" if now < start else "active", "floor_strike": None}
        return {}
    src = FakeSource(clock, market_fn=mf)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "active", w["skip_reason"]
    waits = [r["message"] for r in eng.conn.execute("SELECT message FROM events WHERE kind='entry-wait'")]
    assert any("not set yet" in m for m in waits)  # the wait reason is visible in the event log


def test_target_price_that_never_appears_skips_after_grace_with_reason(tmp_path, clock):
    src = FakeSource(clock, market_fn=lambda start, now: {"floor_strike": None})
    eng = make_engine(tmp_path, clock, src, entry_grace_seconds=30.0)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "skipped"
    assert "Entry grace period" in w["skip_reason"] and "floor_strike" in w["skip_reason"]


def test_start_while_running_does_not_rearm(tmp_path, clock):
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    armed = eng.paper.engine_state()["armed_at"]
    arun(run_for(eng, clock, (T(15, 5) - clock.now()).total_seconds()))
    eng.start_trading()  # second press mid-window
    assert eng.paper.engine_state()["armed_at"] == armed
    assert windows(eng)["2026-10-06T15:00:00.000Z"]["status"] == "active"


def test_order_gate_explains_price_above_limit_and_no_offers(tmp_path, clock):
    src = FakeSource(clock)
    _, ticker = tickers_for(T(15, 0))
    src.books[ticker] = lambda now: book_payload([("0.47", "140.00")], [])  # UP: no NO bids; DOWN ask 0.53
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 2) - clock.now()).total_seconds()))
    wid = windows(eng)["2026-10-06T15:00:00.000Z"]["id"]
    up = eng.paper.order_gate[f"{wid}:UP"]
    down = eng.paper.order_gate[f"{wid}:DOWN"]
    assert up["state"] == "no_offers" and "nobody is selling UP" in up["text"]
    assert down["state"] == "waiting_for_price" and "53c" in down["text"] and "16c away" in down["text"]


def test_fee_changes_404_keeps_series_and_flags_it(tmp_path, clock):
    src = FakeSource(clock)

    async def missing(series_ticker):
        raise KalshiNotFound(404, "not found", "https://api.elections.kalshi.com/trade-api/v2/series/fee_changes")
    src.get_series_fee_changes = missing
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    assert eng.md.series is not None and "fee_changes" in (eng.md.fee_changes_error or "")
    assert windows(eng)["2026-10-06T15:00:00.000Z"]["status"] == "active"


def test_engine_gap_is_recorded(tmp_path, clock):
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    arun(eng.tick())
    clock.advance(600)  # e.g. the PC slept for 10 minutes
    arun(eng.tick())
    assert eng.last_gap and eng.last_gap["seconds"] >= 600
    msgs = [r["message"] for r in eng.conn.execute("SELECT message FROM events WHERE kind='engine'")]
    assert any("was not running" in m for m in msgs)


def test_skew_corrected_clock_applies_whole_second_offset_with_hysteresis():
    base = ManualClock(T(15, 0))
    c = SkewCorrectedClock(base)
    offset = {"v": None}
    c.attach(lambda: offset["v"])
    assert c.now() == T(15, 0)
    offset["v"] = 1.4  # below threshold: ignored
    assert c.now() == T(15, 0)
    offset["v"] = -42.4  # PC 42 s ahead of Kalshi
    assert c.now() == T(14, 59, 18)
    offset["v"] = -42.2  # jitter does not move the clock
    assert c.now() == T(14, 59, 18)


def test_client_measures_server_clock_offset_from_date_header():
    server_time = datetime.now(timezone.utc) - timedelta(seconds=40)  # PC clock 40 s fast

    def handler(req):
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True},
                              headers={"Date": server_time.strftime("%a, %d %b %Y %H:%M:%S GMT")})

    async def go():
        client = KalshiReadOnlyClient("https://api.elections.kalshi.com/trade-api/v2", None,
                                      transport=httpx.MockTransport(handler), min_interval=0)
        for _ in range(3):
            await client.get_exchange_status()
        off = client.clock_offset_seconds
        await client.aclose()
        return off
    off = arun(go())
    assert off is not None and -41.5 < off < -38.5


def test_out_of_order_detection_uses_request_order_not_wall_clock(tmp_path):
    clock = ManualClock(T(15, 5))
    src = FakeSource(clock)
    _, ticker = tickers_for(T(15, 0))
    src.books[ticker] = lambda now: book_payload([("0.30", "5.00")], [("0.60", "5.00")])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.md.fetch_book(ticker, None))
    clock.set(T(15, 4, 50))  # wall clock steps BACKWARDS (e.g. Windows time sync)
    book = arun(eng.md.fetch_book(ticker, None))
    assert book is not None and eng.md.state(ticker).rejected_out_of_order == 0


def test_real_clock_is_default():
    assert isinstance(SkewCorrectedClock().base, Clock)

"""Regression tests for the adversarial review of the 'not buying' fixes."""

import asyncio
import ssl
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx

from app import api
from app.marketdata.client import _explain
from app.schedule import Clock, ManualClock, SkewCorrectedClock
from app.settings_store import update_settings

from conftest import T, FakeSource, book_payload, make_engine, run_for, tickers_for


def arun(coro):
    return asyncio.run(coro)


class StepClock(Clock):
    """Wall and monotonic time that can diverge (a Windows time sync steps only the wall clock)."""

    def __init__(self, wall: datetime):
        self.wall, self.mono = wall, 1000.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, s):
        self.wall += timedelta(seconds=s)
        self.mono += s


def state_of(eng):
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=eng)))
    return arun(api.state(req))


def test_wall_clock_step_is_not_counted_twice():
    base = StepClock(T(15, 14, 0))
    samples = {"offset": 75.0, "reset": 0}
    clock = SkewCorrectedClock(base)

    def reset():
        samples["reset"] += 1
        samples["offset"] = None  # samples cleared; new ones arrive later
    clock.attach(lambda: samples["offset"], reset)
    assert clock.now() == T(15, 15, 15)  # PC 75 s behind: corrected
    base.advance(5)
    base.wall += timedelta(seconds=75)  # Windows "Sync now": wall jumps, monotonic does not
    assert clock.now() == T(15, 15, 20)  # continuous, NOT 15:16:35
    assert samples["reset"] == 1 and clock.discontinuities == 1


def test_offset_hysteresis_does_not_flap():
    base = ManualClock(T(15, 0))
    m = {"v": 2.49}
    c = SkewCorrectedClock(base)
    c.attach(lambda: m["v"])
    first = c.applied_offset
    seen = set()
    for v in (2.51, 2.49, 2.52, 2.48, 2.5):
        m["v"] = v
        seen.add(c.applied_offset)
    assert seen == {first}
    m["v"] = 42.4
    a = c.applied_offset
    m["v"] = 42.6
    assert c.applied_offset == a
    m["v"] = 0.4  # PC clock fixed: correction released
    assert c.applied_offset == 0.0
    m["v"] = 1.9  # below the enter threshold
    assert c.applied_offset == 0.0


def test_offset_change_is_not_reported_as_engine_gap(tmp_path):
    base = ManualClock(T(14, 58))
    clock = SkewCorrectedClock(base)
    off = {"v": None}
    clock.attach(lambda: off["v"])
    eng = make_engine(tmp_path, clock, FakeSource(clock))
    arun(eng.start(run_loop=False))
    arun(eng.tick())
    off["v"] = 40.0  # correction switches on during startup
    base.advance(1)
    arun(eng.tick())
    assert eng.last_gap is None


def test_lease_uses_raw_pc_time_not_corrected_time(tmp_path):
    base = ManualClock(T(14, 58))
    clock = SkewCorrectedClock(base)
    clock.attach(lambda: 60.0)
    from app.config import load_config
    from app.db import connect, init_schema
    from app.engine import Engine
    cfg = load_config(data_dir=tmp_path)
    conn = connect(tmp_path / "paper.sqlite3")
    init_schema(conn)
    eng = Engine(cfg, conn, FakeSource(clock), clock, use_locks=True)
    arun(eng.start(run_loop=False))
    arun(eng.tick())
    beat = conn.execute("SELECT lease_heartbeat FROM engine_state").fetchone()["lease_heartbeat"]
    assert beat.startswith("2026-10-06T14:58:00")  # raw, not 14:59:00
    arun(eng.stop())


def test_diagnosis_funds_counts_current_reservation_and_closed_window(tmp_path, clock):
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    update_settings(eng.conn, {"starting_balance": "20"}, clock.now())
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 2) - clock.now()).total_seconds()))
    d = state_of(eng)["diagnosis"]
    assert not any(c["key"] == "funds" for c in d["checks"]), d
    eng.pause_trading()
    eng.start_trading()  # resume mid-window
    arun(run_for(eng, clock, 2))
    d = state_of(eng)["diagnosis"]
    assert d["status"] != "ok" and any("orders are closed" in c["title"] for c in d["checks"]), d


def test_preview_checks_have_unique_keys(tmp_path):
    from app.marketdata.preview import PreviewSource
    clock = ManualClock(T(14, 58))
    eng = make_engine(tmp_path, clock, PreviewSource(clock), mode="preview", series_ticker="PREVIEWBTC15M")
    arun(eng.start(run_loop=False))
    arun(run_for(eng, clock, 5))
    checks = state_of(eng)["diagnosis"]["checks"]
    keys = [c["key"] for c in checks]
    assert len(keys) == len(set(keys))
    assert not any(c["title"] == "Live market data OK" for c in checks)


def test_slow_tick_is_waiting_not_frozen(tmp_path, clock):
    eng = make_engine(tmp_path, clock, FakeSource(clock))
    arun(eng.start(run_loop=False))
    arun(eng.tick())
    mono = clock.monotonic()
    eng.tick_started_mono, eng.last_tick_done_mono = mono - 12, mono - 13  # 12 s into a slow request
    eng_check = [c for c in state_of(eng)["diagnosis"]["checks"] if c["key"] == "engine"][0]
    assert eng_check["status"] == "wait" and "Kalshi request" in eng_check["title"]
    eng.tick_started_mono, eng.last_tick_done_mono = mono - 45, mono - 46
    eng_check = [c for c in state_of(eng)["diagnosis"]["checks"] if c["key"] == "engine"][0]
    assert eng_check["status"] == "block"


def test_delayed_entry_never_shows_blocked_gate(tmp_path, clock):
    def mf(start, now):
        if now < start + timedelta(seconds=4):
            return {"floor_strike": None}
        return {}
    src = FakeSource(clock, market_fn=mf)
    _, ticker = tickers_for(T(15, 0))
    src.books[ticker] = lambda now: book_payload([("0.47", "20.00")], [("0.50", "20.00")])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    states = []
    for _ in range(int((T(15, 0, 12) - clock.now()).total_seconds())):
        arun(eng.tick())
        clock.advance(1)
        states += [g["state"] for g in eng.paper.order_gate.values()]
    assert states and "blocked" not in states


def test_tls_hints_cover_truststore_and_expired_messages():
    inner = ssl.SSLCertVerificationError(
        "A certificate chain processed, but terminated in a root certificate which is not trusted by the trust provider.")
    outer = httpx.ConnectError("handshake failed")
    outer.__cause__ = inner
    assert "intercepted" in _explain(outer)
    expired = httpx.ConnectError("certificate has expired")
    assert "date and time" in _explain(expired)

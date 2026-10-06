"""Engine + accounting integration tests on a manual clock with a fake market-data source."""

import asyncio
from datetime import timedelta
from decimal import Decimal as Dec

import pytest

from app.db import connect
from app.engine import Engine
from app.money import D
from app.paper import accounting as acc
from app.settings_store import update_settings

from conftest import T, FakeSource, book_payload, make_engine, run_for, tickers_for


def arun(coro):
    return asyncio.run(coro)


def settle_after(result: str, closed_for: int = 60, determined_for: int = 60):
    """Market lifecycle after close: closed -> determined -> finalized (official result)."""
    def fn(start, now):
        close = start + timedelta(minutes=15)
        if now < close:
            return {}
        age = (now - close).total_seconds()
        if age < closed_for:
            return {"status": "closed"}
        if age < closed_for + determined_for:
            return {"status": "determined", "result": result}
        return {"status": "finalized", "result": result,
                "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000",
                "settlement_ts": (close + timedelta(seconds=closed_for + determined_for)).isoformat()}
    return fn


def phase_book(phases):
    """phases: list of (start_time, yes_bids, no_bids) applied from that time on."""
    def fn(now):
        cur = ([], [])
        for at, yes, no in phases:
            if now >= at:
                cur = (yes, no)
        return book_payload(*cur)
    return fn


def windows(eng):
    return {r["window_start"]: r for r in eng.conn.execute("SELECT * FROM windows ORDER BY window_start")}


def orders(eng, window_id):
    return {r["side"]: r for r in eng.conn.execute("SELECT * FROM orders WHERE window_id=?", (window_id,))}


def ledger_kinds(eng):
    return [(r["kind"], r["amount"]) for r in eng.conn.execute("SELECT kind, amount FROM ledger ORDER BY id")]


def summary(eng):
    return acc.account_summary(eng.conn, eng.paper.account_id)


async def _run_single_window(tmp_path, clock, phases, result, fee_mode="none", pair_accounting="net_on_fill",
                             minutes_after=5):
    src = FakeSource(clock, market_fn=settle_after(result))
    _, ticker = tickers_for(T(15, 0))
    src.books[ticker] = phase_book(phases)
    eng = make_engine(tmp_path, clock, src, fee_mode=fee_mode, pair_accounting=pair_accounting)
    await eng.start(run_loop=False)
    eng.start_trading()
    # stop trading after the first window so only one window is entered
    await run_for(eng, clock, (T(15, 14, 50) - clock.now()).total_seconds())
    eng.pause_trading()
    await run_for(eng, clock, 60 * (minutes_after + 1))
    return eng


# ------------------------------------------------------------------ economic outcomes (fees disabled)


def test_both_sides_filled_profit_520_credited_once(tmp_path, clock):
    phases = [(T(15, 1), [("0.30", "5.00")], [("0.63", "20.00")]),   # UP ask 0.37 x20
              (T(15, 6), [("0.63", "20.00")], [("0.30", "5.00")])]   # DOWN ask 0.37 x20
    eng = arun(_run_single_window(tmp_path, clock, phases, "yes"))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    o = orders(eng, w["id"])
    assert o["UP"]["filled_qty"] == "20" and o["DOWN"]["filled_qty"] == "20"
    assert w["status"] == "settled" and w["outcome_class"] == "both_filled"
    assert D(w["net_pnl"]) == Dec("5.20")
    kinds = ledger_kinds(eng)
    assert [k for k, _ in kinds].count("pair_netting") == 1
    assert ("pair_netting", "20") in kinds
    assert "settlement" not in [k for k, _ in kinds]  # all 20 pairs were already paid by netting
    s = summary(eng)
    assert s["cash"] == Dec("1005.20") and s["realized_pnl"] == Dec("5.20") and s["equity"] == Dec("1005.20")


def test_both_sides_hold_to_settlement_mode_also_520_once(tmp_path, clock):
    phases = [(T(15, 1), [("0.30", "5.00")], [("0.63", "20.00")]),
              (T(15, 6), [("0.63", "20.00")], [("0.30", "5.00")])]
    eng = arun(_run_single_window(tmp_path, clock, phases, "no", pair_accounting="hold_to_settlement"))
    kinds = ledger_kinds(eng)
    assert "pair_netting" not in [k for k, _ in kinds]
    assert ("settlement", "20") in kinds
    assert summary(eng)["cash"] == Dec("1005.20")


def test_only_winning_side_profit_1260(tmp_path, clock):
    phases = [(T(15, 1), [("0.30", "5.00")], [("0.63", "20.00")])]
    eng = arun(_run_single_window(tmp_path, clock, phases, "yes"))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["outcome_class"] == "up_only" and D(w["net_pnl"]) == Dec("12.60")
    assert summary(eng)["cash"] == Dec("1012.60")


def test_only_losing_side_loss_740(tmp_path, clock):
    phases = [(T(15, 1), [("0.30", "5.00")], [("0.63", "20.00")])]
    eng = arun(_run_single_window(tmp_path, clock, phases, "no"))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert D(w["net_pnl"]) == Dec("-7.40")
    assert summary(eng)["cash"] == Dec("992.60")


def test_no_fills_zero_pnl(tmp_path, clock):
    phases = [(T(15, 0), [("0.45", "50.00")], [("0.50", "50.00")])]  # asks 0.50 / 0.55
    eng = arun(_run_single_window(tmp_path, clock, phases, "yes"))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["outcome_class"] == "no_fill" and D(w["net_pnl"]) == 0 and w["status"] == "settled"
    s = summary(eng)
    assert s["cash"] == Dec("1000") and s["reserved"] == 0 and s["equity"] == Dec("1000")


def test_visible_37_but_ask_40_no_fill_through_engine(tmp_path, clock):
    def mf(start, now):
        return {"last_price_dollars": "0.3700", "yes_bid_dollars": "0.3700"}
    src = FakeSource(clock, market_fn=mf)
    _, ticker = tickers_for(T(15, 0))
    src.books[ticker] = phase_book([(T(15, 0), [("0.37", "100.00")], [("0.60", "100.00")])])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 16) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    o = orders(eng, w["id"])
    assert o["UP"]["filled_qty"] == "0" and o["UP"]["status"] == "expired"
    assert eng.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_eight_available_fills_eight_rest_expire(tmp_path, clock):
    src = FakeSource(clock)
    _, ticker = tickers_for(T(15, 0))
    src.books[ticker] = phase_book([(T(15, 0), [("0.30", "5.00")], [("0.63", "8.00"), ("0.61", "50.00")])])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 16) - clock.now()).total_seconds()))  # 15 minutes of identical snapshots
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    up = orders(eng, w["id"])["UP"]
    assert up["filled_qty"] == "8" and up["status"] == "expired"
    assert eng.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert eng.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2  # trigger + confirmation


# ------------------------------------------------------------------ fees & reservations


def test_reservation_and_fees_with_taker_model(tmp_path, clock):
    src = FakeSource(clock)
    _, ticker = tickers_for(T(15, 0))
    src.books[ticker] = phase_book([(T(15, 3), [("0.30", "5.00")], [("0.63", "8.00")]),
                                    (T(15, 5), [("0.30", "5.00")], [("0.63", "20.00")])])
    eng = make_engine(tmp_path, clock, src, fee_mode="taker")
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    s = summary(eng)
    # per order: 20*0.37 = 7.40 + fee reserve ceil(0.32634)=0.33 + rounding buffer 0.19 = 7.92
    assert s["reserved"] == Dec("15.84") and s["available"] == Dec("984.16")
    arun(run_for(eng, clock, (T(15, 10) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    up = orders(eng, w["id"])["UP"]
    assert up["filled_qty"] == "20"
    # fills 8 then 12 at 0.37: accumulator charges ceil(0.130536)=0.14 then ceil(0.32634)-0.14=0.19 -> 0.33 total
    fees = [r["fee"] for r in eng.conn.execute("SELECT fee FROM fills ORDER BY filled_at")]
    assert fees == ["0.14", "0.19"] and up["fees_paid"] == "0.33"
    s = summary(eng)
    assert s["cash"] == Dec("1000") - Dec("7.40") - Dec("0.33")
    assert s["reserved"] == Dec("7.92")  # only the DOWN order still holds a reservation
    arun(run_for(eng, clock, (T(15, 15, 30) - clock.now()).total_seconds()))
    down = orders(eng, w["id"])["DOWN"]
    assert down["status"] == "expired" and down["reserved_remaining"] == "0"  # released at close
    assert summary(eng)["reserved"] == Dec("15.84")  # only the newly entered 15:15 window's two orders


def test_insufficient_funds_skips_without_reducing_quantity(tmp_path, clock):
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    update_settings(eng.conn, {"starting_balance": "20"}, clock.now())
    arun(eng.start(run_loop=False))
    _, t1 = tickers_for(T(15, 0))
    src.books[t1] = phase_book([(T(15, 2), [("0.30", "5.00")], [("0.63", "20.00")])])
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 20) - clock.now()).total_seconds()))
    ws = windows(eng)
    assert ws["2026-10-06T15:00:00.000Z"]["status"] in ("active", "awaiting_settlement", "settled")
    second = ws["2026-10-06T15:15:00.000Z"]
    # cash 20 - 7.40 = 12.60 < 14.80 needed (the UP position is unsettled)
    assert second["status"] == "skipped" and "Insufficient paper funds" in second["skip_reason"]
    assert eng.conn.execute("SELECT COUNT(*) FROM orders WHERE window_id=?", (second["id"],)).fetchone()[0] == 0


# ------------------------------------------------------------------ schedule / entry rules


def test_trades_00_15_30_and_skips_45(tmp_path, clock):
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(16, 5) - clock.now()).total_seconds(), step=2))
    ws = windows(eng)
    assert ws["2026-10-06T15:00:00.000Z"]["status"] != "skipped"
    assert ws["2026-10-06T15:15:00.000Z"]["status"] != "skipped"
    assert ws["2026-10-06T15:30:00.000Z"]["status"] != "skipped"
    assert ws["2026-10-06T15:45:00.000Z"]["status"] == "skipped"
    assert "Scheduled skip" in ws["2026-10-06T15:45:00.000Z"]["skip_reason"]
    assert ws["2026-10-06T16:00:00.000Z"]["status"] != "skipped"
    for w in ws.values():
        if w["status"] != "skipped":
            o = orders(eng, w["id"])
            assert set(o) == {"UP", "DOWN"}
            assert o["UP"]["market_ticker"] == o["DOWN"]["market_ticker"] == w["market_ticker"]
            assert o["UP"]["kalshi_side"] == "yes" and o["DOWN"]["kalshi_side"] == "no"
            assert o["UP"]["quantity"] == o["DOWN"]["quantity"] == "20"
            assert o["UP"]["limit_price"] == o["DOWN"]["limit_price"] == "0.37"


def test_fresh_start_mid_window_waits_for_next(tmp_path):
    from app.schedule import ManualClock
    clock = ManualClock(T(15, 7))
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, 9 * 60))
    ws = windows(eng)
    assert ws["2026-10-06T15:00:00.000Z"]["status"] == "skipped"
    assert "waiting for the next eligible window" in ws["2026-10-06T15:00:00.000Z"]["skip_reason"]
    assert ws["2026-10-06T15:15:00.000Z"]["status"] == "active"


def test_entry_grace_period_bounds_late_entry(tmp_path, clock):
    src = FakeSource(clock, discovery_fail_until=T(15, 0, 45))
    eng = make_engine(tmp_path, clock, src, entry_grace_seconds=30.0)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 16) - clock.now()).total_seconds()))
    ws = windows(eng)
    w = ws["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "skipped" and "Entry grace period" in w["skip_reason"]
    assert ws["2026-10-06T15:15:00.000Z"]["status"] == "active"


def test_entry_within_grace_after_discovery_delay(tmp_path, clock):
    src = FakeSource(clock, discovery_fail_until=T(15, 0, 10))
    eng = make_engine(tmp_path, clock, src, entry_grace_seconds=30.0)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    assert windows(eng)["2026-10-06T15:00:00.000Z"]["status"] == "active"


def test_invalid_market_structure_is_skipped(tmp_path, clock):
    src = FakeSource(clock, market_fn=lambda start, now: {"strike_type": "less"})
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "skipped" and "cannot verify that YES means BTC UP" in w["skip_reason"]
    assert eng.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_settings_change_applies_to_next_window_only(tmp_path, clock):
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 5) - clock.now()).total_seconds()))
    update_settings(eng.conn, {"limit_price": "0.30", "contracts_per_side": "10"}, clock.now())
    arun(run_for(eng, clock, 11 * 60))
    ws = windows(eng)
    first = orders(eng, ws["2026-10-06T15:00:00.000Z"]["id"])
    second = orders(eng, ws["2026-10-06T15:15:00.000Z"]["id"])
    assert first["UP"]["limit_price"] == "0.37" and first["UP"]["quantity"] == "20"
    assert second["UP"]["limit_price"] == "0.3" and second["UP"]["quantity"] == "10"


# ------------------------------------------------------------------ pause / resume


def test_pause_cancels_orders_settlement_continues_resume_waits(tmp_path, clock):
    src = FakeSource(clock, market_fn=settle_after("yes"))
    _, t1 = tickers_for(T(15, 0))
    src.books[t1] = phase_book([(T(15, 2), [("0.30", "5.00")], [("0.63", "5.00")])])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 5) - clock.now()).total_seconds()))
    n = eng.pause_trading()
    assert n == 2  # UP (partially filled) and DOWN (pending) canceled
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    o = orders(eng, w["id"])
    assert o["UP"]["status"] == "canceled" and o["UP"]["filled_qty"] == "5"
    assert w["status"] == "awaiting_settlement"
    assert summary(eng)["reserved"] == 0
    arun(run_for(eng, clock, (T(15, 20) - clock.now()).total_seconds()))
    ws = windows(eng)
    assert ws["2026-10-06T15:00:00.000Z"]["status"] == "settled"
    assert D(ws["2026-10-06T15:00:00.000Z"]["net_pnl"]) == Dec("3.15")  # 5 * (1 - 0.37)
    assert "2026-10-06T15:15:00.000Z" not in ws  # no entry while paused
    eng.start_trading()  # resume at 15:20 -> waits for 15:30
    arun(run_for(eng, clock, (T(15, 31) - clock.now()).total_seconds()))
    ws = windows(eng)
    assert ws["2026-10-06T15:15:00.000Z"]["status"] == "skipped"
    assert ws["2026-10-06T15:30:00.000Z"]["status"] == "active"


# ------------------------------------------------------------------ restart / recovery / idempotency


def test_restart_mid_window_recovers_without_duplicates_and_caps_at_20(tmp_path, clock):
    src = FakeSource(clock)
    _, t1 = tickers_for(T(15, 0))
    src.books[t1] = phase_book([(T(15, 2), [("0.30", "5.00")], [("0.63", "8.00")]),
                                (T(15, 6), [("0.30", "5.00")], [("0.63", "40.00")])])
    eng1 = make_engine(tmp_path, clock, src)
    arun(eng1.start(run_loop=False))
    eng1.start_trading()
    arun(run_for(eng1, clock, (T(15, 5) - clock.now()).total_seconds()))
    wid = windows(eng1)["2026-10-06T15:00:00.000Z"]["id"]
    assert orders(eng1, wid)["UP"]["filled_qty"] == "8"
    # simulated crash: no clean stop; a new process opens the same database
    conn2 = connect(tmp_path / "paper.sqlite3")
    eng2 = Engine(eng1.cfg, conn2, src, clock, use_locks=False)
    arun(eng2.start(run_loop=False))
    arun(run_for(eng2, clock, (T(15, 16) - clock.now()).total_seconds()))
    assert conn2.execute("SELECT COUNT(*) FROM orders WHERE window_id=?", (wid,)).fetchone()[0] == 2
    up = {r["side"]: r for r in conn2.execute("SELECT * FROM orders WHERE window_id=?", (wid,))}["UP"]
    assert up["filled_qty"] == "20"
    total = sum(D(r["quantity"]) for r in conn2.execute("SELECT quantity FROM fills WHERE order_id=?", (up["id"],)))
    assert total == Dec("20")
    # the engine was running before the restart, so it keeps trading after it (from the next window)
    assert conn2.execute("SELECT status FROM windows WHERE window_start='2026-10-06T15:15:00.000Z'").fetchone()["status"] == "active"


def test_downtime_across_close_invents_no_fills_and_settles_once(tmp_path, clock):
    src = FakeSource(clock, market_fn=settle_after("no"))
    _, t1 = tickers_for(T(15, 0))
    src.books[t1] = phase_book([(T(15, 2), [("0.30", "5.00")], [("0.63", "4.00")]),
                                (T(15, 8), [("0.30", "5.00")], [("0.63", "100.00")])])
    eng1 = make_engine(tmp_path, clock, src)
    arun(eng1.start(run_loop=False))
    eng1.start_trading()
    arun(run_for(eng1, clock, (T(15, 5) - clock.now()).total_seconds()))
    wid = windows(eng1)["2026-10-06T15:00:00.000Z"]["id"]
    clock.set(T(15, 22))  # offline from 15:05 to 15:22 (liquidity appeared at 15:08 while offline)
    conn2 = connect(tmp_path / "paper.sqlite3")
    eng2 = Engine(eng1.cfg, conn2, src, clock, use_locks=False)
    arun(eng2.start(run_loop=False))
    up = {r["side"]: r for r in conn2.execute("SELECT * FROM orders WHERE window_id=?", (wid,))}["UP"]
    assert up["filled_qty"] == "4" and up["status"] == "expired"
    assert "offline" in up["close_reason"]
    arun(run_for(eng2, clock, 10 * 60))
    w = conn2.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
    assert w["status"] == "settled" and D(w["net_pnl"]) == Dec("-1.48")  # 4 * -0.37
    ws = {r["window_start"]: r for r in conn2.execute("SELECT * FROM windows")}
    assert ws["2026-10-06T15:15:00.000Z"]["status"] == "skipped"  # restarted mid-window
    # settlement idempotency: re-apply directly and via a third engine
    market = arun(src.get_market(t1))
    assert eng2.paper.settle(w, market) == "already settled"
    conn3 = connect(tmp_path / "paper.sqlite3")
    eng3 = Engine(eng1.cfg, conn3, src, clock, use_locks=False)
    arun(eng3.start(run_loop=False))
    arun(run_for(eng3, clock, 120))
    assert conn3.execute("SELECT COUNT(*) FROM settlements WHERE window_id=?", (wid,)).fetchone()[0] == 1
    assert conn3.execute("SELECT COUNT(*) FROM ledger WHERE ref=?", (f"settle:{wid}",)).fetchone()[0] == 0  # loss: no payout row
    cash = D(conn3.execute("SELECT cash_balance FROM accounts WHERE status='active'").fetchone()["cash_balance"])
    assert cash == Dec("1000") - Dec("1.48")


def test_duplicate_entry_is_rejected_by_constraints(tmp_path, clock):
    src = FakeSource(clock)
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 1) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        eng.conn.execute(
            "INSERT INTO orders(id, account_id, window_id, market_ticker, side, kalshi_side, limit_price, quantity, "
            "fee_reserved, reserved_remaining, status, submitted_at, updated_at) "
            "VALUES('x', 1, ?, 'T', 'UP', 'yes', '0.37', '20', '0', '0', 'pending', 'now', 'now')", (w["id"],))


def test_delayed_and_disputed_settlement_waits_then_settles_once(tmp_path, clock):
    state = {"phase": "closed"}

    def mf(start, now):
        if now < start + timedelta(minutes=15):
            return {}
        if state["phase"] == "finalized":
            return {"status": "finalized", "result": "yes", "settlement_value_dollars": "1.0000"}
        return {"status": state["phase"], "result": "yes" if state["phase"] != "closed" else ""}
    src = FakeSource(clock, market_fn=mf)
    _, t1 = tickers_for(T(15, 0))
    src.books[t1] = phase_book([(T(15, 2), [("0.30", "5.00")], [("0.63", "20.00")])])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 14) - clock.now()).total_seconds()))
    eng.pause_trading()
    arun(run_for(eng, clock, 30 * 60, step=5))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "awaiting_settlement" and "awaiting Kalshi determination" in w["settlement_note"]
    state["phase"] = "disputed"
    arun(run_for(eng, clock, 70 * 60, step=10))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "awaiting_settlement" and "SETTLEMENT DELAYED" in w["settlement_note"]
    assert "disputed" in w["settlement_note"]
    state["phase"] = "finalized"
    arun(run_for(eng, clock, 10 * 60, step=10))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert w["status"] == "settled" and D(w["net_pnl"]) == Dec("12.60")
    assert [k for k, _ in ledger_kinds(eng)].count("settlement") == 1


def test_feed_failure_pauses_fills_and_shows_real_error(tmp_path, clock):
    src = FakeSource(clock)
    _, t1 = tickers_for(T(15, 0))
    src.books[t1] = phase_book([(T(15, 0), [("0.45", "5.00")], [("0.50", "5.00")]),
                                (T(15, 3), [("0.30", "5.00")], [("0.63", "20.00")])])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 2, 59) - clock.now()).total_seconds()))
    src.fail = "ConnectError: [Errno 111] Connection refused"
    arun(run_for(eng, clock, 60))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert orders(eng, w["id"])["UP"]["filled_qty"] == "0"
    assert "Connection refused" in eng.md.health.last_error and eng.md.health.state == "error"
    src.fail = None
    arun(run_for(eng, clock, 60))
    assert orders(eng, w["id"])["UP"]["filled_qty"] == "20"


def test_crossed_book_pauses_fills_until_valid(tmp_path, clock):
    src = FakeSource(clock)
    _, t1 = tickers_for(T(15, 0))
    src.books[t1] = phase_book([(T(15, 1), [("0.40", "5.00")], [("0.63", "20.00")]),   # crossed: 0.40 + 0.63 > 1
                                (T(15, 4), [("0.30", "5.00")], [("0.63", "20.00")])])
    eng = make_engine(tmp_path, clock, src)
    arun(eng.start(run_loop=False))
    eng.start_trading()
    arun(run_for(eng, clock, (T(15, 3, 50) - clock.now()).total_seconds()))
    w = windows(eng)["2026-10-06T15:00:00.000Z"]
    assert orders(eng, w["id"])["UP"]["filled_qty"] == "0"
    st = eng.md.state(t1)
    assert not st.book_valid and any("crossed" in p for p in st.book_problems)
    arun(run_for(eng, clock, 30))
    assert orders(eng, w["id"])["UP"]["filled_qty"] == "20"


def test_equity_identity_holds(tmp_path, clock):
    phases = [(T(15, 1), [("0.30", "5.00")], [("0.63", "13.00")]),
              (T(15, 6), [("0.66", "7.00")], [("0.30", "5.00")])]
    eng = arun(_run_single_window(tmp_path, clock, phases, "no", fee_mode="taker"))
    s = summary(eng)
    assert s["equity"] == s["starting_balance"] + s["realized_pnl"] + s["unrealized_pnl"]
    ledger_sum = sum(D(r["amount"]) for r in eng.conn.execute("SELECT amount FROM ledger WHERE account_id=?", (eng.paper.account_id,)))
    assert ledger_sum == s["cash"]

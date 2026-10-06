"""Orchestrator: schedules windows, polls market data, and drives the paper service.

Data flows one way: MarketDataService (read-only network) -> validated snapshots ->
PaperExecutionService (local, no network). The orchestrator never places orders anywhere;
"orders" exist only as rows in the local SQLite database.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .db import tx
from .winpower import keep_awake
from .instance_lock import EngineAlreadyRunning, FileLock, acquire_lease, heartbeat, new_owner_id, release_lease
from .marketdata.discovery import MarketCheck, market_url, validate_window_market
from .marketdata.feed import MarketDataService, Source, SummaryObs
from .marketdata.models import MarketInfo, OrderBook
from .money import s
from .paper import accounting as acc
from .paper.fees import FeeSchedule, resolve_schedule
from .paper.service import PaperExecutionService
from .schedule import WINDOW, Clock, is_eligible_start, iso, parse_ts, window_start_for
from .settings_store import StrategySettings, load_settings

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, source: Source, clock: Clock | None = None,
                 use_locks: bool = True):
        self.cfg = cfg
        self.conn = conn
        self.clock = clock or Clock()
        self.md = MarketDataService(source, self.clock, cfg.series_ticker, cfg.summary_resync_min_seconds)
        self.paper = PaperExecutionService(
            conn, self.clock,
            execution_delay=cfg.execution_delay_seconds,
            stale_after=cfg.stale_after_seconds,
            market_status_stale=cfg.market_status_stale_seconds,
            exchange_status_stale=cfg.exchange_status_stale_seconds,
            pair_accounting=cfg.pair_accounting,
            settlement_delay_warning=cfg.settlement_delay_warning_seconds,
        )
        self.use_locks = use_locks
        self.owner = new_owner_id()
        self.file_lock = FileLock(cfg.lock_path) if use_locks else None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_tick_error: str | None = None
        self.entry_status: dict = {"state": "starting", "message": "Engine starting"}
        self.focus_ticker: str | None = None
        self._entry_block: str | None = None
        self._due: dict[str, float] = {}  # monotonic deadlines (immune to clock steps/offset changes)
        self._last_poll_mono: dict[tuple[str, str], float] = {}
        self._last_tick_mono: float | None = None
        self.tick_started_mono: float | None = None
        self.last_tick_done_mono: float | None = None
        self._settle_due: dict[str, datetime] = {}
        self._processed_book_at: dict[str, datetime] = {}
        self._last_tick_rec: dict[tuple[str, str], tuple[tuple, datetime]] = {}
        self.last_gap: dict | None = None
        self.ticks = 0

    # ------------------------------------------------------------------ lifecycle
    async def start(self, run_loop: bool = True) -> None:
        if self.file_lock:
            self.file_lock.acquire()
        try:
            if self.use_locks:
                deadline = time.monotonic() + self.cfg.lease_ttl_seconds + 2
                while True:
                    try:
                        acquire_lease(self.conn, self.owner, self.clock.raw_now(), self.cfg.lease_ttl_seconds)
                        break
                    except EngineAlreadyRunning:
                        if time.monotonic() > deadline:
                            raise
                        await asyncio.sleep(1.0)
            self.paper.ensure_initialized(self.cfg.starting_balance)
            self.paper.rearm_after_restart()
            self.paper.recover()
        except BaseException:
            if self.file_lock:
                self.file_lock.release()
            raise
        self.started_at = self.clock.now()
        if run_loop:
            self._task = asyncio.create_task(self._run(), name="paper-engine")

    async def stop(self) -> None:
        self._stopping.set()
        if self.use_locks:
            keep_awake(False)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        try:
            self.paper.snapshot("shutdown")
        except Exception:  # noqa: BLE001
            log.exception("final snapshot failed")
        if self.use_locks:
            try:
                release_lease(self.conn, self.owner)
            except Exception:  # noqa: BLE001
                log.exception("lease release failed")
        if self.file_lock:
            self.file_lock.release()
        await self.md.aclose()
        log.info("engine stopped cleanly")

    async def _run(self) -> None:
        log.info("engine loop started (mode=%s, series=%s, source=%s)", self.cfg.mode, self.cfg.series_ticker,
                 self.md.source.base_url)
        while not self._stopping.is_set():
            t0 = time.monotonic()
            try:
                await self.tick()
                self.last_tick_error = None
            except EngineAlreadyRunning as exc:
                log.critical("lost engine lease: %s — stopping engine loop", exc)
                self.last_tick_error = str(exc)
                break
            except Exception as exc:  # noqa: BLE001
                log.exception("engine tick failed")
                self.last_tick_error = f"{type(exc).__name__}: {exc}"
            wait = max(0.05, self.cfg.tick_seconds - (time.monotonic() - t0))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def _retry_soon(self, key: str, seconds: float = 5.0) -> None:
        self._due[key] = self.clock.monotonic() + seconds

    def _detect_gap(self, now: datetime) -> None:
        """Record when the engine was not running (PC asleep, console frozen, overloaded).

        Measured on the monotonic clock, so clock-offset corrections and time syncs are not gaps.
        """
        mono = self.clock.monotonic()
        prev = self._last_tick_mono
        self._last_tick_mono = mono
        if prev is None:
            return
        gap = mono - prev
        if gap <= max(15.0, 5 * self.cfg.tick_seconds):
            return
        tz = ZoneInfo(load_settings(self.conn).display_timezone)
        start = now - timedelta(seconds=gap)
        msg = (f"Engine was not running from {start.astimezone(tz):%H:%M:%S} to {now.astimezone(tz):%H:%M:%S %Z} "
               f"({gap:.0f}s) — computer asleep, console window frozen by a click/selection, or system overloaded")
        self.last_gap = {"from": iso(start), "to": iso(now), "seconds": round(gap, 1), "message": msg}
        self._entry_block = msg
        self.paper.log_event("warn", "engine", msg)

    def _due_now(self, key: str, interval: float, now: datetime | None = None) -> bool:
        mono = self.clock.monotonic()
        nxt = self._due.get(key)
        if nxt is None or mono >= nxt:
            self._due[key] = mono + interval
            return True
        return False

    def _poll_due(self, ticker: str, kind: str, interval: float) -> bool:
        last = self._last_poll_mono.get((ticker, kind))
        return last is None or self.clock.monotonic() - last >= interval

    def _mark_polled(self, ticker: str, kind: str) -> None:
        self._last_poll_mono[(ticker, kind)] = self.clock.monotonic()
        if len(self._last_poll_mono) > 48:
            self._last_poll_mono.pop(next(iter(self._last_poll_mono)))

    # ------------------------------------------------------------------ tick
    async def tick(self) -> None:
        now = self.clock.now()
        self.ticks += 1
        self.tick_started_mono = self.clock.monotonic()
        self._detect_gap(now)
        self.last_tick_at = now
        trading = bool(self.paper.engine_state()["trading_enabled"])
        if self.use_locks:
            keep_awake(trading)
        if self.use_locks and self._due_now("lease", 5, now):
            heartbeat(self.conn, self.owner, self.clock.raw_now())
        if self._due_now("exchange", self.cfg.exchange_status_poll_seconds, now):
            if await self.md.refresh_exchange() is None:
                self._retry_soon("exchange")  # a skipped/failed refresh must not wait a full interval
        if self.md.series is None:
            if self._due_now("series_retry", 15, now):
                if await self.md.refresh_series() is None:
                    self._retry_soon("series_retry")
        elif self._due_now("series", self.cfg.fee_schedule_refresh_seconds, now):
            if await self.md.refresh_series() is None:
                self._retry_soon("series")

        settings = load_settings(self.conn)
        ws = window_start_for(now)
        we = ws + WINDOW

        for w in self.paper.active_windows():
            close = parse_ts(w["market_close_time"])
            if close is not None and now >= close:
                self.paper.close_window(w["id"], "expired", "market close: unfilled remainder canceled")

        chk = await self.md.discover(ws, we, retry_seconds=2.0)
        await self._entry(now, ws, we, settings, chk)

        active = self.paper.active_windows()
        focus_window = active[0] if active else None
        if focus_window is not None:
            self.focus_ticker = focus_window["market_ticker"]
        elif chk is not None and chk.market is not None:
            self.focus_ticker = chk.market.ticker
        else:
            self.focus_ticker = None
        if self.focus_ticker:
            await self._poll_focus(self.focus_ticker, focus_window, chk)

        await self._settlements(now)

        if self._due_now("equity", self.cfg.equity_snapshot_seconds, now):
            self.paper.snapshot("periodic")
        if self._due_now("prune", 6 * 3600, now):
            self._prune(now)
        if (we - now).total_seconds() <= self.cfg.discovery_lead_seconds:
            await self.md.discover(we, we + WINDOW)
        self.last_tick_done_mono = self.clock.monotonic()

    # ------------------------------------------------------------------ entry
    async def _entry(self, now: datetime, ws: datetime, we: datetime, settings: StrategySettings,
                     chk: MarketCheck | None) -> None:
        tz = ZoneInfo(settings.display_timezone)
        local = ws.astimezone(tz)
        ticker = chk.market.ticker if chk is not None and chk.market is not None else None
        existing = self.paper.window_row(ws)
        if existing is not None:
            self.entry_status = {"state": existing["status"], "message": existing["skip_reason"] or f"Window {existing['status']}"}
            return
        eng = self.paper.engine_state()
        if not eng["trading_enabled"]:
            state = "paused" if eng["paused_at"] else "stopped"
            self.entry_status = {"state": state, "message": "Trading is paused — no new entries" if state == "paused"
                                 else "Press Start to begin paper trading at the next eligible window"}
            return
        if not is_eligible_start(ws, settings.eligible_minutes, settings.display_timezone):
            mins = ", ".join(f":{m:02d}" for m in settings.eligible_minutes)
            self.paper.skip(ws, we, False, f"Scheduled skip: window starts at :{local.minute:02d} (eligible starts: {mins})",
                            ticker)
            return
        armed = parse_ts(eng["armed_at"])
        if armed is None or armed > ws:
            armed_local = armed.astimezone(tz) if armed else None
            self.paper.skip(ws, we, True,
                            f"Engine started/resumed at {armed_local:%H:%M:%S %Z} after this window began at "
                            f"{local:%H:%M %Z}; waiting for the next eligible window" if armed_local else
                            "Engine not armed before this window began", ticker)
            return
        late = (now - ws).total_seconds()
        if late > self.cfg.entry_grace_seconds:
            self.paper.skip(ws, we, True, f"Entry grace period ({self.cfg.entry_grace_seconds:.0f}s) expired before "
                                          f"entry: {self._entry_block or 'not ready'}", ticker)
            self._entry_block = None
            return
        block, permanent, summary = await self._entry_prerequisites(ws, we, settings, chk)
        if block:
            if permanent:
                self.paper.skip(ws, we, True, block, ticker)
                self._entry_block = None
            else:
                if block != self._entry_block:
                    self.paper.log_event("info", "entry-wait", f"Window {local:%H:%M %Z}: waiting to enter — {block}",
                                         acc.window_id_for(self.paper.account_id, ws),
                                         dedupe_key=f"wait:{iso(ws)}:{block[:80]}")
                self._entry_block = block
                self.entry_status = {"state": "waiting", "message": f"Waiting to enter ({late:.0f}s into window): {block}"}
            return
        assert summary is not None and self.md.series is not None
        market = summary.market
        base = FeeSchedule(
            series_ticker=self.md.series.ticker or self.cfg.series_ticker,
            fee_type=self.md.series.fee_type,
            fee_multiplier=self.md.series.fee_multiplier,
            effective_from=None,
            source=f"GET /series/{self.cfg.series_ticker}" if self.md.source.kind != "preview" else "PREVIEW sample series",
            fetched_at=iso(self.md.series_fetched_at) or "",
        )
        fee_schedule = resolve_schedule(base, [(c.scheduled_ts, c.fee_type, c.fee_multiplier) for c in self.md.fee_changes], now)
        account_id = self.paper.account_id
        plan = acc.EntryPlan(
            window_id=acc.window_id_for(account_id, ws),
            account_id=account_id,
            window_start=ws,
            window_end=we,
            market_ticker=market.ticker,
            event_ticker=market.event_ticker,
            market_title=market.title,
            market_url=market_url(self.cfg.series_ticker, market.event_ticker),
            strike_type=market.strike_type,
            floor_strike=market.floor_strike,
            rules_primary=market.rules_primary,
            market_open_time=market.open_time,
            market_close_time=market.close_time or we,
            limit_price=settings.limit_price,
            quantity=settings.contracts_per_side,
            fee_mode=settings.fee_mode,
            fee_schedule=fee_schedule,
            settings_json={
                **settings.to_json(),
                "execution_delay_seconds": self.cfg.execution_delay_seconds,
                "stale_after_seconds": self.cfg.stale_after_seconds,
                "entry_grace_seconds": self.cfg.entry_grace_seconds,
                "data_source": self.md.source.kind,
                "validation_checks": chk.checks if chk else [],
                "validation_warnings": chk.warnings if chk else [],
            },
            pair_accounting=self.cfg.pair_accounting,
        )
        outcome, message = self.paper.try_enter(plan)
        self._entry_block = None
        self.entry_status = {"state": outcome, "message": message}

    async def _entry_prerequisites(self, ws: datetime, we: datetime, settings: StrategySettings,
                                   chk: MarketCheck | None) -> tuple[str | None, bool, SummaryObs | None]:
        if chk is None:
            return f"market discovery failed: {self.md.health.last_error or 'no response'}", False, None
        if not chk.ok:
            return f"market validation: {chk.reason}", not chk.retryable, None
        if self.md.series is None:
            return f"fee schedule unavailable: {self.md.health.last_error or 'series not fetched yet'}", False, None
        ex = self.md.exchange
        now = self.clock.now()
        if ex is None or (now - ex.fetched_at).total_seconds() > self.cfg.exchange_status_stale_seconds:
            return "exchange status unavailable", False, None
        if not (ex.exchange_active and ex.trading_active):
            return "exchange reports trading inactive", False, None
        assert chk.market is not None
        summary = await self.md.fetch_summary(chk.market.ticker)
        if summary is None:
            return f"market summary unavailable: {self.md.health.last_error}", False, None
        if summary.market.status != "active":
            return f"market status is {summary.market.status!r} (waiting for 'active')", False, None
        recheck = validate_window_market([summary.market], self.cfg.series_ticker, ws, we)
        if not recheck.ok:
            return f"market re-validation failed: {recheck.reason}", not recheck.retryable, None
        tick = summary.market.tick_problem(settings.limit_price)
        if tick:
            return f"limit price not valid for this market: {tick}", True, None
        return None, False, summary

    # ------------------------------------------------------------------ polling / fills
    async def _poll_focus(self, ticker: str, window: sqlite3.Row | None, chk: MarketCheck | None) -> None:
        st = self.md.state(ticker)
        now = self.clock.now()
        market = st.summary.market if st.summary else (chk.market if chk and chk.market and chk.market.ticker == ticker else None)
        if st.resync_requested:
            log.info("summary/book discrepancy on %s (%s): running a fresh comparison", ticker, st.resync_requested)
            await self.md.fresh_comparison(ticker, market)
            self._record_summary_tick(st.summary)
            self._record_book_tick(st.book, st.book_valid)
        else:
            if self._poll_due(ticker, "summary", self.cfg.market_poll_seconds):
                self._mark_polled(ticker, "summary")
                obs = await self.md.fetch_summary(ticker)
                self._record_summary_tick(obs)
            due_conf = self.paper.confirmation_due_at()
            need_book = self._poll_due(ticker, "book", self.cfg.orderbook_poll_seconds)
            if due_conf is not None and now >= due_conf and (st.last_book_poll is None or st.last_book_poll < due_conf):
                need_book = True
            if window is not None and st.book is not None:
                placed = parse_ts(window["created_at"])
                if placed is not None and st.book.requested_at < placed:
                    need_book = True  # just entered: evaluate the orders on a book taken after they exist
            if need_book:
                self._mark_polled(ticker, "book")
                market = st.summary.market if st.summary else market
                book = await self.md.fetch_book(ticker, market)
                if book is not None:
                    self._record_book_tick(st.book, st.book_valid)
        if window is None or st.book is None:
            return
        summary = st.summary
        if summary is not None and summary.market.status in ("closed", "determined", "finalized", "settled", "disputed", "amended"):
            self.paper.close_window(window["id"], "expired", f"market status {summary.market.status} (closed by exchange)")
            return
        if self._processed_book_at.get(ticker) == st.book.requested_at:
            return
        ex = self.md.exchange
        self.paper.process_book(
            window, st.book, st.book_problems,
            market_status=summary.market.status if summary else None,
            market_status_at=summary.requested_at if summary else None,
            trading_active=(ex.exchange_active and ex.trading_active) if ex else None,
            exchange_status_at=ex.fetched_at if ex else None,
            fee_waiver_until=summary.market.fee_waiver_expiration_time if summary else None,
        )
        self._processed_book_at[ticker] = st.book.requested_at

    def _should_record(self, ticker: str, kind: str, values: tuple, at: datetime) -> bool:
        """Record a price tick when top-of-book changes, or at least every 5 s as a heartbeat."""
        prev = self._last_tick_rec.get((ticker, kind))
        if prev is not None and prev[0] == values and (at - prev[1]).total_seconds() < 5:
            return False
        self._last_tick_rec[(ticker, kind)] = (values, at)
        if len(self._last_tick_rec) > 64:
            self._last_tick_rec.pop(next(iter(self._last_tick_rec)))
        return True

    def _record_book_tick(self, book: OrderBook | None, valid: bool) -> None:
        if book is None or not valid:
            return
        yb, ya, nb, na = book.best_bid("yes"), book.best_ask("yes"), book.best_bid("no"), book.best_ask("no")
        values = tuple((lv.price, lv.size) if lv else None for lv in (yb, ya, nb, na))
        if not self._should_record(book.ticker, "book", values, book.requested_at):
            return
        with tx(self.conn):
            self.conn.execute(
                """INSERT INTO price_ticks(market_ticker, ts, kind, yes_bid, yes_bid_size, yes_ask, yes_ask_size, no_bid,
                       no_bid_size, no_ask, no_ask_size, latency_ms) VALUES(?, ?, 'book', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (book.ticker, iso(book.requested_at), s(yb.price) if yb else None, s(yb.size) if yb else None,
                 s(ya.price) if ya else None, s(ya.size) if ya else None, s(nb.price) if nb else None,
                 s(nb.size) if nb else None, s(na.price) if na else None, s(na.size) if na else None, book.latency_ms),
            )

    def _record_summary_tick(self, obs: SummaryObs | None) -> None:
        if obs is None:
            return
        m: MarketInfo = obs.market
        values = (m.yes_bid, m.yes_bid_size, m.yes_ask, m.yes_ask_size, m.no_bid, m.no_ask, m.last_price, m.status)
        if not self._should_record(m.ticker, "summary", values, obs.requested_at):
            return
        with tx(self.conn):
            self.conn.execute(
                """INSERT INTO price_ticks(market_ticker, ts, kind, yes_bid, yes_bid_size, yes_ask, yes_ask_size, no_bid,
                       no_ask, last_price, latency_ms) VALUES(?, ?, 'summary', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (m.ticker, iso(obs.requested_at), s(m.yes_bid), s(m.yes_bid_size), s(m.yes_ask), s(m.yes_ask_size),
                 s(m.no_bid), s(m.no_ask), s(m.last_price), obs.latency_ms),
            )

    # ------------------------------------------------------------------ settlement
    async def _settlements(self, now: datetime) -> None:
        budget = 3
        for w in self.paper.awaiting_windows():
            if budget <= 0:
                break
            close = parse_ts(w["market_close_time"]) or now
            due = self._settle_due.get(w["id"])
            if due is None:
                due = max(close + timedelta(seconds=5), now) if now < close + timedelta(seconds=5) else now
                self._settle_due[w["id"]] = due
            if now < due:
                continue
            since = (now - close).total_seconds()
            interval = self.cfg.settlement_poll_seconds if since < 600 else (60 if since < 3600 else 300)
            self._settle_due[w["id"]] = now + timedelta(seconds=interval)
            budget -= 1
            obs = await self.md.fetch_summary(w["market_ticker"])
            if obs is None:
                continue
            result = self.paper.settle(w, obs.market)
            if isinstance(result, dict) or result == "already settled":
                self._settle_due.pop(w["id"], None)

    def _prune(self, now: datetime) -> None:
        cutoff = iso(now - timedelta(days=self.cfg.price_tick_retention_days))
        with tx(self.conn):
            self.conn.execute("DELETE FROM price_ticks WHERE ts < ?", (cutoff,))

    # ------------------------------------------------------------------ control (called from the API)
    def start_trading(self) -> None:
        self.paper.start()
        self._entry_block = None

    def pause_trading(self) -> int:
        return self.paper.pause()

    def reset_account(self) -> int:
        self._settle_due.clear()
        return self.paper.reset_account()

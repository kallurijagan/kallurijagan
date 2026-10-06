"""Market-data service: rate-limit-aware REST polling, validation and resynchronization.

Responsibilities (all read-only):
  * fetch exchange status, series fee info, market summaries and order books;
  * validate every book (format, ticks, quantity precision, crossed/locked, ordering);
  * reject out-of-order observations (a response requested before the last accepted one);
  * compare the API market SUMMARY quotes with the BOOK; a discrepancy triggers a fresh,
    back-to-back comparison (rate-limited). A valid book keeps trading even if the summary
    disagrees — the summary is informational, the book is what fills are based on;
  * an invalid book is immediately re-fetched once ("resync"); if it is still invalid,
    the ticker's book is marked unusable and fills pause until a valid book arrives;
  * honour HTTP 429 Retry-After by pausing all requests;
  * record the real error text of any failure for the dashboard. It NEVER substitutes
    sample data for live data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from ..schedule import Clock, iso
from .book import SummaryComparison, compare_summary_to_book, validate_book
from .client import KalshiError, KalshiNotFound, KalshiRateLimited
from .discovery import MarketCheck, validate_window_market
from .models import ExchangeStatus, FeeChange, MarketInfo, OrderBook, SeriesInfo

log = logging.getLogger(__name__)


class Source(Protocol):
    kind: str
    base_url: str
    request_count: int

    async def get_exchange_status(self) -> ExchangeStatus: ...
    async def get_series(self, series_ticker: str) -> SeriesInfo: ...
    async def get_series_fee_changes(self, series_ticker: str) -> list[FeeChange]: ...
    async def find_markets_closing_between(self, series_ticker: str, min_close_ts: int, max_close_ts: int) -> list[MarketInfo]: ...
    async def get_market(self, ticker: str) -> MarketInfo: ...
    async def get_orderbook(self, ticker: str) -> OrderBook: ...
    async def aclose(self) -> None: ...


@dataclass
class SummaryObs:
    market: MarketInfo
    requested_at: datetime
    received_at: datetime

    @property
    def latency_ms(self) -> float:
        return round((self.received_at - self.requested_at).total_seconds() * 1000, 1)


@dataclass
class TickerState:
    ticker: str
    book: OrderBook | None = None
    book_problems: list[str] = field(default_factory=list)
    book_valid: bool = False
    summary: SummaryObs | None = None
    comparison: SummaryComparison | None = None
    comparison_history: list[dict] = field(default_factory=list)
    resync_requested: str | None = None
    last_resync_at: datetime | None = None
    last_book_poll: datetime | None = None
    last_summary_poll: datetime | None = None
    rejected_out_of_order: int = 0
    invalid_after_resync: int = 0


@dataclass
class FeedHealth:
    state: str = "starting"  # starting | ok | error | rate_limited
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    rate_limited_until: datetime | None = None
    retry_at: datetime | None = None

    def to_json(self, now: datetime) -> dict:
        return {
            "state": self.state,
            "last_ok_at": iso(self.last_ok_at),
            "last_ok_age_seconds": round((now - self.last_ok_at).total_seconds(), 1) if self.last_ok_at else None,
            "last_error": self.last_error,
            "last_error_at": iso(self.last_error_at),
            "consecutive_failures": self.consecutive_failures,
            "rate_limited_until": iso(self.rate_limited_until),
            "retry_at": iso(self.retry_at),
        }


class MarketDataService:
    def __init__(self, source: Source, clock: Clock, series_ticker: str, summary_resync_min_seconds: float = 10.0):
        self.source = source
        self.clock = clock
        self.series_ticker = series_ticker
        self.summary_resync_min_seconds = summary_resync_min_seconds
        self.health = FeedHealth()
        self.exchange: ExchangeStatus | None = None
        self.series: SeriesInfo | None = None
        self.fee_changes: list[FeeChange] = []
        self.series_fetched_at: datetime | None = None
        self.tickers: dict[str, TickerState] = {}
        self._discovery: dict[datetime, tuple[datetime, MarketCheck]] = {}

    # ------------------------------------------------------------------ helpers
    def state(self, ticker: str) -> TickerState:
        if ticker not in self.tickers:
            self.tickers[ticker] = TickerState(ticker)
            if len(self.tickers) > 12:
                oldest = next(iter(self.tickers))
                self.tickers.pop(oldest, None)
        return self.tickers[ticker]

    def rate_limited(self) -> bool:
        until = self.health.rate_limited_until
        return until is not None and self.clock.now() < until

    def backing_off(self) -> bool:
        """After consecutive failures, wait 1, 2, 4, then 5 s between requests (keeps retries inside the entry grace period)."""
        return self.health.retry_at is not None and self.clock.now() < self.health.retry_at

    def _ok(self) -> None:
        self.health.state = "ok"
        self.health.last_ok_at = self.clock.now()
        self.health.consecutive_failures = 0
        self.health.retry_at = None

    def _fail(self, exc: Exception, what: str) -> None:
        now = self.clock.now()
        self.health.consecutive_failures += 1
        self.health.last_error = f"{what}: {exc}"
        self.health.last_error_at = now
        if isinstance(exc, KalshiRateLimited):
            self.health.state = "rate_limited"
            self.health.rate_limited_until = now + timedelta(seconds=max(exc.retry_after, 1.0))
        else:
            self.health.state = "error"
            backoff = min(2 ** (self.health.consecutive_failures - 1), 5)
            self.health.retry_at = now + timedelta(seconds=backoff)
        log.warning("market data error (%s): %s", what, exc)

    async def _call(self, what: str, coro_factory):
        if self.rate_limited() or self.backing_off():
            return None
        try:
            result = await coro_factory()
        except KalshiNotFound:
            raise
        except KalshiError as exc:
            self._fail(exc, what)
            return None
        except Exception as exc:  # noqa: BLE001 — surface any unexpected error text verbatim
            self._fail(exc, what)
            return None
        self._ok()
        return result

    # ------------------------------------------------------------------ exchange / series
    async def refresh_exchange(self) -> ExchangeStatus | None:
        status = await self._call("exchange status", self.source.get_exchange_status)
        if status is not None:
            self.exchange = status
        return status

    async def refresh_series(self) -> SeriesInfo | None:
        series = await self._call(f"series {self.series_ticker}", lambda: self.source.get_series(self.series_ticker))
        if series is None:
            return None
        changes = await self._call("series fee changes", lambda: self.source.get_series_fee_changes(self.series_ticker))
        self.series = series
        self.fee_changes = changes or []
        self.series_fetched_at = self.clock.now()
        return series

    # ------------------------------------------------------------------ discovery
    async def discover(self, window_start: datetime, window_end: datetime, retry_seconds: float = 5.0) -> MarketCheck | None:
        cached = self._discovery.get(window_start)
        now = self.clock.now()
        if cached:
            at, chk = cached
            if chk.ok or not chk.retryable or (now - at).total_seconds() < retry_seconds:
                return chk
        lo = int(window_end.timestamp()) - 60
        hi = int(window_end.timestamp()) + 60
        markets = await self._call(
            "market discovery", lambda: self.source.find_markets_closing_between(self.series_ticker, lo, hi)
        )
        if markets is None:
            return None
        chk = validate_window_market(markets, self.series_ticker, window_start, window_end)
        self._discovery[window_start] = (now, chk)
        for old in [k for k in self._discovery if k < window_start - timedelta(hours=2)]:
            self._discovery.pop(old, None)
        return chk

    def cached_discovery(self, window_start: datetime) -> MarketCheck | None:
        hit = self._discovery.get(window_start)
        return hit[1] if hit else None

    # ------------------------------------------------------------------ summary / book
    async def fetch_summary(self, ticker: str) -> SummaryObs | None:
        st = self.state(ticker)
        requested = self.clock.now()
        try:
            market = await self._call(f"market {ticker}", lambda: self.source.get_market(ticker))
        except KalshiNotFound as exc:
            self._fail(exc, f"market {ticker}")
            return None
        if market is None:
            return None
        obs = SummaryObs(market, requested, self.clock.now())
        st.summary = obs
        st.last_summary_poll = requested
        return obs

    async def fetch_book(self, ticker: str, market: MarketInfo | None, allow_resync: bool = True) -> OrderBook | None:
        st = self.state(ticker)
        try:
            book = await self._call(f"orderbook {ticker}", lambda: self.source.get_orderbook(ticker))
        except KalshiNotFound as exc:
            self._fail(exc, f"orderbook {ticker}")
            return None
        if book is None:
            return None
        st.last_book_poll = book.requested_at
        if st.book is not None and book.requested_at <= st.book.requested_at:
            st.rejected_out_of_order += 1
            log.warning("rejected out-of-order book for %s (%s <= %s)", ticker, book.requested_at, st.book.requested_at)
            return None
        problems = validate_book(book, market)
        if problems and allow_resync:
            log.warning("invalid book for %s (%s); resynchronizing", ticker, "; ".join(problems))
            st.book, st.book_problems, st.book_valid = book, problems, False
            st.last_resync_at = self.clock.now()
            again = await self.fetch_book(ticker, market, allow_resync=False)
            if again is not None:
                return again
            return book
        st.book = book
        st.book_problems = problems
        st.book_valid = not problems
        if problems:
            st.invalid_after_resync += 1
            self.health.last_error = f"order book {ticker} invalid after resync: {'; '.join(problems[:3])}"
            self.health.last_error_at = self.clock.now()
        self._compare(st)
        return book

    def _compare(self, st: TickerState) -> None:
        if st.summary is None or st.book is None:
            return
        cmp = compare_summary_to_book(st.summary.market, st.summary.requested_at, st.book)
        st.comparison = cmp
        st.comparison_history.append({"at": iso(self.clock.now()), **cmp.to_json()})
        del st.comparison_history[:-20]
        now = self.clock.now()
        if cmp.status == "discrepancy" and (
            st.last_resync_at is None or (now - st.last_resync_at).total_seconds() >= self.summary_resync_min_seconds
        ):
            st.resync_requested = cmp.reason

    async def fresh_comparison(self, ticker: str, market: MarketInfo | None) -> SummaryComparison | None:
        """Fetch book and summary back-to-back and compare them again."""
        st = self.state(ticker)
        st.resync_requested = None
        st.last_resync_at = self.clock.now()
        await self.fetch_book(ticker, market)
        await self.fetch_summary(ticker)
        self._compare(st)
        st.resync_requested = None
        return st.comparison

    async def aclose(self) -> None:
        await self.source.aclose()

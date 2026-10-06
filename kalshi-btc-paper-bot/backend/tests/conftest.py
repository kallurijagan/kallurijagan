"""Shared deterministic fixtures: a programmable fake Kalshi source and a manual clock.

Nothing here touches the network. The fake source returns API-shaped JSON parsed by the
same model code as the live client, so fixed-point parsing is exercised end to end.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.db import connect, init_schema  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.marketdata.models import ExchangeStatus, FeeChange, MarketInfo, OrderBook, SeriesInfo  # noqa: E402
from app.schedule import NEW_YORK, WINDOW, ManualClock  # noqa: E402
from app.settings_store import update_settings  # noqa: E402

UTC = timezone.utc
SERIES = "KXBTC15M"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def T(h: int, m: int = 0, s: int = 0, day: int = 6, month: int = 10) -> datetime:
    return datetime(2026, month, day, h, m, s, tzinfo=UTC)


def tickers_for(start: datetime, series: str = SERIES) -> tuple[str, str]:
    close_ny = (start + WINDOW).astimezone(NEW_YORK)
    stamp = f"{close_ny:%y}{MONTHS[close_ny.month - 1]}{close_ny:%d%H%M}"
    event = f"{series}-{stamp}"
    return event, f"{event}-{close_ny:%M}"


def market_payload(start: datetime, status: str = "active", **over) -> dict:
    event, ticker = tickers_for(start, over.pop("series", SERIES))
    p = {
        "ticker": ticker,
        "event_ticker": event,
        "market_type": "binary",
        "title": "BTC price up in next 15 mins?",
        "yes_sub_title": "Target Price: $62,000.00",
        "no_sub_title": "Target Price: $62,000.00",
        "open_time": (start).isoformat().replace("+00:00", "Z"),
        "close_time": (start + WINDOW).isoformat().replace("+00:00", "Z"),
        "latest_expiration_time": (start + timedelta(days=7)).isoformat(),
        "status": status,
        "result": "",
        "strike_type": "greater_or_equal",
        "floor_strike": 62000.0,
        "rules_primary": "If the BTC average for the final minute is at or above the target price, the market resolves to Yes.",
        "rules_secondary": "",
        "can_close_early": False,
        "notional_value_dollars": "1.0000",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "1.0000",
        "no_bid_dollars": "0.0000",
        "no_ask_dollars": "1.0000",
        "last_price_dollars": "0.0000",
    }
    p.update(over)
    return p


def book_payload(yes: list[tuple[str, str]], no: list[tuple[str, str]]) -> dict:
    """Bids given best-first for readability; emitted ascending like the real API."""
    return {"orderbook_fp": {"yes_dollars": [list(x) for x in sorted(yes, key=lambda x: x[0])],
                             "no_dollars": [list(x) for x in sorted(no, key=lambda x: x[0])]}}


@dataclass
class FakeSource:
    clock: ManualClock
    kind: str = "live"
    base_url: str = "fake://kalshi"
    request_count: int = 0
    trading_active: bool = True
    fail: str | None = None
    fee_type: str = "quadratic"
    fee_multiplier: str = "1"
    # window start -> callable(now) -> market payload overrides (status etc.)
    market_fn: Callable[[datetime, datetime], dict] | None = None
    # ticker -> callable(now) -> book payload
    books: dict[str, Callable[[datetime], dict]] = field(default_factory=dict)
    extra_markets: list[dict] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    discovery_fail_until: datetime | None = None

    def _check(self, what: str) -> None:
        self.request_count += 1
        self.calls.append(what)
        if self.fail:
            from app.marketdata.client import KalshiConnectionError
            raise KalshiConnectionError(self.fail)

    def _market(self, start: datetime) -> dict:
        now = self.clock.now()
        status = "active" if start <= now < start + WINDOW else ("initialized" if now < start else "closed")
        payload = market_payload(start, status)
        if self.market_fn:
            payload.update(self.market_fn(start, now) or {})
        return payload

    async def get_exchange_status(self) -> ExchangeStatus:
        self._check("exchange")
        return ExchangeStatus(True, self.trading_active, None, self.clock.now())

    async def get_series(self, series_ticker: str) -> SeriesInfo:
        self._check("series")
        return SeriesInfo.from_api({"series": {"ticker": SERIES, "title": "Bitcoin price up or down in 15 minutes",
                                               "frequency": "fifteen_min", "fee_type": self.fee_type,
                                               "fee_multiplier": float(self.fee_multiplier)}})

    async def get_series_fee_changes(self, series_ticker: str) -> list[FeeChange]:
        self._check("fee_changes")
        return []

    async def find_markets_closing_between(self, series_ticker: str, lo: int, hi: int) -> list[MarketInfo]:
        self._check("discover")
        if self.discovery_fail_until and self.clock.now() < self.discovery_fail_until:
            from app.marketdata.client import KalshiConnectionError
            raise KalshiConnectionError("simulated discovery outage")
        out = []
        close = datetime.fromtimestamp(lo + 60, UTC)
        out.append(MarketInfo.from_api(self._market(close - WINDOW)))
        out.extend(MarketInfo.from_api(m) for m in self.extra_markets)
        return [m for m in out if m.close_time and lo <= m.close_time.timestamp() <= hi]

    def _start_for(self, ticker: str) -> datetime:
        stamp = ticker.split("-")[1]
        close = datetime(2000 + int(stamp[:2]), MONTHS.index(stamp[2:5]) + 1, int(stamp[5:7]), int(stamp[7:9]),
                         int(stamp[9:11]), tzinfo=NEW_YORK).astimezone(UTC)
        return close - WINDOW

    async def get_market(self, ticker: str) -> MarketInfo:
        self._check(f"market {ticker}")
        return MarketInfo.from_api(self._market(self._start_for(ticker)))

    async def get_orderbook(self, ticker: str) -> OrderBook:
        self._check(f"book {ticker}")
        now = self.clock.now()
        fn = self.books.get(ticker)
        payload = fn(now) if fn else book_payload([], [])
        return OrderBook.from_api(ticker, payload, now, now, "live")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T(14, 58, 0))


def make_engine(tmp_path: Path, clock: ManualClock, source: FakeSource, fee_mode: str = "none",
                pair_accounting: str = "net_on_fill", db_name: str = "paper.sqlite3", **cfg_over) -> Engine:
    cfg = load_config(data_dir=tmp_path, pair_accounting=pair_accounting, **cfg_over)
    conn = connect(tmp_path / db_name)
    init_schema(conn)
    eng = Engine(cfg, conn, source, clock, use_locks=False)
    from app.settings_store import ensure_defaults
    ensure_defaults(conn, clock.now())
    update_settings(conn, {"fee_mode": fee_mode}, clock.now())
    return eng


async def run_for(eng: Engine, clock: ManualClock, seconds: int, step: float = 1.0) -> None:
    for _ in range(int(seconds / step)):
        await eng.tick()
        clock.advance(step)

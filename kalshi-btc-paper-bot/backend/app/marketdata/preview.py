"""PREVIEW MODE sample data source — NOT REAL MARKET DATA.

Used only when the app is explicitly started in preview mode (``start.ps1 -Preview`` /
KBOT_MODE=preview). It writes to a separate database (data/preview.sqlite3), uses the
series ticker ``PREVIEWBTC15M`` and every API response, page and export is labeled
PREVIEW. The live mode never falls back to this source: if the real feed fails, the
engine shows the error and pauses fills instead.

The synthetic market mimics the verified KXBTC15M structure (one binary market per
15-minute window, strike_type greater_or_equal, YES = UP) so the full UI and engine can be
exercised when Kalshi is unreachable.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from ..config import PREVIEW_SERIES
from .models import ExchangeStatus, FeeChange, Level, MarketInfo, OrderBook, SeriesInfo
from ..money import D
from ..schedule import NEW_YORK, UTC, WINDOW, Clock, iso, window_start_for

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _seed(*parts: object) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


class PreviewSource:
    kind = "preview"

    def __init__(self, clock: Clock, series_ticker: str = PREVIEW_SERIES):
        self.clock = clock
        self.series = series_ticker
        self._paths: dict[datetime, list[float]] = {}
        self.request_count = 0

    @property
    def base_url(self) -> str:
        return "preview://sample-data"

    async def aclose(self) -> None:
        return None

    # -- synthetic BTC path --------------------------------------------------------
    def _path(self, start: datetime) -> list[float]:
        if start not in self._paths:
            rng = random.Random(_seed("path", start.isoformat()))
            price = 60000.0 + rng.uniform(-4000, 4000)
            path = [price]
            drift = rng.uniform(-0.08, 0.08)
            for _ in range(int(WINDOW.total_seconds()) + 1):
                price += drift + rng.gauss(0, 3.0)
                path.append(price)
            self._paths[start] = path
            if len(self._paths) > 64:
                self._paths.pop(next(iter(self._paths)))
        return self._paths[start]

    def _price_at(self, start: datetime, now: datetime) -> float:
        path = self._path(start)
        idx = int(max(0, min((now - start).total_seconds(), len(path) - 1)))
        return path[idx]

    def _strike(self, start: datetime) -> Decimal:
        return Decimal(str(round(self._path(start)[0], 2)))

    def _final(self, start: datetime) -> float:
        tail = self._path(start)[-60:]
        return sum(tail) / len(tail)

    # -- market objects --------------------------------------------------------------
    def _tickers(self, start: datetime) -> tuple[str, str]:
        close_ny = (start + WINDOW).astimezone(NEW_YORK)
        stamp = f"{close_ny:%y}{_MONTHS[close_ny.month - 1]}{close_ny:%d%H%M}"
        event = f"{self.series}-{stamp}"
        return event, f"{event}-{close_ny:%M}"

    def _market(self, start: datetime) -> MarketInfo:
        now = self.clock.now()
        close = start + WINDOW
        event, ticker = self._tickers(start)
        result = ""
        settle_value = None
        settled_ts = None
        if now < start:
            status = "initialized"
        elif now < close:
            status = "active"
        elif now < close + timedelta(seconds=60):
            status = "closed"
        elif now < close + timedelta(seconds=120):
            status = "determined"
            result = "yes" if self._final(start) >= float(self._strike(start)) else "no"
        else:
            status = "finalized"
            result = "yes" if self._final(start) >= float(self._strike(start)) else "no"
            settle_value = "1.0000" if result == "yes" else "0.0000"
            settled_ts = iso(close + timedelta(seconds=120))
        payload = {
            "ticker": ticker,
            "event_ticker": event,
            "market_type": "binary",
            "notional_value_dollars": "1.0000",
            "title": "PREVIEW SAMPLE: BTC price up in next 15 mins?",
            "yes_sub_title": f"Target Price: ${self._strike(start):,}",
            "no_sub_title": f"Target Price: ${self._strike(start):,}",
            "open_time": iso(start),
            "close_time": iso(close),
            "expected_expiration_time": iso(close + timedelta(seconds=120)),
            "latest_expiration_time": iso(close + timedelta(days=7)),
            "status": status,
            "result": result,
            "settlement_value_dollars": settle_value,
            "settlement_ts": settled_ts,
            "strike_type": "greater_or_equal",
            "floor_strike": float(self._strike(start)),
            "rules_primary": (
                "PREVIEW SAMPLE DATA — not a real market. If the simulated BTC average for the final minute is at or "
                f"above the target price of ${self._strike(start):,}, the market resolves Yes (UP)."
            ),
            "rules_secondary": "Synthetic data generated locally for preview/testing only.",
            "can_close_early": False,
            "price_ranges": [{"start": "0.0100", "end": "0.9900", "step": "0.0100"}],
            "expiration_value": f"{self._final(start):.2f}" if result else "",
        }
        if status == "active":
            yes, no = self._book_levels(start, ticker, now)
            if yes:
                payload["yes_bid_dollars"] = f"{yes[0].price:.4f}"
                payload["yes_bid_size_fp"] = f"{yes[0].size:.2f}"
                payload["no_ask_dollars"] = f"{1 - yes[0].price:.4f}"
            if no:
                payload["no_bid_dollars"] = f"{no[0].price:.4f}"
                payload["yes_ask_dollars"] = f"{1 - no[0].price:.4f}"
                payload["yes_ask_size_fp"] = f"{no[0].size:.2f}"
            payload["last_price_dollars"] = f"{yes[0].price if yes else Decimal('0.5'):.4f}"
        return MarketInfo.from_api(payload)

    # -- API surface (same as KalshiReadOnlyClient) -------------------------------------
    async def get_exchange_status(self) -> ExchangeStatus:
        self.request_count += 1
        return ExchangeStatus(True, True, None, self.clock.now())

    async def get_series(self, series_ticker: str) -> SeriesInfo:
        self.request_count += 1
        return SeriesInfo.from_api({
            "ticker": self.series, "title": "PREVIEW SAMPLE: Bitcoin price up or down in 15 minutes",
            "frequency": "fifteen_min", "fee_type": "quadratic", "fee_multiplier": 1,
            "settlement_sources": [{"name": "Synthetic preview data"}], "contract_terms_url": "",
        })

    async def get_series_fee_changes(self, series_ticker: str) -> list[FeeChange]:
        self.request_count += 1
        return []

    async def find_markets_closing_between(self, series_ticker: str, min_close_ts: int, max_close_ts: int) -> list[MarketInfo]:
        self.request_count += 1
        out = []
        t = window_start_for(datetime.fromtimestamp(min_close_ts, UTC)) - WINDOW
        end = datetime.fromtimestamp(max_close_ts, UTC)
        while t + WINDOW <= end + WINDOW:
            close = t + WINDOW
            if min_close_ts <= close.timestamp() <= max_close_ts:
                out.append(self._market(t))
            t += WINDOW
        return out

    async def get_market(self, ticker: str) -> MarketInfo:
        self.request_count += 1
        stamp = ticker.split("-")[1]
        year = 2000 + int(stamp[0:2])
        month = _MONTHS.index(stamp[2:5]) + 1
        close = datetime(year, month, int(stamp[5:7]), int(stamp[7:9]), int(stamp[9:11]), tzinfo=NEW_YORK).astimezone(UTC)
        return self._market(close - WINDOW)

    def _book_levels(self, start: datetime, ticker: str, now: datetime) -> tuple[tuple[Level, ...], tuple[Level, ...]]:
        price = self._price_at(start, now)
        strike = float(self._strike(start))
        remaining = max((start + WINDOW - now).total_seconds(), 1.0)
        z = (price - strike) / (4.2 * math.sqrt(remaining))
        p_up = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        p_up = min(max(p_up, 0.03), 0.97)
        rng = random.Random(_seed("book", ticker, int(now.timestamp() // 2)))
        half_spread = rng.choice([0.01, 0.01, 0.02, 0.03])
        return _ladder(p_up - half_spread, rng), _ladder(1 - p_up - half_spread, rng)

    async def get_orderbook(self, ticker: str) -> OrderBook:
        self.request_count += 1
        now = self.clock.now()
        market = await self.get_market(ticker)
        start = market.open_time
        assert start is not None
        if market.status != "active":
            return OrderBook(ticker, now, now, (), (), "preview")
        yes, no = self._book_levels(start, ticker, now)
        return OrderBook(ticker=ticker, requested_at=now, received_at=now, yes_bids=yes, no_bids=no, source="preview")


def _ladder(best: float, rng: random.Random) -> tuple[Level, ...]:
    top = D(f"{max(best, 0.01):.4f}").quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    levels = []
    for i in range(6):
        price = top - Decimal("0.01") * i
        if price < Decimal("0.01"):
            break
        size = Decimal(rng.choice([3, 5, 8, 12, 20, 35, 60, 110, 250]))
        levels.append(Level(price, size))
    return tuple(levels)

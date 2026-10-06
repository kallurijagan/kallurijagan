"""Read-only Kalshi REST client.

SAFETY GUARANTEE: this client can only issue HTTP GET requests to an allowlist of
public market-data paths. It has no credentials, no signing code, and no methods for
orders or the portfolio. Every outgoing request — including any request made by
reaching into the underlying ``httpx`` client — passes through ``assert_read_only``
via an httpx request hook, which raises ``ForbiddenEndpointError`` before anything is
sent if the method is not GET or the path is not an allowlisted market-data path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..schedule import UTC
from .models import ExchangeStatus, FeeChange, MarketInfo, OrderBook, SeriesInfo

log = logging.getLogger(__name__)

API_PREFIX = "/trade-api/v2"
_TICKER = r"[A-Za-z0-9._-]+"
ALLOWED_GET_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/exchange/status$"),
    re.compile(r"^/series/fee_changes$"),
    re.compile(rf"^/series/{_TICKER}$"),
    re.compile(r"^/markets$"),
    re.compile(rf"^/markets/{_TICKER}$"),
    re.compile(rf"^/markets/{_TICKER}/orderbook$"),
    re.compile(rf"^/historical/markets/{_TICKER}$"),
)
ALLOWED_HOST_SUFFIXES = (".kalshi.com", ".kalshi.co")
FORBIDDEN_HEADERS = ("kalshi-access-key", "kalshi-access-signature", "kalshi-access-timestamp", "authorization")


class KalshiError(Exception):
    """Base error for market-data access. ``str(err)`` is shown to the user verbatim."""


class ForbiddenEndpointError(KalshiError):
    """Raised before sending any request that is not a public market-data GET."""


class KalshiConnectionError(KalshiError):
    pass


class KalshiHTTPError(KalshiError):
    def __init__(self, status_code: int, message: str, url: str):
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}: {message}")


class KalshiNotFound(KalshiHTTPError):
    pass


class KalshiRateLimited(KalshiHTTPError):
    def __init__(self, status_code: int, message: str, url: str, retry_after: float):
        self.retry_after = retry_after
        super().__init__(status_code, message, url)


def assert_read_only(method: str, url: str, headers: Any = None) -> None:
    """Refuse anything other than a GET to an allowlisted public market-data path."""
    if method.upper() != "GET":
        raise ForbiddenEndpointError(f"Blocked {method.upper()} {url}: the paper engine only issues GET requests")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not any(host.endswith(sfx) or host == sfx.lstrip(".") for sfx in ALLOWED_HOST_SUFFIXES):
        raise ForbiddenEndpointError(f"Blocked request to non-Kalshi host {host!r}")
    path = parts.path
    if not path.startswith(API_PREFIX + "/"):
        raise ForbiddenEndpointError(f"Blocked request outside {API_PREFIX}: {path}")
    sub = path[len(API_PREFIX):]
    if not any(p.match(sub) for p in ALLOWED_GET_PATHS):
        raise ForbiddenEndpointError(f"Blocked non-market-data endpoint: GET {sub}")
    if headers is not None:
        lowered = {str(k).lower() for k in headers.keys()}
        bad = lowered.intersection(FORBIDDEN_HEADERS)
        if bad:
            raise ForbiddenEndpointError(f"Blocked request carrying credentials header(s): {sorted(bad)}")


async def _guard_hook(request: httpx.Request) -> None:
    assert_read_only(request.method, str(request.url), request.headers)


class KalshiReadOnlyClient:
    """Async client for Kalshi public market data."""

    kind = "live"

    def __init__(
        self,
        base_url: str,
        fallback_base_url: str | None = None,
        timeout: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = 0.06,
        now: Callable[[], datetime] | None = None,
    ):
        self._now = now or (lambda: datetime.now(UTC))
        self._bases = [base_url.rstrip("/")]
        if fallback_base_url and fallback_base_url.rstrip("/") not in self._bases:
            self._bases.append(fallback_base_url.rstrip("/"))
        for base in self._bases:
            if not urlsplit(base).path.rstrip("/").endswith(API_PREFIX):
                raise ValueError(f"Kalshi base URL must end with {API_PREFIX}: {base}")
        self._active = 0
        self._min_interval = min_interval
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"Accept": "application/json", "User-Agent": "kalshi-btc-paper-bot/1.0 (paper trading; read-only)"},
            event_hooks={"request": [_guard_hook]},
            transport=transport,
            follow_redirects=False,
        )
        self.request_count = 0

    @property
    def base_url(self) -> str:
        return self._bases[self._active]

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], httpx.Response]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        errors: list[str] = []
        order = [self._active] + [i for i in range(len(self._bases)) if i != self._active]
        for idx in order:
            url = f"{self._bases[idx]}{path}"
            assert_read_only("GET", url)
            async with self._lock:
                loop = asyncio.get_running_loop()
                wait = self._min_interval - (loop.time() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = loop.time()
            try:
                self.request_count += 1
                resp = await self._http.get(url, params=clean)
            except ForbiddenEndpointError:
                raise
            except (httpx.ConnectError, httpx.ProxyError, httpx.ConnectTimeout) as exc:
                errors.append(f"{type(exc).__name__}: {exc} ({url})")
                continue  # try the alternate production host
            except httpx.TimeoutException as exc:
                raise KalshiConnectionError(f"Timeout: {type(exc).__name__} contacting {url}") from exc
            except httpx.HTTPError as exc:
                raise KalshiConnectionError(f"{type(exc).__name__}: {exc} ({url})") from exc
            self._active = idx
            if resp.status_code == 429:
                retry = float(resp.headers.get("Retry-After", "2") or 2)
                raise KalshiRateLimited(429, "rate limited", url, retry)
            if resp.status_code == 404:
                raise KalshiNotFound(404, _error_text(resp), url)
            if resp.status_code >= 400:
                raise KalshiHTTPError(resp.status_code, _error_text(resp), url)
            try:
                return resp.json(), resp
            except ValueError as exc:
                raise KalshiHTTPError(resp.status_code, "response was not JSON", url) from exc
        raise KalshiConnectionError(" | ".join(errors))

    async def get_exchange_status(self) -> ExchangeStatus:
        data, _ = await self._get("/exchange/status")
        return ExchangeStatus.from_api(data, self._now())

    async def get_series(self, series_ticker: str) -> SeriesInfo:
        data, _ = await self._get(f"/series/{series_ticker}")
        return SeriesInfo.from_api(data)

    async def get_series_fee_changes(self, series_ticker: str) -> list[FeeChange]:
        data, _ = await self._get("/series/fee_changes", {"series_ticker": series_ticker, "show_historical": "true"})
        out = []
        for item in data.get("series_fee_change_arr") or []:
            change = FeeChange.from_api(item)
            if change and change.series_ticker == series_ticker:
                out.append(change)
        return out

    async def find_markets_closing_between(self, series_ticker: str, min_close_ts: int, max_close_ts: int) -> list[MarketInfo]:
        """Markets in the series whose close time is within [min, max] (any status)."""
        markets: list[MarketInfo] = []
        cursor: str | None = None
        for _ in range(5):
            data, _ = await self._get(
                "/markets",
                {
                    "series_ticker": series_ticker,
                    "min_close_ts": min_close_ts,
                    "max_close_ts": max_close_ts,
                    "limit": 100,
                    "cursor": cursor,
                },
            )
            markets.extend(MarketInfo.from_api(m) for m in data.get("markets") or [])
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return markets

    async def get_market(self, ticker: str) -> MarketInfo:
        try:
            data, _ = await self._get(f"/markets/{ticker}")
        except KalshiNotFound:
            # Markets settled before the historical cutoff move to /historical/markets.
            data, _ = await self._get(f"/historical/markets/{ticker}")
        return MarketInfo.from_api(data.get("market", data))

    async def get_orderbook(self, ticker: str) -> OrderBook:
        requested_at = self._now()
        data, resp = await self._get(f"/markets/{ticker}/orderbook")
        received_at = self._now()
        return OrderBook.from_api(ticker, data, requested_at, received_at, "live", resp.headers.get("Date"))


def _error_text(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or payload)
        return str(payload)[:300]
    except ValueError:
        return (resp.text or resp.reason_phrase or "")[:300]

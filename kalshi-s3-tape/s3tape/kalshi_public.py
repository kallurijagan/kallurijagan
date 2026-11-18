"""Read-only client for Kalshi's PUBLIC market-data endpoints.

This client cannot place, amend or cancel an order:
  * it only ever sends HTTP GET;
  * it never reads, needs or sends an API key or any credential (Kalshi's order endpoints all
    require a signed key; the market-data endpoints used here require none - see the official
    SDK kalshi_python_sync 3.36.0, where /markets, /markets/trades, /series/{ticker},
    /series/fee_changes and /historical/* list no auth settings);
  * it refuses any path outside a fixed allow-list of public market-data endpoints and any
    host other than *.kalshi.com over HTTPS.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

ALLOWED_PATHS = (
    re.compile(r"^/markets$"),
    re.compile(r"^/markets/trades$"),
    re.compile(r"^/series/fee_changes$"),
    re.compile(r"^/series/[A-Z0-9]+$"),
    re.compile(r"^/historical/cutoff$"),
    re.compile(r"^/historical/markets$"),
    re.compile(r"^/historical/trades$"),
)


class NotAllowed(RuntimeError):
    pass


class PublicClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        min_interval_s: float = 0.2,
        timeout_s: float = 30.0,
        max_attempts: int = 6,
        opener: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        parts = urllib.parse.urlsplit(base_url)
        host = parts.hostname or ""
        if parts.scheme != "https" or not host.endswith(".kalshi.com"):
            raise NotAllowed(f"refusing base URL {base_url!r}: only https://*.kalshi.com")
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self._last = 0.0
        self.requests = 0

    def url_for(self, path: str, params: dict | None = None) -> str:
        if not any(p.match(path) for p in ALLOWED_PATHS):
            raise NotAllowed(f"refusing path {path!r}: not a public market-data endpoint")
        query = urllib.parse.urlencode(
            [(k, _param(v)) for k, v in sorted((params or {}).items()) if v is not None]
        )
        return f"{self.base_url}{path}" + (f"?{query}" if query else "")

    def get(self, path: str, params: dict | None = None) -> dict:
        url = self.url_for(path, params)
        delay = 1.0
        for attempt in range(1, self.max_attempts + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(
                url, method="GET",
                headers={"Accept": "application/json", "User-Agent": "s3tape-readonly/1.0"},
            )
            assert req.get_method() == "GET"
            try:
                self.requests += 1
                with self._open(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_attempts:
                    self._sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                body = exc.read()[:300].decode("utf-8", "replace") if hasattr(exc, "read") else ""
                raise RuntimeError(f"GET {url} -> HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < self.max_attempts:
                    self._sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                raise RuntimeError(f"GET {url} failed: {exc}") from exc
        raise RuntimeError(f"GET {url}: no attempts left")

    def paginate(self, path: str, params: dict, key: str, max_pages: int = 10_000) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            page = self.get(path, {**params, "cursor": cursor})
            out.extend(page.get(key) or [])
            cursor = page.get("cursor") or None
            if not cursor:
                return out
        raise RuntimeError(f"{path}: more than {max_pages} pages; stopping rather than guessing")


def _param(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)

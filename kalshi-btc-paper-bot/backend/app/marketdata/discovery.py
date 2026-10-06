"""Find and verify the Kalshi market for a 15-minute window.

Markets are DISCOVERED through GET /markets?series_ticker=...&min_close_ts=...&max_close_ts=...
and never constructed from a guessed ticker. A market is accepted only if every check
passes; otherwise the window is skipped with the failing check as the reason.

Verified structure of KXBTC15M ("BTC price up in next 15 mins?"):
  * one binary market per 15-minute event, e.g. event KXBTC15M-26OCT021330 /
    market KXBTC15M-26OCT021330-30, where 26OCT021330 is the CLOSE time in New York time;
  * open_time = window start, close_time = window start + 15 minutes;
  * strike_type ``greater_or_equal`` with ``floor_strike`` = the reference BTC price
    (CF Benchmarks RTI average) at the window open; YES pays if the settlement value is at
    or above that target -> YES = UP, NO = DOWN.
Hourly BTC markets (KXBTCD) and BTC range markets fail the series, 15-minute-duration
and strike-type checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import MarketInfo
from ..schedule import WINDOW, parse_event_ticker_close_candidates

UP_MAPS_TO_YES_STRIKES = ("greater_or_equal", "greater")
OPEN_TOLERANCE = timedelta(seconds=60)


@dataclass
class MarketCheck:
    ok: bool
    market: MarketInfo | None = None
    reason: str | None = None
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retryable: bool = False


def market_url(series_ticker: str, event_ticker: str) -> str:
    if series_ticker.upper().startswith("PREVIEW"):
        return ""
    return f"https://kalshi.com/markets/{series_ticker.lower()}/bitcoin-price-up-down/{event_ticker.lower()}"


def validate_window_market(
    markets: list[MarketInfo], series_ticker: str, window_start: datetime, window_end: datetime
) -> MarketCheck:
    """Validate the market for one window.

    Two kinds of failure:
      * structural (wrong market type, contract value, range market, a strike_type that maps
        YES to something other than UP, a window that is not this 15-minute window) — the
        window is skipped immediately;
      * not-ready-yet (no market listed yet, target/strike/rules not filled in, several
        candidates) — ``retryable=True``: Kalshi may list the next market before it opens and
        fill in the target price (BTC at the window open) only at the open, so the engine keeps
        re-checking until the entry grace period expires.
    """
    prefix = f"{series_ticker}-"
    in_series = [m for m in markets if m.event_ticker.startswith(prefix) and m.ticker.startswith(prefix)]
    matching = [m for m in in_series if m.close_time == window_end]
    if not matching:
        seen = ", ".join(sorted(f"{m.ticker} (close {m.close_time:%H:%M}Z)" for m in in_series if m.close_time)) or "none"
        return MarketCheck(False, reason=f"No {series_ticker} market closes at {window_end:%Y-%m-%d %H:%M}Z yet "
                                         f"(found: {seen})", retryable=True)
    if len(matching) > 1:
        return MarketCheck(False, reason=f"Ambiguous: {len(matching)} {series_ticker} markets close at the same time "
                                         f"({', '.join(m.ticker for m in matching)})", retryable=True)
    m = matching[0]
    chk = MarketCheck(True, market=m)
    chk.checks.append(f"series {series_ticker}: event {m.event_ticker}, market {m.ticker}")

    if m.market_type != "binary":
        return _fail(chk, f"market_type is {m.market_type!r}, expected 'binary'")
    chk.checks.append("binary market")

    if m.notional_value is None:
        chk.warnings.append("notional_value not reported; $1.00 binary contract assumed from market_type")
    elif m.notional_value != 1:
        return _fail(chk, f"contract value is ${m.notional_value}, expected $1.00 (YES/NO complement invalid)")
    else:
        chk.checks.append("contract value $1.00 (YES ask = 1 - NO bid is valid)")

    if not m.ticker.startswith(m.event_ticker):
        return _fail(chk, f"market ticker {m.ticker} does not belong to event {m.event_ticker}")

    if m.close_time is None:
        return _fail(chk, "market has no close_time yet", retryable=True)
    if m.close_time - window_start != WINDOW:
        return _fail(chk, f"close_time {m.close_time:%H:%M}Z is not 15 minutes after window start {window_start:%H:%M}Z")
    if m.open_time is None:
        chk.warnings.append("open_time not reported; window taken as close_time - 15 minutes")
    elif m.open_time - window_start > OPEN_TOLERANCE:
        return _fail(chk, f"open_time {m.open_time:%Y-%m-%d %H:%M:%S}Z is after the window start "
                          f"{window_start:%H:%M:%S}Z; the market would not cover this 15-minute window")
    elif window_start - m.open_time > WINDOW + OPEN_TOLERANCE:
        return _fail(chk, f"open_time {m.open_time:%Y-%m-%d %H:%M:%S}Z is more than 15 minutes before the window start "
                          f"{window_start:%H:%M}Z (not a 15-minute window market)")
    elif window_start - m.open_time > OPEN_TOLERANCE:
        chk.warnings.append(f"market opened early ({m.open_time:%H:%M:%S}Z, before the {window_start:%H:%M}Z window "
                            "start); the window is identified by close_time and the event ticker")
    chk.checks.append(f"15-minute window {window_start:%H:%M}–{m.close_time:%H:%M}Z")

    encoded = parse_event_ticker_close_candidates(m.event_ticker, series_ticker)
    if not encoded:
        chk.warnings.append(f"could not parse close time from event ticker {m.event_ticker}")
    elif m.close_time not in encoded:
        return _fail(chk, f"event ticker {m.event_ticker} encodes close {encoded[0]:%H:%M}Z but API close_time is "
                          f"{m.close_time:%H:%M}Z")
    else:
        chk.checks.append("event ticker close time matches API close_time")

    if m.cap_strike is not None:
        return _fail(chk, "market has a cap_strike (range market), not an UP/DOWN market")
    if m.strike_type is None:
        return _fail(chk, "strike_type not set yet — waiting before the UP/DOWN mapping can be verified", retryable=True)
    if m.strike_type not in UP_MAPS_TO_YES_STRIKES:
        return _fail(chk, f"strike_type {m.strike_type!r} — cannot verify that YES means BTC UP")
    if m.floor_strike is None:
        return _fail(chk, "target price (floor_strike) not set yet — Kalshi fills it in at the window open; waiting",
                     retryable=True)
    chk.checks.append(f"strike_type {m.strike_type} vs target {m.floor_strike}: YES = UP, NO = DOWN")

    if not m.rules_primary.strip():
        return _fail(chk, "market rules text not published yet; waiting", retryable=True)
    text = f"{m.title} {m.rules_primary} {m.yes_sub_title}".lower()
    if "btc" not in text and "bitcoin" not in text:
        return _fail(chk, "market title/rules do not reference Bitcoin")
    chk.checks.append("rules reference Bitcoin")
    return chk


def _fail(chk: MarketCheck, reason: str, retryable: bool = False) -> MarketCheck:
    chk.ok = False
    chk.reason = reason
    chk.retryable = retryable
    return chk

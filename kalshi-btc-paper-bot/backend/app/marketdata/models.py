"""Typed views of Kalshi Trade API v2 public market-data responses.

Field names follow the official OpenAPI schema (as generated into the official
``kalshi_python_sync`` 3.31.0 SDK, models Market / GetMarketOrderbookResponse):

* Prices are fixed-point DOLLAR strings (``yes_bid_dollars`` = "0.3700"); responses emit
  up to 6 decimals. Valid ticks come from the market's ``price_ranges`` (start/end/step).
* Contract counts are fixed-point strings with 2 decimals (``yes_bid_size_fp`` = "8.00");
  the minimum granularity is 0.01 contracts. They are never truncated here.
* GET /markets/{ticker}/orderbook returns
  ``{"orderbook_fp": {"yes_dollars": [[price, count], ...], "no_dollars": [[price, count], ...]}}``
  — BIDS ONLY, each side sorted ascending (best bid last). A YES bid at X is a NO ask at
  1 - X with the same count, and a NO bid at X is a YES ask at 1 - X.
* An empty side means there is no observable quote on that side — never a zero price.

Integer-cent legacy fields are accepted only as a labeled fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..money import ONE, QTY_STEP, ZERO, D
from ..schedule import parse_ts

YES = "yes"
NO = "no"
SIDE_TO_KALSHI = {"UP": YES, "DOWN": NO}


def _dollars(payload: dict[str, Any], key: str) -> Decimal | None:
    """Read ``<key>_dollars`` (preferred) or legacy integer cents ``<key>``."""
    value = payload.get(f"{key}_dollars")
    if value not in (None, ""):
        return D(value)
    legacy = payload.get(key)
    if isinstance(legacy, int) and not isinstance(legacy, bool):
        return D(legacy) / Decimal(100)
    return None


def _fp(payload: dict[str, Any], key: str) -> Decimal | None:
    value = payload.get(f"{key}_fp")
    if value not in (None, ""):
        return D(value)
    legacy = payload.get(key)
    if isinstance(legacy, int) and not isinstance(legacy, bool):
        return D(legacy)
    return None


def _opt_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return D(value)


@dataclass(frozen=True)
class PriceRange:
    start: Decimal
    end: Decimal
    step: Decimal


@dataclass(frozen=True)
class MarketInfo:
    ticker: str
    event_ticker: str
    market_type: str
    title: str
    yes_sub_title: str
    no_sub_title: str
    open_time: datetime | None
    close_time: datetime | None
    expected_expiration_time: datetime | None
    latest_expiration_time: datetime | None
    status: str
    result: str
    settlement_value: Decimal | None
    settlement_ts: datetime | None
    strike_type: str | None
    floor_strike: Decimal | None
    cap_strike: Decimal | None
    rules_primary: str
    rules_secondary: str
    can_close_early: bool
    fee_waiver_expiration_time: datetime | None
    price_ranges: tuple[PriceRange, ...]
    notional_value: Decimal | None
    # API SUMMARY quotes (from the market object, not the order book).
    yes_bid: Decimal | None
    yes_bid_size: Decimal | None
    yes_ask: Decimal | None
    yes_ask_size: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    last_price: Decimal | None
    expiration_value: str
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "MarketInfo":
        ranges = []
        for item in payload.get("price_ranges") or []:
            try:
                ranges.append(PriceRange(D(item["start"]), D(item["end"]), D(item["step"])))
            except (KeyError, ValueError, TypeError):
                continue
        return cls(
            ticker=str(payload.get("ticker", "")),
            event_ticker=str(payload.get("event_ticker", "")),
            market_type=str(payload.get("market_type", "")),
            title=str(payload.get("title") or ""),
            yes_sub_title=str(payload.get("yes_sub_title") or ""),
            no_sub_title=str(payload.get("no_sub_title") or ""),
            open_time=parse_ts(payload.get("open_time")),
            close_time=parse_ts(payload.get("close_time")),
            expected_expiration_time=parse_ts(payload.get("expected_expiration_time")),
            latest_expiration_time=parse_ts(payload.get("latest_expiration_time")),
            status=str(payload.get("status") or "").lower(),
            result=str(payload.get("result") or "").lower(),
            settlement_value=_dollars(payload, "settlement_value"),
            settlement_ts=parse_ts(payload.get("settlement_ts")),
            strike_type=(payload.get("strike_type") or None),
            floor_strike=_opt_decimal(payload.get("floor_strike")),
            cap_strike=_opt_decimal(payload.get("cap_strike")),
            rules_primary=str(payload.get("rules_primary") or ""),
            rules_secondary=str(payload.get("rules_secondary") or ""),
            can_close_early=bool(payload.get("can_close_early", False)),
            fee_waiver_expiration_time=parse_ts(payload.get("fee_waiver_expiration_time")),
            price_ranges=tuple(ranges),
            notional_value=_dollars(payload, "notional_value"),
            yes_bid=_dollars(payload, "yes_bid"),
            yes_bid_size=_fp(payload, "yes_bid_size"),
            yes_ask=_dollars(payload, "yes_ask"),
            yes_ask_size=_fp(payload, "yes_ask_size"),
            no_bid=_dollars(payload, "no_bid"),
            no_ask=_dollars(payload, "no_ask"),
            last_price=_dollars(payload, "last_price"),
            expiration_value=str(payload.get("expiration_value") or ""),
            raw=payload,
        )

    def tick_problem(self, price: Decimal) -> str | None:
        """None if ``price`` is a valid quote for this market, else the reason."""
        if not (ZERO < price < ONE):
            return f"price {price} outside (0, 1)"
        if not self.price_ranges:
            if price != price.quantize(Decimal("0.01")):
                return f"price {price} is not on the default 1-cent tick (no price_ranges published)"
            return None
        for rng in self.price_ranges:
            if rng.start <= price <= rng.end and rng.step > 0 and (price - rng.start) % rng.step == 0:
                return None
        return f"price {price} is not on a valid tick of {[(str(r.start), str(r.end), str(r.step)) for r in self.price_ranges]}"

    def min_tick(self) -> Decimal:
        steps = [r.step for r in self.price_ranges if r.step > 0]
        return min(steps) if steps else Decimal("0.01")


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    """Bids for both sides, best (highest) first. Asks are derived, never reported."""

    ticker: str
    requested_at: datetime  # local time the request was sent (used for freshness)
    received_at: datetime  # local time the response arrived
    yes_bids: tuple[Level, ...]
    no_bids: tuple[Level, ...]
    source: str = "live"
    server_date: str | None = None  # HTTP Date header (exchange-side, 1 s resolution)
    parse_problems: tuple[str, ...] = ()
    format: str = "orderbook_fp"

    @property
    def latency_ms(self) -> float:
        return round((self.received_at - self.requested_at).total_seconds() * 1000, 1)

    @classmethod
    def from_api(
        cls,
        ticker: str,
        payload: dict[str, Any],
        requested_at: datetime,
        received_at: datetime,
        source: str = "live",
        server_date: str | None = None,
    ) -> "OrderBook":
        problems: list[str] = []
        fmt = "orderbook_fp"
        book = payload.get("orderbook_fp")
        if isinstance(book, dict):
            # A side with no bids may be omitted or null: that is an empty side (no quote), not an error.
            yes_raw, no_raw, cents = book.get("yes_dollars") or [], book.get("no_dollars") or [], False
        else:
            legacy = payload.get("orderbook")
            if not isinstance(legacy, dict):
                problems.append("response has no orderbook_fp object")
                legacy = {}
            if "yes_dollars" in legacy or "no_dollars" in legacy:
                fmt = "orderbook.yes_dollars (legacy)"
                yes_raw, no_raw, cents = legacy.get("yes_dollars") or [], legacy.get("no_dollars") or [], False
            else:
                fmt = "orderbook.yes (legacy integer cents)"
                yes_raw, no_raw, cents = legacy.get("yes") or [], legacy.get("no") or [], True
        yes, p1 = _levels(yes_raw, cents, "yes")
        no, p2 = _levels(no_raw, cents, "no")
        problems.extend(p1 + p2)
        return cls(ticker, requested_at, received_at, yes, no, source, server_date, tuple(problems), fmt)

    def bids(self, side: str) -> tuple[Level, ...]:
        return self.yes_bids if side == YES else self.no_bids

    def asks(self, side: str) -> tuple[Level, ...]:
        """Full executable ask ladder for BUYING ``side``, cheapest first.

        Buying YES executes against resting NO bids: NO bid p, size q  ->  YES ask 1 - p, size q.
        Buying NO executes against resting YES bids: YES bid p, size q ->  NO ask 1 - p, size q.
        (A NO buy price is NOT 1 - YES ask; that would use the wrong side of the spread.)
        """
        opposite = self.no_bids if side == YES else self.yes_bids
        return tuple(sorted((Level(ONE - lv.price, lv.size) for lv in opposite), key=lambda lv: lv.price))

    def best_bid(self, side: str) -> Level | None:
        levels = self.bids(side)
        return levels[0] if levels else None

    def best_ask(self, side: str) -> Level | None:
        levels = self.asks(side)
        return levels[0] if levels else None

    def to_json(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "requested_at": self.requested_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "latency_ms": self.latency_ms,
            "source": self.source,
            "format": self.format,
            "server_date": self.server_date,
            "yes_bids": [[str(lv.price), str(lv.size)] for lv in self.yes_bids],
            "no_bids": [[str(lv.price), str(lv.size)] for lv in self.no_bids],
        }


def _levels(raw: Any, cents: bool, label: str) -> tuple[tuple[Level, ...], list[str]]:
    """Parse [[price, count], ...]; return best (highest) bid first plus any problems."""
    problems: list[str] = []
    if not isinstance(raw, list):
        return (), [f"{label} bids are not a list"]
    seen: dict[Decimal, Decimal] = {}
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            problems.append(f"{label}: malformed level {entry!r}")
            continue
        try:
            price = D(entry[0])
            size = D(entry[1])
        except ValueError:
            problems.append(f"{label}: non-numeric level {entry!r}")
            continue
        if cents:
            price = price / Decimal(100)
        if not (ZERO < price < ONE):
            problems.append(f"{label}: price {price} outside (0, 1)")
            continue
        if size <= ZERO:
            problems.append(f"{label}: non-positive size {size} at {price}")
            continue
        if size != size.quantize(QTY_STEP):
            problems.append(f"{label}: size {size} finer than 0.01 contracts")
            continue
        if price in seen:
            problems.append(f"{label}: duplicate price level {price}")
            continue
        seen[price] = size
    return tuple(Level(p, seen[p]) for p in sorted(seen, reverse=True)), problems


@dataclass(frozen=True)
class ExchangeStatus:
    exchange_active: bool
    trading_active: bool
    estimated_resume_time: datetime | None
    fetched_at: datetime

    @classmethod
    def from_api(cls, payload: dict[str, Any], fetched_at: datetime) -> "ExchangeStatus":
        return cls(
            exchange_active=bool(payload.get("exchange_active", False)),
            trading_active=bool(payload.get("trading_active", False)),
            estimated_resume_time=parse_ts(payload.get("exchange_estimated_resume_time")),
            fetched_at=fetched_at,
        )


@dataclass(frozen=True)
class SeriesInfo:
    ticker: str
    title: str
    frequency: str
    fee_type: str
    fee_multiplier: Decimal
    settlement_sources: tuple[str, ...]
    contract_terms_url: str
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "SeriesInfo":
        series = payload.get("series", payload)
        sources = tuple(
            str(src.get("name") or src.get("url") or "")
            for src in (series.get("settlement_sources") or [])
            if isinstance(src, dict)
        )
        mult = series.get("fee_multiplier")
        return cls(
            ticker=str(series.get("ticker", "")),
            title=str(series.get("title") or ""),
            frequency=str(series.get("frequency") or ""),
            fee_type=str(series.get("fee_type") or ""),
            fee_multiplier=D(mult) if mult is not None else Decimal("1"),
            settlement_sources=sources,
            contract_terms_url=str(series.get("contract_terms_url") or ""),
            raw=series,
        )


@dataclass(frozen=True)
class FeeChange:
    series_ticker: str
    fee_type: str
    fee_multiplier: Decimal
    scheduled_ts: datetime

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "FeeChange | None":
        ts = parse_ts(payload.get("scheduled_ts"))
        if ts is None:
            return None
        mult = payload.get("fee_multiplier")
        return cls(
            series_ticker=str(payload.get("series_ticker", "")),
            fee_type=str(payload.get("fee_type") or ""),
            fee_multiplier=D(mult) if mult is not None else Decimal("1"),
            scheduled_ts=ts,
        )

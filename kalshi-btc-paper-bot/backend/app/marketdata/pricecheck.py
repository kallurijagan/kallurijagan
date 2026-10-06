"""Compare a price the user saw on kalshi.com with recorded API observations.

Like-for-like rules:
  * same market ticker and side (UP = YES, DOWN = NO);
  * quote type selects the measure: "buy" -> best executable ASK from the order book,
    "sell" -> best BID from the order book, "last" -> last trade from the API market
    summary (YES last price; the NO value is derived as 1 - YES last and labeled so);
    "unknown" -> inconclusive;
  * the recorded observation nearest to the user's observation time must be within
    ``max_skew_seconds``; otherwise the comparison is inconclusive;
  * prices are in dollars; the website shows whole cents, so a value that rounds to the
    visible cent is "consistent within display rounding".
Explanations are limited to facts in the recorded data (another measure equals the
visible price, the quote moved around that time, the side had no quote...). Book prices
are never adjusted to match the website.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from ..money import CENT, ONE, ZERO, ceil_cent, s

QUOTE_TYPES = ("buy", "sell", "last", "unknown")


@dataclass(frozen=True)
class Tick:
    """One recorded observation (book-derived or API summary) for a market."""

    ts: datetime
    kind: str  # "book" or "summary"
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    last_price: Decimal | None = None


@dataclass
class ComparisonResult:
    verdict: str  # consistent | discrepancy | inconclusive
    measure: str | None
    api_value: Decimal | None
    api_observed_at: datetime | None
    skew_seconds: float | None
    visible_price: Decimal
    notes: list[str] = field(default_factory=list)
    other_measures: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "verdict": self.verdict,
            "measure": self.measure,
            "api_value": s(self.api_value),
            "api_observed_at": self.api_observed_at.isoformat() if self.api_observed_at else None,
            "skew_seconds": self.skew_seconds,
            "visible_price": s(self.visible_price),
            "notes": self.notes,
            "other_measures": self.other_measures,
        }


def _to_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _nearest(ticks: list[Tick], kind: str, at: datetime) -> Tick | None:
    candidates = [t for t in ticks if t.kind == kind]
    if not candidates:
        return None
    return min(candidates, key=lambda t: abs((t.ts - at).total_seconds()))


def _measures(book: Tick | None, summary: Tick | None, side: str, fee_rate_mult: Decimal | None) -> list[tuple[str, Decimal | None]]:
    yes = side == "UP"
    out: list[tuple[str, Decimal | None]] = []
    if book:
        ask = book.yes_ask if yes else book.no_ask
        bid = book.yes_bid if yes else book.no_bid
        out.append(("order-book executable ask", ask))
        out.append(("order-book best bid", bid))
        if ask is not None and fee_rate_mult is not None:
            fee = ceil_cent(Decimal("0.07") * fee_rate_mult * ask * (ONE - ask))
            out.append(("order-book ask + est. taker fee (1 contract)", ask + fee))
        # The opposite side's quotes, in case the visible price was for the other outcome.
        o_ask = book.no_ask if yes else book.yes_ask
        o_bid = book.no_bid if yes else book.yes_bid
        out.append(("OTHER side's executable ask", o_ask))
        out.append(("OTHER side's best bid", o_bid))
    if summary:
        if summary.last_price is not None:
            out.append(("API summary last trade" + ("" if yes else " (derived: 1 - YES last)"),
                        summary.last_price if yes else ONE - summary.last_price))
        out.append(("API summary ask", summary.yes_ask if yes else summary.no_ask))
        out.append(("API summary bid", summary.yes_bid if yes else summary.no_bid))
    return out


def compare_visible_price(
    visible_price: Decimal,
    side: str,
    quote_type: str,
    observed_at: datetime,
    ticks: list[Tick],
    max_skew_seconds: float = 10.0,
    fee_multiplier: Decimal | None = None,
) -> ComparisonResult:
    if side not in ("UP", "DOWN"):
        raise ValueError("side must be UP or DOWN")
    if quote_type not in QUOTE_TYPES:
        raise ValueError(f"quote_type must be one of {QUOTE_TYPES}")
    if not (ZERO < visible_price < ONE):
        raise ValueError("visible price must be between $0.01 and $0.99")

    book = _nearest(ticks, "book", observed_at)
    summary = _nearest(ticks, "summary", observed_at)
    book_skew = abs((book.ts - observed_at).total_seconds()) if book else None
    summary_skew = abs((summary.ts - observed_at).total_seconds()) if summary else None
    usable_book = book if book_skew is not None and book_skew <= max_skew_seconds else None
    usable_summary = summary if summary_skew is not None and summary_skew <= max_skew_seconds else None

    others = []
    for name, value in _measures(usable_book, usable_summary, side, fee_multiplier):
        others.append({
            "measure": name,
            "value": s(value),
            "rounds_to_visible": value is not None and _to_cent(value) == _to_cent(visible_price),
        })

    if quote_type == "buy":
        measure, tick, skew = "order-book executable ask", usable_book, book_skew
        value = (tick.yes_ask if side == "UP" else tick.no_ask) if tick else None
    elif quote_type == "sell":
        measure, tick, skew = "order-book best bid", usable_book, book_skew
        value = (tick.yes_bid if side == "UP" else tick.no_bid) if tick else None
    elif quote_type == "last":
        measure, tick, skew = "API summary last trade", usable_summary, summary_skew
        value = None
        if tick and tick.last_price is not None:
            value = tick.last_price if side == "UP" else ONE - tick.last_price
            if side == "DOWN":
                measure += " (derived: 1 - YES last)"
    else:
        res = ComparisonResult("inconclusive", None, None, None, None, visible_price, others_notes(others), others)
        res.notes.insert(0, "Quote type unknown: cannot choose a like-for-like API measure.")
        return res

    if tick is None:
        nearest = book_skew if quote_type != "last" else summary_skew
        reason = ("no recorded observation for this market" if nearest is None
                  else f"nearest recorded observation is {nearest:.1f}s away (> {max_skew_seconds:.0f}s)")
        return ComparisonResult("inconclusive", measure, None, None, None, visible_price,
                                [f"Timing: {reason}; the quote may have changed in between."], others)

    result = ComparisonResult("consistent", measure, value, tick.ts, round(skew or 0.0, 3), visible_price, [], others)
    if value is None:
        result.verdict = "discrepancy"
        result.notes.append(f"The API showed no {measure} for {side} at that time (empty side = no observable quote).")
    elif value == visible_price:
        result.notes.append("Exact match.")
    elif _to_cent(value) == _to_cent(visible_price):
        result.notes.append(f"API value {value} rounds to the visible {visible_price} (website shows whole cents).")
    else:
        result.verdict = "discrepancy"
        diff = visible_price - value
        result.notes.append(f"Visible {visible_price} vs API {value} (difference {s(diff)}).")
        result.notes.extend(others_notes([o for o in others if o["measure"] != measure]))
        moved = _movement(ticks, "book" if quote_type != "last" else "summary", observed_at, max_skew_seconds, side, quote_type)
        if moved:
            result.notes.append(moved)
    if skew and skew > 2:
        result.notes.append(f"Observation times differ by {skew:.1f}s.")
    return result


def others_notes(others: list[dict]) -> list[str]:
    return [f"The visible price matches the {o['measure']} ({o['value']}) at that time — the website quote may be of that type."
            for o in others if o["rounds_to_visible"]]


def _movement(ticks: list[Tick], kind: str, at: datetime, window: float, side: str, quote_type: str) -> str | None:
    near = [t for t in ticks if t.kind == kind and abs((t.ts - at).total_seconds()) <= window]
    vals = []
    for t in near:
        if quote_type == "buy":
            v = t.yes_ask if side == "UP" else t.no_ask
        elif quote_type == "sell":
            v = t.yes_bid if side == "UP" else t.no_bid
        else:
            v = t.last_price if side == "UP" or t.last_price is None else ONE - t.last_price
        if v is not None:
            vals.append(v)
    if len(set(vals)) > 1:
        return f"The recorded quote moved between {min(vals)} and {max(vals)} within ±{window:.0f}s of your observation."
    return None

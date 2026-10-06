"""Order-book validation, depth estimates and API-summary consistency checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from ..money import ONE, QTY_STEP, ZERO, s
from ..schedule import window_start_for
from .models import NO, YES, MarketInfo, OrderBook


def validate_book(book: OrderBook, market: MarketInfo | None) -> list[str]:
    """Problems that make a book unusable for fills (empty list = valid).

    Empty sides are VALID (no observable quote). A crossed or locked book (best YES bid +
    best NO bid >= $1.00, i.e. YES bid >= YES ask) is not a state a live continuous book
    should rest in, so it is treated as invalid until a resynchronized snapshot clears it.
    """
    problems = list(book.parse_problems)
    if market is not None:
        if market.ticker != book.ticker:
            problems.append(f"book ticker {book.ticker} != market {market.ticker}")
        if market.market_type and market.market_type != "binary":
            problems.append(f"market_type {market.market_type!r} is not binary")
        if market.notional_value is not None and market.notional_value != ONE:
            problems.append(f"contract value {market.notional_value} != $1.00; YES/NO complement conversion invalid")
        for side, levels in ((YES, book.yes_bids), (NO, book.no_bids)):
            for lv in levels:
                tick = market.tick_problem(lv.price)
                if tick:
                    problems.append(f"{side} bid {tick}")
    yb, nb = book.best_bid(YES), book.best_bid(NO)
    if yb and nb and yb.price + nb.price >= ONE:
        kind = "crossed" if yb.price + nb.price > ONE else "locked"
        problems.append(
            f"{kind} book: best YES bid {yb.price} + best NO bid {nb.price} = {yb.price + nb.price} >= 1.00"
        )
    return problems


@dataclass
class PurchaseEstimate:
    side: str  # yes / no
    requested_qty: Decimal
    limit_price: Decimal
    depth_at_or_below_limit: Decimal = ZERO
    fill_qty: Decimal = ZERO
    vwap: Decimal | None = None
    best_price: Decimal | None = None
    worst_price: Decimal | None = None
    principal: Decimal = ZERO
    est_fee: Decimal = ZERO
    levels: list[dict] = field(default_factory=list)
    next_level_above_limit: dict | None = None

    @property
    def shortfall(self) -> Decimal:
        return max(self.requested_qty - self.fill_qty, ZERO)

    def to_json(self) -> dict:
        return {
            "side": self.side,
            "requested_qty": s(self.requested_qty),
            "limit_price": s(self.limit_price),
            "depth_at_or_below_limit": s(self.depth_at_or_below_limit),
            "fill_qty": s(self.fill_qty),
            "shortfall": s(self.shortfall),
            "vwap": s(self.vwap),
            "best_price": s(self.best_price),
            "worst_price": s(self.worst_price),
            "principal": s(self.principal),
            "est_fee": s(self.est_fee),
            "levels": self.levels,
            "next_level_above_limit": self.next_level_above_limit,
        }


def purchase_estimate(
    book: OrderBook,
    side: str,
    qty: Decimal,
    limit: Decimal,
    consumed_by_level: dict[Decimal, Decimal] | None = None,
    fee_fn=None,
) -> PurchaseEstimate:
    """Walk the derived ask ladder for buying ``qty`` of ``side`` at or below ``limit``.

    ``consumed_by_level`` removes size this order already used (no double counting).
    ``fee_fn(levels) -> Decimal`` estimates the fee for the resulting fills.
    """
    consumed_by_level = consumed_by_level or {}
    est = PurchaseEstimate(side=side, requested_qty=qty, limit_price=limit)
    remaining = qty
    cost = ZERO
    for lv in book.asks(side):
        if lv.price > limit:
            est.next_level_above_limit = {"price": s(lv.price), "size": s(lv.size)}
            break
        avail = lv.size - consumed_by_level.get(lv.price, ZERO)
        avail = avail.quantize(QTY_STEP, rounding=ROUND_FLOOR) if avail > ZERO else ZERO
        est.depth_at_or_below_limit += avail
        take = min(remaining, avail) if remaining > ZERO else ZERO
        est.levels.append({"price": s(lv.price), "displayed": s(lv.size), "available": s(avail), "take": s(take)})
        if take > ZERO:
            if est.best_price is None:
                est.best_price = lv.price
            est.worst_price = lv.price
            cost += take * lv.price
            est.fill_qty += take
            remaining -= take
    est.principal = cost
    if est.fill_qty > ZERO:
        est.vwap = (cost / est.fill_qty).quantize(Decimal("0.000001"))
        if fee_fn is not None:
            est.est_fee = fee_fn([(Decimal(l["price"]), Decimal(l["take"])) for l in est.levels if Decimal(l["take"]) > 0])
    return est


# ---------------------------------------------------------------- summary vs book


SUMMARY_MAX_SKEW_SECONDS = 5.0


@dataclass
class SummaryComparison:
    status: str  # consistent | discrepancy | rejected
    reason: str | None
    skew_seconds: float | None
    rows: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"status": self.status, "reason": self.reason, "skew_seconds": self.skew_seconds, "rows": self.rows}


def compare_summary_to_book(
    summary: MarketInfo,
    summary_fetched_at: datetime,
    book: OrderBook,
    max_skew_seconds: float = SUMMARY_MAX_SKEW_SECONDS,
) -> SummaryComparison:
    """Compare the market object's summary quotes with the order book, like with like.

    Rejected (no conclusion) when the observations are for different markets, from
    different 15-minute windows, or too far apart in time. Summary fields of 0/1.00 on a
    side with no book quote are treated as "no quote".
    """
    if summary.ticker != book.ticker:
        return SummaryComparison("rejected", f"different markets ({summary.ticker} vs {book.ticker})", None)
    if window_start_for(summary_fetched_at) != window_start_for(book.requested_at):
        return SummaryComparison("rejected", "summary and book observations are from different 15-minute windows", None)
    skew = abs((summary_fetched_at - book.requested_at).total_seconds())
    if skew > max_skew_seconds:
        return SummaryComparison("rejected", f"observations {skew:.1f}s apart (> {max_skew_seconds:.0f}s)", round(skew, 3))
    pairs = [
        ("YES bid", summary.yes_bid, book.best_bid(YES)),
        ("YES ask", summary.yes_ask, book.best_ask(YES)),
        ("NO bid", summary.no_bid, book.best_bid(NO)),
        ("NO ask", summary.no_ask, book.best_ask(NO)),
    ]
    rows = []
    mismatches = []
    for label, summ, lvl in pairs:
        book_price = lvl.price if lvl else None
        summ_is_empty = summ is None or summ in (ZERO, ONE)
        if book_price is None and summ_is_empty:
            match = True
        elif book_price is None or summ is None:
            match = False
        else:
            match = summ == book_price
        rows.append({"measure": label, "summary": s(summ), "book": s(book_price), "match": match})
        if not match:
            mismatches.append(label)
    if mismatches:
        return SummaryComparison("discrepancy", f"summary differs from book on {', '.join(mismatches)}", round(skew, 3), rows)
    return SummaryComparison("consistent", None, round(skew, 3), rows)

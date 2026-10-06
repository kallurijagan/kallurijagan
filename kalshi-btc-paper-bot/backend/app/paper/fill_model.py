"""Conservative executable-ask paper-fill model.

These are SIMULATED fills estimated from public order-book depth. They are not
exchange-confirmed executions, they do not reproduce queue priority, and public depth
cannot prove that a hypothetical resting order would have received a maker fill. The
model therefore only ever "takes" liquidity that was visibly offered.

A virtual buy order stays working for the whole window and is re-evaluated on every
fresh book observation:

1. GATES — no evaluation unless: the order is working with remaining quantity; the wall
   clock and the observation are before the market close; the book is fresh
   (requested <= ``stale_after`` s ago) and passed validation; a fresh market summary
   says status ``active``; the exchange reports ``trading_active``.
2. TRIGGER — an observation (taken after the order was submitted) shows executable asks
   at or below the limit with unconsumed size. Nothing fills yet.
3. EXECUTION DELAY + REVALIDATION — the first gate-passing observation REQUESTED at least
   ``execution_delay`` seconds after the trigger is the confirmation book. Fills are
   computed from THAT book only: if the liquidity is gone, nothing fills.
4. FILL — walk the confirmation book's ask ladder (YES asks = 1 - NO bids, NO asks =
   1 - YES bids, cheapest first). Each level at or below the limit contributes
   ``displayed size - size this order already consumed at that price`` (in 0.01-contract
   steps, never truncated to whole contracts), until the order's remaining quantity is
   exhausted. Each leg is priced at its ask (price improvement below the limit is
   possible); nothing ever fills above the limit. All fills are taker-style.

Replenishment assumption (the public feed cannot identify individual resting orders):
size already consumed at a price level is assumed to still be part of the displayed size
at that level. New size becomes available only when the displayed size grows beyond what
this order already consumed there, so repeated unchanged snapshots can never manufacture
additional fills, and liquidity that disappears and returns is not double counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from ..marketdata.models import OrderBook
from ..money import QTY_STEP, ZERO, s

WORKING = ("pending", "partially_filled")


@dataclass(frozen=True)
class OrderView:
    id: str
    side: str  # UP / DOWN
    kalshi_side: str  # yes / no
    limit_price: Decimal
    quantity: Decimal
    filled_qty: Decimal
    submitted_at: datetime
    status: str

    @property
    def remaining(self) -> Decimal:
        return max(self.quantity - self.filled_qty, ZERO)


@dataclass(frozen=True)
class Gates:
    now: datetime
    close_time: datetime
    book_problems: tuple[str, ...]
    market_status: str | None
    market_status_at: datetime | None
    trading_active: bool | None
    exchange_status_at: datetime | None
    stale_after: float
    market_status_stale: float
    exchange_status_stale: float
    engine_trading: bool = True


@dataclass(frozen=True)
class FillLeg:
    price: Decimal  # ask level price = fill price
    quantity: Decimal
    displayed_size: Decimal
    consumed_before: Decimal
    available: Decimal

    def to_json(self) -> dict:
        return {
            "ask_price": s(self.price),
            "quantity": s(self.quantity),
            "displayed_size": s(self.displayed_size),
            "consumed_before": s(self.consumed_before),
            "available": s(self.available),
        }


@dataclass(frozen=True)
class Trigger:
    """Executable liquidity seen on ``book``; confirmation needed after the delay."""

    order_id: str
    book: OrderBook
    executable_qty: Decimal
    detail: dict


@dataclass
class StepResult:
    trigger: Trigger | None = None
    legs: list[FillLeg] = field(default_factory=list)
    blocked: str | None = None
    event: str | None = None
    detail: dict = field(default_factory=dict)
    trigger_book: OrderBook | None = None


def check_gates(order: OrderView, book: OrderBook, g: Gates) -> str | None:
    if not g.engine_trading:
        return "engine paused"
    if order.status not in WORKING:
        return f"order is {order.status}"
    if order.remaining <= ZERO:
        return "order complete"
    if g.now >= g.close_time:
        return "market closed (past close time)"
    if book.requested_at >= g.close_time:
        return "observation taken at/after close"
    if book.requested_at < order.submitted_at:
        return "observation predates the order"
    if g.book_problems:
        return "invalid order book: " + "; ".join(g.book_problems[:3])
    age = (g.now - book.requested_at).total_seconds()
    if age > g.stale_after:
        return f"stale order book ({age:.1f}s old > {g.stale_after:.0f}s)"
    if g.market_status is None or g.market_status_at is None:
        return "market status unknown"
    if (g.now - g.market_status_at).total_seconds() > g.market_status_stale:
        return "market status stale"
    if g.market_status != "active":
        return f"market not active (status: {g.market_status})"
    if g.trading_active is None or g.exchange_status_at is None:
        return "exchange status unknown"
    if (g.now - g.exchange_status_at).total_seconds() > g.exchange_status_stale:
        return "exchange status stale"
    if not g.trading_active:
        return "exchange trading paused"
    return None


def executable_legs(order: OrderView, book: OrderBook, consumed_by_level: dict[Decimal, Decimal]) -> tuple[list[FillLeg], dict]:
    asks = book.asks(order.kalshi_side)
    remaining = order.remaining
    legs: list[FillLeg] = []
    considered = []
    for level in asks:
        if level.price > order.limit_price:
            considered.append({"ask_price": s(level.price), "displayed_size": s(level.size), "above_limit": True})
            break  # cheapest first; everything after is above the limit too
        consumed = consumed_by_level.get(level.price, ZERO)
        available = level.size - consumed
        available = available.quantize(QTY_STEP, rounding=ROUND_FLOOR) if available > ZERO else ZERO
        take = min(remaining, available) if remaining > ZERO else ZERO
        considered.append({"ask_price": s(level.price), "displayed_size": s(level.size),
                           "consumed_before": s(consumed), "available": s(available), "take": s(take)})
        if take > ZERO:
            legs.append(FillLeg(level.price, take, level.size, consumed, available))
            remaining -= take
    best = asks[0] if asks else None
    detail = {
        "order_side": order.side,
        "kalshi_side": order.kalshi_side,
        "ask_derivation": "YES ask = 1 - NO bid" if order.kalshi_side == "yes" else "NO ask = 1 - YES bid",
        "limit_price": s(order.limit_price),
        "remaining_before": s(order.remaining),
        "best_ask": s(best.price) if best else None,
        "best_ask_size": s(best.size) if best else None,
        "ladder": considered,
        "book_requested_at": book.requested_at.isoformat(),
        "book_received_at": book.received_at.isoformat(),
        "book_source": book.source,
    }
    return legs, detail


def step(
    order: OrderView,
    book: OrderBook,
    gates: Gates,
    consumed_by_level: dict[Decimal, Decimal],
    trigger: Trigger | None,
    execution_delay: float,
) -> StepResult:
    """Advance one order by one fresh observation. Pure function."""
    blocked = check_gates(order, book, gates)
    if blocked:
        # Any pending trigger is dropped: liquidity must be re-observed on a valid fresh book.
        return StepResult(trigger=None, blocked=blocked,
                          event=("trigger dropped: " + blocked) if trigger else None)
    legs, detail = executable_legs(order, book, consumed_by_level)
    if trigger is None:
        if legs:
            qty = sum((l.quantity for l in legs), ZERO)
            return StepResult(trigger=Trigger(order.id, book, qty, detail), detail=detail,
                              event=f"executable ask liquidity seen ({s(qty)} @ <= {s(order.limit_price)}); "
                                    f"revalidating after {execution_delay:.1f}s delay")
        return StepResult(detail=detail)
    waited = (book.requested_at - trigger.book.requested_at).total_seconds()
    if waited < execution_delay:
        return StepResult(trigger=trigger, detail=detail)  # still inside the simulated latency
    if not legs:
        return StepResult(trigger=None, detail=detail, event="liquidity no longer executable on revalidation; no fill")
    detail = {**detail, "trigger_book_requested_at": trigger.book.requested_at.isoformat(),
              "execution_delay_seconds": execution_delay, "waited_seconds": round(waited, 3)}
    return StepResult(trigger=None, legs=legs, detail=detail, trigger_book=trigger.book)

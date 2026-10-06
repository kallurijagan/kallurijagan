"""Kalshi trading-fee ESTIMATE for simulated fills.

Model (Kalshi fee schedule; series fee structure from GET /series/{ticker}):

    taker fee = round_up_to_cent( 0.07 * M * C * P * (1 - P) )

C = contracts, P = price in dollars, M = the series ``fee_multiplier`` (scheduled changes
from GET /series/fee_changes are applied by effective time). Every simulated fill is a
taker fill (the paper model only takes displayed asks), so the taker rate always applies;
maker rates are not used. Kalshi documents a per-order fee accumulator so an order filled
in pieces converges to the fee of one equivalent fill; we charge
``ceil_cent(exact fees of all the order's fills so far) - fees already charged``.
A market-level ``fee_waiver_expiration_time`` in the future waives the fee.
No settlement fee is assumed.

Uncertainty and buffer: if Kalshi's accumulator did not apply, each partial fill could be
rounded up separately, adding up to $0.01 per extra fill. Reservations therefore hold
``ceil_cent(max exact fee) + $0.01 * (ceil(quantity) - 1)`` per order. Fee types other than
the quadratic family are not modeled exactly and are flagged ``uncertain``.
Fee mode "none" disables fees for test fixtures only and is labeled loudly in the UI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from ..money import CENT, ONE, ZERO, ceil_cent, s

TAKER_RATE = Decimal("0.07")
MODELED_FEE_TYPES = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
FEE_MODES = ("taker", "none")


@dataclass(frozen=True)
class FeeSchedule:
    series_ticker: str
    fee_type: str
    fee_multiplier: Decimal
    effective_from: str | None
    source: str
    fetched_at: str

    @property
    def uncertain(self) -> bool:
        return self.fee_type not in MODELED_FEE_TYPES

    def to_json(self) -> dict:
        d = asdict(self)
        d["fee_multiplier"] = s(self.fee_multiplier)
        d["uncertain"] = self.uncertain
        d["taker_rate"] = s(TAKER_RATE)
        d["formula"] = "round_up_to_cent(0.07 * M * C * P * (1 - P)), per-order accumulator"
        return d


def exact_fee(multiplier: Decimal, qty: Decimal, price: Decimal, rate: Decimal = TAKER_RATE) -> Decimal:
    return rate * multiplier * qty * price * (ONE - price)


def fee_buffer(qty: Decimal) -> Decimal:
    whole = qty.to_integral_value(rounding=ROUND_CEILING)
    return CENT * max(whole - 1, ZERO)


def max_fee_reservation(schedule: FeeSchedule, qty: Decimal, limit_price: Decimal, fee_mode: str) -> Decimal:
    """Worst-case fee for one order (P(1-P) is largest at the highest price we may pay, capped at 0.50)."""
    if fee_mode == "none":
        return ZERO
    half = Decimal("0.5")
    worst = limit_price if limit_price <= half else half
    return ceil_cent(exact_fee(schedule.fee_multiplier, qty, worst)) + fee_buffer(qty)


def estimate_fee(schedule: FeeSchedule, fee_mode: str, legs: list[tuple[Decimal, Decimal]]) -> Decimal:
    """Fee for a hypothetical set of (price, qty) fills on one order (accumulator model)."""
    if fee_mode == "none":
        return ZERO
    return ceil_cent(sum((exact_fee(schedule.fee_multiplier, q, p) for p, q in legs), ZERO))


def fee_for_fill(
    schedule: FeeSchedule,
    fee_mode: str,
    qty: Decimal,
    price: Decimal,
    prior_exact_total: Decimal,
    prior_charged: Decimal,
    fill_time: datetime,
    fee_waiver_until: datetime | None,
) -> tuple[Decimal, Decimal, dict]:
    """Return (fee to charge now, new exact running total, assumptions record)."""
    waived = fee_waiver_until is not None and fill_time < fee_waiver_until
    if fee_mode == "none" or waived:
        this_exact = ZERO
    else:
        this_exact = exact_fee(schedule.fee_multiplier, qty, price)
    new_exact_total = prior_exact_total + this_exact
    charge = max(ceil_cent(new_exact_total) - prior_charged, ZERO)
    record = {
        "fee_mode": fee_mode,
        "liquidity": "taker",
        "rate": s(TAKER_RATE if fee_mode != "none" else ZERO),
        "fee_type": schedule.fee_type,
        "uncertain": schedule.uncertain,
        "multiplier": s(schedule.fee_multiplier),
        "formula": "round_up_to_cent(0.07 * M * C * P * (1 - P)) with per-order accumulator",
        "exact_this_fill": s(this_exact),
        "exact_order_total": s(new_exact_total),
        "charged_before": s(prior_charged),
        "charged_now": s(charge),
        "fee_waiver_active": waived,
        "schedule_source": schedule.source,
        "schedule_effective_from": schedule.effective_from,
        "estimate": True,
    }
    return charge, new_exact_total, record


def resolve_schedule(base: FeeSchedule, changes: list[tuple[datetime, str, Decimal]], at: datetime) -> FeeSchedule:
    """Apply the latest scheduled fee change that is effective at ``at``."""
    effective = [c for c in changes if c[0] <= at]
    if not effective:
        return base
    ts, fee_type, mult = max(effective, key=lambda c: c[0])
    return FeeSchedule(
        series_ticker=base.series_ticker,
        fee_type=fee_type or base.fee_type,
        fee_multiplier=mult,
        effective_from=ts.isoformat(),
        source=base.source + " + /series/fee_changes",
        fetched_at=base.fetched_at,
    )

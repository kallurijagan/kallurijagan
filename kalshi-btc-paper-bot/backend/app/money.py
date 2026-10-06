"""Decimal helpers. Every money and contract quantity in the app is a Decimal.

Floats never touch account math. Values are stored in SQLite as TEXT and parsed
back with ``D()``.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation, getcontext

getcontext().prec = 28

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
SUBCENT = Decimal("0.0001")
QTY_STEP = Decimal("0.01")  # Kalshi minimum contract granularity
WHOLE = Decimal("1")


def D(value: object, default: Decimal | None = None) -> Decimal:
    """Parse a value into a Decimal without going through binary floats."""
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError("cannot convert empty value to Decimal")
    if isinstance(value, bool):
        raise ValueError("refusing to convert bool to Decimal")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # API payloads (e.g. floor_strike) may arrive as JSON numbers.
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        if default is not None:
            return default
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def money(value: Decimal) -> Decimal:
    """Round half-up to the cent (display/ledger precision for non-direct members)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ceil_cent(value: Decimal) -> Decimal:
    """Round up to the next cent (Kalshi rounds trading fees up)."""
    return value.quantize(CENT, rounding=ROUND_CEILING)


def floor_whole(value: Decimal) -> Decimal:
    """Round down to whole contracts (the paper model only fills whole contracts)."""
    if value <= ZERO:
        return ZERO
    return value.quantize(WHOLE, rounding=ROUND_FLOOR)


def s(value: Decimal | None) -> str | None:
    """Serialize a Decimal for storage/JSON (normalized, never scientific notation)."""
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def s2(value: Decimal | None) -> str | None:
    """Serialize money with at least two decimal places."""
    if value is None:
        return None
    if value == value.quantize(CENT):
        return format(value.quantize(CENT), "f")
    return s(value)

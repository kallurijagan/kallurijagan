"""Exact integer units.

Prices and money are micro-dollars (1.6c = 16000, $1 = 1_000_000), quantities are hundredths
of a contract (200 contracts = 20000), times are UTC microseconds. Days are America/Chicago.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

ONE_DOLLAR = 1_000_000          # micro-dollars
US_PER_S = 1_000_000            # microseconds per second
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_DEC = re.compile(r"^(-?)(\d+)(?:\.(\d*))?$")
_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})$"
)


def _fixed(text: str, places: int, what: str) -> int:
    m = _DEC.match(str(text).strip())
    if not m:
        raise ValueError(f"not a {what}: {text!r}")
    sign, whole, frac = m.groups()
    frac = frac or ""
    if len(frac) > places:
        if set(frac[places:]) - {"0"}:
            raise ValueError(f"{what} finer than the unit: {text!r}")
        frac = frac[:places]
    value = int(whole) * 10**places + int(frac.ljust(places, "0") or "0")
    return -value if sign else value


def dollars_to_micro(text: str) -> int:
    """'0.0160' -> 16000 (exact; refuses anything finer than a micro-dollar)."""
    return _fixed(text, 6, "dollar amount")


def fp_to_hundredths(text: str) -> int:
    """'200.00' -> 20000 (exact; refuses anything finer than 0.01 contracts)."""
    return _fixed(text, 2, "contract count")


def iso_to_us(text: str) -> int:
    """ISO-8601 timestamp -> UTC microseconds. Digits beyond microseconds are truncated."""
    m = _ISO.match(str(text).strip())
    if not m:
        raise ValueError(f"not an ISO-8601 timestamp: {text!r}")
    y, mo, d, h, mi, s, frac, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc)
    delta = dt - _EPOCH
    us = (delta.days * 86400 + delta.seconds) * US_PER_S + int((frac or "")[:6].ljust(6, "0"))
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        us -= sign * (hh * 3600 + mm * 60) * US_PER_S
    return us


def us_to_utc(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


def us_to_iso(us: int) -> str:
    return us_to_utc(us).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---- America/Chicago without the IANA database (Windows Python has no tz data by default) ----

def _nth_sunday_utc_us(year: int, month: int, n: int, utc_hour: int) -> int:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    day = first_sunday + timedelta(days=7 * (n - 1))
    dt = datetime(day.year, day.month, day.day, utc_hour, tzinfo=timezone.utc)
    delta = dt - _EPOCH
    return (delta.days * 86400 + delta.seconds) * US_PER_S


def central_offset_hours(us: int) -> int:
    """-5 (CDT) or -6 (CST). US rule since 2007: daylight time from the second Sunday of March
    02:00 CST (08:00 UTC) to the first Sunday of November 02:00 CDT (07:00 UTC)."""
    year = us_to_utc(us).year
    start = _nth_sunday_utc_us(year, 3, 2, 8)
    end = _nth_sunday_utc_us(year, 11, 1, 7)
    return -5 if start <= us < end else -6


def to_central(us: int) -> datetime:
    """Naive wall-clock datetime in America/Chicago."""
    off = central_offset_hours(us)
    return (us_to_utc(us) + timedelta(hours=off)).replace(tzinfo=None)


def central_date(us: int) -> date:
    return to_central(us).date()


def fmt_central(us: int, seconds: bool = True) -> str:
    zone = "CDT" if central_offset_hours(us) == -5 else "CST"
    return to_central(us).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M") + " " + zone


def fmt_money(micro: int) -> str:
    """Micro-dollars -> '$1,234.56' / '-$3.20' (exact to the cent; shows sub-cent if present)."""
    sign = "-" if micro < 0 else ""
    a = abs(micro)
    dollars, rem = divmod(a, ONE_DOLLAR)
    if rem % 10_000 == 0:
        return f"{sign}${dollars:,}.{rem // 10_000:02d}"
    return f"{sign}${dollars:,}.{rem:06d}".rstrip("0")


def fmt_signed_money(micro: int) -> str:
    return ("+" if micro > 0 else "") + fmt_money(micro)


def fmt_contracts(hundredths: int) -> str:
    whole, rem = divmod(abs(hundredths), 100)
    sign = "-" if hundredths < 0 else ""
    return f"{sign}{whole:,}" if rem == 0 else f"{sign}{whole:,}.{rem:02d}"


def fmt_cents(micro_price: int) -> str:
    """Price in micro-dollars -> cents text ('1.6c', '86c')."""
    whole, rem = divmod(micro_price, 10_000)
    if rem == 0:
        return f"{whole}c"
    return f"{whole}.{rem:04d}".rstrip("0") + "c"

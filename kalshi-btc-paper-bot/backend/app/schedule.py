"""15-minute window schedule.

All timestamps are timezone-aware UTC internally. Eligibility uses the window START
minute, evaluated in the display timezone (America/Chicago by default). Chicago,
New York and UTC differ by whole hours, so the start minute is the same in all of
them, but we still evaluate it in the configured zone so the rule reads exactly as
specified: trade windows that start at :00, :15 and :30; skip the :45 window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WINDOW = timedelta(minutes=15)
UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_ELIGIBLE_MINUTES = (0, 15, 30)


class Clock:
    """Wall clock. Tests substitute a controllable clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class SkewCorrectedClock(Clock):
    """Wall clock aligned to Kalshi server time when this computer's clock is off.

    The offset (server minus local, from HTTP Date headers) is applied only when it is at least
    ``threshold`` seconds, rounded to whole seconds, and only changed when it moves by a full
    second, so ordinary jitter never makes time step backwards.
    """

    def __init__(self, base: Clock | None = None, threshold: float = 2.0):
        self.base = base or Clock()
        self.threshold = threshold
        self._offset_fn = None
        self._applied = 0.0

    def attach(self, offset_fn) -> None:
        self._offset_fn = offset_fn

    @property
    def measured_offset(self) -> float | None:
        return self._offset_fn() if self._offset_fn else None

    @property
    def applied_offset(self) -> float:
        measured = self.measured_offset
        if measured is None:
            return self._applied
        target = float(round(measured)) if abs(measured) >= self.threshold else 0.0
        if abs(target - self._applied) >= 1.0:
            self._applied = target
        return self._applied

    def now(self) -> datetime:
        return self.base.now() + timedelta(seconds=self.applied_offset)

    def monotonic(self) -> float:
        return self.base.monotonic()


class ManualClock(Clock):
    def __init__(self, start: datetime):
        self._now = start.astimezone(UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def set(self, value: datetime) -> None:
        delta = (value - self._now).total_seconds()
        self._now = value.astimezone(UTC)
        self._mono += max(delta, 0.0)

    def advance(self, seconds: float) -> None:
        self.set(self._now + timedelta(seconds=seconds))


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime not allowed; use timezone-aware UTC")
    return value.astimezone(UTC)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def window_start_for(moment: datetime) -> datetime:
    """Start of the 15-minute window containing ``moment`` (UTC)."""
    moment = ensure_utc(moment)
    floored_minute = moment.minute - (moment.minute % 15)
    return moment.replace(minute=floored_minute, second=0, microsecond=0)


def is_eligible_start(start: datetime, eligible_minutes: tuple[int, ...] | list[int], tz_name: str) -> bool:
    start_local = ensure_utc(start).astimezone(ZoneInfo(tz_name))
    if start_local.second or start_local.microsecond or start_local.minute % 15:
        return False
    return start_local.minute in set(eligible_minutes)


def next_eligible_starts(
    after: datetime, eligible_minutes: tuple[int, ...] | list[int], tz_name: str, count: int = 3, inclusive: bool = False
) -> list[datetime]:
    """Eligible window starts strictly after ``after`` (or at it when ``inclusive``)."""
    after = ensure_utc(after)
    candidate = window_start_for(after)
    if candidate < after or (candidate == after and not inclusive):
        candidate += WINDOW
    found: list[datetime] = []
    guard = 0
    while len(found) < count and guard < 4 * 24 * 8:
        if is_eligible_start(candidate, eligible_minutes, tz_name):
            found.append(candidate)
        candidate += WINDOW
        guard += 1
    return found


@dataclass(frozen=True)
class SlotInfo:
    start: datetime
    end: datetime
    eligible: bool
    label: str


def hour_slots(moment: datetime, eligible_minutes: tuple[int, ...] | list[int], tz_name: str) -> list[SlotInfo]:
    """The four windows of the display-timezone hour containing ``moment``."""
    tz = ZoneInfo(tz_name)
    local = ensure_utc(moment).astimezone(tz)
    hour_start_local = local.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_start_local.astimezone(UTC)
    slots = []
    for i in range(4):
        start = hour_start + i * WINDOW
        end = start + WINDOW
        sl = start.astimezone(tz)
        el = end.astimezone(tz)
        slots.append(
            SlotInfo(
                start=start,
                end=end,
                eligible=is_eligible_start(start, eligible_minutes, tz_name),
                label=f"{sl:%H:%M}–{el:%H:%M}",
            )
        )
    return slots


_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def parse_event_ticker_close(event_ticker: str, series_ticker: str) -> datetime | None:
    candidates = parse_event_ticker_close_candidates(event_ticker, series_ticker)
    return candidates[0] if candidates else None


def parse_event_ticker_close_candidates(event_ticker: str, series_ticker: str) -> list[datetime]:
    """All UTC instants the New York wall time in the ticker can denote.

    Usually one; two during the repeated 1 AM hour when daylight saving time ends.
    """
    local = _parse_event_ticker_local(event_ticker, series_ticker)
    if local is None:
        return []
    out: list[datetime] = []
    for fold in (0, 1):
        utc = local.replace(fold=fold, tzinfo=NEW_YORK).astimezone(UTC)
        if utc not in out:
            out.append(utc)
    return out


def _parse_event_ticker_local(event_ticker: str, series_ticker: str) -> datetime | None:
    """Parse the close time encoded in a 15-minute event ticker.

    Observed format: ``KXBTC15M-26OCT021330`` = 2026-10-02 13:30 America/New_York, which is
    the window CLOSE time. Used only as a cross-check of the API's ``close_time``; tickers
    are never constructed or guessed from it.
    """
    prefix = f"{series_ticker}-"
    if not event_ticker.startswith(prefix):
        return None
    stamp = event_ticker[len(prefix):].split("-", 1)[0]
    if len(stamp) != 11:
        return None
    try:
        year = 2000 + int(stamp[0:2])
        month = _MONTHS[stamp[2:5].upper()]
        day = int(stamp[5:7])
        hour = int(stamp[7:9])
        minute = int(stamp[9:11])
        return datetime(year, month, day, hour, minute)
    except (KeyError, ValueError):
        return None

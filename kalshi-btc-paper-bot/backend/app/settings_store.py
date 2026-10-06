"""Editable strategy settings, persisted in SQLite.

Changes apply to the NEXT window that is entered. Each window stores a snapshot of the
settings it was entered with, and existing orders keep their own limit/quantity.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import tx
from .paper.fees import FEE_MODES
from .money import D, s
from .schedule import iso

DEFAULTS = {
    "starting_balance": "1000",
    "limit_price": "0.37",
    "contracts_per_side": "20",
    "eligible_minutes": "[0, 15, 30]",
    "display_timezone": "America/Chicago",
    "fee_mode": "taker",
}


@dataclass(frozen=True)
class StrategySettings:
    starting_balance: Decimal
    limit_price: Decimal
    contracts_per_side: Decimal
    eligible_minutes: tuple[int, ...]
    display_timezone: str
    fee_mode: str

    def to_json(self) -> dict:
        d = asdict(self)
        d["starting_balance"] = s(self.starting_balance)
        d["limit_price"] = s(self.limit_price)
        d["contracts_per_side"] = s(self.contracts_per_side)
        d["eligible_minutes"] = list(self.eligible_minutes)
        return d


class SettingsError(ValueError):
    pass


def _validate(raw: dict[str, str]) -> StrategySettings:
    try:
        starting = D(raw["starting_balance"])
        limit = D(raw["limit_price"])
        qty = D(raw["contracts_per_side"])
        minutes = json.loads(raw["eligible_minutes"]) if isinstance(raw["eligible_minutes"], str) else raw["eligible_minutes"]
    except (ValueError, json.JSONDecodeError) as exc:
        raise SettingsError(str(exc)) from exc
    if not (Decimal("1") <= starting <= Decimal("10000000")):
        raise SettingsError("starting_balance must be between 1 and 10,000,000")
    if starting != starting.quantize(Decimal("0.01")):
        raise SettingsError("starting_balance must have at most 2 decimal places")
    if not (Decimal("0.01") <= limit <= Decimal("0.99")):
        raise SettingsError("limit_price must be between 0.01 and 0.99")
    if limit != limit.quantize(Decimal("0.0001")):
        raise SettingsError("limit_price supports at most 4 decimal places")
    if qty != qty.to_integral_value() or not (Decimal("1") <= qty <= Decimal("10000")):
        raise SettingsError("contracts_per_side must be a whole number between 1 and 10,000")
    if not isinstance(minutes, list) or not minutes:
        raise SettingsError("eligible_minutes must be a non-empty list")
    clean = sorted({int(m) for m in minutes})
    if any(m not in (0, 15, 30, 45) for m in clean):
        raise SettingsError("eligible_minutes may only contain 0, 15, 30, 45")
    tz = str(raw["display_timezone"])
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingsError(f"unknown timezone {tz!r}") from exc
    fee_mode = str(raw["fee_mode"])
    if fee_mode not in FEE_MODES:
        raise SettingsError(f"fee_mode must be one of {FEE_MODES}")
    return StrategySettings(starting, limit, qty, tuple(clean), tz, fee_mode)


def load_settings(conn: sqlite3.Connection) -> StrategySettings:
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    merged = {**DEFAULTS, **rows}
    return _validate(merged)


def ensure_defaults(conn: sqlite3.Connection, now: datetime, starting_balance: Decimal | None = None) -> None:
    with tx(conn):
        for key, value in DEFAULTS.items():
            if key == "starting_balance" and starting_balance is not None:
                value = s(starting_balance) or value
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)", (key, value, iso(now))
            )


def update_settings(conn: sqlite3.Connection, changes: dict, now: datetime) -> StrategySettings:
    allowed = set(DEFAULTS)
    unknown = set(changes) - allowed
    if unknown:
        raise SettingsError(f"unknown setting(s): {sorted(unknown)}")
    current = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    merged = {**DEFAULTS, **current}
    for key, value in changes.items():
        merged[key] = json.dumps(value) if key == "eligible_minutes" and not isinstance(value, str) else str(value)
    validated = _validate(merged)
    as_text = validated.to_json()
    with tx(conn):
        for key in changes:
            value = as_text[key]
            stored = json.dumps(value) if key == "eligible_minutes" else str(value)
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, stored, iso(now)),
            )
    return validated

"""Frozen S3 rules: every command refuses to run if rules_s3.json changed.

The SHA-256 of the exact file bytes is stored next to it in rules_s3.json.sha256. Changing the
rules file (even whitespace) makes load_rules() raise RulesChanged.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RULES_FILE = PACKAGE_ROOT / "rules_s3.json"

_REQUIRED_INT = (
    "entry_delay_us", "buy_price", "buy_qty", "sell_price", "sell_delay_us", "rebuy_price",
    "rebuy_delay_us", "max_side_qty", "starting_cash",
)


class RulesChanged(RuntimeError):
    pass


def fingerprint_path(rules_path: Path) -> Path:
    return rules_path.with_name(rules_path.name + ".sha256")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rules(rules_path: Path = RULES_FILE) -> tuple[dict, str]:
    """Return (rules, sha256). Raise RulesChanged if the file no longer matches its fingerprint."""
    rules_path = Path(rules_path)
    fp_path = fingerprint_path(rules_path)
    if not fp_path.exists():
        raise RulesChanged(f"missing fingerprint file {fp_path}; refusing to run")
    expected = fp_path.read_text(encoding="ascii").split()[0].strip().lower()
    actual = sha256_of(rules_path)
    if actual != expected:
        raise RulesChanged(
            f"{rules_path.name} changed (sha256 {actual} != frozen {expected}); refusing to run. "
            "S3 is frozen: restore the original file instead of editing it."
        )
    rules = json.loads(rules_path.read_bytes())
    _validate(rules)
    return rules, actual


def _validate(r: dict) -> None:
    for key in _REQUIRED_INT:
        if not isinstance(r.get(key), int) or r[key] < 0:
            raise ValueError(f"rules: {key} must be a non-negative integer")
    if r["buy_price"] >= r["sell_price"]:
        raise ValueError("rules: buy_price must be below sell_price")
    if not (0 < r["buy_price"] < 1_000_000 and 0 < r["sell_price"] < 1_000_000):
        raise ValueError("rules: prices must be between 0 and $1")
    if r["buy_qty"] > r["max_side_qty"]:
        raise ValueError("rules: buy_qty above max_side_qty")
    if sorted(r["sides"]) != ["no", "yes"]:
        raise ValueError("rules: sides must be yes and no")
    coins = [s["coin"] for s in r["series"]]
    if r["same_instant_order"]["coin"] != coins:
        raise ValueError("rules: same_instant_order.coin must list the series coins in order")
    for rate in (r["fees"]["base_maker_rate"], r["fees"]["sensitivity_maker_rate"]):
        Fraction(rate)  # must parse exactly


def maker_rates(r: dict) -> dict[str, Fraction]:
    return {
        "base": Fraction(r["fees"]["base_maker_rate"]),
        "fee": Fraction(r["fees"]["sensitivity_maker_rate"]),
    }

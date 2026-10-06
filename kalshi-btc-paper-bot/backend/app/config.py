"""Process configuration (environment variables / optional .env file).

Strategy parameters that the user edits in the dashboard (limit price, contracts per
side, eligible minutes, display timezone, fee mode) live in the database instead, see
``app.settings_store``. Nothing here is a secret: the app only uses public market data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_FALLBACK_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_SERIES = "KXBTC15M"
PREVIEW_SERIES = "PREVIEWBTC15M"


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


@dataclass(frozen=True)
class Config:
    mode: str = "live"  # "live" (real Kalshi data) or "preview" (labeled sample data)
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = PROJECT_ROOT / "data"
    log_level: str = "INFO"

    kalshi_base_url: str = DEFAULT_BASE_URL
    kalshi_fallback_base_url: str = DEFAULT_FALLBACK_BASE_URL
    series_ticker: str = DEFAULT_SERIES
    http_timeout_seconds: float = 8.0

    tick_seconds: float = 1.0
    orderbook_poll_seconds: float = 2.0
    market_poll_seconds: float = 5.0
    exchange_status_poll_seconds: float = 30.0
    settlement_poll_seconds: float = 20.0
    fee_schedule_refresh_seconds: float = 1800.0
    stale_after_seconds: float = 6.0
    market_status_stale_seconds: float = 20.0
    execution_delay_seconds: float = 2.0
    entry_grace_seconds: float = 30.0  # max lateness after window start for discovery delays
    summary_resync_min_seconds: float = 10.0
    exchange_status_stale_seconds: float = 120.0
    discovery_lead_seconds: float = 90.0
    settlement_delay_warning_seconds: float = 3600.0
    equity_snapshot_seconds: float = 60.0
    price_tick_retention_days: int = 14
    lease_ttl_seconds: float = 20.0

    starting_balance: Decimal = Decimal("1000")
    # "net_on_fill": mirror Kalshi's single signed position per market — each matched
    # UP/DOWN pair is worth exactly $1.00 and is credited once, when it forms.
    # "hold_to_settlement": keep both legs open and pay every winning contract at settlement.
    pair_accounting: str = "net_on_fill"

    @property
    def is_preview(self) -> bool:
        return self.mode == "preview"

    @property
    def db_path(self) -> Path:
        return self.data_dir / ("preview.sqlite3" if self.is_preview else "paper.sqlite3")

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ("preview.engine.lock" if self.is_preview else "paper.engine.lock")

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def load_config(env_file: Path | None = None, **overrides: object) -> Config:
    _load_dotenv(env_file or (PROJECT_ROOT / ".env"))
    mode = _env("KBOT_MODE", "live").lower()
    if mode not in {"live", "preview"}:
        raise ValueError("KBOT_MODE must be 'live' or 'preview'")
    data_dir = Path(_env("KBOT_DATA_DIR", str(PROJECT_ROOT / "data")))
    if not data_dir.is_absolute():
        data_dir = (PROJECT_ROOT / data_dir).resolve()
    pair_accounting = _env("KBOT_PAIR_ACCOUNTING", "net_on_fill")
    if pair_accounting not in {"net_on_fill", "hold_to_settlement"}:
        raise ValueError("KBOT_PAIR_ACCOUNTING must be net_on_fill or hold_to_settlement")
    host = _env("KBOT_HOST", "127.0.0.1")
    values: dict[str, object] = dict(
        mode=mode,
        host=host,
        port=int(_env("KBOT_PORT", "8000")),
        data_dir=data_dir,
        log_level=_env("KBOT_LOG_LEVEL", "INFO").upper(),
        kalshi_base_url=_env("KALSHI_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        kalshi_fallback_base_url=_env("KALSHI_FALLBACK_BASE_URL", DEFAULT_FALLBACK_BASE_URL).rstrip("/"),
        series_ticker=PREVIEW_SERIES if mode == "preview" else _env("KALSHI_SERIES_TICKER", DEFAULT_SERIES),
        http_timeout_seconds=_env_float("KBOT_HTTP_TIMEOUT_SECONDS", 8.0),
        orderbook_poll_seconds=_env_float("KBOT_ORDERBOOK_POLL_SECONDS", 2.0),
        market_poll_seconds=_env_float("KBOT_MARKET_POLL_SECONDS", 5.0),
        settlement_poll_seconds=_env_float("KBOT_SETTLEMENT_POLL_SECONDS", 20.0),
        stale_after_seconds=_env_float("KBOT_STALE_AFTER_SECONDS", 6.0),
        execution_delay_seconds=_env_float("KBOT_EXECUTION_DELAY_SECONDS", 2.0),
        entry_grace_seconds=_env_float("KBOT_ENTRY_GRACE_SECONDS", 30.0),
        starting_balance=Decimal(_env("KBOT_STARTING_BALANCE", "1000")),
        pair_accounting=pair_accounting,
    )
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]

"""SQLite storage.

Durability rules:
  * WAL journal + synchronous=FULL, every state change in a BEGIN IMMEDIATE transaction.
  * Deterministic primary keys / UNIQUE constraints make every write idempotent:
      windows   UNIQUE(account_id, window_start)
      orders    id = "<window id>:<UP|DOWN>", UNIQUE(window_id, side)
      fills     id = "<order id>:<observation key>:<price>"
      ledger    UNIQUE(ref)   (e.g. "fill:<fill id>", "settle:<window id>")
      settlements PRIMARY KEY(window_id)
    A crash, duplicate poll or restart therefore cannot duplicate orders, fills or money.
  * Money and quantities are Decimal values stored as TEXT.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    starting_balance TEXT NOT NULL,
    cash_balance TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    trading_enabled INTEGER NOT NULL DEFAULT 0,
    armed_at TEXT,
    paused_at TEXT,
    lease_owner TEXT,
    lease_heartbeat TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fee_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    fee_type TEXT NOT NULL,
    fee_multiplier TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS windows (
    id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('skipped', 'active', 'awaiting_settlement', 'settled', 'abandoned')),
    skip_reason TEXT,
    market_ticker TEXT,
    event_ticker TEXT,
    market_title TEXT,
    market_url TEXT,
    strike_type TEXT,
    floor_strike TEXT,
    rules_primary TEXT,
    market_open_time TEXT,
    market_close_time TEXT,
    settings_json TEXT,
    fee_schedule_json TEXT,
    pair_accounting TEXT,
    up_qty TEXT NOT NULL DEFAULT '0',
    up_cost TEXT NOT NULL DEFAULT '0',
    down_qty TEXT NOT NULL DEFAULT '0',
    down_cost TEXT NOT NULL DEFAULT '0',
    fees TEXT NOT NULL DEFAULT '0',
    pairs_credited TEXT NOT NULL DEFAULT '0',
    pair_credit_amount TEXT NOT NULL DEFAULT '0',
    mark_up TEXT,
    mark_down TEXT,
    mark_source TEXT,
    mark_ts TEXT,
    market_status TEXT,
    market_result TEXT,
    settlement_value TEXT,
    settlement_note TEXT,
    payout TEXT,
    net_pnl TEXT,
    outcome_class TEXT,
    orders_closed_at TEXT,
    settled_at TEXT,
    last_settlement_check TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, window_start)
);
CREATE INDEX IF NOT EXISTS idx_windows_status ON windows(account_id, status);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    window_id TEXT NOT NULL REFERENCES windows(id),
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('UP', 'DOWN')),
    kalshi_side TEXT NOT NULL CHECK (kalshi_side IN ('yes', 'no')),
    limit_price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    filled_qty TEXT NOT NULL DEFAULT '0',
    principal_cost TEXT NOT NULL DEFAULT '0',
    fees_paid TEXT NOT NULL DEFAULT '0',
    fee_exact_total TEXT NOT NULL DEFAULT '0',
    fee_reserved TEXT NOT NULL,
    reserved_remaining TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'partially_filled', 'filled', 'expired', 'canceled')),
    close_reason TEXT,
    settled INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT NOT NULL,
    first_fill_at TEXT,
    last_fill_at TEXT,
    closed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (window_id, side)
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(account_id, status);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_key TEXT NOT NULL UNIQUE,
    market_ticker TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    server_date TEXT,
    source TEXT NOT NULL,
    market_status TEXT,
    book_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    order_id TEXT NOT NULL REFERENCES orders(id),
    window_id TEXT NOT NULL REFERENCES windows(id),
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    level_price TEXT NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    principal TEXT NOT NULL,
    fee TEXT NOT NULL,
    liquidity TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    trigger_observation_id INTEGER REFERENCES observations(id),
    filled_at TEXT NOT NULL,
    calc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount TEXT NOT NULL,
    cash_after TEXT NOT NULL,
    ref TEXT NOT NULL UNIQUE,
    window_id TEXT,
    order_id TEXT,
    memo TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
    window_id TEXT PRIMARY KEY REFERENCES windows(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    market_ticker TEXT NOT NULL,
    market_status TEXT NOT NULL,
    result TEXT NOT NULL,
    yes_payout TEXT NOT NULL,
    no_payout TEXT NOT NULL,
    kalshi_settled_ts TEXT,
    up_qty TEXT NOT NULL,
    down_qty TEXT NOT NULL,
    pairs_credited TEXT NOT NULL,
    payout TEXT NOT NULL,
    principal TEXT NOT NULL,
    fees TEXT NOT NULL,
    net_pnl TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    ts TEXT NOT NULL,
    cash TEXT NOT NULL,
    reserved TEXT NOT NULL,
    position_value TEXT NOT NULL,
    equity TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_account ON equity_snapshots(account_id, ts);

CREATE TABLE IF NOT EXISTS price_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_ticker TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('book', 'summary')),
    yes_bid TEXT, yes_bid_size TEXT, yes_ask TEXT, yes_ask_size TEXT,
    no_bid TEXT, no_bid_size TEXT, no_ask TEXT, no_ask_size TEXT,
    last_price TEXT,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_market ON price_ticks(market_ticker, ts);

CREATE TABLE IF NOT EXISTS price_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quote_type TEXT NOT NULL,
    visible_price TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    window_id TEXT,
    dedupe_key TEXT UNIQUE
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    with tx(conn):
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception (nested calls join the outer tx)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

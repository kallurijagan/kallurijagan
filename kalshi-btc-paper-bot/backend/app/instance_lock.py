"""Guard against two trading engines running on the same paper account.

Two layers:
  1. An exclusive OS lock on ``data/<mode>.engine.lock`` (msvcrt on Windows, fcntl
     elsewhere). The OS releases it automatically if the process dies.
  2. A lease row in the database (owner id + heartbeat). A second engine that somehow
     reaches the same database refuses to trade while another owner's heartbeat is fresh.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .db import tx
from .schedule import iso, parse_ts


class EngineAlreadyRunning(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise EngineAlreadyRunning(
                f"Another trading engine is already running for this account (lock held: {self.path}). "
                "Stop the other instance first."
            ) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} host={socket.gethostname()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def new_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def ensure_engine_row(conn: sqlite3.Connection, now: datetime) -> None:
    with tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO engine_state(id, trading_enabled, updated_at) VALUES(1, 0, ?)", (iso(now),)
        )


def acquire_lease(conn: sqlite3.Connection, owner: str, now: datetime, ttl_seconds: float) -> None:
    ensure_engine_row(conn, now)
    with tx(conn):
        row = conn.execute("SELECT lease_owner, lease_heartbeat FROM engine_state WHERE id=1").fetchone()
        holder, beat = row["lease_owner"], parse_ts(row["lease_heartbeat"])
        if holder and holder != owner and beat and (now - beat).total_seconds() < ttl_seconds:
            raise EngineAlreadyRunning(
                f"Database lease held by another engine ({holder}, heartbeat {row['lease_heartbeat']})."
            )
        conn.execute(
            "UPDATE engine_state SET lease_owner=?, lease_heartbeat=?, updated_at=? WHERE id=1",
            (owner, iso(now), iso(now)),
        )


def heartbeat(conn: sqlite3.Connection, owner: str, now: datetime) -> None:
    with tx(conn):
        row = conn.execute("SELECT lease_owner FROM engine_state WHERE id=1").fetchone()
        if row["lease_owner"] != owner:
            raise EngineAlreadyRunning(f"Lease taken over by {row['lease_owner']}")
        conn.execute("UPDATE engine_state SET lease_heartbeat=? WHERE id=1", (iso(now),))


def release_lease(conn: sqlite3.Connection, owner: str) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE engine_state SET lease_owner=NULL, lease_heartbeat=NULL WHERE id=1 AND lease_owner=?", (owner,)
        )

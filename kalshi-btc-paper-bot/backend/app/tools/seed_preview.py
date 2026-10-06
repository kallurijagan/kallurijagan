"""Fast-forward the PREVIEW engine over past hours of SYNTHETIC sample data.

    python -m app.tools.seed_preview --hours 6     (or:  .\\start.ps1 -Preview -SeedPreview)

Only ever writes the separate PREVIEW database (data/preview.sqlite3); refuses to run
against the live paper database. Useful to see the Analytics layout populated before any
live history exists. Everything it creates is labeled PREVIEW / sample data.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from ..config import PREVIEW_SERIES, load_config
from ..db import connect, init_schema
from ..engine import Engine
from ..marketdata.preview import PreviewSource
from ..schedule import UTC, ManualClock


async def seed(hours: float) -> None:
    cfg = load_config(mode="preview", series_ticker=PREVIEW_SERIES)
    assert cfg.is_preview and cfg.db_path.name == "preview.sqlite3"
    end = datetime.now(UTC) - timedelta(seconds=5)
    clock = ManualClock(end - timedelta(hours=hours))
    conn = connect(cfg.db_path)
    init_schema(conn)
    eng = Engine(cfg, conn, PreviewSource(clock), clock, use_locks=True)
    await eng.start(run_loop=False)
    if not eng.paper.engine_state()["trading_enabled"]:
        eng.start_trading()
    steps = 0
    while clock.now() < end:
        await eng.tick()
        clock.advance(2)
        steps += 1
    await eng.stop()
    n = conn.execute("SELECT COUNT(*) FROM windows WHERE status != 'skipped'").fetchone()[0]
    print(f"PREVIEW database seeded with {n} entered sample windows ({steps} ticks) at {cfg.db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=6.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(seed(args.hours))


if __name__ == "__main__":
    main()

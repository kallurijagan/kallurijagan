"""FastAPI application: starts ONE paper engine in-process and serves the dashboard.

The engine keeps running when the browser tab is closed; it stops only when this process
stops (Ctrl+C in the start.ps1 window performs a graceful shutdown).
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .config import FRONTEND_DIST, Config, load_config
from .db import connect, init_schema
from .engine import Engine
from .instance_lock import EngineAlreadyRunning
from .marketdata.client import KalshiReadOnlyClient
from .marketdata.preview import PreviewSource
from .schedule import Clock, SkewCorrectedClock

log = logging.getLogger("kbot")


def setup_logging(cfg: Config) -> None:
    """Console + rotating file logs, written from a background thread.

    Handlers run on a QueueListener thread so a slow or blocked console (e.g. a text selection
    in the Windows console) can never stall the engine's event loop.
    """
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_kbot_configured", False):
        return
    root.setLevel(cfg.log_level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log_file = cfg.log_dir / ("preview.log" if cfg.is_preview else "paper-bot.log")
    fileh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    q: queue.SimpleQueue = queue.SimpleQueue()
    listener = logging.handlers.QueueListener(q, console, fileh, respect_handler_level=True)
    listener.start()
    atexit.register(listener.stop)
    root.addHandler(logging.handlers.QueueHandler(q))
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []  # propagate to root -> also lands in data/logs
        logging.getLogger(name).propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root._kbot_configured = True  # type: ignore[attr-defined]


def build_source(cfg: Config, clock: Clock):
    if cfg.is_preview:
        return PreviewSource(clock, cfg.series_ticker)
    client = KalshiReadOnlyClient(cfg.kalshi_base_url, cfg.kalshi_fallback_base_url, cfg.http_timeout_seconds,
                                  now=clock.now)
    if isinstance(clock, SkewCorrectedClock):
        clock.attach(lambda: client.clock_offset_seconds, client.reset_clock_samples)
    return client


def create_app(cfg: Config | None = None, engine_factory=None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        clock = SkewCorrectedClock() if not cfg.is_preview else Clock()
        conn = connect(cfg.db_path)
        init_schema(conn)
        engine = engine_factory(cfg, conn, clock) if engine_factory else Engine(cfg, conn, build_source(cfg, clock), clock)
        try:
            await engine.start()
        except EngineAlreadyRunning as exc:
            log.critical("%s", exc)
            conn.close()
            raise
        app.state.engine = engine
        log.info("PAPER TRADING dashboard: http://%s:%s  (mode=%s, db=%s)", cfg.host, cfg.port, cfg.mode, cfg.db_path)
        try:
            yield
        finally:
            log.info("shutting down: stopping engine")
            await engine.stop()
            conn.close()

    app = FastAPI(title="Kalshi BTC 15m Paper Bot", version=__version__, lifespan=lifespan)
    app.include_router(router)

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception):  # noqa: ANN001
        log.exception("API error")
        return JSONResponse(status_code=500, content={"paper": True, "error": f"{type(exc).__name__}: {exc}"})

    dist = Path(FRONTEND_DIST)
    if (dist / "index.html").is_file():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse(status_code=404, content={"error": "not found"})
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def no_frontend():
            return HTMLResponse(
                "<h1>PAPER TRADING backend is running</h1><p>The dashboard has not been built yet. "
                "Run <code>setup.ps1</code> (or <code>npm run build</code> in <code>frontend/</code>).</p>"
                "<p>API: <a href='/api/state'>/api/state</a></p>"
            )

    return app

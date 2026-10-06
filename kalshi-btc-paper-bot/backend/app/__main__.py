"""Run the paper bot: ``python -m app [--preview] [--port N]``.

Binds to 127.0.0.1 by default. There is no live-trading mode.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Kalshi 15-minute BTC PAPER trading bot (simulated money)")
    parser.add_argument("--preview", action="store_true", help="labeled sample data, separate preview database")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None, help="default 127.0.0.1 (localhost only)")
    args = parser.parse_args()
    if args.preview:
        os.environ["KBOT_MODE"] = "preview"
    if args.port:
        os.environ["KBOT_PORT"] = str(args.port)
    if args.host:
        os.environ["KBOT_HOST"] = args.host

    import uvicorn

    from .config import load_config
    from .main import create_app
    from .winpower import disable_quick_edit

    disable_quick_edit()  # a click in the console window must not freeze the engine

    cfg = load_config()
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to {cfg.host} exposes the dashboard beyond this computer.", file=sys.stderr)
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower(),
                                           access_log=False, log_config=None))
    app.state.uvicorn_server = server  # lets stop.ps1 request a graceful shutdown
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

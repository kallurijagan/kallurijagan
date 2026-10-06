"""The paper engine must never be able to call a real exchange order endpoint."""

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.config import load_config
from app.db import connect, init_schema
from app.engine import Engine
from app.marketdata.client import ForbiddenEndpointError, KalshiReadOnlyClient, assert_read_only
from app.settings_store import ensure_defaults, update_settings

from conftest import T, book_payload, market_payload, tickers_for

APP = Path(__file__).resolve().parents[1] / "app"
BASE = "https://api.elections.kalshi.com/trade-api/v2"


@pytest.mark.parametrize("method,path", [
    ("POST", "/portfolio/orders"),
    ("POST", "/portfolio/orders/batched"),
    ("DELETE", "/portfolio/orders/abc"),
    ("POST", "/portfolio/orders/abc/amend"),
    ("POST", "/portfolio/orders/abc/decrease"),
    ("GET", "/portfolio/balance"),
    ("GET", "/portfolio/positions"),
    ("GET", "/portfolio/orders"),
    ("PUT", "/markets/X"),
    ("POST", "/markets"),
])
def test_guard_rejects_order_and_portfolio_endpoints(method, path):
    with pytest.raises(ForbiddenEndpointError):
        assert_read_only(method, BASE + path)


def test_guard_rejects_other_hosts_and_credentials():
    with pytest.raises(ForbiddenEndpointError):
        assert_read_only("GET", "https://evil.example.com/trade-api/v2/markets")
    with pytest.raises(ForbiddenEndpointError):
        assert_read_only("GET", BASE + "/markets", {"KALSHI-ACCESS-KEY": "x"})
    assert_read_only("GET", BASE + "/markets/KXBTC15M-26OCT061115-15/orderbook")  # allowed


def test_http_hook_blocks_even_direct_use_of_the_underlying_client():
    seen = []
    transport = httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, json={}))

    async def go():
        client = KalshiReadOnlyClient(BASE, None, transport=transport)
        with pytest.raises(ForbiddenEndpointError):
            await client._http.post(BASE + "/portfolio/orders", json={"ticker": "X", "side": "yes", "count": 1})
        with pytest.raises(ForbiddenEndpointError):
            await client._http.get(BASE + "/portfolio/balance")
        await client.aclose()
    asyncio.run(go())
    assert seen == []  # nothing reached the transport


def test_client_exposes_no_order_methods():
    names = {n for n in dir(KalshiReadOnlyClient) if not n.startswith("__")}
    forbidden = re.compile(r"order(?!book)|cancel|amend|decrease|portfolio|position|balance|create", re.I)
    assert not [n for n in names if forbidden.search(n)]


def test_paper_package_has_no_network_imports():
    for py in (APP / "paper").glob("*.py"):
        text = py.read_text()
        assert "import httpx" not in text and "from httpx" not in text, py
        assert "marketdata.client" not in text and "KalshiReadOnlyClient" not in text, py
        assert "marketdata.feed" not in text, py


def test_no_portfolio_paths_anywhere_in_app():
    for py in APP.rglob("*.py"):
        text = py.read_text()
        assert "/portfolio" not in text, py


def test_full_engine_run_against_mock_kalshi_only_issues_allowlisted_gets(tmp_path, clock):
    """Run a whole window through the LIVE client over a mock transport; record every request."""
    seen: list[httpx.Request] = []
    start = T(15, 0)
    event, ticker = tickers_for(start)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        now = clock.now()
        path = req.url.path.replace("/trade-api/v2", "")
        status = "active" if start <= now < start + timedelta(minutes=15) else "initialized" if now < start else "finalized"
        market = market_payload(start, status, **({"result": "yes", "settlement_value_dollars": "1.0000"} if status == "finalized" else {}))
        if path == "/exchange/status":
            return httpx.Response(200, json={"exchange_active": True, "trading_active": True})
        if path == "/series/KXBTC15M":
            return httpx.Response(200, json={"series": {"ticker": "KXBTC15M", "title": "BTC 15m", "frequency": "fifteen_min",
                                                        "fee_type": "quadratic", "fee_multiplier": 1}})
        if path == "/series/fee_changes":
            return httpx.Response(200, json={"series_fee_change_arr": []})
        if path == "/markets":
            lo = int(req.url.params["min_close_ts"])
            close = (start + timedelta(minutes=15)).timestamp()
            ok = lo <= close <= int(req.url.params["max_close_ts"])
            return httpx.Response(200, json={"markets": [market] if ok else [], "cursor": ""})
        if path == f"/markets/{ticker}":
            return httpx.Response(200, json={"market": market})
        if path == f"/markets/{ticker}/orderbook":
            return httpx.Response(200, json=book_payload([("0.30", "5.00")], [("0.63", "20.00")]))
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async def go():
        cfg = load_config(data_dir=tmp_path)
        conn = connect(tmp_path / "paper.sqlite3")
        init_schema(conn)
        ensure_defaults(conn, clock.now())
        update_settings(conn, {"fee_mode": "taker"}, clock.now())
        client = KalshiReadOnlyClient(BASE, None, transport=httpx.MockTransport(handler), min_interval=0, now=clock.now)
        eng = Engine(cfg, conn, client, clock, use_locks=False)
        await eng.start(run_loop=False)
        eng.start_trading()
        for _ in range(20 * 60):
            await eng.tick()
            clock.advance(1)
        fills = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        await client.aclose()
        return fills

    fills = asyncio.run(go())
    assert fills >= 1  # the run really exercised simulated execution
    assert seen, "no requests recorded"
    for req in seen:
        assert req.method == "GET", req
        assert "/portfolio" not in req.url.path
        assert_read_only(req.method, str(req.url), req.headers)
        assert not any(h.lower().startswith("kalshi-access") for h in req.headers)
    assert all(not req.content for req in seen)  # GETs carry no body
    json.dumps([str(r.url) for r in seen])

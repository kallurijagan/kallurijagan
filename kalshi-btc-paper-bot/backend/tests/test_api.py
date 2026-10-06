"""HTTP API smoke tests (one engine, one account, shared by all three layouts)."""

from fastapi.testclient import TestClient

from app.config import load_config
from app.engine import Engine
from app.main import create_app

from conftest import FakeSource


def make_client(tmp_path):
    cfg = load_config(data_dir=tmp_path)

    def factory(cfg, conn, clock):
        return Engine(cfg, conn, FakeSource(clock), clock, use_locks=True)

    return TestClient(create_app(cfg, engine_factory=factory))


def test_state_settings_control_reset_and_exports(tmp_path):
    with make_client(tmp_path) as c:
        st = c.get("/api/state").json()
        assert st["paper"] is True and st["paper_label"] == "PAPER TRADING"
        assert st["account"]["starting_balance"] == "1000" and st["account"]["cash"] == "1000"
        assert st["engine"]["state"] == "stopped"
        assert [s["eligible"] for s in st["schedule"]["hour_slots"]] == [True, True, True, False]
        assert st["settings"] == {"starting_balance": "1000", "limit_price": "0.37", "contracts_per_side": "20",
                                  "eligible_minutes": [0, 15, 30], "display_timezone": "America/Chicago",
                                  "fee_mode": "taker"}

        assert c.put("/api/settings", json={"limit_price": "1.5"}).status_code == 400
        assert c.put("/api/settings", json={"eligible_minutes": [0, 7]}).status_code == 400
        ok = c.put("/api/settings", json={"limit_price": "0.35", "eligible_minutes": [0, 15, 30]})
        assert ok.status_code == 200 and ok.json()["settings"]["limit_price"] == "0.35"

        assert c.post("/api/control/start").json()["state"] == "running"
        assert c.get("/api/state").json()["engine"]["state"] == "running"
        assert c.post("/api/control/pause").json()["state"] == "paused"
        assert c.get("/api/state").json()["engine"]["state"] == "paused"

        for kind in ("orders", "fills", "settlements", "windows", "ledger", "equity", "comparisons"):
            r = c.get(f"/api/export/{kind}.csv")
            assert r.status_code == 200 and r.text.startswith("# PAPER TRADING export"), kind
        assert c.get("/api/export/secrets.csv").status_code == 404

        assert c.post("/api/account/reset", json={"confirm": "yes"}).status_code == 400
        r = c.post("/api/account/reset", json={"confirm": "RESET"})
        assert r.status_code == 200 and r.json()["account_id"] == 2
        st = c.get("/api/state").json()
        assert st["account"]["account_id"] == 2 and st["account"]["cash"] == "1000"
        assert st["engine"]["state"] == "stopped"

        a = c.get("/api/analytics").json()
        assert "NOT evidence" in a["caveat"]
        assert c.get("/api/windows").status_code == 200
        assert c.get("/api/prices").status_code == 200


def test_second_engine_on_same_data_dir_is_refused(tmp_path):
    import pytest
    from app.instance_lock import EngineAlreadyRunning

    with make_client(tmp_path):
        with pytest.raises(EngineAlreadyRunning):
            with make_client(tmp_path):
                pass


def test_price_check_compare_endpoint_validates_and_records(tmp_path):
    import time

    with make_client(tmp_path) as c:
        for _ in range(30):  # wait for the engine to discover the market and record a tick
            if c.get("/api/state").json()["market"]:
                break
            time.sleep(0.2)
        bad = c.post("/api/price-check/compare", json={"side": "UP", "quote_type": "mid", "price": "37"})
        assert bad.status_code == 400
        r = c.post("/api/price-check/compare", json={"side": "UP", "quote_type": "unknown", "price": "37"})
        assert r.status_code == 200 and r.json()["comparison"]["verdict"] == "inconclusive"
        assert len(c.get("/api/price-check/comparisons").json()["comparisons"]) == 1

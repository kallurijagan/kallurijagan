"""Frozen rules, read-only public client, units, Central time, data loading, comebacks."""

import gzip
import json
import shutil
from datetime import datetime, timezone

import pytest

from conftest import P, S, T0, tr, win
from s3tape import kalshi_public
from s3tape.data import load_pull
from s3tape.engine import comeback_cycles
from s3tape.rules import RULES_FILE, RulesChanged, fingerprint_path, load_rules
from s3tape.units import (central_offset_hours, dollars_to_micro, fmt_central, fp_to_hundredths,
                          iso_to_us)


# ---------------------------------------------------------------- frozen rules
def test_frozen_rules_load():
    rules, digest = load_rules()
    assert rules["buy_price"] == 16_000 and rules["sell_price"] == 860_000
    assert rules["buy_qty"] == 20_000 and rules["max_side_qty"] == 20_000
    assert rules["rebuy"] is True and rules["open_minutes"] == [0, 15]
    assert rules["starting_cash"] == 1_000_000_000
    assert len(digest) == 64


def test_changed_rules_refused(tmp_path):
    rp = tmp_path / "rules_s3.json"
    shutil.copy(RULES_FILE, rp)
    shutil.copy(fingerprint_path(RULES_FILE), fingerprint_path(rp))
    load_rules(rp)
    rp.write_text(rp.read_text().replace('"sell_price": 860000', '"sell_price": 850000'))
    with pytest.raises(RulesChanged):
        load_rules(rp)
    rp.write_text(RULES_FILE.read_text() + " ")          # whitespace counts too
    with pytest.raises(RulesChanged):
        load_rules(rp)
    shutil.copy(RULES_FILE, rp)
    fingerprint_path(rp).unlink()
    with pytest.raises(RulesChanged):
        load_rules(rp)


def test_commands_refuse_to_run_with_changed_rules(monkeypatch, tmp_path):
    from s3tape import fetch, rules, run
    rp = tmp_path / "rules_s3.json"
    rp.write_text(RULES_FILE.read_text().replace("16000", "15000"))
    shutil.copy(fingerprint_path(RULES_FILE), fingerprint_path(rp))
    real = rules.load_rules
    monkeypatch.setattr(fetch, "load_rules", lambda: real(rp))
    monkeypatch.setattr(run, "load_rules", lambda: real(rp))
    with pytest.raises(RulesChanged):
        fetch.main(["--out", str(tmp_path / "x")])
    with pytest.raises(RulesChanged):
        run.main(["--data", str(tmp_path), "--out", str(tmp_path / "y")])


# ---------------------------------------------------------------- read-only client
class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_public_client_get_only_no_credentials_allow_list():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return _Resp({"trades": [{"trade_id": "a"}], "cursor": ""})

    c = kalshi_public.PublicClient(opener=opener, sleep=lambda s: None, min_interval_s=0)
    out = c.paginate("/markets/trades", {"ticker": "KXBTC15M-X", "limit": 1000}, "trades")
    assert out == [{"trade_id": "a"}]
    req = seen[0]
    assert req.get_method() == "GET"
    assert req.full_url.startswith("https://api.elections.kalshi.com/trade-api/v2/markets/trades?")
    assert not any(h.lower().startswith(("authorization", "kalshi-access")) for h in req.headers)
    for path in ("/portfolio/orders", "/portfolio/orders/abc", "/portfolio/balance",
                 "/markets/KXBTC15M-X/orderbook/../../portfolio/orders", "/series/../portfolio/orders"):
        with pytest.raises(kalshi_public.NotAllowed):
            c.url_for(path)
    with pytest.raises(kalshi_public.NotAllowed):
        kalshi_public.PublicClient(base_url="https://example.com/trade-api/v2")
    with pytest.raises(kalshi_public.NotAllowed):
        kalshi_public.PublicClient(base_url="http://api.elections.kalshi.com/trade-api/v2")


def test_no_order_code_anywhere():
    import pathlib
    src = "\n".join(p.read_text() for p in pathlib.Path(kalshi_public.__file__).parent.glob("*.py"))
    for needle in ("method=\"POST\"", "method='POST'", "DELETE", "PUT", "/portfolio", "KALSHI_PRIVATE_KEY",
                   "KALSHI_API_KEY", "create_order", "cancel_order"):
        assert needle not in src, needle


# ---------------------------------------------------------------- units and Central time
def test_exact_units():
    assert dollars_to_micro("0.0160") == 16_000
    assert dollars_to_micro("0.016000") == 16_000
    assert dollars_to_micro("0.86") == 860_000
    assert dollars_to_micro("1") == 1_000_000
    with pytest.raises(ValueError):
        dollars_to_micro("0.0000001")
    assert fp_to_hundredths("200.00") == 20_000
    assert fp_to_hundredths("2.5") == 250
    with pytest.raises(ValueError):
        fp_to_hundredths("0.001")
    assert iso_to_us("1970-01-01T00:00:01Z") == 1_000_000
    assert iso_to_us("2026-11-18T10:00:12.123456789Z") == iso_to_us("2026-11-18T10:00:12.123456Z")
    assert iso_to_us("2026-11-18T04:00:00-06:00") == iso_to_us("2026-11-18T10:00:00Z")


def test_central_time_matches_iana_rules():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        tz = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        pytest.skip("no IANA data on this machine")
    for year in (2025, 2026, 2027, 2030):
        start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
        for k in range(0, 366 * 24 * 4):
            ts = start + k * 900
            off = datetime.fromtimestamp(ts, tz).utcoffset().total_seconds() / 3600
            assert central_offset_hours(ts * S) == off, (year, ts)
    assert fmt_central(iso_to_us("2026-11-18T10:00:00Z")) == "2026-11-18 04:00:00 CST"
    assert fmt_central(iso_to_us("2026-07-01T10:00:00Z")) == "2026-07-01 05:00:00 CDT"


# ---------------------------------------------------------------- data loading
def _market(ticker, open_iso, close_iso, result="yes", status="finalized", volume="200.00", settle=None):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "open_time": open_iso,
            "close_time": close_iso, "status": status, "result": result, "volume_fp": volume,
            "settlement_ts": settle}


def _trade(i, iso, yes, size, taker="ask"):
    return {"trade_id": f"x{i}", "ticker": "", "count_fp": size, "yes_price_dollars": yes,
            "no_price_dollars": f"{1 - float(yes):.4f}", "taker_book_side": taker,
            "taker_outcome_side": "no" if taker == "ask" else "yes", "created_time": iso, "is_block_trade": False}


def test_load_pull_selects_s3_windows_and_orders_trades(tmp_path):
    rules, _ = load_rules()
    mk = [
        _market("KXBTC15M-26NOV180500-00", "2026-11-18T10:00:00Z", "2026-11-18T10:15:00Z",
                settle="2026-11-18T10:15:04Z"),
        _market("KXETH15M-26NOV180515-15", "2026-11-18T10:15:00Z", "2026-11-18T10:30:00Z", result="no"),
        _market("KXBTC15M-26NOV180545-30", "2026-11-18T10:30:00Z", "2026-11-18T10:45:00Z"),
        _market("KXBTC15M-26NOV180600-00", "2026-11-18T11:00:00Z", "2026-11-18T11:15:00Z",
                result="", status="closed"),
    ]
    trades = [  # API order: newest first
        {"ticker": mk[0]["ticker"], "source": "live", "api_index": 0,
         "raw": _trade(2, "2026-11-18T10:05:00.000Z", "0.0150", "50.00")},
        {"ticker": mk[0]["ticker"], "source": "live", "api_index": 1,
         "raw": _trade(1, "2026-11-18T10:05:00.000Z", "0.0160", "150.00")},
    ]
    (tmp_path / "manifest.json").write_text(json.dumps({
        "pulled_at_us": 0, "since_us": 0, "until_us": 0,
        "completeness": [{"ticker": m["ticker"], "complete": True} for m in mk]}))
    with gzip.open(tmp_path / "markets.jsonl.gz", "wt") as f:
        for m in mk:
            f.write(json.dumps({"source": "live", "raw": m}) + "\n")
    with gzip.open(tmp_path / "trades.jsonl.gz", "wt") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    windows, info = load_pull(tmp_path, rules)
    assert [w.ticker for w in windows] == [mk[0]["ticker"], mk[1]["ticker"]]
    assert [w.coin for w in windows] == ["BTC", "ETH"]
    btc = windows[0]
    assert [t.trade_id for t in btc.trades] == ["x1", "x2"]      # exchange order for equal times
    assert btc.trades[0].yes_px == 16_000 and btc.trades[0].qty == 15_000
    assert btc.settle_us == iso_to_us("2026-11-18T10:15:04Z") and not btc.settle_time_assumed
    assert windows[1].settle_time_assumed
    why = {r["ticker"]: r["why"] for r in info["listed"]}
    assert "opens at :30" in why[mk[2]["ticker"]]
    assert why[mk[3]["ticker"]].startswith("EXCLUDED")


# ---------------------------------------------------------------- comeback tracker
def test_comeback_cycles_counts_repeat_reversals():
    trades = [
        tr(1, 5, 10_000, 10),       # before open + 12 s: ignored
        tr(2, 20, 15_000, 10),      # YES low
        tr(3, 40, 870_000, 10),     # YES high: comeback 1
        tr(4, 60, 16_000, 10),      # YES low again
        tr(5, 80, 900_000, 10),     # comeback 2
        tr(6, 100, 990_000, 10),    # NO at 1c: NO low, no recovery
    ]
    cyc = comeback_cycles(win(trades), P)
    assert len([c for c in cyc["yes"] if c[1] is not None]) == 2
    assert cyc["no"] == [(T0 + 100 * S, None)]

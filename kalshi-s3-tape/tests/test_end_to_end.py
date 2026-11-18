"""Pipeline wiring: fetch (against a fake Kalshi server) -> run -> report. Synthetic data only."""

import json
import urllib.parse

from s3tape import fetch, kalshi_public, run

UNTIL = "2026-11-18T12:00:00Z"


def _m(ticker, open_iso, close_iso, result, volume):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "open_time": open_iso,
            "close_time": close_iso, "status": "finalized", "result": result, "volume_fp": volume,
            "settlement_ts": close_iso.replace(":00Z", ":05Z")}


MARKETS = {
    "KXBTC15M": [_m("KXBTC15M-26NOV180515-15", "2026-11-18T11:00:00Z", "2026-11-18T11:15:00Z", "yes", "800.00"),
                 _m("KXBTC15M-26NOV180530-30", "2026-11-18T11:15:00Z", "2026-11-18T11:30:00Z", "no", "0.00")],
    "KXETH15M": [_m("KXETH15M-26NOV180515-15", "2026-11-18T11:00:00Z", "2026-11-18T11:15:00Z", "no", "200.00")],
}


def _t(i, iso, yes, size, taker):
    return {"trade_id": f"tid{i}", "ticker": "", "count_fp": size, "yes_price_dollars": yes,
            "no_price_dollars": f"{1 - float(yes):.4f}", "taker_book_side": taker,
            "taker_outcome_side": "yes" if taker == "bid" else "no", "created_time": iso,
            "is_block_trade": False}


TRADES = {  # newest first, like the API
    "KXBTC15M-26NOV180515-15": [
        _t(4, "2026-11-18T11:14:00.000Z", "0.9900", "200.00", "bid"),
        _t(3, "2026-11-18T11:09:00.000Z", "0.8700", "200.00", "bid"),
        _t(2, "2026-11-18T11:05:00.000Z", "0.0150", "200.00", "ask"),
        _t(1, "2026-11-18T11:00:05.000Z", "0.5000", "200.00", "bid"),
    ],
    "KXETH15M-26NOV180515-15": [_t(9, "2026-11-18T11:07:00.000Z", "0.4000", "200.00", "ask")],
}


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(req, timeout):
    assert req.get_method() == "GET"
    u = urllib.parse.urlsplit(req.full_url)
    q = dict(urllib.parse.parse_qsl(u.query))
    path = u.path.split("/trade-api/v2", 1)[1]
    if path == "/historical/cutoff":
        return _Resp({"market_settled_ts": "2026-09-01T00:00:00Z", "trades_created_ts": "2026-09-01T00:00:00Z",
                      "orders_updated_ts": "2026-09-01T00:00:00Z"})
    if path.startswith("/series/fee_changes"):
        return _Resp({"series_fee_change_arr": []})
    if path.startswith("/series/"):
        return _Resp({"series": {"ticker": path.split("/")[-1], "fee_type": "quadratic", "fee_multiplier": 1}})
    if path == "/markets":
        return _Resp({"markets": MARKETS[q["series_ticker"]], "cursor": ""})
    if path == "/markets/trades":
        return _Resp({"trades": TRADES.get(q["ticker"], []), "cursor": ""})
    raise AssertionError(f"unexpected path {path}")


def test_fetch_then_run_writes_report(tmp_path, monkeypatch, capsys):
    real = kalshi_public.PublicClient
    monkeypatch.setattr(fetch, "PublicClient",
                        lambda: real(opener=fake_opener, sleep=lambda s: None, min_interval_s=0))
    assert fetch.main(["--hours", "24", "--until", UNTIL, "--out", str(tmp_path / "pulls")]) == 0
    pulls = list((tmp_path / "pulls").glob("pull_*"))
    assert len(pulls) == 1
    man = json.loads((pulls[0] / "manifest.json").read_text())
    assert man["markets"] == 3 and man["trades"] == 5
    assert man["markets_incomplete"] == 0          # trade sizes add up to each market's volume

    assert run.main(["--latest", str(tmp_path / "pulls"), "--out", str(tmp_path / "results")]) == 0
    out = tmp_path / "results" / pulls[0].name
    md = (out / "summary.md").read_text()
    assert "SIMULATED, paper only - no real orders" in md
    summary = json.loads((out / "summary.json").read_text())
    s3 = summary["runs"]["rebuy_through_fee0"]
    # BTC 11:00 window (result YES): YES bought 200 at 1.6c (through 1.5c) and sold at 86c (through
    # 87c) = +$168.80; the YES re-buy never fills. The 99c trade at 11:14 is NO at 1c, through our NO
    # buy at 1.6c: NO bought 200, unsold, settles at $0 = -$3.20. ETH: nothing fills. -> +$165.60
    assert s3["pnl"] == 168_800_000 - 3_200_000
    assert s3["rebuys_posted"] == 1 and s3["rebuy_fills"] == 0
    assert (out / "fills.csv").read_text().count("\n") == 1 + 3 * 8   # 3 fills in each of the 8 runs
    assert "Caught (at least one 86c sale): through-only 1 of 1, first-in-line 1 of 1." in md

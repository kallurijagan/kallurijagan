"""Download the last N hours of Kalshi PUBLIC data for the S3 series (read-only, no API key).

    python -m s3tape.fetch --hours 24 --out <folder>

Writes a NEW folder <out>/pull_<UTC stamp>/ (never overwrites anything):
    manifest.json      what was asked, when, request counts, completeness per market
    cutoff.json        GET /historical/cutoff (raw)
    series.json        GET /series/{ticker} and GET /series/fee_changes (raw)
    markets.jsonl.gz   one raw market object per line (as returned by the API)
    trades.jsonl.gz    {"ticker", "source", "api_index", "raw": <trade object as returned>}

Completeness check: for every market, the sum of trade sizes must equal the market's
volume_fp. Mismatches are reported, never patched.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

from .kalshi_public import PublicClient
from .rules import load_rules
from .units import US_PER_S, fp_to_hundredths, fmt_central, iso_to_us, us_to_iso, us_to_utc


def _ts(obj: dict, key: str) -> int | None:
    try:
        return iso_to_us(obj[key])
    except (KeyError, TypeError, ValueError):
        return None


def fetch(client: PublicClient, series: list[str], since_us: int, until_us: int, log=print) -> dict:
    since_s, until_s = since_us // US_PER_S, until_us // US_PER_S
    cutoff = client.get("/historical/cutoff")
    settled_cut = _ts(cutoff, "market_settled_ts")
    trades_cut = _ts(cutoff, "trades_created_ts")

    series_raw: dict = {}
    markets: dict[str, dict] = {}
    market_source: dict[str, str] = {}
    for tk in series:
        series_raw[tk] = {
            "series": client.get(f"/series/{tk}"),
            "fee_changes": client.get("/series/fee_changes", {"series_ticker": tk, "show_historical": True}),
        }
        live = client.paginate(
            "/markets",
            {"series_ticker": tk, "min_close_ts": since_s, "max_close_ts": until_s, "limit": 1000},
            "markets",
        )
        for m in live:
            markets[m["ticker"]] = m
            market_source[m["ticker"]] = "live"
        if settled_cut is None or settled_cut > since_us:
            log(f"{tk}: historical cutoff {cutoff.get('market_settled_ts')} is inside the period; "
                "also reading GET /historical/markets")
            for m in client.paginate("/historical/markets", {"series_ticker": tk, "limit": 1000},
                                     "markets", max_pages=500):
                c = _ts(m, "close_time")
                if c is not None and since_us < c <= until_us and m["ticker"] not in markets:
                    markets[m["ticker"]] = m
                    market_source[m["ticker"]] = "historical"

    chosen = sorted(
        (m for m in markets.values() if (c := _ts(m, "close_time")) is not None and since_us < c <= until_us),
        key=lambda m: (_ts(m, "close_time"), m["ticker"]),
    )
    trade_lines: list[dict] = []
    completeness: list[dict] = []
    for i, m in enumerate(chosen, 1):
        tk = m["ticker"]
        rows = [("live", t) for t in client.paginate("/markets/trades", {"ticker": tk, "limit": 1000}, "trades")]
        opened = _ts(m, "open_time")
        if trades_cut is None or (opened is not None and trades_cut > opened):
            seen = {t["trade_id"] for _, t in rows}
            hist = client.paginate("/historical/trades", {"ticker": tk, "limit": 1000}, "trades")
            rows += [("historical", t) for t in hist if t["trade_id"] not in seen]
        total = sum(fp_to_hundredths(t["count_fp"]) for _, t in rows)
        try:
            volume = fp_to_hundredths(m.get("volume_fp"))
        except (TypeError, ValueError):
            volume = None
        completeness.append({
            "ticker": tk, "status": m.get("status"), "result": m.get("result"),
            "trades": len(rows), "trade_qty": total, "volume_qty": volume,
            "complete": volume is not None and total == volume,
        })
        for idx, (src, t) in enumerate(rows):
            trade_lines.append({"ticker": tk, "source": src, "api_index": idx, "raw": t})
        if i % 20 == 0 or i == len(chosen):
            log(f"  {i}/{len(chosen)} markets, {len(trade_lines)} trades, {client.requests} requests")
    return {
        "cutoff": cutoff, "series": series_raw, "markets": chosen,
        "market_source": market_source, "trades": trade_lines, "completeness": completeness,
    }


def write_pull(out_root: Path, got: dict, meta: dict) -> Path:
    stamp = us_to_utc(meta["pulled_at_us"]).strftime("%Y%m%dT%H%M%SZ")
    folder = Path(out_root) / f"pull_{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "cutoff.json").write_text(json.dumps(got["cutoff"], indent=1), encoding="utf-8")
    (folder / "series.json").write_text(json.dumps(got["series"], indent=1), encoding="utf-8")
    with gzip.open(folder / "markets.jsonl.gz", "wt", encoding="utf-8") as f:
        for m in got["markets"]:
            f.write(json.dumps({"source": got["market_source"].get(m["ticker"], "live"), "raw": m}) + "\n")
    with gzip.open(folder / "trades.jsonl.gz", "wt", encoding="utf-8") as f:
        for line in got["trades"]:
            f.write(json.dumps(line) + "\n")
    incomplete = [c for c in got["completeness"] if not c["complete"]]
    manifest = {
        **meta,
        "markets": len(got["markets"]),
        "trades": len(got["trades"]),
        "markets_incomplete": len(incomplete),
        "completeness": got["completeness"],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return folder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24.0, help="look-back window (default 24)")
    ap.add_argument("--until", help="end of the period, ISO-8601 UTC (default: now)")
    ap.add_argument("--out", required=True, help="folder that will receive pull_<stamp>/")
    args = ap.parse_args(argv)

    rules, digest = load_rules()          # refuses to run if S3's frozen rules changed
    series = [s["ticker"] for s in rules["series"]]
    pulled_at = time.time_ns() // 1000
    until_us = iso_to_us(args.until) if args.until else pulled_at
    since_us = until_us - int(args.hours * 3600 * US_PER_S)
    print(f"PUBLIC DATA ONLY - read-only GET requests, no API key, no orders.")
    print(f"Period: markets closing after {fmt_central(since_us)} up to {fmt_central(until_us)}")
    client = PublicClient()
    got = fetch(client, series, since_us, until_us)
    meta = {
        "pulled_at_us": pulled_at, "pulled_at": us_to_iso(pulled_at),
        "since_us": since_us, "until_us": until_us, "hours": args.hours,
        "since": us_to_iso(since_us), "until": us_to_iso(until_us),
        "series": series, "rules_sha256": digest, "base_url": client.base_url,
        "requests": client.requests,
    }
    folder = write_pull(Path(args.out), got, meta)
    bad = [c for c in got["completeness"] if not c["complete"]]
    print(f"Saved {len(got['markets'])} markets and {len(got['trades'])} trades to {folder}")
    print(f"Completeness (sum of trade sizes == market volume): "
          f"{len(got['completeness']) - len(bad)} of {len(got['completeness'])} markets match")
    for c in bad[:20]:
        print(f"  MISMATCH {c['ticker']}: trades {c['trade_qty']} vs volume {c['volume_qty']} "
              f"(hundredths; status {c['status']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

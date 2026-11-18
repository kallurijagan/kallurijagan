"""Load a pull folder written by s3tape.fetch into engine Windows (exact integer units)."""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from pathlib import Path

from .engine import ONE, Trade, Window
from .units import dollars_to_micro, fp_to_hundredths, iso_to_us

_TAKER_BOOK = {"yes": "bid", "no": "ask"}   # buy-YES/sell-NO -> bid; buy-NO/sell-YES -> ask


def parse_trade(raw: dict) -> tuple[Trade, list[str]]:
    problems: list[str] = []
    if raw.get("yes_price_dollars") is not None:
        yes_px = dollars_to_micro(raw["yes_price_dollars"])
    else:
        yes_px = int(raw["yes_price"]) * 10_000
    if raw.get("no_price_dollars") is not None:
        no_px = dollars_to_micro(raw["no_price_dollars"])
        if yes_px + no_px != ONE:
            problems.append(f"trade {raw.get('trade_id')}: yes {yes_px} + no {no_px} != $1")
    if raw.get("count_fp") is not None:
        qty = fp_to_hundredths(raw["count_fp"])
    else:
        qty = int(raw["count"]) * 100
    side = raw.get("taker_book_side")
    if side not in ("bid", "ask"):
        side = _TAKER_BOOK.get(raw.get("taker_outcome_side") or raw.get("taker_side") or "")
    return Trade(
        trade_id=str(raw["trade_id"]),
        t_us=iso_to_us(raw["created_time"]),
        yes_px=yes_px,
        qty=qty,
        taker_book_side=side,
        is_block=bool(raw.get("is_block_trade", False)),
    ), problems


def _series_of(market: dict) -> str:
    ev = market.get("event_ticker") or market["ticker"]
    return ev.split("-")[0]


def load_pull(folder: Path, rules: dict) -> tuple[list[Window], dict]:
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    coin_of = {s["ticker"]: s["coin"] for s in rules["series"]}
    with gzip.open(folder / "markets.jsonl.gz", "rt", encoding="utf-8") as f:
        markets = [json.loads(line)["raw"] for line in f if line.strip()]
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    with gzip.open(folder / "trades.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                by_ticker[d["ticker"]].append(d)

    windows: list[Window] = []
    listed: list[dict] = []
    problems: list[str] = []
    complete = {c["ticker"]: c for c in manifest.get("completeness", [])}
    for m in markets:
        tk = m["ticker"]
        coin = coin_of.get(_series_of(m))
        open_us, close_us = iso_to_us(m["open_time"]), iso_to_us(m["close_time"])
        row = {"ticker": tk, "coin": coin, "open_us": open_us, "close_us": close_us,
               "status": m.get("status"), "result": m.get("result") or "", "use": False, "why": ""}
        listed.append(row)
        if coin is None:
            row["why"] = "not an S3 series"
            continue
        minute = (open_us // 60_000_000) % 60
        if open_us % 60_000_000 or minute not in rules["open_minutes"]:
            row["why"] = f"opens at :{minute:02d} (S3 trades :00 and :15 windows only)"
            continue
        if row["result"] not in ("yes", "no"):
            row["why"] = f"EXCLUDED: no official result yet (status {m.get('status')})"
            continue
        if tk in complete and not complete[tk]["complete"]:
            row["flag_incomplete"] = True
        settle_us, assumed = close_us, True
        if m.get("settlement_ts"):
            settle_us, assumed = iso_to_us(m["settlement_ts"]), False
        lines = by_ticker.get(tk, [])
        parsed = []
        for d in lines:
            tr, probs = parse_trade(d["raw"])
            problems.extend(probs)
            parsed.append((tr, d["source"], d["api_index"]))
        # API pages list newest first; for equal timestamps keep the exchange's sequence.
        newest_first = len(parsed) > 1 and parsed[0][0].t_us >= parsed[-1][0].t_us
        parsed.sort(key=lambda x: (x[0].t_us, x[1] != "historical", -x[2] if newest_first else x[2]))
        seen: set[str] = set()
        trades = []
        for tr, _, _ in parsed:
            if tr.trade_id in seen:
                problems.append(f"{tk}: duplicate trade {tr.trade_id} dropped")
                continue
            seen.add(tr.trade_id)
            trades.append(tr)
        windows.append(Window(tk, coin, open_us, close_us, row["result"], settle_us, trades, assumed))
        row["use"] = True
    info = {"manifest": manifest, "listed": listed, "problems": problems}
    return windows, info

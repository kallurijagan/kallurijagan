"""Run the S3 trade-tape backtest on a pull folder and write the report.

    python -m s3tape.run --data <pull folder> --out <results folder>
    python -m s3tape.run --latest <folder holding pull_* folders> --out <results folder>

Runs S3 and "S3 without re-buys", each with both fill bounds (through-only = fewest fills,
first-in-line = most fills) and both fee cases (0 and the 0.0175 maker sensitivity), on ONE
shared $1,000 paper account. SIMULATED fills only - nothing here is a real execution.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from .data import load_pull
from .engine import FILL_MODELS, Engine, Params, RunConfig, comeback_cycles
from .rules import load_rules, maker_rates
from .units import (central_date, fmt_cents, fmt_central, fmt_contracts, fmt_money,
                    fmt_signed_money)

VARIANTS = (("S3", True), ("S3 without re-buys", False))
MODEL_NAME = {"through": "through-only (fewest fills)", "first_in_line": "first-in-line (most fills)"}


def run_all(windows, params: Params, rates: dict) -> dict[RunConfig, Engine]:
    out = {}
    for label, rebuy in VARIANTS:
        for fm in FILL_MODELS:
            for rate in (rates["base"], rates["fee"]):
                cfg = RunConfig(label, rebuy, fm, rate)
                out[cfg] = Engine(windows, params, cfg).run()
    return out


def aggregate(eng: Engine) -> dict:
    a: dict = defaultdict(int)
    by_coin: dict = defaultdict(int)
    by_day: dict = defaultdict(int)
    best = worst = None
    flag_counts: dict = defaultdict(int)
    for w, win in enumerate(eng.windows):
        pnl = eng.window_pnl(w)
        by_coin[win.coin] += pnl
        by_day[str(central_date(win.open_us))] += pnl
        a["pnl"] += pnl
        a["windows"] += 1
        any_buy = False
        for side, st in eng.state[w].items():
            a["bought"] += st.bought
            a["sold"] += st.sold
            a["buy_fills"] += st.buy_fills
            a["sell_fills"] += st.sell_fills
            a["rebuys_posted"] += st.rebuys_posted
            a["rebuy_qty_posted"] += st.rebuy_qty_posted
            a["rebuy_fills"] += st.rebuy_fills
            a["rebuy_qty_filled"] += st.rebuy_qty_filled
            a["rebuy_round_trip_qty"] += st.rebuy_round_trip_qty
            a["fees"] += st.buy_fees + st.sell_fees
            a["rejected"] += st.rejected
            a["cash_limited"] += st.cash_limited
            a["cap_bound"] += st.cap_bound
            a["skipped_after_close"] += st.skipped_after_close
            if win.result == side:
                a["held_won"] += st.held_at_settle
            else:
                a["held_lost"] += st.held_at_settle
            any_buy = any_buy or st.bought > 0
            if st.sold > 0:
                a["sides_with_round_trip"] += 1
        a["windows_with_fill"] += int(any_buy)
        for fl in eng.flags[w]:
            flag_counts[fl] += 1
        if best is None or pnl > best[0]:
            best = (pnl, win.ticker)
        if worst is None or pnl < worst[0]:
            worst = (pnl, win.ticker)
    dd, cum = eng.max_drawdown()
    assert cum == a["pnl"]
    a["max_drawdown"] = dd
    a["by_coin"] = dict(by_coin)
    a["by_day"] = dict(sorted(by_day.items()))
    a["best"], a["worst"] = best, worst
    a["flags"] = dict(flag_counts)
    a["min_free"], a["min_cash"], a["min_free_net"] = eng.min_free, eng.min_cash, eng.min_free_net
    a["final_cash"] = eng.cash
    a["fee_overruns"] = eng.fee_overruns
    a["block_trades"] = eng.block_trades
    a["after_close_trades"] = eng.after_close_trades
    return dict(a)


def _t(x):
    return "" if x is None else fmt_central(x)


def write_outputs(out: Path, info: dict, runs: dict, windows, params: Params, meta: dict, rates: dict) -> str:
    out.mkdir(parents=True, exist_ok=True)
    aggs = {cfg: aggregate(eng) for cfg, eng in runs.items()}

    with open(out / "windows.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["variant", "fill_model", "maker_rate", "ticker", "coin", "open_central", "close_central",
                     "result", "side", "bought", "initial_filled", "sold", "rebuys_posted", "rebuy_qty_filled",
                     "held_at_settle", "payout", "buy_cost", "sell_proceeds", "fees", "side_pnl", "window_pnl",
                     "flags"])
        for cfg, eng in runs.items():
            for w, win in enumerate(eng.windows):
                for side, st in eng.state[w].items():
                    wr.writerow([cfg.label, cfg.fill_model, str(cfg.maker_rate), win.ticker, win.coin,
                                 fmt_central(win.open_us), fmt_central(win.close_us), win.result, side,
                                 fmt_contracts(st.bought), fmt_contracts(st.initial_filled), fmt_contracts(st.sold),
                                 st.rebuys_posted, fmt_contracts(st.rebuy_qty_filled),
                                 fmt_contracts(st.held_at_settle), fmt_money(st.payout), fmt_money(st.buy_cost),
                                 fmt_money(st.sell_proceeds), fmt_money(st.buy_fees + st.sell_fees),
                                 fmt_money(st.pnl()), fmt_money(eng.window_pnl(w)),
                                 ";".join(sorted(eng.flags[w]))])
    with open(out / "fills.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["variant", "fill_model", "maker_rate", "time_central", "ticker", "coin", "order", "kind",
                     "side", "action", "price", "qty", "fee", "trade_id", "trade_yes_price", "trade_qty", "how",
                     "order_posted_central"])
        for cfg, eng in runs.items():
            for r in eng.fills:
                wr.writerow([cfg.label, cfg.fill_model, str(cfg.maker_rate), fmt_central(r["t_us"]), r["ticker"],
                             r["coin"], r["oid"], r["kind"], r["side"], r["action"], fmt_cents(r["price"]),
                             fmt_contracts(r["qty"]), fmt_money(r["fee"]), r["trade_id"],
                             fmt_cents(r["trade_yes_px"]), fmt_contracts(r["trade_qty"]), r["how"],
                             fmt_central(r["order_posted_us"])])
    with open(out / "decisions.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["variant", "fill_model", "maker_rate", "time_central", "ticker", "coin", "side", "action",
                     "kind", "price", "qty_wanted", "qty_posted", "order", "outcome", "last_trade_yes_price",
                     "last_trade_taker_book_side", "last_trade_age_s"])
        for cfg, eng in runs.items():
            for r in eng.decisions:
                age = r["last_trade_age_us"]
                wr.writerow([cfg.label, cfg.fill_model, str(cfg.maker_rate), fmt_central(r["t_us"]), r["ticker"],
                             r["coin"], r["side"], r["action"], r["kind"], fmt_cents(r["price"]),
                             fmt_contracts(r["qty_wanted"]), fmt_contracts(r["qty_posted"]), r["oid"] or "",
                             r["outcome"], "" if r["last_trade_yes_px"] is None else fmt_cents(r["last_trade_yes_px"]),
                             r["last_trade_taker"] or "", "" if age is None else f"{age / 1e6:.3f}"])

    # Comeback tracker (data only, independent of any run), then whether each run caught it.
    base = {fm: runs[RunConfig("S3", True, fm, rates["base"])] for fm in FILL_MODELS}
    base_nr = {fm: runs[RunConfig("S3 without re-buys", False, fm, rates["base"])] for fm in FILL_MODELS}
    idx = {fm: {win.ticker: w for w, win in enumerate(e.windows)} for fm, e in base.items()}
    comebacks = []
    dips = 0
    for win in windows:
        cyc = comeback_cycles(win, params)
        for side, cycles in cyc.items():
            if cycles:
                dips += 1
            done = [c for c in cycles if c[1] is not None]
            if not done:
                continue
            row = {"ticker": win.ticker, "coin": win.coin, "side": side, "result": win.result,
                   "open_us": win.open_us, "first_low_us": done[0][0], "first_high_us": done[0][1],
                   "cycles": len(done)}
            for fm in FILL_MODELS:
                for tag, rr in (("s3", base), ("norebuy", base_nr)):
                    eng = rr[fm]
                    st = eng.state[idx[fm][win.ticker]][side]
                    row[f"{tag}_{fm}_bought"] = st.bought
                    row[f"{tag}_{fm}_sold"] = st.sold
                    row[f"{tag}_{fm}_pnl"] = st.pnl()
            comebacks.append(row)
    with open(out / "comebacks.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        cols = ["open_central", "ticker", "coin", "side", "result", "cycles", "first_low_central", "first_high_central"]
        extra = [f"{tag}_{fm}_{k}" for fm in FILL_MODELS for tag in ("s3", "norebuy") for k in ("bought", "sold", "pnl")]
        wr.writerow(cols + extra)
        for r in comebacks:
            wr.writerow([fmt_central(r["open_us"]), r["ticker"], r["coin"], r["side"], r["result"], r["cycles"],
                         fmt_central(r["first_low_us"]), fmt_central(r["first_high_us"])] +
                        [fmt_money(r[k]) if k.endswith("pnl") else fmt_contracts(r[k]) for k in extra])

    md = render_summary(info, runs, aggs, comebacks, dips, windows, params, meta, rates)
    (out / "summary.md").write_text(md, encoding="utf-8")
    js = {
        "meta": meta,
        "runs": {cfg.key: {"label": cfg.label, "fill_model": cfg.fill_model, "maker_rate": str(cfg.maker_rate),
                           **{k: v for k, v in agg.items()}} for cfg, agg in aggs.items()},
    }
    (out / "summary.json").write_text(json.dumps(js, indent=1, default=str), encoding="utf-8")
    return md



def render_summary(info, runs, aggs, comebacks, dips, windows, params, meta, rates) -> str:
    rb, rf = rates["base"], rates["fee"]

    def A(label, fm, rate=None):
        return aggs[RunConfig(label, label == "S3", fm, rb if rate is None else rate)]

    L = []
    L.append("# S3 trade-tape backtest - recent Kalshi data (SIMULATED, paper only - no real orders)\n")
    man = info["manifest"]
    listed = info["listed"]
    used = [r for r in listed if r["use"]]
    s3_slots = [r for r in listed if r["coin"] and "opens at" not in r["why"]]
    excluded = [r for r in s3_slots if not r["use"]]
    L.append(f"- Data: Kalshi PUBLIC trade tape (GET /markets, /markets/trades; no API key), pulled "
             f"{fmt_central(man['pulled_at_us'])}. Markets closing after {fmt_central(man['since_us'])} up to "
             f"{fmt_central(man['until_us'])}.")
    by_coin_listed = defaultdict(int)
    for r in listed:
        if r["coin"]:
            by_coin_listed[r["coin"]] += 1
    L.append(f"- Kalshi listed {sum(by_coin_listed.values())} markets in the period "
             f"({', '.join(f'{c} {n}' for c, n in sorted(by_coin_listed.items()))}); S3 trades the :00 and :15 "
             f"windows: {len(s3_slots)}; simulated: {len(used)}; excluded: {len(excluded)} "
             f"(no official result yet).")
    comp = man.get("completeness", [])
    ok = sum(1 for c in comp if c["complete"])
    L.append(f"- Completeness: {ok} of {len(comp)} markets' trades add up exactly to Kalshi's reported volume.")
    L.append(f"- Frozen rules: rules_s3.json sha256 `{meta['rules_sha256']}`.")
    L.append("- Engine: independent trade-tape model written in the cloud session. It is NOT "
             "backtests/sell85/engine.py and has NOT been checked against the s1_price_grid numbers.\n")

    L.append("## Result: shared $1,000 paper account, $0 maker fees\n")
    L.append("| | through-only (fewest fills) | first-in-line (most fills) |")
    L.append("|---|---:|---:|")
    for label, _ in VARIANTS:
        L.append(f"| {label} | {fmt_signed_money(A(label, 'through')['pnl'])} | "
                 f"{fmt_signed_money(A(label, 'first_in_line')['pnl'])} |")
    L.append(f"| Effect of re-buys | "
             f"{fmt_signed_money(A('S3', 'through')['pnl'] - A('S3 without re-buys', 'through')['pnl'])} | "
             f"{fmt_signed_money(A('S3', 'first_in_line')['pnl'] - A('S3 without re-buys', 'first_in_line')['pnl'])} |")
    L.append(f"| S3 with the 0.0175 maker fee | {fmt_signed_money(A('S3', 'through', rf)['pnl'])} | "
             f"{fmt_signed_money(A('S3', 'first_in_line', rf)['pnl'])} |")
    L.append(f"| S3 without re-buys, 0.0175 maker fee | "
             f"{fmt_signed_money(A('S3 without re-buys', 'through', rf)['pnl'])} | "
             f"{fmt_signed_money(A('S3 without re-buys', 'first_in_line', rf)['pnl'])} |\n")

    L.append("## S3 by coin and by Central day ($0 fees)\n")
    L.append("| | through-only | first-in-line |")
    L.append("|---|---:|---:|")
    for coin in params.coin_order:
        L.append(f"| {coin} | {fmt_signed_money(A('S3', 'through')['by_coin'].get(coin, 0))} | "
                 f"{fmt_signed_money(A('S3', 'first_in_line')['by_coin'].get(coin, 0))} |")
    L.append(f"| Shared | {fmt_signed_money(A('S3', 'through')['pnl'])} | "
             f"{fmt_signed_money(A('S3', 'first_in_line')['pnl'])} |")
    days = sorted(set(A("S3", "through")["by_day"]) | set(A("S3", "first_in_line")["by_day"]))
    for d in days:
        L.append(f"| {d} (Central, by window open) | {fmt_signed_money(A('S3', 'through')['by_day'].get(d, 0))} | "
                 f"{fmt_signed_money(A('S3', 'first_in_line')['by_day'].get(d, 0))} |")
    L.append("")

    L.append("## What happened (S3 vs S3 without re-buys, $0 fees)\n")
    L.append("| | S3 through | S3 first-in-line | no re-buys through | no re-buys first-in-line |")
    L.append("|---|---:|---:|---:|---:|")
    cols = [A("S3", "through"), A("S3", "first_in_line"),
            A("S3 without re-buys", "through"), A("S3 without re-buys", "first_in_line")]

    def row(name, fn):
        L.append(f"| {name} | " + " | ".join(fn(c) for c in cols) + " |")

    row("Windows simulated", lambda c: str(c["windows"]))
    row("Windows with any buy fill", lambda c: str(c["windows_with_fill"]))
    row("Contracts bought at 1.6c", lambda c: fmt_contracts(c["bought"]))
    row("Contracts sold at 86c (round trips)", lambda c: fmt_contracts(c["sold"]))
    row("Re-buys posted / filled (orders)", lambda c: f"{c['rebuys_posted']} / {c['rebuy_fills']}")
    row("Re-bought contracts filled / sold again", lambda c: f"{fmt_contracts(c['rebuy_qty_filled'])} / "
                                                            f"{fmt_contracts(c['rebuy_round_trip_qty'])}")
    row("Held to settlement: won / lost (contracts)", lambda c: f"{fmt_contracts(c['held_won'])} / "
                                                               f"{fmt_contracts(c['held_lost'])}")
    row("Best window", lambda c: fmt_signed_money(c["best"][0]) if c["best"] else "")
    row("Worst window", lambda c: fmt_signed_money(c["worst"][0]) if c["worst"] else "")
    row("Max drawdown", lambda c: fmt_money(c["max_drawdown"]))
    row("Lowest free cash (sides kept separate)", lambda c: fmt_money(c["min_free"][0]))
    row("Lowest free cash (netted, approximate)", lambda c: fmt_money(c["min_free_net"][0]))
    row("Final cash", lambda c: fmt_money(c["final_cash"]))
    row("Cash-limited decisions", lambda c: str(c["cash_limited"]))
    row("Crossing rejections (tape proxy)", lambda c: str(c["rejected"]))
    row("Windows: both sides held at once", lambda c: str(c["flags"].get("both-sides-held", 0)))
    row("Windows: resting sell unbacked if netted", lambda c: str(c["flags"].get("unbacked-sell-if-netted", 0)))
    row("Block trades ignored", lambda c: str(c["block_trades"]))
    L.append("")

    L.append("## Comeback tracker (the windows S3 lives on)\n")
    L.append(f"A side traded at or below {fmt_cents(params.buy_price)} after open + 12 s in {dips} window-sides; "
             f"in {len(comebacks)} of them it later traded at or above {fmt_cents(params.sell_price)} before the close.\n")
    if comebacks:
        L.append("| Window open (Central) | Coin | Side | Comebacks | Result | S3 through: sold, P&L | "
                 "S3 first-in-line: sold, P&L |")
        L.append("|---|---|---|---:|---|---|---|")
        for r in comebacks:
            L.append(f"| {fmt_central(r['open_us'], seconds=False)} | {r['coin']} | {r['side'].upper()} | {r['cycles']} | "
                     f"{r['result'].upper()} | {fmt_contracts(r['s3_through_sold'])}, "
                     f"{fmt_signed_money(r['s3_through_pnl'])} | {fmt_contracts(r['s3_first_in_line_sold'])}, "
                     f"{fmt_signed_money(r['s3_first_in_line_pnl'])} |")
        caught_t = sum(1 for r in comebacks if r["s3_through_sold"] > 0)
        caught_f = sum(1 for r in comebacks if r["s3_first_in_line_sold"] > 0)
        L.append(f"\nCaught (at least one 86c sale): through-only {caught_t} of {len(comebacks)}, "
                 f"first-in-line {caught_f} of {len(comebacks)}.\n")

    L.append("## Assumptions and limits (read before trusting any number)\n")
    L.append("- Every fill is SIMULATED from the public trade tape; none is a real execution.")
    L.append("- The real queue at 1.6c / 86c is not in the public data. through-only assumes everyone "
             "ahead of us stays until the price trades through; first-in-line assumes we are first. "
             "Reality is somewhere between, and our own orders could change what others do.")
    L.append("- The maker (post-only) check uses the last trade as a stand-in for the book.")
    L.append("- Settlement cash is credited at Kalshi's settlement_ts (the close time where it is missing).")
    L.append("- YES and NO are kept as separate positions. A real Kalshi account nets them; P&L is identical "
             "(asserted for every window), only cash timing differs (both cash paths shown above).")
    L.append("- 24 hours is a tiny sample for a strategy that loses $3.20 most windows and wins about $169 "
             "rarely; one comeback more or less changes the sign. It says nothing reliable about the next 30 days.")
    if info["problems"]:
        L.append(f"- Data problems found: {len(info['problems'])} (first: {info['problems'][0]})")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--data", help="a pull_<stamp> folder written by s3tape.fetch")
    g.add_argument("--latest", help="folder holding pull_* folders; uses the newest")
    ap.add_argument("--out", required=True, help="results folder (a subfolder per pull is created)")
    args = ap.parse_args(argv)

    rules, digest = load_rules()          # refuses to run if S3's frozen rules changed
    params = Params.from_rules(rules)
    rates = maker_rates(rules)
    if args.data:
        folder = Path(args.data)
    else:
        pulls = sorted(Path(args.latest).glob("pull_*"))
        if not pulls:
            print(f"No pull_* folders in {args.latest}; run s3tape.fetch first.")
            return 1
        folder = pulls[-1]
    windows, info = load_pull(folder, rules)
    if not windows:
        print("No settled S3 windows in this pull; nothing to simulate.")
        return 1
    runs = run_all(windows, params, rates)
    meta = {"pull": folder.name, "rules_sha256": digest, "windows": len(windows)}
    md = write_outputs(Path(args.out) / folder.name, info, runs, windows, params, meta, rates)
    print(md)
    print(f"Files: {Path(args.out) / folder.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

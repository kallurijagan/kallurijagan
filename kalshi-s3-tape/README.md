# S3 trade-tape backtest on recent Kalshi data (paper only)

A quick, **simulated** backtest of strategy **S3 "1.6c / 86c with re-buys"** on the last N
hours (default 24) of Kalshi's **public** trade tape for KXBTC15M and KXETH15M. It also runs
"S3 without re-buys" so the effect of re-buys is visible. Nothing here is a real execution.

## Safety: it cannot trade

- `s3tape/kalshi_public.py` sends **GET requests only**, to a fixed allow-list of public
  market-data endpoints (`/markets`, `/markets/trades`, `/series/{ticker}`,
  `/series/fee_changes`, `/historical/cutoff`, `/historical/markets`, `/historical/trades`)
  on `https://*.kalshi.com`. Any other path or host is refused.
- It never reads, needs or sends an API key. Kalshi's order endpoints all require a signed
  key; the endpoints above require none (official SDK `kalshi_python_sync` 3.36.0: these
  endpoints list no auth settings, `/portfolio/orders` lists the three key headers).
- A test fails if POST/PUT/DELETE, `/portfolio`, or the API-key variable names ever appear
  in the package.

## What it is, and is not

- **Independent engine.** Written in a cloud session that could not see
  `backtests/sell85/engine.py` or `backtests/results/s1_price_grid`. It is **not** the
  sell85 copy that the S3 plan asks for, and it has **not** been checked against the grid's
  +$15,635.39 / +$18,159.23. Treat it as a second opinion, not a replacement.
- **Frozen rules.** `rules_s3.json` holds S3 exactly as written; its SHA-256 is in
  `rules_s3.json.sha256`. Every command refuses to run if the file changed (even whitespace).

## Model (all integers: micro-dollars, hundredths of a contract, UTC microseconds)

- Windows opening at :00 and :15 only. At open + 12 s, post-only buys of 200 YES and 200 NO
  at 1.6c. 1 s after each buy fill, a sell of that quantity at 86c. With re-buys: 1 s after
  each sell fill, a buy of that quantity at 1.6c on the same side if that time is before the
  close (no limit on round trips; a side never holds + works more than 200). Unfilled orders
  are cancelled at the close; unsold contracts settle at the official result.
- One shared $1,000 account (BTC + ETH). Each working buy reserves price x size (+ fee in the
  fee case); never borrow; reduce size when short. Same instant: BTC before ETH, YES before NO.
- Unified YES book: buy YES @p = bid @p, sell YES @s = ask @s, buy NO @p = ask @(1-p),
  sell NO @s = bid @(1-s).
- **Fill bounds** (the queue at our price is not in public data):
  - `through` - fills only from trades strictly through our price (the level emptied, so
    everyone ahead of us is gone) - the fewest fills;
  - `first_in_line` - also fills from trades at our price, as if first in line - the most.
  Each trade fills at most its own size, once, best price for the taker first, then the
  earliest-posted order. Only trades strictly after an order is posted can fill it. Block
  trades (matched off the order book) never fill.
- **Maker check** (assumption): with no historical book, a decision is rejected and never
  re-posted only when the most recent trade shows the other side of the book at/through our
  price.
- **Fees:** $0 maker fees (base) and the 0.0175 maker-rate sensitivity:
  `ceil_to_cent(0.0175 x C x P x (1 - P))` per execution.
- **Netting:** Kalshi keeps one signed position per market. The simulation keeps YES and NO
  separate (assumption), asserts for every window that P&L is identical under net-position
  accounting, counts windows where both sides were held at once or a resting sell would have
  had no net position behind it, and reports an approximate netted cash path.
- Settlement cash arrives at the market's `settlement_ts` (close time if missing).

## Run it

On the PC (place this folder at `edge_kalshi\backtests\s3tape\`):

```
cd C:\Users\Neelesh\Downloads\edge_kalshi
backtests\s3tape\run_24h.cmd
```

That downloads the last 24 h into `kalshi_live\public_tape\pull_<UTC stamp>\` (a new folder
each time; nothing is overwritten) and writes the report to
`backtests\results\s3_tape_recent\pull_<stamp>\` (`summary.md`, `windows.csv`, `fills.csv`,
`decisions.csv`, `comebacks.csv`, `summary.json`). Step by step:

```
cd C:\Users\Neelesh\Downloads\edge_kalshi\backtests\s3tape
..\..\.venv-bt\Scripts\python.exe -m s3tape.fetch --hours 24 --out ..\..\kalshi_live\public_tape
..\..\.venv-bt\Scripts\python.exe -m s3tape.run --latest ..\..\kalshi_live\public_tape --out ..\results\s3_tape_recent
..\..\.venv-bt\Scripts\python.exe -m pytest -q tests
```

Anywhere else with Python 3.11+ (standard library only; pytest for the tests):

```
python -m s3tape.fetch --hours 24 --out data
python -m s3tape.run --latest data --out results
python -m pytest -q tests
```

## Data checks

- Completeness: for every market, the trade sizes must add up exactly to Kalshi's reported
  `volume_fp`; mismatches are listed, never patched.
- Windows without an official result yet are excluded and listed, never guessed.
- Prices must satisfy YES + NO = $1 on every trade; problems are reported.

## Sources

Checked against Kalshi's official OpenAPI-generated SDK `kalshi_python_sync` 3.36.0 (PyPI),
because docs.kalshi.com was not reachable from the cloud session. Re-check on docs.kalshi.com:
Get Trades (`GET /markets/trades`: `count_fp`, `yes_price_dollars`, `taker_book_side`,
`created_time`, `is_block_trade`), Get Markets (`GET /markets`: `min_close_ts`/`max_close_ts`,
`result`, `status`, `volume_fp`, `settlement_ts`), Get Series (`fee_type`, `fee_multiplier`),
Get Series Fee Changes, and Historical Data (`/getting_started/historical_data`, cited by the
SDK) for the `/historical/cutoff` split.

## Limits

24 hours is a tiny sample for a strategy that loses $3.20 most windows and wins about $169
rarely: one comeback more or less changes the sign. The real queue position, and our own
effect on other traders, cannot be known from public data.

# Kalshi BTC 15-minute Paper Trading Bot

**PAPER TRADING ONLY.** A local Windows application that runs a fixed two-sided strategy on
Kalshi's 15-minute Bitcoin UP/DOWN markets (series `KXBTC15M`). It uses **real Kalshi
production market data** and a **simulated $1,000 account**. Orders, fills, cash, positions and
settlements exist only in a local SQLite database. The program has no exchange credentials, no
order-signing code and no route to any order endpoint. There is no live-trading switch.

- Backend: Python 3.12+, FastAPI, SQLite, `Decimal` for all money and contract arithmetic
- Dashboard: React + TypeScript + Vite, Recharts charts, three selectable layouts
- Windows PowerShell scripts: `setup.ps1`, `start.ps1`, `stop.ps1`, `test.ps1`

---

## Quick start (Windows PowerShell)

Prerequisites: Windows 10/11, **Python 3.12+** (python.org, tick "Add python.exe to PATH"),
and **Node.js 20.19+ or 22.12+** (nodejs.org LTS).

```powershell
cd C:\path\to\kalshi-btc-paper-bot
powershell -ExecutionPolicy Bypass -File .\setup.ps1        # one time: venv, pinned deps, npm ci, build
powershell -ExecutionPolicy Bypass -File .\start.ps1        # starts engine + dashboard
```

Open **http://127.0.0.1:8000** (start.ps1 opens it for you), then press **Start**. Trading
begins at the next window that starts at :00, :15 or :30 (America/Chicago).

| Command | What it does |
|---|---|
| `.\start.ps1` | Live Kalshi data, paper money. Dashboard at http://127.0.0.1:8000 |
| `.\start.ps1 -Port 8001` | Same, on another port |
| `.\start.ps1 -LiveCheck` | Read-only connectivity check: prints a real ticker, its bids, derived asks, depth and timestamps, then exits |
| `.\start.ps1 -Preview` | **Synthetic sample data** (clearly labeled), separate `data\preview.sqlite3` |
| `.\start.ps1 -Preview -SeedPreview` | Same, after fast-forwarding 6 h of sample history so the analytics have content |
| `.\start.ps1 -Dev` | Also runs the Vite dev server with hot reload at http://127.0.0.1:5173 |
| `.\stop.ps1` | Graceful shutdown (or press Ctrl+C in the start window) |
| `.\test.ps1` | Backend test suite + dashboard type-check/build |

If PowerShell blocks scripts, run them as shown above with `-ExecutionPolicy Bypass`, or once per
session: `Set-ExecutionPolicy -Scope Process Bypass`.

**Keep it running.** The engine runs inside the backend process, not in the browser. Closing the
browser tab does not stop trading. Shutting down or sleeping the computer, or closing the
start.ps1 window, does stop it. While trading is enabled the bot asks Windows not to go to sleep,
but closing a laptop lid can still suspend it. **Don't click inside the start.ps1 window**: the bot
turns off the console's QuickEdit mode, but if the window title ever starts with "Select", press
Esc. After a restart it recovers open orders and positions and resumes settlement checks. It
never invents fills for time it was offline, and it waits for the next eligible window before
entering again. Offline gaps are recorded in the activity log.

## "Why isn't it buying?"

Every layout has a **"Why isn't it buying?"** panel at the top. It gives one plain answer and a
checklist behind it. The common answers, in order of likelihood:

| The panel says | What it means / what to do |
|---|---|
| *Orders are live, waiting for a seller at 37c or less (UP best ask 51c, ...)* | **Normal.** The orders exist and are watching the book. A fill needs someone selling at 37¢ or less, which takes a big enough BTC move that one side becomes cheap. Most windows fill one side or none. A bid or last trade at 37¢ does not count. |
| *Trading has not been started* / *Trading is paused* | Press **Start** / **Resume** (there's a button right in the panel). |
| *Waiting for the next window — started after this one began* | Start, Resume or a restart after a window began waits for the next :00/:15/:30 window. The :45 window is always skipped. |
| *Waiting: Entering this window…* | Up to 30 s after the window opens, the bot waits for Kalshi to publish the market's target price and mark it active. |
| *Cannot get live Kalshi data* | Your PC can't reach Kalshi. Run `.\start.ps1 -LiveCheck`. It prints the real error and a hint, for example for antivirus HTTPS scanning, DNS/VPN problems, or a 403 from Kalshi/your network. |
| *This window was skipped: …* | The stated reason applies to that window only; the next window is tried again. |
| *Engine is not running its loop* | The backend is frozen or stopped. Press Esc in the start.ps1 window, or restart it. |

Each order card also shows its own reason, for example *Waiting for price: best UP ask is 51c x 20,
needs <= 37c (14c away)* or *No offers: nobody is selling UP right now*.

---

## Choosing a layout

Click **"Choose layout"** in the top-right of the header. A dialog shows a preview of each
layout's structure. Click one to switch instantly. Your choice is remembered in this browser
(localStorage). You can also link directly with `http://127.0.0.1:8000/?layout=terminal`
(`simple`, `terminal` or `analytics`). All three layouts read the same backend and the same
account. Switching never starts another engine or creates another account.

1. **Simple Dashboard**: large equity and P&L, a big countdown, the current market and target,
   the hourly schedule (the :45 window is shown as skipped), large UP and DOWN order cards with
   intended and filled quantities, and a big Start/Pause/Resume button.
2. **Trading Terminal** (dark and compact): full order-book depth ladders (reported bids plus
   derived asks, with levels at or below your limit highlighted), bid/ask/last-trade charts for
   each side, working orders with the reason each is or isn't filling, recent fills (each with an
   **Evidence** button), the price-check panel, the event log, timestamps (UTC and Chicago),
   request latency, data age and connection health.
3. **Analytics Dashboard**: equity curve, net P&L per complete window, frequency and P&L of
   both-sides / UP-only / DOWN-only / partial / no-fill outcomes, fill rate and average price per
   side, fee totals, price improvement, skip reasons, and searchable history of windows, orders,
   fills and settlements with CSV export.

The header in every layout has: the persistent **PAPER TRADING** label, data and engine status,
Start / Pause / Resume, Price check, Export CSV, Settings and Reset.

## Using the price-check panel

Open it with **Price check** in the header, or see it in the right column of the Terminal
layout. For the exact market being tracked it shows:

- **Order book (what fills use).** For UP (= YES) and DOWN (= NO): best bid with quantity, and
  the **executable ask** with quantity. The ask is derived because Kalshi reports bids only:
  `YES ask = 1.00 - NO bid` and `NO ask = 1.00 - YES bid`, with the same quantity.
- **Depth for your order.** Contracts available at or below the limit (37¢), the estimated fill
  quantity for a 20-contract purchase, the depth-weighted average price, the worst price, the
  principal, the estimated fee, any shortfall, and the next ask above the limit. One contract
  quoted at 37¢ does not mean 20 can fill there. For a working order it also shows the estimate
  for its remaining quantity, net of liquidity it has already used.
- **API market summary.** These are separately labeled. Kalshi's market object also reports
  bid, ask and last trade. These are not the book and are never used for fills.
- **Summary vs book consistency check.** This compares like with like. It is rejected when the
  two observations come from different 15-minute windows or are more than 5 s apart. Use "Run
  fresh comparison" to fetch both back-to-back.
- The exact ticker, a link to that market on kalshi.com, the side mapping, the data source,
  observation time (local and UTC), request latency and data age.

**Comparing with the Kalshi website.** Enter the price you saw in cents, the side, the quote
type (*buy quote* = what you'd pay, *sell quote* = what you'd receive, *last trade*, or
*unknown*), and when you saw it. The app compares it with the recorded API observation of the
same type that is nearest in time:

- **consistent**: same value, or the API value rounds to the visible whole-cent price.
- **discrepancy**: the values differ. The app then lists only causes supported by the recorded
  data. For example, the visible price may equal the last trade, the bid, the other side's
  quote, or an ask that includes the fee. Or the quote may have moved within ±10 s of your time.
- **inconclusive**: the quote type is unknown, or no recorded observation is within 10 s.

Book prices are never adjusted to match the website. Nothing in trading depends on the website
(no scraping).

---

## Strategy, exactly as implemented

- Only Kalshi's **15-minute BTC UP/DOWN** markets (`KXBTC15M`). Markets are discovered through
  the API and validated (see below). Hourly BTC markets, range markets and perpetuals are rejected.
- Windows are traded when they **start** at :00, :15 or :30 in the display timezone; the :45
  window is skipped. Times are shown in America/Chicago with daylight-saving time handled, and
  stored in UTC.
- At the start of an eligible window, it creates two virtual limit buys on the same market:
  **20 UP (YES) at most $0.37** and **20 DOWN (NO) at most $0.37**.
- Both orders work for the whole window and are re-evaluated on every fresh order-book
  observation. Any unfilled remainder is canceled at market close. Filled contracts are held to
  Kalshi's official settlement. There are no exits, no price chasing, no extra entries and no
  compounding. Size never exceeds 20 per side per market, including across restarts (database
  constraints enforce one order per side per window).
- **Fresh start mid-window:** waits for the next eligible window. **Entry grace:** if market
  discovery is late, entry is allowed up to 30 s after the window start
  (`KBOT_ENTRY_GRACE_SECONDS`). After that the window is skipped and the reason is recorded.
- Settings (limit, size, eligible minutes, timezone, fee model, starting balance for resets)
  apply from the **next new window**. Existing orders keep their own parameters.

## Market discovery and validation

Before any order is created, every market must pass all of these checks. Each failed check
becomes a visible skip reason.

- series ticker `KXBTC15M` and exactly one market closing at the window end;
- `market_type == "binary"` and a contract value (`notional_value_dollars`) of $1.00;
- `close_time - window_start == 15 min` and `open_time` equal to the window start (within 60 s);
- the close time encoded in the event ticker (`KXBTC15M-26OCT021330` = 13:30 New York time)
  matches the API `close_time`;
- `strike_type` is `greater_or_equal`/`greater` with a `floor_strike` target and no
  `cap_strike`, which proves **YES = BTC at/above the target = UP** and **NO = DOWN**;
- the title or rules reference Bitcoin; `status == "active"` before entry; the limit price lies
  on the market's tick grid (`price_ranges`).

The link format is `https://kalshi.com/markets/kxbtc15m/bitcoin-price-up-down/<event ticker>`.

## Paper fill model (simulated, conservative, and its limits)

Fills are **estimates from publicly displayed order-book depth**. They are not exchange
executions. They do not reproduce queue priority. Public depth cannot prove that a
hypothetical resting order would have received a maker fill, so the model only **takes visibly
offered asks**.

1. **Gates.** There are no fills unless:
   - the book is fresh (requested 6 s ago or less) and passed validation;
   - a fresh market status says `active`;
   - the exchange reports `trading_active`;
   - the wall clock and the observation are before the close;
   - trading isn't paused.

   A stale, crossed, locked, malformed, out-of-order or incomplete book pauses fills. An invalid
   book is re-fetched immediately; if it is still invalid, fills stay paused until a valid book
   arrives. A normal gap between last trade and ask does **not** pause anything.
2. **Trigger, then delay and revalidation.** When a book shows executable asks at or below the
   limit, nothing fills yet. The first fresh book requested at least 2 s later
   (`KBOT_EXECUTION_DELAY_SECONDS`) is the confirmation. Fills come only from **that** book.
   Liquidity that vanished fills nothing.
3. **Walk the ladder.** The model walks the confirmation book's derived asks from the cheapest
   up, taking only levels at or below the limit. It respects displayed depth and the order's
   remaining quantity, in 0.01-contract steps (fractional depth is never truncated). Each leg is
   priced at its ask, so price improvement below 37¢ happens naturally. **Nothing ever fills
   above the limit.** A bid at 37¢, a last trade at 37¢ or a chart touching 37¢ never counts.
4. **No reuse of liquidity.** The model tracks per order and price level how much it has already
   taken: `available = displayed size - already consumed at that level`. Unchanged snapshots
   therefore can't create more fills. The replenishment assumption is conservative: the public
   feed can't identify individual orders, so consumed size is assumed to still be part of the
   displayed size, and only growth beyond it is new.
5. **Recorded evidence.** Every fill stores the trigger and confirmation book snapshots (bids as
   reported, local request/receive times, the HTTP `Date` header), the ladder walk and the fee
   calculation. Click **Evidence** on any fill.

## Fees (estimated, recorded per fill)

The taker fee is `round_up_to_cent(0.07 × M × C × P × (1 − P))`, where `M` is the series
`fee_multiplier` from `GET /series/KXBTC15M`. Scheduled changes from
`GET /series/fee_changes` apply by effective time, and an active market
`fee_waiver_expiration_time` waives the fee. The per-order fee accumulator is modeled, so an
order filled in pieces costs what one equivalent fill would. All simulated fills are taker fills.
Reservations add a documented buffer of $0.01 per possible extra fill, in case rounding happens
per fill. At 20 × $0.37 the fee is $0.33 per side and the reservation is $7.92 per side
($15.84 per window). Unmodeled fee types are flagged "uncertain". A no-fee mode exists for tests
and is labeled loudly in the UI.

## Account, reservations, netting and settlement

- The account is created once with $1,000 and persists across restarts. It tracks cash, reserved
  cash, available cash, filled exposure and cost, estimated position value (unmatched contracts
  marked at the best bid), realized and unrealized P&L, fees, and equity history.
- Both orders' worst-case principal plus fees is **reserved before entry**. If available cash
  (cash minus outstanding reservations; unsettled positions have already been paid for) is
  short, the window is **skipped**. Quantities are never reduced. Reservations are released when
  orders fill, expire or are canceled.
- **Opposing positions.** Kalshi keeps one signed position per market (`position_fp`), so buying
  NO while holding YES offsets it. Each UP+DOWN pair is worth exactly $1.00 whatever the outcome.
  Both legs are preserved in full for analysis. Each pair's $1.00 is credited **exactly once**
  when it forms (ledger key `pair:<window>:<pairs>`). Settlement pays only the unmatched
  remainder: `(UP - pairs) × YES payout + (DOWN - pairs) × NO payout`.
- **Settlement** uses Kalshi's official final state only: `status == finalized` with
  `result`/`settlement_value_dollars`. `closed`, `determined`, `disputed` and `amended` are shown
  explicitly and waited out. Delays over an hour are flagged. Inconsistent results are not
  applied. The winner is never inferred from a BTC exchange.
- Every write is one SQLite transaction with durable IDs and UNIQUE keys (orders, fills, ledger
  refs, settlements). A crash, duplicate poll or restart can't duplicate orders, fills or money.

With fees off, for one market: 20 UP + 20 DOWN at 37¢ = **+$5.20**; only 20 winning = **+$12.60**;
only 20 losing = **−$7.40**; no fills = **$0**. These are tested.

Performance is reported **per complete window** (both legs, fees, settlement). A high share of
winning individual legs is not evidence that this two-sided strategy is profitable.

## Controls

- **Start / Resume**: enables entries from the next eligible window start.
- **Pause**: cancels remaining virtual orders and blocks entries. Settlement tracking continues.
- **Export CSV**: orders, fills, settlements, windows, ledger, equity, price comparisons.
- **Reset**: typed `RESET` confirmation, with export buttons first. It archives the current
  account and creates a new one with the configured starting balance. Trading stays stopped
  until Start. Reset is never automatic.

## Operation details

- Services bind to **127.0.0.1** only.
- Logs are in `data\logs\paper-bot.log` (rotating). Data files are `data\paper.sqlite3` (live)
  and `data\preview.sqlite3` (preview).
- **Single-engine guard:** an exclusive OS lock on `data\paper.engine.lock` plus a database
  lease. A second instance refuses to start. Closing the browser or switching layouts never
  creates another engine.
- Market data is polled over REST: the book every 2 s, the market every 5 s, settlement every
  20 s and then backing off. That is about 1 request per second, well below Kalshi's
  basic-tier read limits. HTTP 429 `Retry-After` is honoured, and failures back off up to 5 s.
- If the feed fails, the dashboard shows the **real error text** and fills pause. Live mode
  never substitutes sample or random data.
- **WebSockets are not used.** They need API credentials, and public REST provides everything
  this strategy needs. No credentials are read, stored or logged.
- **HTTPS** is verified against the Windows certificate store (via `truststore`). That way
  antivirus HTTPS scanning or a corporate proxy that the browser trusts doesn't break the Kalshi
  connection, and certificates are still fully checked.
- **Clock.** Window timing follows Kalshi's server clock, measured from the HTTP `Date` header.
  If your PC clock is off by 2 s or more, the engine corrects for it and the panel suggests
  syncing Windows time.
- **Market listing timing.** Kalshi may list the next market before its window opens and fill in
  the target price at the open. Fields that aren't filled in yet are re-checked until the 30 s
  entry grace. Only structural mismatches (wrong market type, range market, a strike type where
  YES isn't UP, a different window) skip a window immediately.

## Configuration

`setup.ps1` copies `.env.example` to `.env`. No secrets are required. Everything has safe
defaults: host/port, data folder, Kalshi base URLs, polling intervals, stale threshold, execution
delay, entry grace, pair-accounting mode. Strategy parameters are edited in **Settings**.

## Tests

`.\test.ps1` runs 104 deterministic backend tests and type-checks and builds the dashboard. They
cover:

- the schedule, including Chicago DST, and market and side mapping;
- fixed-point and legacy parsing, and the NO-bid-63 → YES-ask-37 conversion and full ladders;
- depth, VWAP and fees; "visible 37¢ but ask 40¢ → no fill"; "8 at 37¢ then 39¢ → at most 8";
- stale, crossed and paused data; no double consumption; reservations and insufficient funds;
- restart recovery and downtime with no invented fills; pause and resume; settlement delays and
  idempotency; netting credited once; the four economic outcomes;
- summary/book comparisons from different windows being rejected; visible-price comparisons;
- the single-engine guard; and a full window run through the real HTTP client over a mock
  transport, asserting every request is an allowlisted GET with no order or portfolio endpoint.

## Known limitations

- Paper fills are estimates from displayed depth. Real fills depend on queue position, hidden
  intent, other participants reacting to your order, and latency that public data can't show.
  Maker (resting) fills are not simulated.
- Polling every 2 s can miss liquidity that appears and disappears between polls. Liquidity that
  disappears during the 2 s execution delay is (conservatively) not filled.
- Fees are estimates of Kalshi's published formula and rounding. Exact charges can differ
  (rounding of sub-penny prices, account-specific programs).
- Market-structure facts were verified against Kalshi's official OpenAPI schema (official SDK
  `kalshi_python_sync` 3.31.0) and real public tickers. docs.kalshi.com and the Kalshi API were
  not reachable from the environment this was built in. Run `.\start.ps1 -LiveCheck` on your
  computer to confirm live connectivity. If Kalshi changes the market structure, validation will
  skip windows with a stated reason rather than guess.
- The engine runs only while your computer and the backend window are running.
- Kalshi's live market and order-book responses could not be observed from the build
  environment. The pre-open listing behaviour is handled defensively (see above). If something
  still doesn't match, the "Why isn't it buying?" panel and `.\start.ps1 -LiveCheck`, which also
  shows how the next window's market is listed, will show the exact reason.

See [docs/DESIGN.md](docs/DESIGN.md) for architecture, data model and implementation choices.

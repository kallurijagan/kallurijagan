# Design and implementation choices

## Architecture

```
          Kalshi production REST (public, unauthenticated GETs only)
                                   │
       backend/app/marketdata/     ▼   (network, read-only)
  ┌───────────────────────────────────────────────────────────────┐
  │ client.py   KalshiReadOnlyClient: GET allowlist + httpx hook  │
  │ models.py   Decimal parsing of *_dollars / *_fp fields        │
  │ book.py     validation, ask ladder, depth/VWAP, summary check │
  │ discovery.py market/series/window/mapping validation          │
  │ feed.py     polling, resync, out-of-order rejection, backoff  │
  │ pricecheck.py visible-price comparison (like-for-like)        │
  │ preview.py  labeled synthetic source (preview mode only)      │
  └───────────────────────────────────────────────────────────────┘
                    │ validated snapshots (no network handles)
                    ▼
       backend/app/engine.py   orchestrator: schedule, entry, polling, close, settlement polling
                    │
                    ▼
       backend/app/paper/      (local only, NEVER imports httpx/client — enforced by a test)
  ┌───────────────────────────────────────────────────────────────┐
  │ fill_model.py  pure executable-ask model (gates, trigger,     │
  │                delay+revalidation, ladder walk, consumption)  │
  │ fees.py        fee estimate, accumulator, reservation buffer  │
  │ accounting.py  ledger, reservations, netting, settlement      │
  │ service.py     PaperExecutionService (start/pause/reset, …)   │
  └───────────────────────────────────────────────────────────────┘
                    │ SQLite (WAL, synchronous=FULL, BEGIN IMMEDIATE)
                    ▼
       backend/app/api.py  FastAPI  ──►  frontend/ (React; 3 layouts, same API)
```

One process runs the engine (an asyncio task, 1 s ticks) and serves the API and the built
dashboard on 127.0.0.1. The browser only polls `/api/state` (1 s) and the history endpoints.

## Verified Kalshi API facts used

Sources: the official Kalshi OpenAPI schema as generated into the official Python SDK
`kalshi_python_sync` 3.31.0 (models `Market`, `GetMarketOrderbookResponse`/`OrderbookCountFp`,
`Series`, `FeeType`, `ExchangeStatus`, `MarketPosition`, `Settlement`; endpoint auth settings),
plus real public KXBTC15M tickers and kalshi.com market URLs. docs.kalshi.com and the API hosts
were blocked by the network policy of the environment this was built in. The runtime validation
re-checks these facts on every market, and `start.ps1 -LiveCheck` verifies them live.

| Fact | Where it's used |
|---|---|
| Base `https://api.elections.kalshi.com/trade-api/v2` (SDK also lists `external-api.kalshi.com`) | client (fallback host) |
| `GET /markets`, `/markets/{t}`, `/markets/{t}/orderbook`, `/series/{t}`, `/series/fee_changes`, `/exchange/status`, `/historical/markets/{t}` need no auth | allowlist |
| Prices are dollar strings (`yes_bid_dollars`, up to 6 dp); counts are `*_fp` strings, 2 dp, min 0.01 | `models.py` |
| Orderbook = `orderbook_fp.yes_dollars` / `no_dollars`, `[price, count]`, bids only, ascending | `OrderBook.from_api` |
| YES bid X ≡ NO ask 1−X with the same size | `OrderBook.asks()` |
| Market `status` ∈ initialized, inactive, active, closed, determined, disputed, amended, finalized; `result` ∈ yes, no, scalar, "" | gates, settlement |
| `strike_type` (greater_or_equal …), `floor_strike`, `cap_strike`, `price_ranges` (start/end/step), `notional_value_dollars`, `fee_waiver_expiration_time` | discovery, ticks, fees |
| Series `fee_type` ∈ quadratic, quadratic_with_maker_fees, quadratic_with_combo_maker_fees, flat; `fee_multiplier` | fees |
| Positions are one signed `position_fp` per market | netting |
| Event ticker `KXBTC15M-YYMONDDHHMM` = close time in New York; market `<event>-MM` | cross-check only (never constructed) |

## State machines

Window (`windows.status`): `skipped` (with reason) | `active` → `awaiting_settlement` →
`settled`; `abandoned` only when an account is reset before settlement.

Order (`orders.status`): `pending` → `partially_filled` → `filled`, or the remainder becomes
`expired` (market close) or `canceled` (pause/reset). `settled=1` after the window settles. The
UI shows Pending / Partially filled / Filled / Expired / Canceled / Settled.

## Idempotency and durability keys

| Table | Key | Prevents |
|---|---|---|
| windows | `UNIQUE(account_id, window_start)` | duplicate entries/skips per window |
| orders | `id = <window>:<UP/DOWN>`, `UNIQUE(window_id, side)` | more than one order (20 contracts) per side |
| observations | `UNIQUE(obs_key = ticker/requested_at/source)` | duplicate evidence rows |
| fills | `id = <order>:obs<observation id>:<price>` | the same observation/level filling twice |
| ledger | `UNIQUE(ref)` (`deposit:`, `fill:`, `fee:`, `pair:<window>:<cum pairs>`, `settle:<window>`) | double debits/credits |
| settlements | `PRIMARY KEY(window_id)` | applying a settlement twice |
| engine_state | single row: trading flag, armed_at, lease | two engines / entries after restart mid-window |

Every state change is in one `BEGIN IMMEDIATE` transaction, so a crash rolls back a half-done
fill or settlement.

## Implementation choices (and why)

1. **REST polling, no WebSockets.** Public REST provides the book, summary, status and
   settlement without credentials. WebSockets would require API keys. Without a sequence-gap
   protocol, each REST snapshot is a full book; out-of-order responses are rejected by request
   time.
2. **Execution delay with revalidation.** Simulates order latency honestly: the decision uses
   one book and the fill uses a later one.
3. **Taker-only fills.** Public depth can't prove a resting order's queue position, so maker
   fills are not simulated. Price improvement comes from walking asks below the limit.
4. **Whole ladder, 0.01-contract precision.** Matches Kalshi's minimum granularity. Displayed
   fractional depth is used as-is.
5. **Conservative consumption.** Per order and per price level; only growth beyond consumed size
   is new liquidity.
6. **Pair netting on fill (default).** Mirrors Kalshi's signed position. `hold_to_settlement` is
   available via config. Both credit each pair once and give identical final P&L.
7. **Marks.** Unmatched contracts are valued at the fresh best bid (a liquidation estimate), or
   at the determined payout once Kalshi has determined the result. Pairs are worth $1.00.
8. **Fees.** Taker formula with the series multiplier and scheduled changes, a per-order
   accumulator, and a reservation buffer for per-fill rounding uncertainty.
9. **Entry rules.** Entry requires the engine to have been armed (started, resumed or restarted)
   **before** the window start, plus a 30 s grace for discovery delays. Settings are snapshotted
   per window.
10. **Preview mode.** Separate database, separate series name (`PREVIEWBTC15M`), and labels in
    the UI, CSV file names and logs. It exists only so the UI can be explored when Kalshi is
    unreachable. Live mode never falls back to it.
11. **Single process.** The engine and API share one asyncio loop and one SQLite connection.
    Control actions run on the loop between ticks, so they can't interleave with a fill
    transaction.

12. **Retry vs skip.** Validation failures are either *structural* (skip the window now) or
    *not-ready-yet* (no market listed, `floor_strike`/`strike_type`/rules not filled in,
    several candidates), which are re-checked every ~2 s until the 30 s entry grace expires. A
    failure cached by the next-window prefetch, before the window started, is always re-checked
    at the window start.
13. **Clock.** `SkewCorrectedClock` applies Kalshi's server time (the median of HTTP `Date`
    header samples, whole seconds, with hysteresis) when the PC clock is off by 2 s or more.
    Out-of-order book detection uses a request sequence number, not wall-clock time.
14. **Windows robustness.** It verifies TLS against the OS certificate store, writes logs from a
    queue thread so a blocked console can't stall the event loop, disables console QuickEdit,
    prevents system sleep while trading is enabled, and records engine gaps (sleep or freeze) in
    the event log.
15. **Diagnosis.** `/api/state.diagnosis` ranks the checks (engine loop, trading switch, data
    feed, clock, exchange, fees, funds, schedule and window state, book validity, per-order
    gate) into one headline. Each order's gate explains in words why it is or isn't filling,
    for example "best ask 51c, needs <= 37c (14c away)".

## API (all responses carry `"paper": true`)

`GET /api/state` (everything the dashboards show), `/api/windows`, `/api/orders`, `/api/fills`,
`/api/fills/{id}` (evidence), `/api/settlements`, `/api/equity`, `/api/prices`,
`/api/analytics`, `/api/events`, `/api/settings` (GET/PUT),
`POST /api/control/start|pause|resume`, `POST /api/account/reset {"confirm":"RESET"}`,
`POST /api/price-check/compare`, `POST /api/price-check/refresh`,
`GET /api/price-check/comparisons`, `GET /api/export/{orders|fills|settlements|windows|ledger|equity|comparisons}.csv`,
`POST /api/admin/shutdown` (localhost only; used by stop.ps1).

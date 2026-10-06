"""Read-only LIVE data check against Kalshi production public endpoints.

    python -m app.tools.live_check            (or:  .\\start.ps1 -LiveCheck)

Discovers the current KXBTC15M market through the API, validates it, fetches its order
book and market summary, and prints the real ticker, bids, derived asks, quantities and
observation times. It also shows the NEXT window's market as currently listed (to see what
it looks like before it opens) and this computer's clock offset from Kalshi.

It only issues allowlisted GET requests (same guarded client as the engine) and never
touches the paper database. Exit code 0 = live data verified, 1 = not verified (the real
error is printed). It never falls back to sample data. Output is plain ASCII so it can be
redirected to a file on Windows.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from decimal import Decimal

from ..config import load_config
from ..marketdata.book import compare_summary_to_book, purchase_estimate, validate_book
from ..marketdata.client import KalshiReadOnlyClient
from ..marketdata.discovery import market_url, validate_window_market
from ..marketdata.models import NO, YES
from ..money import s
from ..schedule import UTC, WINDOW, window_start_for


def _lv(levels, n=6):
    return ", ".join(f"{lv.price} x {lv.size}" for lv in levels[:n]) or "(none)"


def _p(text: str = "") -> None:
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)


async def main() -> int:
    cfg = load_config()
    client = KalshiReadOnlyClient(cfg.kalshi_base_url, cfg.kalshi_fallback_base_url, cfg.http_timeout_seconds)
    _p(f"Kalshi read-only live check - {datetime.now(UTC).isoformat()}  base={cfg.kalshi_base_url}")
    ok = True
    try:
        status = await client.get_exchange_status()
        _p(f"exchange_active={status.exchange_active} trading_active={status.trading_active}")
        if not (status.exchange_active and status.trading_active):
            _p("  NOTE: Kalshi reports trading is not active right now; the bot will not enter windows until it is.")
        series = await client.get_series(cfg.series_ticker)
        _p(f"series {series.ticker}: {series.title!r} frequency={series.frequency} fee_type={series.fee_type} "
           f"fee_multiplier={series.fee_multiplier}")
        now = datetime.now(UTC)
        ws = window_start_for(now)
        we = ws + WINDOW
        markets = await client.find_markets_closing_between(cfg.series_ticker, int(we.timestamp()) - 60,
                                                            int(we.timestamp()) + 60)
        chk = validate_window_market(markets, cfg.series_ticker, ws, we)
        if not chk.ok or chk.market is None:
            _p(f"VALIDATION FAILED for the current window {ws:%H:%M}Z: {chk.reason} "
               f"({'will be retried' if chk.retryable else 'structural'})")
            return 1
        m = chk.market
        _p(f"\nCURRENT market {m.ticker}  event {m.event_ticker}  status={m.status}")
        _p(f"  url {market_url(cfg.series_ticker, m.event_ticker)}")
        _p(f"  open_time {m.open_time}  close_time {m.close_time}  strike_type={m.strike_type} target={m.floor_strike}")
        for c in chk.checks:
            _p(f"  [ok] {c}")
        for w in chk.warnings:
            _p(f"  [warning] {w}")
        _p(f"  rules: {m.rules_primary[:300]}")
        book = await client.get_orderbook(m.ticker)
        summ_at = datetime.now(UTC)
        summary = await client.get_market(m.ticker)
        _p(f"\nORDER BOOK (requested {book.requested_at.isoformat()}, received {book.received_at.isoformat()}, "
           f"latency {book.latency_ms} ms, server Date header {book.server_date})")
        problems = validate_book(book, summary)
        if problems:
            ok = False
        _p(f"  validation: {'OK' if not problems else 'INVALID: ' + '; '.join(problems)}")
        _p(f"  YES bids (reported): {_lv(book.yes_bids)}")
        _p(f"  NO  bids (reported): {_lv(book.no_bids)}")
        _p(f"  UP/YES asks (derived 1 - NO bid): {_lv(book.asks(YES))}")
        _p(f"  DOWN/NO asks (derived 1 - YES bid): {_lv(book.asks(NO))}")
        for side, k in (("UP", YES), ("DOWN", NO)):
            est = purchase_estimate(book, k, Decimal("20"), Decimal("0.37"))
            _p(f"  {side}: depth <= 0.37: {s(est.depth_at_or_below_limit)}; 20-lot estimate fill {s(est.fill_qty)} "
               f"vwap {s(est.vwap)} worst {s(est.worst_price)} principal ${s(est.principal)}")
        _p(f"\nAPI SUMMARY (requested {summ_at.isoformat()}): yes_bid={summary.yes_bid} yes_ask={summary.yes_ask} "
           f"no_bid={summary.no_bid} no_ask={summary.no_ask} last={summary.last_price}")
        cmp = compare_summary_to_book(summary, summ_at, book)
        _p(f"summary vs book: {cmp.status} {cmp.reason or ''}")

        nws, nwe = we, we + WINDOW
        nxt = await client.find_markets_closing_between(cfg.series_ticker, int(nwe.timestamp()) - 60,
                                                        int(nwe.timestamp()) + 60)
        nchk = validate_window_market(nxt, cfg.series_ticker, nws, nwe)
        if nchk.market is not None:
            n = nchk.market
            _p(f"\nNEXT window market as listed now: {n.ticker} status={n.status} open_time={n.open_time} "
               f"strike_type={n.strike_type} target={n.floor_strike}")
        _p(f"  next-window validation now: {'OK' if nchk.ok else nchk.reason} "
           f"({'re-checked until the window opens' if not nchk.ok and nchk.retryable else 'final'})")

        off = client.clock_offset_seconds
        if off is not None:
            _p(f"\nclock: Kalshi server time minus this PC = {off:+.1f}s"
               + ("  (more than 5s: sync Windows time; the bot corrects for it automatically)" if abs(off) > 5 else ""))
        if ok:
            _p(f"\nLIVE DATA VERIFIED for {m.ticker}. Requests made: {client.request_count} (all GET).")
            return 0
        _p("\nLIVE DATA RECEIVED BUT THE ORDER BOOK FAILED VALIDATION (see above). Fills would be paused.")
        return 1
    except Exception as exc:  # noqa: BLE001 - report any failure verbatim
        _p(f"\nLIVE CHECK FAILED - real error: {type(exc).__name__}: {exc}")
        _p("No sample data was used. Check internet access, VPN, firewall/antivirus HTTPS scanning,")
        _p("and that api.elections.kalshi.com is reachable from this computer.")
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Read-only LIVE data check against Kalshi production public endpoints.

    python -m app.tools.live_check            (or:  .\\start.ps1 -LiveCheck)

Discovers the current KXBTC15M market through the API, validates it, fetches its order
book and market summary, and prints the real ticker, bids, derived asks, quantities and
observation times. It only issues allowlisted GET requests (same guarded client as the
engine) and never touches the paper database. Exit code 0 = live data verified,
1 = could not verify (the real error is printed). It never falls back to sample data.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from ..config import load_config
from ..marketdata.book import compare_summary_to_book, purchase_estimate, validate_book
from ..marketdata.client import KalshiError, KalshiReadOnlyClient
from ..marketdata.discovery import market_url, validate_window_market
from ..marketdata.models import NO, YES
from ..money import s
from ..schedule import UTC, WINDOW, window_start_for
from decimal import Decimal


def _lv(levels, n=6):
    return ", ".join(f"{lv.price}×{lv.size}" for lv in levels[:n]) or "(none)"


async def main() -> int:
    cfg = load_config()
    client = KalshiReadOnlyClient(cfg.kalshi_base_url, cfg.kalshi_fallback_base_url, cfg.http_timeout_seconds)
    print(f"Kalshi read-only live check — {datetime.now(UTC).isoformat()}  base={cfg.kalshi_base_url}")
    try:
        status = await client.get_exchange_status()
        print(f"exchange_active={status.exchange_active} trading_active={status.trading_active}")
        series = await client.get_series(cfg.series_ticker)
        print(f"series {series.ticker}: {series.title!r} frequency={series.frequency} fee_type={series.fee_type} "
              f"fee_multiplier={series.fee_multiplier}")
        now = datetime.now(UTC)
        ws = window_start_for(now)
        we = ws + WINDOW
        markets = await client.find_markets_closing_between(cfg.series_ticker, int(we.timestamp()) - 60, int(we.timestamp()) + 60)
        chk = validate_window_market(markets, cfg.series_ticker, ws, we)
        if not chk.ok or chk.market is None:
            print(f"VALIDATION FAILED for window {ws:%H:%M}Z: {chk.reason}")
            return 1
        m = chk.market
        print(f"market {m.ticker}  event {m.event_ticker}  status={m.status}")
        print(f"  url {market_url(cfg.series_ticker, m.event_ticker)}")
        print(f"  window {m.open_time} -> {m.close_time}   strike_type={m.strike_type} target={m.floor_strike}")
        for c in chk.checks:
            print(f"  ✓ {c}")
        for w in chk.warnings:
            print(f"  ! {w}")
        print(f"  rules: {m.rules_primary[:300]}")
        t0 = datetime.now(UTC)
        book = await client.get_orderbook(m.ticker)
        summ_at = datetime.now(UTC)
        summary = await client.get_market(m.ticker)
        print(f"\nORDER BOOK (requested {book.requested_at.isoformat()}, received {book.received_at.isoformat()}, "
              f"latency {book.latency_ms} ms, server Date header {book.server_date})")
        problems = validate_book(book, summary)
        print(f"  validation: {'OK' if not problems else problems}")
        print(f"  YES bids (reported): {_lv(book.yes_bids)}")
        print(f"  NO  bids (reported): {_lv(book.no_bids)}")
        print(f"  UP/YES asks (derived 1 - NO bid): {_lv(book.asks(YES))}")
        print(f"  DOWN/NO asks (derived 1 - YES bid): {_lv(book.asks(NO))}")
        for side, k in (("UP", YES), ("DOWN", NO)):
            est = purchase_estimate(book, k, Decimal("20"), Decimal("0.37"))
            print(f"  {side}: depth<=0.37 {s(est.depth_at_or_below_limit)}; 20-lot est fill {s(est.fill_qty)} "
                  f"vwap {s(est.vwap)} worst {s(est.worst_price)} principal ${s(est.principal)}")
        print(f"\nAPI SUMMARY (requested {summ_at.isoformat()}): yes_bid={summary.yes_bid} yes_ask={summary.yes_ask} "
              f"no_bid={summary.no_bid} no_ask={summary.no_ask} last={summary.last_price}")
        cmp = compare_summary_to_book(summary, summ_at, book)
        print(f"summary vs book: {cmp.status} {cmp.reason or ''}")
        print(f"\nLIVE DATA VERIFIED for {m.ticker} (t0 {t0.isoformat()}). Requests made: {client.request_count} (all GET).")
        return 0
    except KalshiError as exc:
        print(f"\nLIVE CHECK FAILED — real error: {exc}")
        print("No sample data was used. Check internet access/firewall to api.elections.kalshi.com.")
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

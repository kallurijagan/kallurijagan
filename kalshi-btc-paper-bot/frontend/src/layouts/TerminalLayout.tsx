import { usePoll } from "../api";
import { PriceChart } from "../components/Charts";
import { Card, FeedBadge, KalshiLink, Pill, useWindowCountdown } from "../components/common";
import { FillsTable } from "../components/History";
import { OrderBookPanel } from "../components/OrderBook";
import { PriceCheckPanel } from "../components/PriceCheck";
import { EventsFeed, ExposureLine, ScheduleStrip, WindowList } from "../components/Trading";
import { useApp } from "../context";
import { ago, cents, fmtTime, fmtUtc, qty, usd } from "../format";
import type { PriceTick } from "../types";
import { FeedAlert } from "./SimpleLayout";

export function TerminalLayout() {
  const { state, tz } = useApp();
  const { label: cd } = useWindowCountdown();
  const m = state.market;
  const ticks = usePoll<{ ticks: PriceTick[] }>(m ? `/api/prices?ticker=${encodeURIComponent(m.ticker)}&minutes=16` : null, 3000);
  const limit = Number(state.settings.limit_price);
  const working = state.open_windows.flatMap((w) => (w.orders ?? []).map((o) => ({ ...o, window: w })));
  const a = state.account;
  return (
    <div className="layout-terminal">
      <div className="term-topbar">
        <div className="tb-group">
          <KalshiLink url={m?.url} ticker={m?.ticker} />
          {m && <Pill status={m.status === "active" ? "active" : m.status} label={m.status} />}
          <span className="tb-countdown" title="Time left in the current 15-minute window">⏱ {cd}</span>
          <span>tgt {m?.target_price ? `$${Number(m.target_price).toLocaleString("en-US")}` : "—"}</span>
        </div>
        <div className="tb-group">
          <FeedBadge compact />
          <span>book {state.book ? `${ago(state.book.age_seconds)} · ${state.book.latency_ms}ms` : "—"}</span>
          <span>exch {state.feed.exchange ? (state.feed.exchange.trading_active ? "trading" : "PAUSED") : "?"}</span>
          <span>req {state.feed.requests}</span>
          <Pill status={state.engine.state} />
        </div>
        <div className="tb-group mono">
          <span>{fmtUtc(state.server_time)}</span>
          <span>{fmtTime(state.server_time, tz)}</span>
        </div>
        <div className="tb-group">
          <span>eq <strong>{usd(a.equity)}</strong></span>
          <span>cash {usd(a.cash)}</span>
          <span>avail {usd(a.available)}</span>
          <span>rsv {usd(a.reserved)}</span>
          <span className={Number(a.total_pnl) >= 0 ? "pos" : "neg"}>P&amp;L {usd(a.total_pnl, { sign: true })}</span>
        </div>
      </div>
      <FeedAlert />
      <div className="term-grid">
        <div className="term-col">
          <Card dense title="Order book depth"><OrderBookPanel /></Card>
          <Card dense title="Hour schedule"><ScheduleStrip compact /></Card>
        </div>
        <div className="term-col">
          <Card dense title="UP (YES): bid / executable ask / last trade">
            <PriceChart ticks={ticks.data?.ticks ?? []} side="UP" limit={limit} tz={tz} dark height={190} />
          </Card>
          <Card dense title="DOWN (NO): bid / executable ask / last trade">
            <PriceChart ticks={ticks.data?.ticks ?? []} side="DOWN" limit={limit} tz={tz} dark height={190} />
          </Card>
          <Card dense title="Working & recent orders">
            {working.length === 0 ? (
              <p className="muted small">No open orders. {state.engine.entry_status.message}</p>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Side</th><th>Limit</th><th>Intended</th><th>Filled</th><th>Remain</th><th>Avg</th><th>Reserved</th><th>Status</th><th>Gate / reason</th><th>Submitted</th></tr></thead>
                  <tbody>
                    {working.map((o) => (
                      <tr key={o.id}>
                        <td>{o.side}</td><td>{cents(o.limit_price)}</td><td>{qty(o.quantity)}</td><td>{qty(o.filled_qty)}</td>
                        <td>{qty(o.remaining_qty)}</td><td>{o.avg_fill_price ? cents(o.avg_fill_price) : "—"}</td><td>{usd(o.reserved_remaining)}</td>
                        <td><Pill status={o.settled ? "settled" : o.status} /></td>
                        <td className="note-cell">{o.gate ?? o.close_reason ?? ""}</td>
                        <td className="mono">{fmtTime(o.submitted_at, tz)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <ExposureLine w={state.current_window} />
          </Card>
          <Card dense title="Recent simulated fills">
            {state.recent_fills.length ? <FillsTable rows={state.recent_fills.slice(0, 12)} compact /> : <p className="muted small">No fills yet. Fills require a fresh executable ask at or below {cents(limit)}.</p>}
          </Card>
        </div>
        <div className="term-col">
          <Card dense title="Price check"><PriceCheckPanel compact /></Card>
          <Card dense title="Event log"><EventsFeed limit={30} /></Card>
        </div>
      </div>
      <Card dense title="Open windows (active / awaiting settlement)">
        <WindowList windows={state.open_windows} empty="No open windows." />
      </Card>
    </div>
  );
}

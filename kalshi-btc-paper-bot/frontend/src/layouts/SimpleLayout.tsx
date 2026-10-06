import { useState } from "react";
import { usePoll } from "../api";
import { Card, Stat, useWindowCountdown } from "../components/common";
import { EventsFeed, ExposureLine, MarketHeader, NextWindows, OrderCard, ScheduleStrip, WindowList } from "../components/Trading";
import { WhyPanel } from "../components/WhyPanel";
import { useApp } from "../context";
import { fmtTime, pnlClass, usd } from "../format";
import type { WindowRow } from "../types";

export function FeedAlert() {
  const { state } = useApp();
  const f = state.feed;
  if (f.state === "ok" || f.state === "starting") {
    if (state.book && !state.book.valid)
      return <div className="alert alert-bad" role="alert"><strong>Order book invalid — simulated fills paused.</strong> {state.book.problems.join("; ")}</div>;
    return null;
  }
  return (
    <div className="alert alert-bad" role="alert">
      <strong>{f.state === "rate_limited" ? "Kalshi rate limit — waiting before the next request." : "Live market data unavailable — simulated fills are paused."}</strong>
      <div className="alert-detail"><code>{f.last_error}</code></div>
      <div className="muted small">No sample or random prices are substituted. Source: {f.base_url}. Failures in a row: {f.consecutive_failures}.</div>
    </div>
  );
}

export function SimpleLayout() {
  const { state, tz, actions } = useApp();
  const a = state.account;
  const w = state.current_window;
  const { label: cd } = useWindowCountdown();
  const orders = Object.fromEntries((w?.orders ?? []).map((o) => [o.side, o]));
  const settled = usePoll<{ windows: WindowRow[] }>("/api/windows?status=settled&limit=6", 15000);
  const awaiting = state.open_windows.filter((x) => x.status === "awaiting_settlement");
  const [busy, setBusy] = useState(false);
  const toggle = async () => {
    setBusy(true);
    try {
      await (state.engine.state === "running" ? actions.pause() : actions.start());
    } finally {
      setBusy(false);
    }
  };
  const cur = state.schedule.current;
  return (
    <div className="layout-simple">
      <FeedAlert />
      <WhyPanel />
      <section className="simple-hero" aria-label="Account">
        <Stat big label="Paper equity" value={usd(a.equity)} sub={`Started with ${usd(a.starting_balance)}`} />
        <Stat big label="Total P&L" value={usd(a.total_pnl, { sign: true })} tone={pnlClass(a.total_pnl)}
          sub={<>Realized {usd(a.realized_pnl, { sign: true })} · Unrealized {usd(a.unrealized_pnl, { sign: true })}</>} />
        <div className="hero-tiles">
          <Stat label="Cash" value={usd(a.cash)} />
          <Stat label="Available" value={usd(a.available)} />
          <Stat label="Reserved for orders" value={usd(a.reserved)} />
          <Stat label="Est. fees paid" value={usd(a.fees_total)} />
        </div>
      </section>

      <div className="simple-grid">
        <Card title="Current window" className="simple-window">
          <div className="countdown-row">
            <div className="countdown" aria-live="off">{cd}</div>
            <div>
              <div className="window-label">{fmtTime(cur.start, tz, false)} – {fmtTime(cur.end, tz, false)}</div>
              <div className="muted">{cur.eligible ? "Eligible window" : "Skipped window (:45)"} · {state.engine.entry_status.message}</div>
            </div>
          </div>
          <MarketHeader />
          <ScheduleStrip />
          <div className="next-wrap">
            <div className="muted small">Next eligible windows</div>
            <NextWindows />
          </div>
        </Card>

        <Card title="Controls" className="simple-controls">
          <button className={`btn btn-xl ${state.engine.state === "running" ? "btn-warn" : "btn-primary"}`} disabled={busy} onClick={toggle}>
            {state.engine.state === "running" ? "Pause trading" : state.engine.state === "paused" ? "Resume trading" : "Start paper trading"}
          </button>
          <ul className="help-list">
            <li><strong>Start/Resume</strong> waits for the next window that starts at :00, :15 or :30 ({tz}).</li>
            <li><strong>Pause</strong> cancels remaining virtual orders and blocks new entries; settlement tracking continues.</li>
            <li>The engine keeps running if you close this tab. Keep this computer and the backend window running.</li>
          </ul>
          <button className="btn" onClick={actions.openPriceCheck}>Open price check</button>
        </Card>
      </div>

      <section className="simple-orders" aria-label="Current window orders">
        <OrderCard big side="UP" order={orders.UP} window={w} />
        <OrderCard big side="DOWN" order={orders.DOWN} window={w} />
      </section>
      <ExposureLine w={w} />

      <div className="simple-grid even">
        <Card title="Awaiting official settlement">
          <WindowList compact windows={awaiting} empty="Nothing waiting for settlement." />
        </Card>
        <Card title="Recently settled windows">
          <WindowList compact windows={settled.data?.windows ?? []} empty="No settled windows yet." />
        </Card>
      </div>
      <details className="card simple-log">
        <summary>Activity log (entries, waits, skips, fills, settlements)</summary>
        <div className="card-body"><EventsFeed limit={40} /></div>
      </details>
    </div>
  );
}

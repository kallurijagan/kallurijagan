import { useApp } from "../context";
import { cents, countdown, fmtTime, OUTCOME_LABEL, qty, usd } from "../format";
import type { OrderRow, WindowRow } from "../types";
import { Empty, KalshiLink, Pill, useWindowCountdown } from "./common";

/** The current hour's four windows; the :45 window is visibly skipped. */
export function ScheduleStrip(props: { compact?: boolean }) {
  const { state, tz } = useApp();
  const { remaining } = useWindowCountdown();
  return (
    <div className={`schedule ${props.compact ? "compact" : ""}`} role="list" aria-label={`Hourly schedule (${tz})`}>
      {state.schedule.hour_slots.map((s) => (
        <div
          key={s.start}
          role="listitem"
          className={`slot ${s.eligible ? "eligible" : "skip"} ${s.is_current ? "current" : ""} st-${s.status}`}
          title={s.note ?? undefined}
        >
          <div className="slot-label">{s.label}</div>
          <div className="slot-state">
            {!s.eligible ? "Skipped (:45)" : s.status === "upcoming" ? "Trade" : s.status === "current" ? "Not entered" : s.status.replace(/_/g, " ")}
          </div>
          {s.is_current && <div className="slot-count">{countdown(remaining)} left</div>}
          {s.net_pnl !== null && <div className={`slot-pnl ${Number(s.net_pnl) >= 0 ? "pos" : "neg"}`}>{usd(s.net_pnl, { sign: true })}</div>}
        </div>
      ))}
    </div>
  );
}

export function NextWindows() {
  const { state, tz, skewMs } = useApp();
  const now = Date.now() + skewMs;
  return (
    <ul className="next-list">
      {state.schedule.next_eligible.map((w) => (
        <li key={w.start}>
          <span>{fmtTime(w.start, tz, false)} – {fmtTime(w.end, tz, false).split(" ")[0]}</span>
          <span className="muted">in {countdown((new Date(w.start).getTime() - now) / 1000)}</span>
        </li>
      ))}
    </ul>
  );
}

function statusOf(o: OrderRow): string {
  return o.settled ? "settled" : o.status;
}

export function OrderCard(props: { order: OrderRow | undefined; side: "UP" | "DOWN"; window: WindowRow | null; big?: boolean }) {
  const { state } = useApp();
  const o = props.order;
  const sideName = props.side === "UP" ? "UP · buys YES" : "DOWN · buys NO";
  if (!o) {
    return (
      <div className={`order-card side-${props.side.toLowerCase()} ${props.big ? "big" : ""} empty-order`}>
        <div className="oc-head"><span className="oc-side">{sideName}</span></div>
        <div className="oc-target">Target {cents(state.settings.limit_price)} × {qty(state.settings.contracts_per_side)}</div>
        <p className="muted small">No order in the current window.</p>
      </div>
    );
  }
  const filled = Number(o.filled_qty);
  const total = Number(o.quantity);
  const pctFilled = total > 0 ? Math.min(100, (filled / total) * 100) : 0;
  return (
    <div className={`order-card side-${props.side.toLowerCase()} ${props.big ? "big" : ""}`}>
      <div className="oc-head">
        <span className="oc-side">{sideName}</span>
        <Pill status={statusOf(o)} />
      </div>
      <div className="oc-target">
        Limit <strong>{cents(o.limit_price)}</strong> · intended <strong>{qty(o.quantity)}</strong>
      </div>
      <div className="oc-progress" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={filled} aria-label={`${props.side} filled`}>
        <div className="oc-bar" style={{ width: `${pctFilled}%` }} />
      </div>
      <dl className="oc-kv">
        <div><dt>Filled</dt><dd>{qty(o.filled_qty)} / {qty(o.quantity)}</dd></div>
        <div><dt>Avg price</dt><dd>{o.avg_fill_price ? cents(o.avg_fill_price) : "—"}</dd></div>
        <div><dt>Cost</dt><dd>{usd(o.principal_cost)}</dd></div>
        <div><dt>Est. fees</dt><dd>{usd(o.fees_paid)}</dd></div>
        <div><dt>Reserved</dt><dd>{usd(o.reserved_remaining)}</dd></div>
      </dl>
      {o.gate && <div className="oc-gate" title="Why the order is/isn't filling right now">{o.gate}</div>}
      {o.close_reason && o.status !== "filled" && <div className="oc-gate muted">{o.close_reason}</div>}
    </div>
  );
}

export function ExposureLine(props: { w: WindowRow | null }) {
  const w = props.w;
  if (!w || (Number(w.up_qty) === 0 && Number(w.down_qty) === 0)) return <p className="muted small">No filled exposure in this window.</p>;
  return (
    <div className="exposure">
      <span>Matched pairs: <strong>{qty(w.matched_pairs)}</strong> (worth $1.00 each)</span>
      {w.unmatched_side ? (
        <span className="unmatched">Unmatched exposure: <strong>{qty(w.unmatched_qty)} {w.unmatched_side}</strong> — wins only if BTC finishes {w.unmatched_side === "UP" ? "at/above" : "below"} target</span>
      ) : (
        <span>No unmatched exposure</span>
      )}
      <span>Fees {usd(w.fees)}</span>
      {w.outcome_class && <span>{OUTCOME_LABEL[w.outcome_class]}</span>}
    </div>
  );
}

export function MarketHeader(props: { compact?: boolean }) {
  const { state, tz } = useApp();
  const m = state.market;
  if (!m)
    return (
      <Empty title="No market data">
        {state.feed.last_error ? <span className="error-text">{state.feed.last_error}</span> : "Waiting for market discovery…"}
      </Empty>
    );
  return (
    <div className={`market-head ${props.compact ? "compact" : ""}`}>
      <div className="mh-main">
        <KalshiLink url={m.url} ticker={m.ticker} />
        <Pill status={m.status === "active" ? "active" : m.status} label={`market ${m.status}`} />
        {m.validation_ok === false && <Pill status="error" label="validation failed" title={m.validation_error ?? ""} />}
      </div>
      <div className="mh-sub">
        <span>Target <strong>{m.target_price ? `$${Number(m.target_price).toLocaleString("en-US", { minimumFractionDigits: 2 })}` : "—"}</strong></span>
        <span>UP = YES · DOWN = NO</span>
        <span className="muted">{fmtTime(m.open_time, tz, false)} → {fmtTime(m.close_time, tz, false)}</span>
      </div>
    </div>
  );
}

export function WindowList(props: { windows: WindowRow[]; empty: string; compact?: boolean }) {
  const { tz } = useApp();
  if (!props.windows.length) return <p className="muted small">{props.empty}</p>;
  if (props.compact)
    return (
      <ul className="win-compact">
        {props.windows.map((w) => (
          <li key={w.id} title={w.market_ticker ?? ""}>
            <span>{fmtTime(w.window_start, tz, false, true)}</span>
            <span className="muted">{w.outcome_class ? OUTCOME_LABEL[w.outcome_class] : w.status.replace(/_/g, " ")}{w.market_result ? ` · ${w.market_result.toUpperCase()}` : ""}</span>
            <strong className={Number(w.net_pnl) > 0 ? "pos" : Number(w.net_pnl) < 0 ? "neg" : ""}>{w.net_pnl !== null ? usd(w.net_pnl, { sign: true }) : w.settlement_note ?? "pending"}</strong>
          </li>
        ))}
      </ul>
    );
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr><th>Window</th><th>Market</th><th>UP</th><th>DOWN</th><th>Fees</th><th>Status</th><th>Net</th></tr>
        </thead>
        <tbody>
          {props.windows.map((w) => (
            <tr key={w.id}>
              <td>{fmtTime(w.window_start, tz, false, true)}</td>
              <td><code className="ticker">{w.market_ticker ?? "—"}</code></td>
              <td>{qty(w.up_qty)}</td>
              <td>{qty(w.down_qty)}</td>
              <td>{usd(w.fees)}</td>
              <td title={w.settlement_note ?? w.skip_reason ?? ""}><Pill status={w.status} />{w.settlement_note && <div className="muted tiny">{w.settlement_note}</div>}</td>
              <td className={Number(w.net_pnl) > 0 ? "pos" : Number(w.net_pnl) < 0 ? "neg" : ""}>{w.net_pnl !== null ? usd(w.net_pnl, { sign: true }) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EventsFeed(props: { limit?: number }) {
  const { state, tz } = useApp();
  const evs = state.events.slice(0, props.limit ?? 40);
  if (!evs.length) return <p className="muted small">No activity yet.</p>;
  return (
    <ul className="events">
      {evs.map((e, i) => (
        <li key={i} className={`ev ev-${e.level}`}>
          <time>{fmtTime(e.ts, tz)}</time>
          <span className="ev-kind">{e.kind}</span>
          <span className="ev-msg">{e.message}</span>
        </li>
      ))}
    </ul>
  );
}

import { useEffect, useState } from "react";
import { usePoll } from "../api";
import { useApp } from "../context";
import { cents, fmtTime, OUTCOME_LABEL, qty, usd } from "../format";
import type { FillRow, OrderRow, WindowRow } from "../types";
import { Pill } from "./common";

type Tab = "windows" | "orders" | "fills" | "settlements";

interface SettlementRow {
  window_id: string;
  market_ticker: string;
  market_status: string;
  result: string;
  yes_payout: string;
  no_payout: string;
  up_qty: string;
  down_qty: string;
  pairs_credited: string;
  payout: string;
  principal: string;
  fees: string;
  net_pnl: string;
  applied_at: string;
}

function useDebounced(value: string, ms = 300) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return v;
}

export function HistoryTabs(props: { initial?: Tab; dense?: boolean }) {
  const [tab, setTab] = useState<Tab>(props.initial ?? "windows");
  const [search, setSearch] = useState("");
  const q = useDebounced(search);
  const url = `/api/${tab}?limit=300${q ? `&search=${encodeURIComponent(q)}` : ""}`;
  const { data, error } = usePoll<Record<string, unknown>>(url, 10000);
  const rows = (data?.[tab] as unknown[]) ?? [];
  return (
    <div className={`history ${props.dense ? "dense" : ""}`}>
      <div className="history-bar">
        <div className="tabs" role="tablist">
          {(["windows", "orders", "fills", "settlements"] as Tab[]).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        <input className="search" type="search" placeholder="Search ticker, side, status, result…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search history" />
        <a className="btn btn-small" href={`/api/export/${tab}.csv`} download>CSV</a>
      </div>
      {error && <p className="error-text small">{error}</p>}
      {!data && !error && <p className="muted small">Loading…</p>}
      {data && rows.length === 0 && <p className="muted small empty-inline">{q ? `No ${tab} match “${q}”.` : `No ${tab} yet. They appear here once the engine enters a window.`}</p>}
      {rows.length > 0 && tab === "windows" && <WindowsTable rows={rows as WindowRow[]} />}
      {rows.length > 0 && tab === "orders" && <OrdersTable rows={rows as OrderRow[]} />}
      {rows.length > 0 && tab === "fills" && <FillsTable rows={rows as FillRow[]} />}
      {rows.length > 0 && tab === "settlements" && <SettlementsTable rows={rows as SettlementRow[]} />}
    </div>
  );
}

function WindowsTable({ rows }: { rows: WindowRow[] }) {
  const { tz } = useApp();
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>Window ({tz})</th><th>Market</th><th>Status</th><th>Outcome</th><th>UP filled</th><th>DOWN filled</th><th>Fees</th><th>Result</th><th>Net P&amp;L</th><th>Notes</th></tr></thead>
        <tbody>
          {rows.map((w) => (
            <tr key={w.id}>
              <td>{fmtTime(w.window_start, tz, false, true)}</td>
              <td><code className="ticker">{w.market_ticker ?? "—"}</code></td>
              <td><Pill status={w.status} /></td>
              <td>{w.outcome_class ? OUTCOME_LABEL[w.outcome_class] : "—"}</td>
              <td>{qty(w.up_qty)}</td>
              <td>{qty(w.down_qty)}</td>
              <td>{usd(w.fees)}</td>
              <td>{w.market_result ? w.market_result.toUpperCase() : "—"}</td>
              <td className={Number(w.net_pnl) > 0 ? "pos" : Number(w.net_pnl) < 0 ? "neg" : ""}>{w.net_pnl !== null ? usd(w.net_pnl, { sign: true }) : "—"}</td>
              <td className="note-cell">{w.skip_reason ?? w.settlement_note ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OrdersTable({ rows }: { rows: OrderRow[] }) {
  const { tz } = useApp();
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>Submitted</th><th>Market</th><th>Side</th><th>Limit</th><th>Intended</th><th>Filled</th><th>Cost</th><th>Fees</th><th>Status</th><th>Close reason</th></tr></thead>
        <tbody>
          {rows.map((o) => (
            <tr key={o.id}>
              <td>{fmtTime(o.submitted_at, tz, true, true)}</td>
              <td><code className="ticker">{o.market_ticker}</code></td>
              <td>{o.side} ({o.kalshi_side.toUpperCase()})</td>
              <td>{cents(o.limit_price)}</td>
              <td>{qty(o.quantity)}</td>
              <td>{qty(o.filled_qty)}</td>
              <td>{usd(o.principal_cost)}</td>
              <td>{usd(o.fees_paid)}</td>
              <td><Pill status={o.settled ? "settled" : o.status} /></td>
              <td className="note-cell">{o.close_reason ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function FillsTable({ rows, compact }: { rows: FillRow[]; compact?: boolean }) {
  const { tz, actions } = useApp();
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>Time</th>{!compact && <th>Market</th>}<th>Side</th><th>Qty</th><th>Price</th><th>Cost</th><th>Est. fee</th><th></th></tr></thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f.id}>
              <td>{fmtTime(f.filled_at, tz, true, !compact)}</td>
              {!compact && <td><code className="ticker">{f.market_ticker}</code></td>}
              <td>{f.side}</td>
              <td>{qty(f.quantity)}</td>
              <td>{cents(f.price)}</td>
              <td>{usd(f.principal)}</td>
              <td>{usd(f.fee)}</td>
              <td><button className="btn btn-small" onClick={() => actions.openFill(f.id)} title="Show the order-book observation and calculation behind this simulated fill">Evidence</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SettlementsTable({ rows }: { rows: SettlementRow[] }) {
  const { tz } = useApp();
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>Applied</th><th>Market</th><th>Kalshi status</th><th>Result</th><th>UP/DOWN qty</th><th>Pairs netted</th><th>Payout</th><th>Principal</th><th>Fees</th><th>Net</th></tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.window_id}>
              <td>{fmtTime(s.applied_at, tz, true, true)}</td>
              <td><code className="ticker">{s.market_ticker}</code></td>
              <td>{s.market_status}</td>
              <td>{s.result.toUpperCase()}</td>
              <td>{qty(s.up_qty)} / {qty(s.down_qty)}</td>
              <td>{qty(s.pairs_credited)}</td>
              <td>{usd(s.payout)}</td>
              <td>{usd(s.principal)}</td>
              <td>{usd(s.fees)}</td>
              <td className={Number(s.net_pnl) > 0 ? "pos" : Number(s.net_pnl) < 0 ? "neg" : ""}>{usd(s.net_pnl, { sign: true })}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

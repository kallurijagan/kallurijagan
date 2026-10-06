import { useEffect, useState, type ReactNode } from "react";
import { getJSON, sendJSON } from "../api";
import { useApp } from "../context";
import { cents, fmtTime, fmtUtc, qty, usd } from "../format";
import type { Settings } from "../types";

export function Modal(props: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && props.onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [props]);
  return (
    <div className="modal-backdrop" onClick={props.onClose}>
      <div className={`modal ${props.wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-label={props.title} onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>{props.title}</h2>
          <button className="btn btn-icon" aria-label="Close" onClick={props.onClose}>✕</button>
        </header>
        {props.children}
      </div>
    </div>
  );
}

const TIMEZONES = ["America/Chicago", "America/New_York", "America/Denver", "America/Los_Angeles", "UTC"];

export function SettingsDialog(props: { onClose: () => void }) {
  const { state, actions } = useApp();
  const [s, setS] = useState<Settings>(state.settings);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const toggleMinute = (m: number) =>
    setS((x) => ({ ...x, eligible_minutes: x.eligible_minutes.includes(m) ? x.eligible_minutes.filter((v) => v !== m) : [...x.eligible_minutes, m].sort((a, b) => a - b) }));
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await sendJSON("/api/settings", "PUT", s);
      actions.notify("Settings saved — they apply from the next new window.");
      await actions.refresh();
      props.onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };
  const reservation = Number(s.limit_price) * Number(s.contracts_per_side) * 2;
  return (
    <Modal title="Strategy settings" onClose={props.onClose}>
      <p className="note">Changes take effect at the <strong>next new window</strong>. Orders that already exist keep their own price and quantity.</p>
      <div className="form-grid">
        <label>Limit price per contract ($)
          <input inputMode="decimal" value={s.limit_price} onChange={(e) => setS({ ...s, limit_price: e.target.value })} />
        </label>
        <label>Contracts per side
          <input inputMode="numeric" value={s.contracts_per_side} onChange={(e) => setS({ ...s, contracts_per_side: e.target.value })} />
        </label>
        <fieldset>
          <legend>Eligible window start minutes</legend>
          <div className="check-row">
            {[0, 15, 30, 45].map((m) => (
              <label key={m} className="check">
                <input type="checkbox" checked={s.eligible_minutes.includes(m)} onChange={() => toggleMinute(m)} /> :{String(m).padStart(2, "0")}
              </label>
            ))}
          </div>
        </fieldset>
        <label>Display timezone
          <select value={s.display_timezone} onChange={(e) => setS({ ...s, display_timezone: e.target.value })}>
            {TIMEZONES.map((tz) => <option key={tz}>{tz}</option>)}
          </select>
        </label>
        <label>Starting balance for the next reset ($)
          <input inputMode="decimal" value={s.starting_balance} onChange={(e) => setS({ ...s, starting_balance: e.target.value })} />
        </label>
        <label>Fee model
          <select value={s.fee_mode} onChange={(e) => setS({ ...s, fee_mode: e.target.value })}>
            <option value="taker">Kalshi taker fee estimate (recommended)</option>
            <option value="none">NO FEES (testing only — unrealistic)</option>
          </select>
        </label>
      </div>
      {s.fee_mode === "none" && <p className="warn-text">Fees disabled: results will overstate profit. Use only for testing.</p>}
      <p className="muted small">Maximum principal per window at these settings: {usd(Number.isFinite(reservation) ? reservation : null)} plus estimated fees and a rounding buffer.</p>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="modal-actions">
        <button className="btn" onClick={props.onClose}>Cancel</button>
        <button className="btn btn-primary" disabled={saving} onClick={save}>Save settings</button>
      </div>
    </Modal>
  );
}

export function ResetDialog(props: { onClose: () => void }) {
  const { state, actions } = useApp();
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const doReset = async () => {
    try {
      await sendJSON("/api/account/reset", "POST", { confirm });
      actions.notify("Paper account reset. Press Start to trade again.");
      await actions.refresh();
      props.onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <Modal title="Reset paper account" onClose={props.onClose}>
      <p>
        This archives paper account #{state.account.account_id} (equity {usd(state.account.equity)}) and creates a new one with{" "}
        <strong>{usd(state.settings.starting_balance)}</strong>. Working virtual orders are canceled, unsettled windows stop being tracked,
        and trading stays stopped until you press Start. It cannot be undone.
      </p>
      <div className="export-first">
        <strong>Export first (recommended):</strong>
        {["orders", "fills", "settlements", "windows", "ledger", "equity"].map((k) => (
          <a key={k} className="btn btn-small" href={`/api/export/${k}.csv`} download>{k}.csv</a>
        ))}
      </div>
      <label className="block">Type <code>RESET</code> to confirm
        <input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" aria-label="Type RESET to confirm" />
      </label>
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="modal-actions">
        <button className="btn" onClick={props.onClose}>Cancel</button>
        <button className="btn btn-danger" disabled={confirm !== "RESET"} onClick={doReset}>Reset account</button>
      </div>
    </Modal>
  );
}

interface FillDetail {
  id: string;
  side: string;
  price: string;
  quantity: string;
  principal: string;
  fee: string;
  filled_at: string;
  market_ticker: string;
  calc: Record<string, unknown> & { leg?: Record<string, string>; ladder?: Record<string, unknown>[]; fee?: Record<string, unknown> };
  observation?: { requested_at: string; received_at: string; source: string; book: { yes_bids: string[][]; no_bids: string[][]; latency_ms?: number } };
  trigger_observation?: { requested_at: string; book: { yes_bids: string[][]; no_bids: string[][] } };
}

export function FillDialog(props: { fillId: string; onClose: () => void }) {
  const { tz } = useApp();
  const [fill, setFill] = useState<FillDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    getJSON<{ fill: FillDetail }>(`/api/fills/${encodeURIComponent(props.fillId)}`).then((r) => setFill(r.fill)).catch((e) => setError(String(e)));
  }, [props.fillId]);
  return (
    <Modal title="Simulated fill — supporting evidence" onClose={props.onClose} wide>
      {error && <p className="error-text">{error}</p>}
      {!fill && !error && <p className="muted">Loading…</p>}
      {fill && (
        <div className="fill-detail">
          <p className="note">
            This is a <strong>simulated</strong> fill estimated from the displayed order book. It is not an exchange-confirmed execution and does not model queue priority.
          </p>
          <dl className="kv">
            <dt>Market</dt><dd><code>{fill.market_ticker}</code></dd>
            <dt>Side</dt><dd>{fill.side} ({fill.side === "UP" ? "YES" : "NO"})</dd>
            <dt>Quantity × price</dt><dd>{qty(fill.quantity)} × {cents(fill.price)} = {usd(fill.principal)}</dd>
            <dt>Estimated fee</dt><dd>{usd(fill.fee)}</dd>
            <dt>Filled at</dt><dd>{fmtTime(fill.filled_at, tz, true, true)} · {fmtUtc(fill.filled_at)}</dd>
            <dt>Trigger book (requested)</dt><dd>{fill.trigger_observation ? fmtUtc(fill.trigger_observation.requested_at) : "—"}</dd>
            <dt>Confirmation book (requested)</dt><dd>{fill.observation ? fmtUtc(fill.observation.requested_at) : "—"} · source {fill.observation?.source}</dd>
            <dt>Ask derivation</dt><dd>{String(fill.calc.ask_derivation ?? "")}</dd>
            <dt>Execution delay</dt><dd>{String(fill.calc.execution_delay_seconds ?? "—")}s (waited {String(fill.calc.waited_seconds ?? "—")}s)</dd>
          </dl>
          <h3>Ask ladder at confirmation (at/below limit {cents(String(fill.calc.limit_price ?? ""))})</h3>
          <table className="table">
            <thead><tr><th>Ask</th><th>Displayed</th><th>Already consumed</th><th>Available</th><th>Taken</th></tr></thead>
            <tbody>
              {(fill.calc.ladder ?? []).map((l, i) => {
                const r = l as Record<string, string | boolean>;
                return r.above_limit ? (
                  <tr key={i} className="muted"><td>{cents(String(r.ask_price))}</td><td>{qty(String(r.displayed_size))}</td><td colSpan={3}>above limit — not executable</td></tr>
                ) : (
                  <tr key={i}><td>{cents(String(r.ask_price))}</td><td>{qty(String(r.displayed_size))}</td><td>{qty(String(r.consumed_before))}</td><td>{qty(String(r.available))}</td><td>{qty(String(r.take))}</td></tr>
                );
              })}
            </tbody>
          </table>
          <h3>Fee calculation</h3>
          <pre className="code">{JSON.stringify(fill.calc.fee, null, 2)}</pre>
          <details>
            <summary>Raw order book observations (bids as reported by Kalshi)</summary>
            <pre className="code">{JSON.stringify({ confirmation: fill.observation?.book, trigger: fill.trigger_observation?.book }, null, 2)}</pre>
          </details>
        </div>
      )}
    </Modal>
  );
}

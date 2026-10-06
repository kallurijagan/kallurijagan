import { useState } from "react";
import { sendJSON, usePoll } from "../api";
import { useApp } from "../context";
import { ago, cents, fmtTime, fmtUtc, qty, usd, utcToZonedLocalInput, zonedLocalToUtcIso } from "../format";
import type { Comparison, Estimate, PriceCheckSide } from "../types";
import { Empty, KalshiLink, Pill } from "./common";

function EstimateBlock(props: { e: Estimate; title: string }) {
  const e = props.e;
  const short = Number(e.shortfall) > 0;
  return (
    <div className="pc-est">
      <div className="pc-est-title">{props.title}</div>
      <dl className="kv kv-tight">
        <dt>Depth ≤ {cents(e.limit_price)}</dt><dd>{qty(e.depth_at_or_below_limit)} contracts</dd>
        <dt>Est. fill qty</dt><dd className={short ? "warn-text" : ""}>{qty(e.fill_qty)} of {qty(e.requested_qty)}{short && ` (short ${qty(e.shortfall)})`}</dd>
        <dt>Avg (depth-weighted)</dt><dd>{e.vwap ? cents(e.vwap) : "—"}</dd>
        <dt>Worst price</dt><dd>{e.worst_price ? cents(e.worst_price) : "—"}</dd>
        <dt>Principal</dt><dd>{usd(e.principal)}</dd>
        <dt>Est. fee</dt><dd>{usd(e.est_fee)}</dd>
        <dt>Next ask above limit</dt><dd>{e.next_level_above_limit ? `${cents(e.next_level_above_limit.price)} × ${qty(e.next_level_above_limit.size)}` : "—"}</dd>
      </dl>
    </div>
  );
}

function SideColumn(props: { side: "UP" | "DOWN"; d: PriceCheckSide }) {
  const d = props.d;
  return (
    <div className={`pc-side side-${props.side.toLowerCase()}`}>
      <h3>{props.side} <span className="muted">= {d.kalshi_side}</span></h3>
      <table className="table table-tight">
        <tbody>
          <tr><th scope="row">Book best bid</th><td>{d.best_bid ? `${cents(d.best_bid.price)} × ${qty(d.best_bid.size)}` : "no bid"}</td></tr>
          <tr><th scope="row">Book executable ask</th><td>{d.executable_ask ? `${cents(d.executable_ask.price)} × ${qty(d.executable_ask.size)}` : "no ask (no observable offer)"}</td></tr>
          <tr><th scope="row" className="muted">derived from</th><td className="muted">{d.executable_ask?.derived_from ?? (props.side === "UP" ? "1 − best NO bid" : "1 − best YES bid")}</td></tr>
        </tbody>
      </table>
      <EstimateBlock e={d.estimate} title={`Buying ${qty(d.estimate.requested_qty)} at ≤ ${cents(d.estimate.limit_price)} (fresh order)`} />
      {d.current_order_estimate && (
        <EstimateBlock e={d.current_order_estimate} title="Your working order (remaining, after liquidity already used)" />
      )}
      <div className="pc-summary">
        <div className="pc-est-title">API market summary (not the book)</div>
        <dl className="kv kv-tight">
          <dt>Summary bid</dt><dd>{cents(d.summary.bid)}</dd>
          <dt>Summary ask</dt><dd>{cents(d.summary.ask)}</dd>
          <dt>Last trade</dt><dd>{cents(d.summary.last_trade)}{d.summary.last_trade_note && <span className="muted tiny"> ({d.summary.last_trade_note})</span>}</dd>
        </dl>
      </div>
    </div>
  );
}

export function PriceCheckPanel(props: { compact?: boolean }) {
  const { state, tz, actions } = useApp();
  const pc = state.price_check;
  const m = state.market;
  const [refreshing, setRefreshing] = useState(false);
  if (!pc || !m)
    return <Empty title="No order book observation yet">{state.feed.last_error ? <span className="error-text">{state.feed.last_error}</span> : "Waiting for market data."}</Empty>;
  const cmp = state.summary_comparison;
  const refresh = async () => {
    setRefreshing(true);
    try {
      await sendJSON("/api/price-check/refresh", "POST");
      await actions.refresh();
    } catch (e) {
      actions.notify(String(e), "error");
    } finally {
      setRefreshing(false);
    }
  };
  return (
    <div className={`price-check ${props.compact ? "compact" : ""}`}>
      <div className="pc-meta">
        <KalshiLink url={m.url} ticker={pc.ticker} />
        <span>Mapping: UP→YES, DOWN→NO <span className="muted">({m.strike_type}, target ${m.target_price})</span></span>
        <span>Source: {pc.observation.source}{state.preview && " (SAMPLE)"}</span>
        <span>Observed {fmtTime(pc.observation.requested_at, tz)} · {fmtUtc(pc.observation.requested_at)}</span>
        <span>Latency {pc.observation.latency_ms} ms · age {ago(pc.observation.age_seconds)}</span>
        <span>Tick {m.tick_size ? cents(m.tick_size) : "—"} · contract {m.contract_value ? usd(m.contract_value) : "—"}</span>
        {!pc.observation.valid && <span className="error-text">Book invalid: {pc.observation.problems.join("; ")}</span>}
        {pc.fee_estimate_uncertain && <span className="warn-text">Fee estimate uncertain</span>}
      </div>
      <p className="note small">
        One contract quoted at {cents(pc.limit_price)} does not mean {qty(pc.quantity)} can fill there — the estimates below walk the
        derived ask ladder level by level. Asks are computed as <code>1.00 − opposite-side bid</code>; Kalshi reports bids only.
      </p>
      <div className="pc-sides">
        <SideColumn side="UP" d={pc.sides.UP} />
        <SideColumn side="DOWN" d={pc.sides.DOWN} />
      </div>
      <div className="pc-consistency">
        <div className="pc-est-title">
          API summary vs order book{" "}
          {cmp ? <Pill status={cmp.status} /> : <span className="muted">not compared yet</span>}
          <button className="btn btn-small" disabled={refreshing} onClick={refresh}>Run fresh comparison</button>
        </div>
        {cmp?.reason && <p className="small">{cmp.reason}{cmp.skew_seconds !== null && ` (observations ${cmp.skew_seconds}s apart)`}</p>}
        {cmp && cmp.rows.length > 0 && (
          <table className="table table-tight">
            <thead><tr><th>Measure</th><th>Summary</th><th>Book</th><th></th></tr></thead>
            <tbody>
              {cmp.rows.map((r) => (
                <tr key={r.measure}><td>{r.measure}</td><td>{cents(r.summary)}</td><td>{cents(r.book)}</td><td>{r.match ? "✓" : "≠"}</td></tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted tiny">A summary/book difference triggers a fresh back-to-back comparison. Fills are based on the book only; they pause only if the book itself is stale or invalid.</p>
      </div>
      <CompareForm />
    </div>
  );
}

function CompareForm() {
  const { state, tz, skewMs, actions } = useApp();
  const [side, setSide] = useState<"UP" | "DOWN">("UP");
  const [quoteType, setQuoteType] = useState("buy");
  const [price, setPrice] = useState("");
  const [when, setWhen] = useState(() => utcToZonedLocalInput(new Date(Date.now() + skewMs).toISOString(), tz));
  const [result, setResult] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const history = usePoll<{ comparisons: Comparison[] }>("/api/price-check/comparisons?limit=8", 15000);
  const submit = async () => {
    setError(null);
    const iso = zonedLocalToUtcIso(when, tz);
    if (!iso) return setError("Enter the observation time as date and time.");
    try {
      const r = await sendJSON<{ comparison: Comparison }>("/api/price-check/compare", "POST", {
        side, quote_type: quoteType, price, price_unit: "cents", observed_at: iso, ticker: state.price_check?.ticker,
      });
      setResult(r.comparison);
      history.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      actions.notify("Comparison failed", "error");
    }
  };
  return (
    <div className="compare">
      <div className="pc-est-title">Compare with a price you saw on kalshi.com (optional)</div>
      <div className="compare-form">
        <label>Side
          <select value={side} onChange={(e) => setSide(e.target.value as "UP" | "DOWN")}><option value="UP">UP (YES)</option><option value="DOWN">DOWN (NO)</option></select>
        </label>
        <label>Quote type
          <select value={quoteType} onChange={(e) => setQuoteType(e.target.value)}>
            <option value="buy">Buy quote (what you'd pay)</option>
            <option value="sell">Sell quote (what you'd receive)</option>
            <option value="last">Last trade</option>
            <option value="unknown">Unknown</option>
          </select>
        </label>
        <label>Price (¢)
          <input inputMode="decimal" placeholder="37" value={price} onChange={(e) => setPrice(e.target.value)} />
        </label>
        <label>Seen at ({tz})
          <input type="datetime-local" step={1} value={when} onChange={(e) => setWhen(e.target.value)} />
        </label>
        <button className="btn" onClick={() => setWhen(utcToZonedLocalInput(new Date(Date.now() + skewMs).toISOString(), tz))}>Now</button>
        <button className="btn btn-primary" disabled={!price} onClick={submit}>Compare</button>
      </div>
      {error && <p className="error-text small" role="alert">{error}</p>}
      {result && <ComparisonResult c={result} />}
      {history.data && history.data.comparisons.length > 0 && (
        <details>
          <summary>Recent comparisons ({history.data.comparisons.length})</summary>
          <ul className="cmp-history">
            {history.data.comparisons.map((c, i) => (
              <li key={i}><Pill status={c.verdict} /> {c.side} {c.quote_type} {cents(c.visible_price)} vs {c.measure ?? "—"} {cents(c.api_value)} <span className="muted">· {fmtTime(c.observed_at, tz)}</span></li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function ComparisonResult({ c }: { c: Comparison }) {
  const { tz } = useApp();
  return (
    <div className={`cmp-result v-${c.verdict}`}>
      <div><Pill status={c.verdict} /> <strong>{c.side}</strong> {c.quote_type} quote {cents(c.visible_price)} vs {c.measure ?? "no like-for-like measure"} {c.api_value ? cents(c.api_value) : ""}</div>
      {c.api_observed_at && <div className="muted small">API observation {fmtTime(c.api_observed_at, tz)} ({c.skew_seconds}s from your time)</div>}
      <ul>{c.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
      {c.other_measures.length > 0 && (
        <details>
          <summary>All recorded measures at that time</summary>
          <table className="table table-tight">
            <tbody>{c.other_measures.map((o) => <tr key={o.measure}><td>{o.measure}</td><td>{cents(o.value)}</td><td>{o.rounds_to_visible ? "= visible" : ""}</td></tr>)}</tbody>
          </table>
        </details>
      )}
      <p className="muted tiny">Book prices are never adjusted to match the website. Causes are listed only when the recorded data supports them.</p>
    </div>
  );
}

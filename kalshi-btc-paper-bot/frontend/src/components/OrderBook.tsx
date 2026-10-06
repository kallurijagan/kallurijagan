import { useApp } from "../context";
import { ago, cents, qty } from "../format";
import type { Level } from "../types";
import { Empty } from "./common";

/** One side's ladder: derived asks (top, highest first) over reported bids. */
function Ladder(props: { title: string; asks: Level[]; bids: Level[]; limit: number; askFrom: string }) {
  const asks = props.asks.slice(0, 8).reverse();
  const bids = props.bids.slice(0, 8);
  const max = Math.max(1, ...asks.map((l) => Number(l.size)), ...bids.map((l) => Number(l.size)));
  const row = (l: Level, kind: "ask" | "bid") => {
    const exec = kind === "ask" && Number(l.price) <= props.limit;
    return (
      <tr key={kind + l.price} className={`lad-${kind} ${exec ? "executable" : ""}`}>
        <td className="lad-price">{cents(l.price)}{exec && <span className="exec-tag" title="Executable at or below your limit">≤ limit</span>}</td>
        <td className="lad-size">
          <div className="depth" style={{ width: `${(Number(l.size) / max) * 100}%` }} aria-hidden="true" />
          <span>{qty(l.size)}</span>
        </td>
      </tr>
    );
  };
  return (
    <div className="ladder">
      <div className="ladder-title">{props.title}</div>
      <table className="ladder-table">
        <thead><tr><th>Price</th><th>Contracts</th></tr></thead>
        <tbody>
          <tr className="lad-caption"><td colSpan={2}>ASKS (derived: {props.askFrom})</td></tr>
          {asks.length ? asks.map((l) => row(l, "ask")) : <tr><td colSpan={2} className="muted">No asks — no observable offer</td></tr>}
          <tr className="lad-caption"><td colSpan={2}>BIDS (reported)</td></tr>
          {bids.length ? bids.map((l) => row(l, "bid")) : <tr><td colSpan={2} className="muted">No bids</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export function OrderBookPanel() {
  const { state } = useApp();
  const b = state.book;
  if (!b) return <Empty title="No order book yet">{state.feed.last_error ?? "Waiting for the first observation."}</Empty>;
  const limit = Number(state.settings.limit_price);
  return (
    <div>
      <div className="book-meta">
        <code className="ticker">{b.ticker}</code>
        <span className={b.valid ? "" : "error-text"}>{b.valid ? "valid" : "INVALID — fills paused"}</span>
        <span>age {ago(b.age_seconds)}</span>
        <span>latency {b.latency_ms} ms</span>
        <span className="muted">{b.source}</span>
      </div>
      {!b.valid && <p className="error-text small">{b.problems.join("; ")}</p>}
      <div className="ladders">
        <Ladder title="UP / YES" asks={b.yes_asks} bids={b.yes_bids} limit={limit} askFrom="1 − NO bid" />
        <Ladder title="DOWN / NO" asks={b.no_asks} bids={b.no_bids} limit={limit} askFrom="1 − YES bid" />
      </div>
    </div>
  );
}

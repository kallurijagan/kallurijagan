import { useEffect, useRef } from "react";
import type { LayoutId } from "../types";

const OPTIONS: { id: LayoutId; name: string; blurb: string; best: string }[] = [
  { id: "simple", name: "Simple Dashboard", blurb: "Spacious cards: big balance & P&L, countdown, UP/DOWN order cards, one-click Start/Pause.", best: "Checking in at a glance" },
  { id: "terminal", name: "Trading Terminal", blurb: "Dark, dense: full order-book depth, bid/ask charts, working orders, fills, price-check, timestamps.", best: "Watching a live window" },
  { id: "analytics", name: "Analytics Dashboard", blurb: "Equity & per-window P&L charts, fill/fee statistics, outcome breakdown, searchable history.", best: "Reviewing performance" },
];

/** Schematic thumbnails drawn with CSS blocks — real structure of each layout, not screenshots. */
function Preview({ id }: { id: LayoutId }) {
  if (id === "simple")
    return (
      <div className="lp lp-simple" aria-hidden="true">
        <div className="lp-row"><div className="lp-hero" /><div className="lp-hero lp-hero-2" /></div>
        <div className="lp-strip" />
        <div className="lp-row"><div className="lp-card lp-up" /><div className="lp-card lp-down" /></div>
        <div className="lp-btn" />
      </div>
    );
  if (id === "terminal")
    return (
      <div className="lp lp-terminal" aria-hidden="true">
        <div className="lp-bar" />
        <div className="lp-grid3">
          <div className="lp-col"><div className="lp-ladder" /><div className="lp-ladder" /></div>
          <div className="lp-col"><div className="lp-chart" /><div className="lp-chart" /><div className="lp-table" /></div>
          <div className="lp-col"><div className="lp-panel" /><div className="lp-table" /></div>
        </div>
      </div>
    );
  return (
    <div className="lp lp-analytics" aria-hidden="true">
      <div className="lp-row"><div className="lp-tile" /><div className="lp-tile" /><div className="lp-tile" /><div className="lp-tile" /></div>
      <div className="lp-row"><div className="lp-area" /><div className="lp-bars" /></div>
      <div className="lp-table lp-table-wide" />
    </div>
  );
}

export function LayoutChooser(props: { current: LayoutId; onPick: (id: LayoutId) => void; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && props.onClose();
    window.addEventListener("keydown", onKey);
    ref.current?.querySelector<HTMLButtonElement>("button.lc-option.selected")?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [props]);
  return (
    <div className="modal-backdrop" onClick={props.onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" aria-labelledby="lc-title" ref={ref} onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2 id="lc-title">Choose layout</h2>
          <button className="btn btn-icon" aria-label="Close" onClick={props.onClose}>✕</button>
        </header>
        <p className="muted">
          All three layouts show the same paper account from the same running engine. Switching only changes the
          view — it never starts another engine or creates another account. Your choice is remembered in this browser.
        </p>
        <div className="lc-grid">
          {OPTIONS.map((o) => (
            <button
              key={o.id}
              className={`lc-option ${props.current === o.id ? "selected" : ""}`}
              aria-pressed={props.current === o.id}
              onClick={() => props.onPick(o.id)}
            >
              <Preview id={o.id} />
              <span className="lc-name">{o.name} {props.current === o.id && <span className="lc-current">✓ current</span>}</span>
              <span className="lc-blurb">{o.blurb}</span>
              <span className="lc-best">Best for: {o.best}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

import { useState } from "react";
import { useApp } from "../context";
import { usd } from "../format";
import { FeedBadge, Pill } from "./common";

const LAYOUT_NAME = { simple: "Simple", terminal: "Terminal", analytics: "Analytics" } as const;

export function Header() {
  const { state, actions, layout } = useApp();
  const eng = state.engine;
  const [busy, setBusy] = useState(false);
  const run = (fn: () => Promise<void>) => async () => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  return (
    <header className="app-header">
      <div className="brand">
        <span className="paper-badge" role="status" aria-label="Paper trading — simulated money only">
          {state.paper_label}
        </span>
        {state.preview && (
          <span className="preview-badge" title="Synthetic sample data — not real Kalshi prices">PREVIEW · SAMPLE DATA</span>
        )}
        <div className="brand-text">
          <strong>BTC 15-min UP/DOWN</strong>
          <span className="muted small">Kalshi data · simulated money · equity {usd(state.account.equity)}</span>
        </div>
      </div>
      <div className="header-status">
        <FeedBadge />
        <Pill status={eng.state} label={eng.state === "running" ? "Engine running" : eng.state === "paused" ? "Paused" : "Not started"} />
      </div>
      <nav className="header-actions" aria-label="Controls">
        {eng.state === "running" ? (
          <button className="btn btn-warn" disabled={busy} onClick={run(actions.pause)} title="Cancel remaining virtual orders and stop new entries (settlement tracking continues)">
            Pause
          </button>
        ) : (
          <button className="btn btn-primary" disabled={busy} onClick={run(actions.start)} title="Trade from the next eligible window start">
            {eng.state === "paused" ? "Resume" : "Start"}
          </button>
        )}
        <button className="btn" onClick={actions.openPriceCheck}>Price check</button>
        <ExportMenu />
        <button className="btn" onClick={actions.openSettings}>Settings</button>
        <button className="btn btn-ghost-danger" onClick={actions.openReset}>Reset</button>
        <button className="btn btn-layout" onClick={actions.openLayoutChooser} aria-haspopup="dialog">
          <span aria-hidden="true">▦</span> Choose layout: <strong>{LAYOUT_NAME[layout]}</strong>
        </button>
      </nav>
    </header>
  );
}

export function ExportMenu() {
  const [open, setOpen] = useState(false);
  return (
    <div className="menu">
      <button className="btn" aria-expanded={open} onClick={() => setOpen((o) => !o)}>Export CSV ▾</button>
      {open && (
        <div className="menu-pop" role="menu" onMouseLeave={() => setOpen(false)}>
          {["orders", "fills", "settlements", "windows", "ledger", "equity", "comparisons"].map((k) => (
            <a key={k} role="menuitem" href={`/api/export/${k}.csv`} download onClick={() => setOpen(false)}>
              {k[0].toUpperCase() + k.slice(1)}.csv
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

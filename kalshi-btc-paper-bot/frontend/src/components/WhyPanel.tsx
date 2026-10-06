import { useState } from "react";
import { useApp } from "../context";
import { fmtTime } from "../format";
import type { DiagnosisCheck } from "../types";

const ICON: Record<DiagnosisCheck["status"], string> = { ok: "✓", wait: "…", block: "✕", info: "i" };
const LABEL: Record<DiagnosisCheck["status"], string> = { ok: "OK", wait: "Waiting", block: "Blocking", info: "Note" };

/** One plain-language answer to "why isn't it buying?", with the checklist behind it. */
export function WhyPanel(props: { compact?: boolean }) {
  const { state, tz, actions } = useApp();
  const d = state.diagnosis;
  const [open, setOpen] = useState<boolean | null>(null);
  if (!d) return null;
  const expanded = open ?? d.status === "blocked";
  const tone = d.status === "blocked" ? "bad" : d.status === "ok" ? "good" : "wait";
  const needsStart = d.checks.some((c) => c.key === "trading" && c.status === "block");
  return (
    <section className={`why why-${tone} ${props.compact ? "compact" : ""}`} aria-live="polite" aria-label="Why is it or isn't it buying">
      <div className="why-main">
        <div className="why-title">
          <span className="why-q">Why isn't it buying?</span>
          <strong className="why-headline">{d.headline}</strong>
        </div>
        {d.detail && <div className="why-detail">{d.detail}</div>}
        {d.next_entry_at && d.status !== "waiting_for_price" && (
          <div className="why-next muted small">Next possible entry: {fmtTime(d.next_entry_at, tz, false)}</div>
        )}
      </div>
      <div className="why-actions">
        {needsStart && (
          <button className="btn btn-primary" onClick={() => void actions.start()}>
            {state.engine.state === "paused" ? "Resume" : "Start"}
          </button>
        )}
        <button className="btn btn-small" aria-expanded={expanded} onClick={() => setOpen(!expanded)}>
          {expanded ? "Hide checklist" : "Show checklist"}
        </button>
      </div>
      {expanded && (
        <ul className="why-checks">
          {d.checks.map((c) => (
            <li key={c.key} className={`why-check st-${c.status}`}>
              <span className="why-icon" aria-hidden="true">{ICON[c.status]}</span>
              <span className="sr-only">{LABEL[c.status]}: </span>
              <span className="why-check-title">{c.title}</span>
              {c.detail && <span className="why-check-detail">{c.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

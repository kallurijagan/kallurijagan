import type { ReactNode } from "react";
import { useNow } from "../api";
import { useApp } from "../context";
import { ago, countdown, STATUS_LABEL } from "../format";

export function Card(props: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; dense?: boolean; id?: string }) {
  return (
    <section className={`card ${props.dense ? "dense" : ""} ${props.className ?? ""}`} id={props.id}>
      {(props.title || props.actions) && (
        <header className="card-head">
          {props.title && <h2>{props.title}</h2>}
          {props.actions && <div className="card-actions">{props.actions}</div>}
        </header>
      )}
      <div className="card-body">{props.children}</div>
    </section>
  );
}

export function Empty(props: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{props.title}</div>
      {props.children && <div className="empty-body">{props.children}</div>}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  pending: "info",
  partially_filled: "warn",
  filled: "good",
  expired: "muted",
  canceled: "muted",
  settled: "good",
  skipped: "muted",
  active: "info",
  awaiting_settlement: "warn",
  abandoned: "muted",
  running: "good",
  paused: "warn",
  stopped: "muted",
  ok: "good",
  error: "bad",
  rate_limited: "warn",
  starting: "info",
  consistent: "good",
  discrepancy: "warn",
  rejected: "muted",
  inconclusive: "muted",
};

const STATUS_ICON: Record<string, string> = { good: "●", warn: "▲", bad: "■", info: "◆", muted: "○" };

/** Status = icon + label + color (never color alone). */
export function Pill(props: { status: string; label?: string; title?: string }) {
  const tone = STATUS_TONE[props.status] ?? "muted";
  return (
    <span className={`pill tone-${tone}`} title={props.title}>
      <span aria-hidden="true" className="pill-icon">{STATUS_ICON[tone]}</span>
      {props.label ?? STATUS_LABEL[props.status] ?? props.status.replace(/_/g, " ")}
    </span>
  );
}

export function Stat(props: { label: string; value: ReactNode; sub?: ReactNode; tone?: string; big?: boolean }) {
  return (
    <div className={`stat ${props.big ? "stat-big" : ""}`}>
      <div className="stat-label">{props.label}</div>
      <div className={`stat-value ${props.tone ?? ""}`}>{props.value}</div>
      {props.sub && <div className="stat-sub">{props.sub}</div>}
    </div>
  );
}

/** Seconds remaining in the current 15-minute window, using the server clock. */
export function useWindowCountdown(): { remaining: number; label: string } {
  const { state, skewMs } = useApp();
  const now = useNow(250) + skewMs;
  const remaining = (new Date(state.schedule.current.end).getTime() - now) / 1000;
  return { remaining, label: countdown(remaining) };
}

export function FeedBadge(props: { compact?: boolean }) {
  const { state } = useApp();
  const f = state.feed;
  const label =
    f.state === "ok"
      ? `Live data OK · ${ago(f.last_ok_age_seconds)}`
      : f.state === "error"
        ? "Market data ERROR — fills paused"
        : f.state === "rate_limited"
          ? "Rate limited — waiting"
          : "Connecting…";
  return (
    <span className="feed-badge" title={f.last_error ?? f.base_url}>
      <Pill status={f.state} label={props.compact ? f.state.toUpperCase() : label} />
      {state.preview && <span className="preview-tag">SAMPLE DATA</span>}
    </span>
  );
}

export function KalshiLink(props: { url?: string | null; ticker?: string | null }) {
  if (!props.ticker) return <span className="muted">—</span>;
  if (!props.url) return <code className="ticker">{props.ticker}</code>;
  return (
    <a className="ticker-link" href={props.url} target="_blank" rel="noreferrer noopener" title="Open this exact market on kalshi.com">
      <code className="ticker">{props.ticker}</code> <span aria-hidden="true">↗</span>
    </a>
  );
}

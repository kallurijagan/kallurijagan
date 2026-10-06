import type { Dec } from "./types";

export const num = (v: Dec | number | undefined): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

/** $1,234.56 (negative as -$1.23). Display only — values come pre-computed from the backend. */
export function usd(v: Dec | number | undefined, opts: { sign?: boolean; digits?: number } = {}): string {
  const n = num(v);
  if (n === null) return "—";
  const digits = opts.digits ?? 2;
  const abs = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const sign = n < 0 ? "-" : opts.sign && n > 0 ? "+" : "";
  return `${sign}$${abs}`;
}

/** Contract price shown in cents with up to 2 sub-cent decimals: 0.37 -> 37¢, 0.365 -> 36.5¢ */
export function cents(v: Dec | number | undefined): string {
  const n = num(v);
  if (n === null) return "—";
  const c = Math.round(n * 10000) / 100;
  return `${Number.isInteger(c) ? c.toFixed(0) : c.toString()}¢`;
}

export function dollarsPrice(v: Dec | number | undefined): string {
  const n = num(v);
  if (n === null) return "—";
  return `$${n.toFixed(n * 100 === Math.round(n * 100) ? 2 : 4)}`;
}

export function qty(v: Dec | number | undefined): string {
  const n = num(v);
  if (n === null) return "—";
  return Number.isInteger(n) ? n.toLocaleString("en-US") : n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function pct(v: Dec | number | undefined, digits = 1): string {
  const n = num(v);
  if (n === null) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function fmtTime(iso: string | null | undefined, tz: string, withSeconds = true, withDate = false): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hour: "2-digit",
    minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined,
    month: withDate ? "short" : undefined,
    day: withDate ? "numeric" : undefined,
    hour12: false,
    timeZoneName: "short",
  }).format(d);
}

export function fmtUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function countdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

export function ago(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return "<1s ago";
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

export const OUTCOME_LABEL: Record<string, string> = {
  both_filled: "Both sides filled",
  both_partial: "Both sides, partial",
  up_only: "UP only",
  down_only: "DOWN only",
  no_fill: "No fill",
};

export const STATUS_LABEL: Record<string, string> = {
  pending: "Pending",
  partially_filled: "Partially filled",
  filled: "Filled",
  expired: "Expired",
  canceled: "Canceled",
  settled: "Settled",
  skipped: "Skipped",
  active: "Active",
  awaiting_settlement: "Awaiting settlement",
  abandoned: "Abandoned",
};

/** Convert a wall-clock time in ``tz`` (from <input type=datetime-local>) to a UTC ISO string. */
export function zonedLocalToUtcIso(local: string, tz: string): string | null {
  const m = local.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return null;
  const [y, mo, d, h, mi, s] = m.slice(1).map((x) => (x === undefined ? 0 : Number(x)));
  const asUtc = Date.UTC(y, mo - 1, d, h, mi, s);
  const offsetAt = (ms: number) => {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hour12: false, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(new Date(ms));
    const get = (t: string) => Number(parts.find((p) => p.type === t)?.value);
    const wall = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour") % 24, get("minute"), get("second"));
    return wall - ms;
  };
  let guess = asUtc - offsetAt(asUtc);
  guess = asUtc - offsetAt(guess);
  return new Date(guess).toISOString();
}

export function utcToZonedLocalInput(iso: string, tz: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, hour12: false, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(iso));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "00";
  const hour = get("hour") === "24" ? "00" : get("hour");
  return `${get("year")}-${get("month")}-${get("day")}T${hour}:${get("minute")}:${get("second")}`;
}

export function pnlClass(v: Dec | number | undefined): string {
  const n = num(v);
  if (n === null || n === 0) return "flat";
  return n > 0 ? "pos" : "neg";
}

import { useEffect, useState } from "react";
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  LabelList,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { cents, fmtTime, num, OUTCOME_LABEL, usd } from "../format";
import type { Analytics, EquityPoint, PriceTick } from "../types";

// Validated categorical palette (slots 1-3 pass all-pairs CVD checks in both modes).
const LIGHT = { s1: "#2a78d6", s2: "#eb6834", s3: "#1baf7a", grid: "#e1e0d9", axis: "#c3c2b7", muted: "#898781", ink: "#52514e", surface: "#fcfcfb", pos: "#2a78d6", neg: "#e34948" };
const DARK = { s1: "#3987e5", s2: "#d95926", s3: "#199e70", grid: "#2c2c2a", axis: "#383835", muted: "#898781", ink: "#c3c2b7", surface: "#1a1a19", pos: "#3987e5", neg: "#e66767" };

export function usePrefersDark(): boolean {
  const [dark, setDark] = useState(() => typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const fn = () => setDark(mq.matches);
    mq.addEventListener("change", fn);
    return () => mq.removeEventListener("change", fn);
  }, []);
  return dark;
}

function palette(dark: boolean) {
  return dark ? DARK : LIGHT;
}

interface TipPayload { name?: string; value?: number | string; color?: string; dataKey?: string }

function ChartTip(props: { active?: boolean; payload?: TipPayload[]; label?: string | number; tz: string; fmt: (v: number) => string; dark: boolean }) {
  if (!props.active || !props.payload?.length) return null;
  return (
    <div className={`chart-tip ${props.dark ? "dark" : ""}`}>
      <div className="chart-tip-time">{typeof props.label === "number" ? fmtTime(new Date(props.label).toISOString(), props.tz) : props.label}</div>
      {props.payload.map((p) => (
        <div key={String(p.dataKey)} className="chart-tip-row">
          <span className="swatch" style={{ background: p.color }} aria-hidden="true" />
          <span>{p.name}</span>
          <strong>{p.value === null || p.value === undefined ? "—" : props.fmt(Number(p.value))}</strong>
        </div>
      ))}
    </div>
  );
}

/** Book-derived bid/ask (and API last trade) over time for ONE side, with the limit marked. */
export function PriceChart(props: { ticks: PriceTick[]; side: "UP" | "DOWN"; limit: number; tz: string; dark: boolean; height?: number }) {
  const c = palette(props.dark);
  const up = props.side === "UP";
  const book = props.ticks.filter((t) => t.kind === "book");
  const summary = props.ticks.filter((t) => t.kind === "summary");
  const data = [
    ...book.map((t) => ({ t: new Date(t.ts).getTime(), ask: num(up ? t.yes_ask : t.no_ask), bid: num(up ? t.yes_bid : t.no_bid) })),
    ...summary.map((t) => {
      const lp = num(t.last_price);
      return { t: new Date(t.ts).getTime(), last: lp === null ? null : up ? lp : 1 - lp };
    }),
  ].sort((a, b) => a.t - b.t);
  if (!book.length) return <div className="chart-empty">No order-book observations recorded for this market yet.</div>;
  const vals = data.flatMap((d) => [("ask" in d ? d.ask : null), ("bid" in d ? d.bid : null), ("last" in d ? d.last : null)]).filter((v): v is number => v !== null);
  const span = data.length ? data[data.length - 1].t - data[0].t : 0;
  const lo = Math.max(0, Math.min(props.limit, ...vals) - 0.05);
  const hi = Math.min(1, Math.max(props.limit, ...vals) + 0.05);
  return (
    <ResponsiveContainer width="100%" height={props.height ?? 200}>
      <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={c.grid} vertical={false} />
        <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time" stroke={c.axis} tick={{ fill: c.muted, fontSize: 11 }}
          tickFormatter={(v) => fmtTime(new Date(v).toISOString(), props.tz, span < 900_000).split(" ")[0]} minTickGap={40} />
        <YAxis domain={[lo, hi]} stroke={c.axis} tick={{ fill: c.muted, fontSize: 11 }} tickFormatter={(v) => cents(v)} width={44} />
        <Tooltip content={<ChartTip tz={props.tz} fmt={(v) => cents(v)} dark={props.dark} />} />
        <Legend wrapperStyle={{ fontSize: 12, color: c.ink }} />
        <ReferenceLine y={props.limit} stroke={c.muted} strokeWidth={1} label={{ value: `limit ${cents(props.limit)}`, fill: c.muted, fontSize: 11, position: "insideTopRight" }} />
        <Line name={`${props.side} ask (book, 1 − ${up ? "NO" : "YES"} bid)`} dataKey="ask" stroke={c.s1} strokeWidth={2} dot={false} type="stepAfter" connectNulls isAnimationActive={false} />
        <Line name={`${props.side} bid (book)`} dataKey="bid" stroke={c.s2} strokeWidth={2} dot={false} type="stepAfter" connectNulls isAnimationActive={false} />
        <Line name={up ? "Last trade (API summary)" : "Last trade (API, 1 − YES last)"} dataKey="last" stroke={c.s3} strokeWidth={2} dot={false} type="stepAfter" connectNulls isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Account equity over time (single series — the title names it, no legend box). */
export function EquityChart(props: { points: EquityPoint[]; tz: string; dark: boolean; starting: number; height?: number }) {
  const c = palette(props.dark);
  const data = props.points.map((p) => ({ t: new Date(p.ts).getTime(), equity: num(p.equity) }));
  if (data.length < 2) return <div className="chart-empty">Equity history appears after the engine has run for a few minutes.</div>;
  const vals = data.map((d) => d.equity ?? props.starting);
  const lo = Math.min(props.starting, ...vals);
  const hi = Math.max(props.starting, ...vals);
  const pad = Math.max((hi - lo) * 0.15, 1);
  return (
    <ResponsiveContainer width="100%" height={props.height ?? 240}>
      <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 4 }}>
        <CartesianGrid stroke={c.grid} vertical={false} />
        <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} stroke={c.axis} tick={{ fill: c.muted, fontSize: 11 }}
          tickFormatter={(v) => fmtTime(new Date(v).toISOString(), props.tz, false, true).replace(/ [A-Z]{2,4}$/, "")} minTickGap={60} />
        <YAxis domain={[Math.floor(lo - pad), Math.ceil(hi + pad)]} stroke={c.axis} tick={{ fill: c.muted, fontSize: 11 }} tickFormatter={(v) => usd(v, { digits: 0 })} width={64} />
        <Tooltip content={<ChartTip tz={props.tz} fmt={(v) => usd(v)} dark={props.dark} />} />
        <ReferenceLine y={props.starting} stroke={c.muted} strokeWidth={1} label={{ value: `start ${usd(props.starting, { digits: 0 })}`, fill: c.muted, fontSize: 11, position: "insideBottomRight" }} />
        <Area name="Equity (cash + est. position value)" dataKey="equity" stroke="none" fill={c.s1} fillOpacity={0.1} isAnimationActive={false} />
        <Line name="Equity (cash + est. position value)" dataKey="equity" stroke={c.s1} strokeWidth={2} dot={false} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

/** Net P&L per COMPLETE settled window; color = sign (diverging pair), legend present. */
export function PnlBars(props: { rows: Analytics["per_window"]; tz: string; dark: boolean; height?: number }) {
  const c = palette(props.dark);
  const data = props.rows.slice(-60).map((r) => ({
    label: fmtTime(r.window_start, props.tz, false, true).replace(/ [A-Z]{2,4}$/, ""),
    pnl: num(r.net_pnl) ?? 0,
    outcome: OUTCOME_LABEL[r.outcome_class] ?? r.outcome_class,
    result: r.result,
  }));
  if (!data.length) return <div className="chart-empty">Per-window P&amp;L appears once windows settle.</div>;
  return (
    <ResponsiveContainer width="100%" height={props.height ?? 240}>
      <BarChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 4 }} barCategoryGap={2}>
        <CartesianGrid stroke={c.grid} vertical={false} />
        <XAxis dataKey="label" stroke={c.axis} tick={{ fill: c.muted, fontSize: 11 }} minTickGap={30} />
        <YAxis stroke={c.axis} tick={{ fill: c.muted, fontSize: 11 }} tickFormatter={(v) => usd(v, { digits: 0 })} width={56} />
        <ReferenceLine y={0} stroke={c.axis} />
        <Tooltip
          cursor={{ fill: props.dark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.04)" }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null;
            const p = payload[0].payload as (typeof data)[number];
            return (
              <div className={`chart-tip ${props.dark ? "dark" : ""}`}>
                <div className="chart-tip-time">{p.label}</div>
                <div>{p.outcome} · result {p.result?.toUpperCase() || "—"}</div>
                <strong>{usd(p.pnl, { sign: true })}</strong>
              </div>
            );
          }}
        />
        <Legend
          content={() => (
            <div className="chart-legend">
              <span><span className="legend-swatch" style={{ background: c.pos }} aria-hidden="true" />Profitable window</span>
              <span><span className="legend-swatch" style={{ background: c.neg }} aria-hidden="true" />Losing window</span>
            </div>
          )}
        />
        <Bar dataKey="pnl" name="Net P&L per window" maxBarSize={24} isAnimationActive={false}>
          {data.map((d, i) => (
            <Cell key={i} fill={d.pnl >= 0 ? c.pos : c.neg} radius={(d.pnl >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4]) as unknown as number} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** How often each fill outcome occurred (one series, one color, values at the bar tips). */
export function OutcomeBars(props: { outcomes: Analytics["outcomes"]; dark: boolean }) {
  const c = palette(props.dark);
  const order = ["both_filled", "both_partial", "up_only", "down_only", "no_fill"];
  const data = order.map((k) => ({ name: OUTCOME_LABEL[k], windows: props.outcomes[k]?.windows ?? 0 }));
  if (!data.some((d) => d.windows > 0)) return <div className="chart-empty">No entered windows yet.</div>;
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 40, bottom: 0, left: 8 }}>
        <CartesianGrid stroke={c.grid} horizontal={false} />
        <XAxis type="number" allowDecimals={false} stroke={c.axis} tick={{ fill: c.muted, fontSize: 11 }} />
        <YAxis type="category" dataKey="name" width={130} stroke={c.axis} tick={{ fill: c.ink, fontSize: 12 }} />
        <Tooltip cursor={{ fill: props.dark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.04)" }}
          content={({ active, payload }) => active && payload?.length ? (
            <div className={`chart-tip ${props.dark ? "dark" : ""}`}><strong>{payload[0].payload.name}</strong>: {payload[0].value} window(s)</div>
          ) : null} />
        <Bar dataKey="windows" name="Windows" fill={c.s1} maxBarSize={24} radius={[0, 4, 4, 0]} isAnimationActive={false}>
          <LabelList dataKey="windows" position="right" fill={c.ink} fontSize={12} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

import { Fragment } from "react";
import { usePoll } from "../api";
import { EquityChart, OutcomeBars, PnlBars, usePrefersDark } from "../components/Charts";
import { Card, Stat } from "../components/common";
import { HistoryTabs } from "../components/History";
import { useApp } from "../context";
import { OUTCOME_LABEL, pct, pnlClass, qty, usd, cents } from "../format";
import type { Analytics, EquityPoint } from "../types";
import { FeedAlert } from "./SimpleLayout";
import { WhyPanel } from "../components/WhyPanel";

export function AnalyticsLayout() {
  const { state, tz } = useApp();
  const dark = usePrefersDark();
  const an = usePoll<Analytics>("/api/analytics", 10000);
  const eq = usePoll<{ equity: EquityPoint[] }>("/api/equity?limit=5000", 15000);
  const d = an.data;
  return (
    <div className="layout-analytics">
      <FeedAlert />
      <WhyPanel compact />
      <div className="caveat" role="note">
        <strong>How to read this:</strong> {d?.caveat ?? "Performance is measured per complete market window."} Fills are simulated estimates from
        displayed order-book depth, not exchange executions; fees are estimates.
      </div>
      <section className="tile-row" aria-label="Summary statistics">
        <Stat label="Net P&L (settled windows)" value={d ? usd(d.pnl.total_net, { sign: true }) : "—"} tone={pnlClass(d?.pnl.total_net)} />
        <Stat label="Avg per settled window" value={d?.pnl.average_per_settled_window ? usd(d.pnl.average_per_settled_window, { sign: true }) : "—"} />
        <Stat label="Windows settled" value={d ? qty(d.windows.settled) : "—"} sub={d ? `${d.pnl.profitable_windows} profitable · ${d.pnl.losing_windows} losing · ${d.pnl.flat_windows} flat` : undefined} />
        <Stat label="Est. fees paid" value={d ? usd(d.fees.total) : "—"} sub={d?.fees.average_per_entered_window ? `${usd(d.fees.average_per_entered_window)} per entered window` : undefined} />
        <Stat label="Price improvement vs limit" value={d ? usd(d.fills.price_improvement_total) : "—"} />
        <Stat label="Paper equity" value={usd(state.account.equity)} sub={`Unrealized ${usd(state.account.unrealized_pnl, { sign: true })}`} />
      </section>
      <div className="grid2">
        <Card title="Equity over time">
          <EquityChart points={eq.data?.equity ?? []} tz={tz} dark={dark} starting={Number(state.account.starting_balance)} />
        </Card>
        <Card title="Net P&L per complete window (both legs + fees + settlement)">
          <PnlBars rows={d?.per_window ?? []} tz={tz} dark={dark} />
        </Card>
      </div>
      <div className="grid2">
        <Card title="Fill outcome frequency">
          {d ? (
            <>
              <OutcomeBars outcomes={d.outcomes} dark={dark} />
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Outcome</th><th>Windows</th><th>Share</th><th>Settled</th><th>Net P&amp;L</th><th>Avg / window</th></tr></thead>
                  <tbody>
                    {Object.entries(d.outcomes).map(([k, v]) => (
                      <tr key={k}>
                        <td>{OUTCOME_LABEL[k]}</td>
                        <td>{v.windows}</td>
                        <td>{d.windows.entered ? pct(v.windows / d.windows.entered, 0) : "—"}</td>
                        <td>{v.settled}</td>
                        <td className={pnlClass(v.net_pnl)}>{usd(v.net_pnl, { sign: true })}</td>
                        <td>{v.settled ? usd(Number(v.net_pnl) / v.settled, { sign: true }) : "—"}</td>
                      </tr>
                    ))}
                    <tr className="subtle-row"><td>Windows with a partial fill (any side)</td><td>{d.windows.partial_fill_windows}</td><td colSpan={4}></td></tr>
                  </tbody>
                </table>
              </div>
            </>
          ) : <p className="muted">Loading…</p>}
        </Card>
        <Card title="Fill & schedule statistics">
          {d ? (
            <>
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Side</th><th>Fills</th><th>Contracts filled</th><th>Intended</th><th>Fill rate</th><th>Avg price</th></tr></thead>
                  <tbody>
                    {["UP", "DOWN"].map((s) => {
                      const r = d.fills.by_side[s];
                      return (
                        <tr key={s}><td>{s}</td><td>{r?.fills ?? 0}</td><td>{qty(r?.contracts)}</td><td>{qty(r?.intended_contracts)}</td><td>{pct(r?.fill_rate)}</td><td>{r?.avg_price ? cents(r.avg_price) : "—"}</td></tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <dl className="kv">
                <dt>Windows entered</dt><dd>{d.windows.entered}</dd>
                <dt>Active / awaiting settlement</dt><dd>{d.windows.active} / {d.windows.awaiting_settlement}</dd>
                <dt>Skipped windows</dt><dd>{d.windows.skipped}</dd>
                {Object.entries(d.windows.skip_reasons).map(([k, v]) => (<Fragment key={k}><dt className="indent">{k}</dt><dd>{v}</dd></Fragment>))}
                <dt>Best / worst window</dt><dd>{d.pnl.best_window ? usd(d.pnl.best_window, { sign: true }) : "—"} / {d.pnl.worst_window ? usd(d.pnl.worst_window, { sign: true }) : "—"}</dd>
              </dl>
            </>
          ) : <p className="muted">Loading…</p>}
        </Card>
      </div>
      <Card title="Trade & settlement history">
        <HistoryTabs />
      </Card>
    </div>
  );
}

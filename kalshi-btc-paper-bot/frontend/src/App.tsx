import { useCallback, useEffect, useMemo, useState } from "react";
import { safeStorageGet, safeStorageSet, sendJSON, useNow, usePoll } from "./api";
import { FillDialog, Modal, ResetDialog, SettingsDialog } from "./components/Dialogs";
import { Header } from "./components/Header";
import { LayoutChooser } from "./components/LayoutChooser";
import { PriceCheckPanel } from "./components/PriceCheck";
import { AppCtx, type Actions, type Ctx } from "./context";
import { AnalyticsLayout } from "./layouts/AnalyticsLayout";
import { SimpleLayout } from "./layouts/SimpleLayout";
import { TerminalLayout } from "./layouts/TerminalLayout";
import type { AppState, LayoutId } from "./types";

const LAYOUT_KEY = "kbot.layout";
const LAYOUTS: LayoutId[] = ["simple", "terminal", "analytics"];

function initialLayout(): LayoutId {
  const fromUrl = new URLSearchParams(window.location.search).get("layout");
  if (fromUrl && (LAYOUTS as string[]).includes(fromUrl)) return fromUrl as LayoutId;
  const saved = safeStorageGet(LAYOUT_KEY);
  return saved && (LAYOUTS as string[]).includes(saved) ? (saved as LayoutId) : "simple";
}

type Dialog = null | { kind: "settings" } | { kind: "reset" } | { kind: "layout" } | { kind: "pricecheck" } | { kind: "fill"; id: string };

/** Ticks every second so the age of the shown data keeps counting while the backend is unreachable. */
function StaleBanner(props: { error: string; okAt: number | null }) {
  const now = useNow(1000);
  return (
    <div className="alert alert-bad" role="alert">
      <strong>The local engine is not responding</strong> ({props.error}). Showing data from{" "}
      {props.okAt ? `${Math.round((now - props.okAt) / 1000)}s ago` : "earlier"}. If you clicked inside the start.ps1
      window, press Esc there; otherwise check that start.ps1 is still running.
    </div>
  );
}

export default function App() {
  const { data: state, error, reload, okAt } = usePoll<AppState>("/api/state", 1000);
  const [layout, setLayoutState] = useState<LayoutId>(initialLayout);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [toast, setToast] = useState<{ msg: string; kind: "info" | "error" } | null>(null);
  const [skewMs, setSkewMs] = useState(0);

  useEffect(() => {
    if (state?.server_time) setSkewMs(new Date(state.server_time).getTime() - Date.now());
  }, [state?.server_time]);

  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 5000);
    return () => window.clearTimeout(id);
  }, [toast]);

  useEffect(() => {
    document.documentElement.dataset.layout = layout;
    document.title = state ? `${state.preview ? "PREVIEW · " : ""}PAPER · BTC 15m · ${layout}` : "BTC 15m Paper Bot";
  }, [layout, state]);

  const setLayout = useCallback((id: LayoutId) => {
    setLayoutState(id);
    safeStorageSet(LAYOUT_KEY, id);
    const url = new URL(window.location.href);
    url.searchParams.delete("layout");
    window.history.replaceState(null, "", url.toString());
  }, []);

  const notify = useCallback((msg: string, kind: "info" | "error" = "info") => setToast({ msg, kind }), []);

  const actions: Actions = useMemo(() => ({
    start: async () => {
      try {
        const r = await sendJSON<{ message: string }>("/api/control/start", "POST");
        notify(r.message);
      } catch (e) {
        notify(String(e), "error");
      }
      await reload();
    },
    pause: async () => {
      try {
        const r = await sendJSON<{ message: string; canceled_orders: number }>("/api/control/pause", "POST");
        notify(`${r.message} (${r.canceled_orders} order(s) canceled)`);
      } catch (e) {
        notify(String(e), "error");
      }
      await reload();
    },
    refresh: reload,
    openSettings: () => setDialog({ kind: "settings" }),
    openReset: () => setDialog({ kind: "reset" }),
    openLayoutChooser: () => setDialog({ kind: "layout" }),
    openPriceCheck: () => setDialog({ kind: "pricecheck" }),
    openFill: (id: string) => setDialog({ kind: "fill", id }),
    setLayout,
    notify,
  }), [reload, notify, setLayout]);

  if (!state) {
    return (
      <div className="boot">
        <div className="paper-badge">PAPER TRADING</div>
        <h1>Kalshi BTC 15-minute paper bot</h1>
        {error ? (
          <p className="error-text" role="alert">Cannot reach the local backend: {error}. Is <code>start.ps1</code> running?</p>
        ) : (
          <p className="muted">Connecting to the local engine…</p>
        )}
      </div>
    );
  }

  const ctx: Ctx = { state, tz: state.timezone, skewMs, actions, layout };
  return (
    <AppCtx.Provider value={ctx}>
      <div className={`app app--${layout} ${state.preview ? "is-preview" : ""}`}>
        <Header />
        {error && <StaleBanner error={error} okAt={okAt} />}
        {state.engine.last_tick_error && <div className="alert alert-bad">Engine error: {state.engine.last_tick_error}</div>}
        <main>
          {layout === "simple" && <SimpleLayout />}
          {layout === "terminal" && <TerminalLayout />}
          {layout === "analytics" && <AnalyticsLayout />}
        </main>
        <footer className="app-footer">
          <span className="paper-badge small">PAPER TRADING</span>
          <span>Simulated orders, fills and money. Real Kalshi public market data ({state.feed.source === "preview" ? "PREVIEW: synthetic sample data" : state.feed.base_url}). No real orders are ever sent.</span>
          <span className="muted">v{state.version} · times in {state.timezone}</span>
        </footer>
        {toast && <div className={`toast ${toast.kind}`} role="status">{toast.msg}</div>}
        {dialog?.kind === "layout" && (
          <LayoutChooser current={layout} onPick={(id) => { setLayout(id); setDialog(null); }} onClose={() => setDialog(null)} />
        )}
        {dialog?.kind === "settings" && <SettingsDialog onClose={() => setDialog(null)} />}
        {dialog?.kind === "reset" && <ResetDialog onClose={() => setDialog(null)} />}
        {dialog?.kind === "fill" && <FillDialog fillId={dialog.id} onClose={() => setDialog(null)} />}
        {dialog?.kind === "pricecheck" && (
          <Modal title="Price check — order book vs API summary vs kalshi.com" onClose={() => setDialog(null)} wide>
            <PriceCheckPanel />
          </Modal>
        )}
      </div>
    </AppCtx.Provider>
  );
}

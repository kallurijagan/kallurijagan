import { createContext, useContext } from "react";
import type { AppState, LayoutId } from "./types";

export interface Actions {
  start: () => Promise<void>;
  pause: () => Promise<void>;
  refresh: () => Promise<void>;
  openSettings: () => void;
  openReset: () => void;
  openLayoutChooser: () => void;
  openPriceCheck: () => void;
  openFill: (fillId: string) => void;
  setLayout: (id: LayoutId) => void;
  notify: (msg: string, kind?: "info" | "error") => void;
}

export interface Ctx {
  state: AppState;
  tz: string;
  /** server clock minus browser clock, in ms (countdowns use the server's time). */
  skewMs: number;
  actions: Actions;
  layout: LayoutId;
}

export const AppCtx = createContext<Ctx | null>(null);

export function useApp(): Ctx {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error("AppCtx missing");
  return ctx;
}

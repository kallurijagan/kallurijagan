import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {}

export async function getJSON<T>(url: string, timeoutMs = 5000): Promise<T> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(url, { headers: { Accept: "application/json" }, signal: ctrl.signal });
  } catch (e) {
    throw new ApiError(ctrl.signal.aborted ? `no response from the local engine within ${timeoutMs / 1000}s` : String(e));
  } finally {
    window.clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export async function sendJSON<T>(url: string, method: "POST" | "PUT", body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(typeof data.detail === "string" ? data.detail : `${res.status} ${res.statusText}`);
  return data as T;
}

/** Poll an endpoint; keeps the last good value and reports the latest error separately. */
export function usePoll<T>(url: string | null, intervalMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [okAt, setOkAt] = useState<number | null>(null);
  const urlRef = useRef(url);
  urlRef.current = url;

  const load = useCallback(async () => {
    const u = urlRef.current;
    if (!u) return;
    try {
      const d = await getJSON<T>(u);
      if (urlRef.current === u) {
        setData(d);
        setError(null);
        setOkAt(Date.now());
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (!url) return;
    let alive = true;
    let timer: number | undefined;
    const run = async () => {
      await load();
      if (alive) timer = window.setTimeout(run, intervalMs);
    };
    run();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [url, intervalMs, load]);

  return { data, error, reload: load, okAt };
}

/** Wall clock that ticks, used for countdowns (corrected by the server clock offset). */
export function useNow(tickMs = 500) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), tickMs);
    return () => window.clearInterval(id);
  }, [tickMs]);
  return now;
}

export function safeStorageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function safeStorageSet(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode etc.) — the choice just isn't remembered */
  }
}

import { useCallback, useEffect, useRef, useState } from "react";
import { DEFAULT_BASE_URL, health } from "@/lib/silkscreen/client";

/** Long enough that a status dot is never the reason the engine is busy. */
export const HEALTH_POLL_INTERVAL_MS = 10_000;

export interface EngineHealth {
  /** The URL this result is about, so a caller need not track it separately. */
  baseUrl: string;
  /** True only when `/healthz` answered `ok: true`. Never optimistic. */
  ok: boolean;
  /** Why it is not ok, in the service's own words. Empty when ok. */
  detail: string;
  /** A probe is in flight right now. */
  checking: boolean;
  /** Epoch ms of the last completed probe, or null before the first one. */
  lastCheckedAt: number | null;
  /** Probe now. A no-op while a probe is already in flight. */
  recheck: () => void;
}

/**
 * Poll the engine's `/healthz` on a timer.
 *
 * The engine not running is an ORDINARY state for this app — Kaleo is a
 * desktop app and the Python service is a separate process the user starts —
 * so this never throws and never surfaces as an error. `client.health()`
 * already returns its reason instead of raising; this hook only adds the
 * timer, the in-flight guard and the unmount guard.
 *
 * Polling during a run is safe: the service is a `ThreadingHTTPServer`, so a
 * `/healthz` GET does not queue behind a streaming `/generate/stream`.
 */
export interface EngineHealthOptions {
  /**
   * Called once each time the engine comes back: a probe answered ok after
   * an earlier probe had not. The very first probe never fires it — "up at
   * launch" is not a recovery — and neither does a URL change, since a new
   * address starts its own history.
   */
  onRecovered?: (baseUrl: string) => void;
}

export function useEngineHealth(
  baseUrl: string = DEFAULT_BASE_URL,
  intervalMs: number = HEALTH_POLL_INTERVAL_MS,
  token: string = "",
  options: EngineHealthOptions = {}
): EngineHealth {
  const [state, setState] = useState<{
    ok: boolean;
    detail: string;
    lastCheckedAt: number | null;
  }>({ ok: false, detail: "", lastCheckedAt: null });
  const [checking, setChecking] = useState(false);

  const mountedRef = useRef(true);
  // Only one probe at a time, so a fast `recheck()` finger cannot stack them.
  const inFlightRef = useRef(false);
  // The previous probe's answer for THIS url, or null before its first one;
  // the false→true edge is what `onRecovered` reports. A ref rather than
  // state so the callback identity never changes with it.
  const lastOkRef = useRef<{ baseUrl: string; ok: boolean } | null>(null);
  const onRecoveredRef = useRef(options.onRecovered);
  onRecoveredRef.current = options.onRecovered;

  const record = useCallback(
    (ok: boolean) => {
      const previous = lastOkRef.current;
      lastOkRef.current = { baseUrl, ok };
      if (ok && previous && previous.baseUrl === baseUrl && !previous.ok) {
        try {
          onRecoveredRef.current?.(baseUrl);
        } catch (error) {
          console.warn("[kaleo engine] onRecovered threw:", error);
        }
      }
    },
    [baseUrl]
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const probe = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    if (mountedRef.current) setChecking(true);
    try {
      const result = await health(baseUrl, token);
      if (!mountedRef.current) return;
      record(result.ok);
      setState({
        ok: result.ok,
        detail: result.detail,
        lastCheckedAt: Date.now(),
      });
    } catch (error) {
      // `health()` is documented not to throw; if that ever changes, a status
      // dot is still not worth an unhandled rejection.
      if (!mountedRef.current) return;
      record(false);
      setState({
        ok: false,
        detail: (error as Error)?.message ?? "unreachable",
        lastCheckedAt: Date.now(),
      });
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setChecking(false);
    }
  }, [baseUrl, token, record]);

  useEffect(() => {
    void probe();
    const timer = window.setInterval(() => void probe(), intervalMs);
    return () => window.clearInterval(timer);
  }, [probe, intervalMs]);

  const recheck = useCallback(() => {
    void probe();
  }, [probe]);

  return {
    baseUrl,
    ok: state.ok,
    detail: state.detail,
    checking,
    lastCheckedAt: state.lastCheckedAt,
    recheck,
  };
}

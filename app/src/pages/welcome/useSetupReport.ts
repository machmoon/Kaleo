import { useCallback, useEffect, useRef, useState } from "react";
import { SilkscreenError } from "@/lib/silkscreen/client";
import { fetchSetup, type SetupReport } from "@/lib/setup/service";

export interface SetupReportState {
  report: SetupReport | null;
  /** The engine could not be asked; the cards say so and stay skippable. */
  down: boolean;
  /** A reachable engine refused or answered badly, in its own words. */
  error: string;
  loading: boolean;
  refresh: () => Promise<SetupReport | null>;
}

/**
 * `GET /setup`, once on mount and on demand.
 *
 * The wizard never simulates a connection itself: what the cards show is
 * what the engine said, and when the engine is down every card says so
 * rather than pretending a state.
 */
export function useSetupReport(baseUrl: string, token: string): SetupReportState {
  const [report, setReport] = useState<SetupReport | null>(null);
  const [down, setDown] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async (): Promise<SetupReport | null> => {
    setLoading(true);
    try {
      const next = await fetchSetup(baseUrl, token);
      if (!mounted.current) return next;
      setReport(next);
      setDown(false);
      setError("");
      return next;
    } catch (err) {
      if (!mounted.current) return null;
      const kind = err instanceof SilkscreenError ? err.kind : "server";
      setDown(kind === "offline" || kind === "timeout");
      setError(kind === "offline" || kind === "timeout" ? "" : (err as Error).message);
      return null;
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [baseUrl, token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { report, down, error, loading, refresh };
}

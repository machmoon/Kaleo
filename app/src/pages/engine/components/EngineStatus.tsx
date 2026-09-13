import { useRef } from "react";
import { useEngineHealth, type EngineHealth } from "@/hooks";
import { cn } from "@/lib/utils";

/**
 * How long ago, in words, without pulling in a date library.
 */
function ago(timestamp: number): string {
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

export interface EngineStatusProps {
  health: EngineHealth;
  /** Hide the text label — the dot alone, for a cramped floating bar. */
  compact?: boolean;
  /** Show the failure reason and the last-probe time under the label. */
  showDetail?: boolean;
  className?: string;
}

/**
 * The engine's liveness in a dot and a word.
 *
 * Presentational on purpose: the caller owns the `useEngineHealth` poll so that
 * putting this in the floating bar and the dashboard header at the same time
 * does not double the probe rate.
 *
 * "Unreachable" and "never connected" are deliberately different labels. The
 * engine is a separate process the user starts by hand, so the overwhelmingly
 * common case is that it was never running — telling that user the connection
 * was "lost" sends them looking for a crash that never happened.
 */
export const EngineStatus = ({
  health,
  compact = false,
  showDetail = false,
  className,
}: EngineStatusProps) => {
  const { ok, detail, checking, lastCheckedAt } = health;

  // Whether this app has ever seen the engine answer, for as long as the view
  // has been mounted. Not persisted: a remembered success from a previous
  // launch says nothing about the process running right now.
  const everConnected = useRef(false);
  if (ok) everConnected.current = true;

  // Probed at least once and never answered — a first-run state, not a fault.
  const neverConnected = !ok && !everConnected.current;

  const state = ok
    ? "connected"
    : checking && !everConnected.current
      ? "checking"
      : neverConnected
        ? "never"
        : "unreachable";

  const label = {
    connected: "Engine connected",
    checking: "Checking engine…",
    never: "Engine not connected",
    unreachable: "Engine unreachable",
  }[state];

  const dotClass = {
    connected: "bg-chart-2",
    checking: "bg-muted-foreground animate-pulse",
    never: "bg-muted-foreground",
    unreachable: "bg-destructive",
  }[state];

  return (
    <div
      data-testid="engine-status"
      data-state={state}
      title={
        ok
          ? "The engine answered /healthz with ok: true"
          : detail
            ? `/healthz: ${detail}`
            : label
      }
      className={cn("flex items-center gap-2 select-none", className)}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-2 shrink-0 rounded-full transition-colors",
          dotClass,
          // A probe in flight over a known state pulses without losing the
          // colour, so the dot never flickers back to grey mid-run.
          checking && everConnected.current ? "animate-pulse" : ""
        )}
      />
      {compact ? (
        <span className="sr-only">{label}</span>
      ) : (
        <div className="flex flex-col leading-tight">
          <span className="text-xs lg:text-sm font-medium">{label}</span>
          {showDetail && (
            <span className="text-[10px] lg:text-xs text-muted-foreground">
              {state === "never"
                ? detail
                  ? `No successful connection yet — ${detail}`
                  : "No successful connection yet"
                : state === "unreachable"
                  ? detail || "stopped answering"
                  : lastCheckedAt
                    ? `Checked ${ago(lastCheckedAt)}`
                    : "Not checked yet"}
            </span>
          )}
        </div>
      )}
    </div>
  );
};

export interface EngineStatusLiveProps
  extends Omit<EngineStatusProps, "health"> {
  baseUrl: string;
}

/**
 * `EngineStatus` with its own poll, for places that have no health state to
 * hand it — the floating bar, say, which is a different React tree from the
 * dashboard.
 */
export const EngineStatusLive = ({
  baseUrl,
  ...rest
}: EngineStatusLiveProps) => {
  const health = useEngineHealth(baseUrl);
  return <EngineStatus health={health} {...rest} />;
};

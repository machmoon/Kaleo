import { createContext, useContext, type ReactNode } from "react";
import {
  useSilkscreenRunState,
  type SilkscreenRun,
  type UseSilkscreenRunOptions,
} from "@/hooks/useSilkscreenRun";

export type {
  RunHistoryEntry,
  RunRequestDraft,
  RunStatus,
  SilkscreenRun,
} from "@/hooks/useSilkscreenRun";
export {
  DEFAULT_HISTORY_LIMIT,
  DEFAULT_TIME_LIMIT_S,
  EMPTY_REQUEST,
} from "@/hooks/useSilkscreenRun";

const RunContext = createContext<SilkscreenRun | undefined>(undefined);

export interface RunProviderProps extends UseSilkscreenRunOptions {
  children: ReactNode;
}

/**
 * The single owner of "what is the engine doing right now".
 *
 * There is exactly one of these, mounted above everything that renders a board.
 * That is not tidiness — it is the money rule: the state machine holds the
 * only `AbortController` and the only in-flight guard, and a second provider
 * would be a second way to bill the user for one prompt. It also means the
 * `/healthz` poll happens once for the whole app rather than once per status
 * dot.
 */
export const RunProvider = ({ children, ...options }: RunProviderProps) => {
  const value = useSilkscreenRunState(options);
  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
};

export const useSilkscreenRun = (): SilkscreenRun => {
  const context = useContext(RunContext);

  if (!context) {
    throw new Error("useSilkscreenRun must be used within a RunProvider");
  }

  return context;
};

/** Short alias. Same hook, same single provider — nothing here is a second one. */
export const useRun = useSilkscreenRun;

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { SETUP_VERSION, getSetting, setSetting } from "@/lib/settings/store";
import {
  INITIAL_STATE,
  hydrate,
  needsSetup as needsSetupFrom,
  progress,
  reduce,
  remaining,
  type SetupAction,
  type SetupCardId,
  type SetupState,
} from "@/lib/setup/machine";

/** What the Rust side says on `kaleo-setup-changed` (contract C3). */
interface SetupChanged {
  completed?: boolean;
  skipped?: string[];
}

export interface UseSetup {
  state: SetupState;
  /** False once the store says the wizard is over; the page then renders nothing. */
  needsSetup: boolean;
  index: number;
  total: number;
  dispatch: (action: SetupAction) => void;
  /** Mark a card connected/enabled (or not), for the skipped list. */
  setCard: (card: SetupCardId, done: boolean) => void;
  /** `invoke("setup_finish")` with the cards still undone, then mark the store. */
  finish: () => Promise<void>;
}

function readState(): SetupState {
  try {
    return hydrate({
      step: getSetting("setup.step"),
      skipped: getSetting("setup.skipped"),
      remaining: getSetting("setup.remaining"),
    });
  } catch {
    return INITIAL_STATE;
  }
}

function readCompleted(): boolean {
  try {
    return getSetting("setup.completed") === true;
  } catch {
    return false;
  }
}

/**
 * The wizard's state, persisted after every reduce.
 *
 * `setup.step` is the resume point, `setup.skipped` the cards walked past
 * and `setup.remaining` the cards a closed window would leave undone — the
 * Rust close-mid-setup path copies the last into the middle. Persistence is
 * fire-and-forget: the store mirrors to localStorage synchronously and the
 * disk write trails by its autosave.
 */
export function useSetup(): UseSetup {
  const [state, setState] = useState<SetupState>(readState);
  const [completed, setCompleted] = useState<boolean>(readCompleted);
  const stateRef = useRef(state);
  stateRef.current = state;

  const persist = useCallback((next: SetupState) => {
    void setSetting("setup.step", next.step);
    void setSetting("setup.skipped", next.skipped);
    void setSetting("setup.remaining", remaining(next));
  }, []);

  const dispatch = useCallback(
    (action: SetupAction) => {
      const next = reduce(stateRef.current, action);
      if (next === stateRef.current) return;
      stateRef.current = next;
      setState(next);
      persist(next);
    },
    [persist],
  );

  const setCard = useCallback(
    (card: SetupCardId, done: boolean) => dispatch({ type: done ? "complete" : "uncomplete", card }),
    [dispatch],
  );

  // Re-hydrate when the shell finishes setup for us (window closed mid-way).
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    listen<SetupChanged>("kaleo-setup-changed", (event) => {
      if (event.payload?.completed) setCompleted(true);
      const next = readState();
      stateRef.current = next;
      setState(next);
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => {
        // Outside Tauri (tests, a browser tab) there is no event bus.
      });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  const finish = useCallback(async () => {
    const skipped = Array.from(new Set([...stateRef.current.skipped, ...remaining(stateRef.current)]));
    try {
      await invoke("setup_finish", { skipped });
    } catch (error) {
      // No shell (a browser tab, a test): the store is still the gate the
      // next launch reads, so mark it here rather than looping the wizard.
      console.warn("[kaleo setup] setup_finish:", error);
    }
    void setSetting("setup.skipped", skipped);
    void setSetting("setup.remaining", []);
    void setSetting("setup.completed", true);
    void setSetting("setup.version", SETUP_VERSION);
    void setSetting("setup.completedAt", Date.now());
    setCompleted(true);
  }, []);

  const { index, total } = useMemo(() => progress(state), [state]);

  return {
    state,
    needsSetup: needsSetupFrom({ completed }),
    index,
    total,
    dispatch,
    setCard,
    finish,
  };
}

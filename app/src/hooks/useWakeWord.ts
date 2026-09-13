import { useCallback, useEffect, useRef, useState } from "react";
import {
  clampWakeBudget,
  CONTINUATION_MS,
  createLocalListener,
  createWakeWordListener,
  describeWakeBackend,
  DEFAULT_WINDOW_CAP,
  type WakeBackendName,
  type WakeDetection,
  type WakeGlobals,
  type WakeWordListener,
} from "@/lib/wake-word";
import { ensureCaptureAccess } from "@/lib/capture-permission";
import { clearMicLevel, publishMicLevel } from "@/hooks/useMicLevel";
import { STORAGE_KEYS } from "@/config";
import { getCustomizableState, updateWakeWord } from "@/lib/storage/customizable.storage";
import {
  debugTriggerLocalWake,
  localWakeStatus,
  startLocalWake,
  stopLocalWake,
  subscribeLocalWake,
  type LocalWakeStatus,
} from "@/lib/local-wake";

/** How long the ear keeps showing what it heard before it goes back to listening. */
export const HEARD_FLASH_MS = 5_000;

export interface UseWakeWordOptions {
  baseUrl: string;
  token?: string;
  /** The overlay is hidden by the global shortcut; a hidden overlay never listens. */
  hidden?: boolean;
  /** One detection: the spoken words after the wake, raw — no desk prefix. */
  onWake: (utterance: string) => void;
  /** Test seams: the listener factory and the webview globals it inspects. */
  create?: typeof createWakeWordListener;
  globals?: WakeGlobals;
  /** Subscribe to `ada-wake` only (tests inject). */
  subscribeLocal?: typeof subscribeLocalWake;
  /** Probe Rust `wake_status`. Tests inject; default is a Tauri invoke. */
  probeLocal?: () => Promise<LocalWakeStatus>;
  startLocal?: typeof startLocalWake;
  /**
   * Drop the Rust mic. Whatever it resolves to is discarded, so the type is
   * `Promise<unknown>` rather than `typeof stopLocalWake`: the narrower type
   * rejected the obvious test double (one that hands back a status object
   * like its sibling `startLocal` does) for a value nothing reads.
   */
  stopLocal?: () => Promise<unknown>;
  /**
   * Keep the Gemini / VAD ear armed after a wake, until the listening budget
   * is spent or the ear is muted. On by default: an ear that stops after one
   * hit is not a wake word, and the budget — visible, and never spent while
   * muted — is what makes staying armed honest rather than open-ended.
   */
  idleGeminiWake?: boolean;
  /** Model calls one unmute may spend. Defaults to DEFAULT_WINDOW_CAP. */
  budget?: number;
  /**
   * Use the on-device spotter when `wake_status` says it is available.
   *
   * On by default, and a switch rather than a hard preference because
   * `wake_status` reports on a *file*, not on a model that works: it answers
   * `available` whenever a keyword file is on disk, so a failed training run
   * (recall 0.000 at every checkpoint, accuracy at chance) still turns the
   * ear green, opens the microphone and then never fires. That is worse
   * than having no spotter, because the ear claims to be listening. Until
   * `wake_status` can tell a working model from a file, turning this off is
   * how the page falls straight through to the recorded-window path, which
   * costs money per utterance but does fire.
   */
  preferLocal?: boolean;
}

export interface WakeWord {
  /** The persisted switch. Off by default; Settings and the ear share it. */
  enabled: boolean;
  setEnabled: (enabled: boolean) => void;
  /** The microphone is open and a detector is running. */
  listening: boolean;
  /** What followed the last wake word (empty when only the name was said). */
  lastHeard: string | null;
  /** Last engine transcript while listening (even when it was not a wake). */
  lastGlimpse: string | null;
  /** True for HEARD_FLASH_MS after a wake, so the ear can show it. */
  justHeard: boolean;
  /** Plain-language failure; null unless the last arming failed or was refused. */
  error: string | null;
  /** The listener's own last word on its state ("stopped after 15 windows…"). */
  detail: string;
  backend: WakeBackendName | null;
  /**
   * `windows` backend only: model calls spent since this unmute, and the
   * budget they are spent against. These survive re-arming — the ear keeps
   * listening across wakes, so a counter that reset per arming would show a
   * cost the session is not paying.
   */
  sent: number;
  cap: number;
  /** Calls left in the budget; zero means the ear stopped and said so. */
  remaining: number;
  /** True when listening stopped because the budget ran out, not by choice. */
  budgetSpent: boolean;
  /** Raise the budget for this session (clamped). Spends nothing by itself. */
  setBudget: (calls: number) => void;
  /**
   * Give the ear another full budget and start listening again — the honest
   * answer to "I ran out": more listening is a decision, taken here.
   */
  listenAgain: () => void;
  start: () => void;
  stop: () => void;
  /**
   * Mock/dev only: fire `ada-wake` without speaking (`KALEO_WAKE_MOCK=1`).
   * No-op when the Rust command is missing.
   */
  debugTrigger: () => void;
}

function readEnabled(): boolean {
  try {
    return getCustomizableState().wakeWord.isEnabled;
  } catch {
    return false;
  }
}

/**
 * The ear: one wake-word detector, owned here, and never two.
 *
 * The switch is persisted, off by default, and shared with Settings: either
 * surface may flip it. Unmuted, the ear stays armed across wakes and quiet
 * spells — that is what a wake word is — and what bounds it is a visible
 * budget of model calls, not a cap of one listen: silence spends nothing,
 * so the only thing that costs is something actually being said. Spending
 * the budget stops the ear and says so (`budgetSpent`), and `listenAgain`
 * is how more listening is asked for. A failure switches the ear off
 * visibly, and the next click is the user's decision again.
 *
 * `preferLocal` picks the free on-device spotter when Rust reports one; see
 * the option, and note that "reports one" currently means "a keyword file
 * exists", not "a model that fires".
 */
export function useWakeWord({
  baseUrl,
  token,
  hidden = false,
  onWake,
  create = createWakeWordListener,
  globals,
  subscribeLocal = subscribeLocalWake,
  probeLocal = localWakeStatus,
  startLocal = startLocalWake,
  stopLocal = stopLocalWake,
  idleGeminiWake = true,
  budget = DEFAULT_WINDOW_CAP,
  preferLocal = true,
}: UseWakeWordOptions): WakeWord {
  const [enabled, setEnabledState] = useState<boolean>(readEnabled);
  const [listening, setListening] = useState(false);
  const [lastHeard, setLastHeard] = useState<string | null>(null);
  const [lastGlimpse, setLastGlimpse] = useState<string | null>(null);
  const [justHeard, setJustHeard] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState("");
  const [backend, setBackend] = useState<WakeBackendName | null>(null);
  const [sent, setSent] = useState(0);
  const [cap, setCap] = useState(() => clampWakeBudget(budget));
  const [budgetSpent, setBudgetSpent] = useState(false);
  // Bumped after a wake so the arming effect runs again with the switch on.
  const [arming, setArming] = useState(0);

  const listenerRef = useRef<WakeWordListener | null>(null);
  const mountedRef = useRef(true);
  const onWakeRef = useRef(onWake);
  onWakeRef.current = onWake;
  const heardTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Bumped by stop() so an in-flight mic-permission wait cannot arm after. */
  const startGenRef = useRef(0);
  /** Until this time, the next speech window needs no wake word. */
  const continuationUntilRef = useRef(0);
  /** Fires when that window closes, so the ear can say the name is needed again. */
  const continuationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** After a local hit with no command, take one Gemini clip — not another spot. */
  const commandClipRef = useRef(false);
  /**
   * Calls spent since the last unmute. Kept in a ref as well as in state
   * because every re-arming reads it synchronously to tell the listener where
   * the budget stands; a stale read would hand back calls already paid for.
   */
  const spentRef = useRef(0);
  const capRef = useRef(clampWakeBudget(budget));
  capRef.current = cap;

  /** Close the follow-up window and say so, rather than going quiet. */
  const endContinuation = useCallback((why: string) => {
    continuationUntilRef.current = 0;
    if (continuationTimerRef.current) {
      clearTimeout(continuationTimerRef.current);
      continuationTimerRef.current = null;
    }
    if (why && mountedRef.current) setDetail(why);
  }, []);

  const openContinuation = useCallback(() => {
    continuationUntilRef.current = Date.now() + CONTINUATION_MS;
    if (continuationTimerRef.current) clearTimeout(continuationTimerRef.current);
    continuationTimerRef.current = setTimeout(() => {
      continuationTimerRef.current = null;
      continuationUntilRef.current = 0;
      if (mountedRef.current) {
        setDetail("you didn’t say anything, so I’m back to waiting for “Hey Ada”");
      }
    }, CONTINUATION_MS);
  }, []);

  const setEnabled = useCallback((next: boolean) => {
    setEnabledState(next);
    if (next) {
      // A prior failure must not stick on the tooltip after the user asks
      // again — otherwise the ear looks broken when it is only retrying.
      setError(null);
      // Unmuting on a spent budget is the user asking for another one; that
      // is the only thing that resets the counter, and it is always a click.
      if (spentRef.current >= capRef.current) {
        spentRef.current = 0;
        setSent(0);
      }
      setBudgetSpent(false);
    }
    try {
      updateWakeWord(next);
    } catch {
      // The switch still works for this session.
    }
  }, []);

  // Settings lives in the dashboard webview; the ear lives in the overlay.
  // localStorage writes from one window arrive here as `storage` events.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEYS.CUSTOMIZABLE) return;
      const next = readEnabled();
      setEnabledState((prev) => (prev === next ? prev : next));
      if (next) setError(null);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const stop = useCallback(() => {
    startGenRef.current += 1;
    const current = listenerRef.current;
    listenerRef.current = null;
    current?.stop();
    // The microphone is closed, so the level is gone — not zero, gone. Said
    // here as well as in the listener because this path also covers a
    // listener that was let go rather than stopped.
    clearMicLevel();
    if (mountedRef.current) setListening(false);
  }, []);

  const start = useCallback(() => {
    if (listenerRef.current) return;
    setError(null);
    const gen = startGenRef.current;
    void (async () => {
      const captureCommand = commandClipRef.current;
      commandClipRef.current = false;
      // Prefer on-device wake whenever Rust says it is ready — even if a
      // test injected `create` (that factory is for the post-wake command
      // clip / Gemini fallback, not for skipping a working spotter).
      if (!captureCommand && preferLocal) {
        const status = await probeLocal();
        if (!mountedRef.current || gen !== startGenRef.current || listenerRef.current) return;
        if (status.available) {
          const mine = createLocalListener(
            {
              start: async () => {
                await startLocal();
              },
              stop: async () => {
                await stopLocal().catch(() => undefined);
              },
              subscribe: subscribeLocal,
            },
            {
              onState: (state, text) => {
                if (!mountedRef.current) return;
                setDetail(text);
                setListening(state === "listening");
                if (state === "error") {
                  if (listenerRef.current === mine) listenerRef.current = null;
                  setError(text);
                  setEnabled(false);
                }
              },
              onWake: (detection: WakeDetection) => {
                if (listenerRef.current === mine) listenerRef.current = null;
                if (!mountedRef.current) return;
                setListening(false);
                const spoken = detection.utterance.trim();
                setLastHeard(spoken);
                setJustHeard(true);
                if (heardTimerRef.current) clearTimeout(heardTimerRef.current);
                heardTimerRef.current = setTimeout(() => {
                  heardTimerRef.current = null;
                  if (mountedRef.current) setJustHeard(false);
                }, HEARD_FLASH_MS);
                // Rust does not record the command. An empty hit means
                // "Hey Ada" only — open one Gemini clip for the rest, then
                // re-arm the on-device spotter. Do not route an empty string.
                if (!spoken) {
                  commandClipRef.current = true;
                  openContinuation();
                  setArming((n) => n + 1);
                  return;
                }
                onWakeRef.current(spoken);
                setArming((n) => n + 1);
              },
            }
          );
          listenerRef.current = mine;
          setBackend("local");
          try {
            await mine.start();
          } catch (err: unknown) {
            if (listenerRef.current === mine) listenerRef.current = null;
            if (!mountedRef.current) return;
            setListening(false);
            setError((err as Error)?.message || "could not start on-device wake");
            setEnabled(false);
          }
          return;
        }
      }

      try {
        await ensureCaptureAccess();
      } catch (err: unknown) {
        if (!mountedRef.current || gen !== startGenRef.current) return;
        setListening(false);
        setError((err as Error)?.message || "could not use the microphone");
        setEnabled(false);
        return;
      }
      if (!mountedRef.current || gen !== startGenRef.current || listenerRef.current) return;

      const mine = create(
        {
          baseUrl,
          token,
          globals,
          cap: capRef.current,
          // The budget belongs to the unmute, not to one arming: a re-armed
          // listener continues the count rather than handing back calls.
          spent: spentRef.current,
          // Only inside a follow-up window does speech without the name
          // count as the command. Outside one, "Ada" is required — an ear
          // that acts on any sentence in the room is not a wake word.
          continuing: () => Date.now() < continuationUntilRef.current,
        },
        {
          onState: (state, text) => {
            if (!mountedRef.current) return;
            setDetail(text);
            if (state === "listening") {
              setListening(true);
              return;
            }
            setListening(false);
            if (state === "error") {
              if (listenerRef.current === mine) listenerRef.current = null;
              setError(text);
              setEnabled(false);
            } else if (state === "capped") {
              // The microphone is already released; the last paid window is
              // still settling, so the listener is let go rather than stopped
              // (stopping would abort what was just paid for). The ear does
              // not go quiet here: `budgetSpent` is the state the control
              // shows, and it says the listening ran out rather than that
              // nothing happened.
              if (listenerRef.current === mine) listenerRef.current = null;
              setBudgetSpent(true);
              endContinuation("");
              setEnabled(false);
            }
          },
          onWindow: (count, ceiling) => {
            if (!mountedRef.current) return;
            spentRef.current = count;
            setSent(count);
            setCap(ceiling);
          },
          onGlimpse: (text) => {
            if (!mountedRef.current) return;
            setLastGlimpse(text);
          },
          // The room level goes onto a module-level bus rather than into
          // React state on purpose. It arrives ten times a second, and a
          // state field here would re-render the whole overlay at 10 Hz to
          // move one circle; anything that wants to draw it subscribes with
          // `useMicLevel` and re-renders only itself. The bus is also where
          // the honesty lives: it reports *no signal* when nothing is
          // measuring, which is a different answer from a level of zero.
          onLevel: (peak) => {
            if (peak === null || !mountedRef.current || listenerRef.current !== mine) {
              clearMicLevel();
              return;
            }
            publishMicLevel(peak);
          },
          onWake: (detection: WakeDetection) => {
            // The listener has already stopped itself.
            if (listenerRef.current === mine) listenerRef.current = null;
            if (!mountedRef.current) return;
            setListening(false);
            setLastHeard(detection.utterance);
            setJustHeard(true);
            if (heardTimerRef.current) clearTimeout(heardTimerRef.current);
            heardTimerRef.current = setTimeout(() => {
              heardTimerRef.current = null;
              if (mountedRef.current) setJustHeard(false);
            }, HEARD_FLASH_MS);
            // Bare name → keep listening for the rest without “Ada” again.
            // Unless the clip was never gated: a bare “Ada” from an unmeasured
            // window is the one case where a real detection and the model
            // hallucinating the primed name over silence look identical, and a
            // continuation window spends further paid clips on the guess.
            const ungatedBareName =
              !detection.utterance.trim() && detection.gated === false;
            if (!detection.utterance.trim() && !ungatedBareName) {
              // The name alone, from a window a gate confirmed: the rest of
              // the sentence is still coming, so accept it without the name.
              openContinuation();
            } else {
              // A wake that carried its command needs no follow-up window —
              // "Hey Ada, make me an LDO" arrives in one clip and
              // `matchWakeWord` already handed back the tail.
              endContinuation("");
            }
            // Raw transcript only. Desk snaps travel beside the utterance
            // on the page (ada-path); a forged “[desk:]” prefix must not.
            onWakeRef.current(detection.utterance);
            // Staying armed is what makes this a wake word rather than a
            // one-shot listen — bounded by the budget, which the control
            // shows and which silence never spends.
            if (ungatedBareName) {
              // Nothing was heard that a gate could confirm; re-arming here
              // would spend the next clip on a name the model may have
              // invented over silence.
              setEnabled(false);
            } else if (spentRef.current >= capRef.current) {
              setBudgetSpent(true);
              setEnabled(false);
            } else if (captureCommand) {
              setArming((n) => n + 1);
            } else if (detection.backend === "windows" && !idleGeminiWake) {
              setEnabled(false);
            } else {
              setArming((n) => n + 1);
            }
          },
        }
      );
      if (!mine) {
        setBackend(null);
        setError(describeWakeBackend(null));
        setEnabled(false);
        return;
      }
      listenerRef.current = mine;
      setBackend(mine.backend);
      // The running total, not zero: this arming continues one unmute's
      // budget, and resetting the number here would under-report the cost.
      setSent(spentRef.current);
      setLastGlimpse(null);
      mine.start().catch((err: unknown) => {
        if (listenerRef.current === mine) listenerRef.current = null;
        if (!mountedRef.current) return;
        setListening(false);
        setError((err as Error)?.message || "could not start listening");
        setEnabled(false);
      });
    })();
  }, [
    baseUrl,
    token,
    create,
    globals,
    setEnabled,
    subscribeLocal,
    probeLocal,
    startLocal,
    stopLocal,
    idleGeminiWake,
    preferLocal,
    openContinuation,
    endContinuation,
  ]);

  const setBudget = useCallback((calls: number) => {
    const next = clampWakeBudget(calls);
    capRef.current = next;
    setCap(next);
    if (spentRef.current < next) setBudgetSpent(false);
  }, []);

  /** Another full budget, and start listening again. Always a decision. */
  const listenAgain = useCallback(() => {
    spentRef.current = 0;
    setSent(0);
    setBudgetSpent(false);
    setEnabled(true);
  }, [setEnabled]);

  useEffect(() => {
    if (!enabled || hidden) {
      stop();
      return;
    }
    start();
    return () => stop();
  }, [enabled, hidden, arming, start, stop]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (heardTimerRef.current) clearTimeout(heardTimerRef.current);
      if (continuationTimerRef.current) clearTimeout(continuationTimerRef.current);
      const current = listenerRef.current;
      listenerRef.current = null;
      current?.stop();
    };
  }, []);

  return {
    enabled,
    setEnabled,
    listening,
    lastHeard,
    lastGlimpse,
    justHeard,
    error,
    detail,
    backend,
    sent,
    cap,
    remaining: Math.max(0, cap - sent),
    budgetSpent,
    setBudget,
    listenAgain,
    start,
    stop,
    debugTrigger: () => {
      void debugTriggerLocalWake().catch(() => undefined);
    },
  };
}

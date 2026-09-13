// The words on the folded pill.
//
// The pill used to be three icons — a dot, a mic, an arrow — and Pat's verdict
// was that it was too small to be worth folding to: it could not answer the
// two questions you fold the strip down and still need answered, which are
// "is the microphone hot, and what is it billing me" and "is anything
// happening". So the pill carries a word for each. That is also what lets it
// stay small honestly: the one thing on a folded strip that can spend money is
// the ear, and the ear's word spells out what it is spending.
//
// Both words are measurements or states, never a mood. Neither invents a
// number: the paid-window count is the listener's own `sent`/`cap`.

import { WAKE_WORD } from "@/lib/wake-word";
import type { WakeBackendName } from "@/lib/wake-word";

/** Just enough of `WakeWord` to say what the ear is doing and what it costs. */
export interface EarWordInput {
  enabled: boolean;
  listening: boolean;
  justHeard: boolean;
  lastHeard: string | null;
  error: string | null;
  backend: WakeBackendName | null;
  sent: number;
  cap: number;
}

export interface RunWordInput {
  /** The live run's status; anything but `idle` has something on screen. */
  status: string;
  /** The step flow's status. */
  stepsStatus: string;
  /** `/healthz` answered ok on the last probe. */
  engineOk: boolean;
  /** False until the first probe lands: "I do not know yet" is its own answer. */
  engineSettled: boolean;
}

/**
 * What the ear is doing, as a phrase.
 *
 * `windows` is the paid backend — every 4 s window is one model call — so its
 * phrase carries the running bill. `speech` and `local` cost nothing and say
 * which recognizer they are, because "listening" alone would let a free listen
 * and a paid one look identical.
 */
export function earWord(wake: EarWordInput | null | undefined): string {
  if (!wake) return `${WAKE_WORD} off`;
  if (wake.justHeard) {
    return wake.lastHeard
      ? `heard “${WAKE_WORD}, ${wake.lastHeard}”`
      : `heard “${WAKE_WORD}” — tell me what you need`;
  }
  if (wake.listening) {
    if (wake.backend === "windows") {
      return `Listening for “${WAKE_WORD}” · ${wake.sent}/${wake.cap} paid windows`;
    }
    if (wake.backend === "local") {
      return `Listening for “${WAKE_WORD}” · on-device`;
    }
    return `Listening for “${WAKE_WORD}” · OS`;
  }
  if (wake.enabled) return `Opening the mic for one listen`;
  if (wake.error) return `${WAKE_WORD} off — ${wake.error}`;
  return `${WAKE_WORD} off`;
}

/**
 * What the run is doing, as a phrase.
 *
 * The engine's state outranks the run's: a strip folded down beside a dead
 * engine would otherwise read "Nothing running", which is true and useless.
 * And "Nothing running" is printed rather than left blank, because a blank
 * space is the state a folded overlay is accused of being in when a paid run
 * finished invisibly.
 */
export function runWord(input: RunWordInput): string {
  if (!input.engineSettled) return "Checking the engine";
  if (!input.engineOk) return "Engine down";
  if (input.status === "running" || input.stepsStatus === "running") return "Working";
  if (input.stepsStatus === "waiting") return "Waiting for you";
  if (input.stepsStatus === "error" || input.status === "error") return "A stage failed";
  if (input.status === "done" || input.stepsStatus === "done") return "Run finished";
  return "Nothing running";
}

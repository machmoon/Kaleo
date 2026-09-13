/**
 * What the page does with a wake-word detection, as data.
 *
 * "Ada, make me a 3.3 V LDO board" and "Ada, route it again" arrive through
 * the same ear, and the difference between them is a paid board and a reply
 * to the run already open. The decision lives here, pure, so the page cannot
 * quietly grow a path where a sentence spoken at the wrong moment starts a
 * second run.
 */

export interface WakeFlowInput {
  /** What followed the wake word; empty means the engineer only said "Ada". */
  utterance: string;
  /** A run is in flight in either state machine. */
  busy: boolean;
  /** The step-by-step flow's status. */
  stepsStatus: "idle" | "running" | "waiting" | "done" | "error";
}

export type WakeAction =
  /**
   * The sentence cannot be acted on and is dropped. This is no longer the
   * answer to "you spoke while a run was in flight" — that is a `command`
   * now, which the page parks. It survives for the deictic case in
   * `ada-path.ts`, where there is genuinely nothing to point at yet.
   */
  | { kind: "ignore"; reason: string }
  /** A step run is open: the sentence is a reply to it (approve, restart). */
  | { kind: "command"; text: string }
  /** Nothing is open: the sentence is the intent of a new board. */
  | { kind: "start"; intent: string }
  /** Only the name was said: open the bar and wait for the sentence. */
  | { kind: "listen" };

export function wakeAction(input: WakeFlowInput): WakeAction {
  const text = input.utterance.trim();
  if (input.busy || input.stepsStatus === "running") {
    // Speaking while a run is in flight used to be dropped with "say it again
    // when this run is done", which is the app refusing a sentence it heard
    // perfectly well. It is a command now: the page parks it and says what it
    // will do with it when the stage lands. Nothing about that spends money —
    // a parked sentence still has to be confirmed — so the guard that
    // mattered (one paid run at a time) is unchanged.
    return { kind: "command", text };
  }
  if (
    input.stepsStatus === "waiting" ||
    input.stepsStatus === "done" ||
    input.stepsStatus === "error"
  ) {
    // Even an empty utterance is a reply here: the interpreter answers it
    // with the list of what could be said, which is the right prompt.
    return { kind: "command", text };
  }
  if (!text) return { kind: "listen" };
  return { kind: "start", intent: text };
}

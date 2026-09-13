/**
 * Whether the overlay shows the full bar or the idle pill.
 *
 * Idle, the overlay is a small pill: engine dot, a mic, an arrow. That is
 * the junior hardware engineer at the next bench, between tasks: present,
 * not in the way. Everything else (the text field, options, progress, results) appears
 * only when there is a reason for it. The reasons are listed here, as data,
 * so the page cannot quietly grow a fourth one that hides a paid run behind
 * a collapsed pill.
 */

export interface OverlayModeInput {
  /** The user opened the bar with the arrow (or a transcript landed). */
  expanded: boolean;
  /** A run is in flight, in either state machine. */
  busy: boolean;
  /** The step-by-step flow is open (waiting, running, error…). */
  stepsActive: boolean;
  /** The live run's status. Anything but idle has something to show. */
  status: string;
}

/** Why the overlay is expanded; `null` means it is the idle pill. */
export type OverlayReason = "user" | "busy" | "steps" | "result";

export function overlayReason(input: OverlayModeInput): OverlayReason | null {
  // A run in flight or a finished one must never sit behind the pill: a
  // collapsed overlay during a paid run is the "felt like nothing happened"
  // bug in a new coat.
  if (input.busy) return "busy";
  // An explicit collapse wins over steps and results. It cannot win over
  // `busy`, because a run in flight owns the cancel button. Everything that
  // needs an answer calls `setExpanded(true)` on the way in, so the panel
  // still comes back on its own -- what changed is that the engineer can put
  // it away in between, which is the difference between a control strip and
  // a window that will not close.
  if (!input.expanded) return null;
  if (input.stepsActive) return "steps";
  if (input.status !== "idle") return "result";
  // A typed draft does not pin the bar open: closing it keeps the text in
  // state, and the arrow brings it back. The arrow has to win, or the one
  // way to tidy the screen would be to delete what you wrote.
  if (input.expanded) return "user";
  return null;
}

export function isOverlayExpanded(input: OverlayModeInput): boolean {
  return overlayReason(input) !== null;
}

/**
 * What the strip carries where the text field normally sits.
 *
 * Listening used to unfold the whole overlay, which is the wrong shape for
 * "I am listening": the strip grew, the window moved under the cursor, and
 * the field it opened was one nobody was going to type in. The founder's
 * model instead: the strip stays exactly the size it is and swaps what is
 * inside it — the field steps out of the way for the listening state, and
 * comes back with whatever was already typed still in it.
 *
 * `micOpen` must be the microphone, never the switch. A bar that showed the
 * listening state while the mic was still opening would be the same lie the
 * green dot told, one control to the left.
 */
export type BarContent = "field" | "listening";

export interface BarContentInput {
  /** The microphone is actually open (see micIsOpen), not merely unmuted. */
  micOpen: boolean;
  /**
   * I am talking. The speech duck closes the microphone while I speak, so
   * `micOpen` goes false for the length of every reply — and without this
   * the field would drop back into the strip mid-sentence and out again.
   * The swap holds; what changes is the sentence (see `listeningLine`),
   * because a bar that said "I'm listening" here would be the microphone
   * lie one state over.
   */
  speaking?: boolean;
  /** The overlay is hidden by the global shortcut; it claims nothing then. */
  hidden?: boolean;
}

export function barContent(input: BarContentInput): BarContent {
  if (input.hidden) return "field";
  return input.micOpen || input.speaking === true ? "listening" : "field";
}

// The first-launch Setup Assistant, as a pure step machine.
//
// One decision per screen, Continue/Back, "Set Up Later" everywhere — the
// macOS Setup Assistant shape. The machine is deliberately DOM-free and
// store-free: `useSetup` persists whatever `reduce` returns, so a reload
// resumes where the user left off, and the tests here can walk every path
// without a window.
//
// Two vocabularies, kept apart on purpose:
//   - a *step* is a screen (`SETUP_STEPS`, one dot each);
//   - a *card* is a thing the user can connect or enable on a screen
//     (`SETUP_CARDS`). `setup.skipped` and `setup.remaining` record cards,
//     not steps — "Skipped: Google, Billing" is a sentence about cards, and
//     the Rust close-mid-setup path copies `remaining` into `skipped` so a
//     closed window is honest about what never got connected.

export const SETUP_STEPS = [
  "hello",
  "appearance",
  "engine",
  "accounts",
  "permissions",
  "done",
] as const;
export type SetupStepId = (typeof SETUP_STEPS)[number];

export const SETUP_CARDS = [
  "kicad",
  "google",
  "stripe",
  "microsoft",
  "notifications",
  "voice",
] as const;
export type SetupCardId = (typeof SETUP_CARDS)[number];

/** Which cards live on which screen. Screens without cards map to `[]`. */
export const CARDS_BY_STEP: Record<SetupStepId, readonly SetupCardId[]> = {
  hello: [],
  appearance: [],
  // KiCad is the canvas this whole product sits on, so the screen that asks
  // "is the outside world ready" asks about both, not only the engine.
  engine: ["kicad"],
  accounts: ["google", "stripe", "microsoft"],
  permissions: ["notifications", "voice"],
  done: [],
};

/** The human name of a card, for the "Skipped: …" line. */
export const CARD_NAMES: Record<SetupCardId, string> = {
  kicad: "KiCad",
  google: "Google",
  stripe: "Billing",
  microsoft: "Microsoft",
  notifications: "Notifications",
  voice: "Hey Ada",
};

export interface SetupState {
  step: SetupStepId;
  /** Cards the user actually connected or switched on. */
  completed: SetupCardId[];
  /** Cards the user walked past. Recomputed when they walk back. */
  skipped: SetupCardId[];
  /**
   * Set by `exit` (Shift+Esc, confirmed) — the wizard is over without the
   * done screen. `useSetup` answers it by calling `setup_finish`.
   */
  finished: boolean;
}

export type SetupAction =
  | { type: "continue" }
  | { type: "back" }
  /**
   * "Set Up Later": leave this screen without doing it — its cards are
   * recorded as skipped and the wizard moves on one step, never to the end.
   * Ending the whole wizard is `exit`, behind its own confirm.
   */
  | { type: "skip" }
  /** Shift+Esc, confirmed: skip everything left and end the wizard now. */
  | { type: "exit" }
  | { type: "jump"; step: SetupStepId }
  /**
   * Advance only if the wizard is still on `from`. An engine probe that
   * answers after the user already pressed Continue must not push them two
   * screens ahead.
   */
  | { type: "autoAdvance"; from: SetupStepId }
  | { type: "complete"; card: SetupCardId }
  | { type: "uncomplete"; card: SetupCardId };

export const INITIAL_STATE: SetupState = {
  step: "hello",
  completed: [],
  skipped: [],
  finished: false,
};

export function isSetupStepId(value: unknown): value is SetupStepId {
  return typeof value === "string" && (SETUP_STEPS as readonly string[]).includes(value);
}

export function isSetupCardId(value: unknown): value is SetupCardId {
  return typeof value === "string" && (SETUP_CARDS as readonly string[]).includes(value);
}

/** Zero-based index of a step, for dots and the aria-live sentence. */
export function stepIndex(step: SetupStepId): number {
  return SETUP_STEPS.indexOf(step);
}

/**
 * The steps that actually draw a dot. `hello` hides the row and `done` has no
 * footer at all, so counting them promises six screens and then lights the
 * second dot on the first screen anyone sees a counter on.
 *
 * The rule is OpenWhispr's, in `vendor/openwhispr/src/components/onboarding/flow.ts`:
 * `COMPACT_STEPS` are excluded from the count outright, and its comment states
 * the reason we hit here — "landing on `languages` reads as '1 of N', not
 * '3 of N' for two steps the user never saw a counter on". `stepIndex` still
 * runs on the full `SETUP_STEPS`, because ordering and counting are different
 * questions.
 */
export const DOTTED_STEPS: readonly SetupStepId[] = SETUP_STEPS.filter(
  (step) => step !== "hello" && step !== "done",
);

export function progress(state: SetupState): { index: number; total: number } {
  const index = DOTTED_STEPS.indexOf(state.step);
  return { index: index === -1 ? 0 : index, total: DOTTED_STEPS.length };
}

/**
 * Cards not yet connected and not yet skipped — what a window closed right
 * now would leave undone. In catalogue order so the sentence built from it
 * reads the same every time.
 */
export function remaining(state: SetupState): SetupCardId[] {
  return SETUP_CARDS.filter(
    (card) => !state.completed.includes(card) && !state.skipped.includes(card),
  );
}

function uniq(cards: readonly SetupCardId[]): SetupCardId[] {
  return SETUP_CARDS.filter((card) => cards.includes(card));
}

/** Leaving `step` forward: its unconnected cards become skipped. */
function skipCardsOf(state: SetupState, step: SetupStepId): SetupCardId[] {
  const passed = CARDS_BY_STEP[step].filter((card) => !state.completed.includes(card));
  return uniq([...state.skipped, ...passed]);
}

/** Arriving at `step` again: its cards are back in play. */
function unskipCardsOf(state: SetupState, step: SetupStepId): SetupCardId[] {
  const cards = CARDS_BY_STEP[step];
  return state.skipped.filter((card) => !cards.includes(card));
}

function withSkippedRemaining(state: SetupState): SetupCardId[] {
  return uniq([...state.skipped, ...remaining(state)]);
}

export function reduce(state: SetupState, action: SetupAction): SetupState {
  const index = stepIndex(state.step);
  switch (action.type) {
    case "continue": {
      if (state.step === "done") return state;
      const next = SETUP_STEPS[index + 1];
      return { ...state, step: next, skipped: skipCardsOf(state, state.step) };
    }
    case "autoAdvance": {
      if (state.step !== action.from) return state;
      return reduce(state, { type: "continue" });
    }
    case "back": {
      if (index === 0) return state;
      const prev = SETUP_STEPS[index - 1];
      return { ...state, step: prev, skipped: unskipCardsOf(state, prev) };
    }
    case "jump": {
      return { ...state, step: action.step, skipped: unskipCardsOf(state, action.step) };
    }
    case "skip": {
      return reduce(state, { type: "continue" });
    }
    case "exit": {
      return {
        ...state,
        step: "done",
        skipped: withSkippedRemaining(state),
        finished: true,
      };
    }
    case "complete": {
      if (state.completed.includes(action.card)) return state;
      return {
        ...state,
        completed: uniq([...state.completed, action.card]),
        skipped: state.skipped.filter((card) => card !== action.card),
      };
    }
    case "uncomplete": {
      if (!state.completed.includes(action.card)) return state;
      return { ...state, completed: state.completed.filter((card) => card !== action.card) };
    }
  }
}

/**
 * Whether the wizard should run at all. The Rust gate reads the same key;
 * this is the JS side of the same question so `useSetup` can render nothing
 * (rather than a half-wizard) when the store already says finished.
 */
export function needsSetup(stored: { completed?: unknown } | null | undefined): boolean {
  return stored?.completed !== true;
}

/** Rebuild a state from what the store held. Anything unreadable falls back. */
export function hydrate(stored: {
  step?: unknown;
  skipped?: unknown;
  remaining?: unknown;
}): SetupState {
  const step = isSetupStepId(stored.step) ? stored.step : INITIAL_STATE.step;
  const skipped = Array.isArray(stored.skipped)
    ? uniq(stored.skipped.filter(isSetupCardId))
    : [];
  // `remaining` is derived, but a stored copy tells us which cards were
  // completed: everything neither remaining nor skipped. Except when both
  // lists are empty — that is the store's *default* (a fresh install, before
  // the first reduce ever persisted), and reading it as "all five connected"
  // would hand `setup_finish` nothing to skip. The one real state it also
  // describes, every card connected and none skipped, loses only the skipped
  // list it would not have had anyway.
  const rem = Array.isArray(stored.remaining)
    ? stored.remaining.filter(isSetupCardId)
    : null;
  const completed =
    rem === null || (rem.length === 0 && skipped.length === 0)
      ? []
      : SETUP_CARDS.filter((card) => !rem.includes(card) && !skipped.includes(card));
  return { step, completed, skipped, finished: false };
}

/** The "Skipped: Google, Billing." sentence, or "" when nothing was. */
export function skippedSentence(skipped: readonly SetupCardId[]): string {
  const names = uniq(skipped).map((card) => CARD_NAMES[card]);
  if (!names.length) return "";
  return `Skipped: ${names.join(", ")}. Find them in Settings.`;
}

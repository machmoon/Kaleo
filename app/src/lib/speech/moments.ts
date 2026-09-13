// What gets said, at the four moments where a spoken reply is worth hearing.
//
// The rule this module is written against is subtraction, not addition: a
// voice that narrates every event is muted within a minute and then it is
// worse than silence, because the mute takes the useful lines with it. So a
// moment earns a sentence only when it is a *turn in a conversation* — a
// point where the machine has stopped and the human is expected to answer:
//
//  1. a stage finished and is waiting to be approved (I stopped; your move);
//  2. a stage failed or the run refused (you asked; here is why not);
//  3. I understood a spoken command and want it confirmed (did I hear you?);
//  4. I could not make sense of what was said (say one of these instead).
//
// Deliberately NOT spoken: a stage starting, progress ticks, activity
// frames, history browsing, anything the eye already has on the strip.
//
// Pure functions, no DOM and no storage, for the same reason `summarize.ts`
// is: what this app says out loud is reviewable in two files.

import type { StepName, StepResponse } from "@/lib/silkscreen/types";

/** The stages, named for the ear rather than for the rail's badge. */
const SPOKEN_STAGE: Record<StepName, string> = {
  propose: "the schematic",
  place: "the placement",
  route: "the copper",
  review: "the review",
  sourcing: "the parts list",
  order: "the fab package",
  case: "the case",
};

/** What approving each stage does, as the sentence that asks for it. */
const SPOKEN_ACTION: Record<StepName, string> = {
  propose: "draft the schematic",
  place: "place the parts",
  route: "route the copper",
  review: "review it",
  sourcing: "source the parts",
  order: "prepare the fab package",
  case: "design the case",
};

/** One word that selects each stage out loud, from the command vocabulary. */
const SPOKEN_WORD: Record<StepName, string> = {
  propose: "schematic",
  place: "place it",
  route: "route it",
  review: "review it",
  sourcing: "source it",
  order: "order it",
  case: "case",
};

export function stageNoun(step: StepName): string {
  return SPOKEN_STAGE[step] ?? "that stage";
}

export function stageAction(step: StepName): string {
  return SPOKEN_ACTION[step] ?? "run it";
}

/** Cap a machine error so a stack trace cannot become a minute of speech. */
export function shorten(text: string, max = 180): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return `${flat.slice(0, max - 1).trimEnd()}…`;
}

/**
 * A stage landed and nothing else runs until a human says so.
 *
 * The two facts that matter to someone not looking at the screen: what is
 * done, and where it is — "in KiCad" is the whole reason to look up. Where
 * the bridge failed the sentence says so rather than sending them to an
 * application that has nothing new in it.
 */
export function stageReadyLine(
  latest: StepResponse,
  available: readonly StepName[]
): string {
  if (available.length === 0) {
    return "Every stage has run. Nothing was ordered.";
  }
  const noun = stageNoun(latest.step);
  const head = noun.charAt(0).toUpperCase() + noun.slice(1);
  const shownInKicad = latest.shown_in_kicad === true;
  const where =
    latest.step === "case"
      ? " It is a STEP file beside the board."
      : latest.step === "review" || latest.step === "sourcing" || latest.step === "order"
        ? " It is here on the strip."
        : shownInKicad
          ? " It is open in KiCad."
          : " I could not open it in KiCad; it is on disk.";
  const next = available[0];
  return `${head} is done.${where} Say “${SPOKEN_WORD[next]}” when you have looked, and I will ask you to confirm.`;
}

/** A stage failed. Named, with the engine's own reason, and nothing invented. */
export function stageFailedLine(
  step: StepName | null,
  message: string | null | undefined
): string {
  const what = step ? stageNoun(step) : "that stage";
  const why = shorten(message ?? "");
  return why ? `I could not finish ${what}. ${why}` : `I could not finish ${what}.`;
}

/**
 * I heard something while a run was in flight and dropped it.
 *
 * Spoken because the alternative is what the founder already met once: they
 * talk, nothing happens, and there is no way to tell being ignored from not
 * being heard.
 */
export function refusalLine(reason: string): string {
  const why = shorten(reason, 80);
  return why
    ? `I heard you, but ${why}. Say it again when this run is done.`
    : "I heard you, but I cannot act on it right now.";
}

/** I understood a spoken command; a human still has to commit it. */
export function armedLine(step: StepName | "restart"): string {
  if (step === "restart") return "Start over, and throw this run away? Confirm it.";
  return `${stageAction(step)}? Confirm it.`;
}

/** I did not understand; the answer is the list of what I would understand. */
export function unknownCommandLine(available: readonly StepName[]): string {
  if (available.length === 0) {
    return "I did not follow that. No stage is waiting — say “start over” to begin a new board.";
  }
  const options = available.map((step) => `“${SPOKEN_WORD[step]}”`);
  const list =
    options.length === 1
      ? options[0]
      : `${options.slice(0, -1).join(", ")} or ${options[options.length - 1]}`;
  return `I did not follow that. You can say ${list}, or “start over”.`;
}

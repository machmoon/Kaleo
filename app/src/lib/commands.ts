// The ⌘K vocabulary, as a list the menu can draw.
//
// A typed sentence the interpreter does not recognise used to go nowhere:
// `interpretCommand` answered `unknown` and the engineer was left guessing
// which words this app knows. The menu is the answer, so it must be built
// from the interpreter's own table — `COMMAND_VOCABULARY` in
// `silkscreen/steps.ts` — and never from a second list that could drift.
//
// Two rules decide what is in the list at all:
//   - Only what may actually run right now is here. An impossible command is
//     absent, not greyed: a disabled row still reads as an offer.
//   - Everything that costs money carries its price, and a stage the engine
//     already started costs `0 calls` and says why.
//
// Pure and DOM-free; the menu that draws it has nothing to test but layout.

import {
  COMMAND_VOCABULARY,
  STEP_DESCRIPTORS,
  WHERE_LABEL,
  phrasesFor,
} from "./silkscreen/steps";
import type { StepName, StepResponse } from "./silkscreen/types";

/** The three headings, in the order the menu draws them. */
export const COMMAND_GROUPS = ["Next", "This run", "Replay"] as const;

export type CommandGroup = (typeof COMMAND_GROUPS)[number];

export interface CommandItem {
  /** Stable identity — what the page switches on. Never an array index. */
  id: string;
  group: CommandGroup;
  label: string;
  /** The half-sentence under the label: where it runs, or what it costs you. */
  detail: string | null;
  /** `1 call` / `0 calls`, or null for something that spends nothing. */
  cost: string | null;
  /** True when running this makes a paid engine call. */
  paid: boolean;
  /** True when this drops work already paid for: red, and it always arms. */
  danger: boolean;
  /** True when `↵` arms it for a human to confirm rather than doing it. */
  arms: boolean;
  key: string | null;
  step?: StepName;
  /** The file this item acts on, for Reveal and Open. */
  path?: string;
  /**
   * The sentences `interpretCommand` accepts for this item, so the menu can
   * show the words as well as the button. Empty for an item the interpreter
   * has no phrase for — those are menu-only, and the menu says nothing about
   * typing them.
   */
  phrases: readonly string[];
}

/** Everything the list needs to know about the run it is offered for. */
export interface CommandContext {
  status: "idle" | "running" | "waiting" | "done" | "error";
  /** The engine's own `next`: the only steps that may be approved. */
  available: readonly StepName[];
  /** Steps the engine started by itself — approving one collects a result. */
  background: readonly StepName[];
  history: readonly StepResponse[];
  /** A run loaded from a recording: nothing on it may spend. */
  replayed?: boolean;
  /**
   * Whether this build can open a recorded run. False today — no replay
   * exists — and the group is then absent rather than offered and dead.
   */
  canReplay?: boolean;
}

/** The last path component, for either separator. */
export function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/**
 * Which file "Reveal" opens the folder of: the most finished artifact this
 * run produced, in the order a finished run produces them.
 */
const REVEAL_PREFERENCE = [
  "order",
  "board",
  "placed_board",
  "bom",
  "case",
  "schematic",
  "project",
] as const;

function revealTarget(files: Readonly<Record<string, string>>): string | null {
  for (const key of REVEAL_PREFERENCE) {
    const value = files[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

/**
 * Every command that may run right now, grouped.
 *
 * `Next` is empty while a step is in flight and while a recorded run is on
 * screen: in the first case the engine is busy, in the second there is
 * nothing to spend, and in both an item would be a lie.
 */
export function commandItems(context: CommandContext): CommandItem[] {
  const { status, available, background, history } = context;
  const replayed = context.replayed === true;
  const latest = history[history.length - 1];
  const files: Readonly<Record<string, string>> = latest?.files ?? {};
  const items: CommandItem[] = [];

  if (!replayed && status !== "running") {
    for (const step of available) {
      const descriptor = STEP_DESCRIPTORS[step];
      const started = background.includes(step);
      items.push({
        id: `step:${step}`,
        group: "Next",
        label: descriptor.action,
        detail: started
          ? "already started, collects the result"
          : `in ${WHERE_LABEL[descriptor.where]}`,
        cost: started ? "0 calls" : "1 call",
        paid: !started,
        danger: false,
        arms: true,
        key: "↵",
        step,
        phrases: phrasesFor({ kind: "approve", step }),
      });
    }
  }

  const reveal = revealTarget(files);
  if (reveal) {
    items.push({
      id: "reveal",
      group: "This run",
      label: `Reveal ${basename(reveal)}`,
      detail: "Finder",
      cost: null,
      paid: false,
      danger: false,
      arms: false,
      key: "R",
      path: reveal,
      phrases: [],
    });
  }

  const board = files.board ?? files.placed_board ?? null;
  if (board) {
    items.push({
      id: "open",
      group: "This run",
      label: "Open the board file",
      // The app hands the file to the OS; it cannot promise which program
      // answers, so it does not say "pcbnew".
      detail: "whichever program owns .kicad_pcb",
      cost: null,
      paid: false,
      danger: false,
      arms: false,
      key: "O",
      path: board,
      phrases: [],
    });
  }

  if (status === "running") {
    items.push({
      id: "cancel",
      group: "This run",
      label: "Cancel",
      detail: "stops waiting; the engine may finish, nothing more is charged",
      cost: null,
      paid: false,
      danger: false,
      arms: false,
      key: "esc",
      phrases: [],
    });
  } else if (status === "waiting" || status === "error" || status === "done") {
    items.push({
      id: "stop",
      group: "This run",
      label: "Stop here",
      detail: "spends nothing, files stay",
      cost: null,
      paid: false,
      danger: false,
      arms: false,
      key: "esc",
      phrases: [],
    });
  }

  if (!replayed && history.length > 0) {
    const n = history.length;
    items.push({
      id: "restart",
      group: "This run",
      label: "Start over",
      detail: `drops ${n} paid stage${n === 1 ? "" : "s"}, asks first`,
      cost: null,
      paid: false,
      danger: true,
      // Never immediate. It spends nothing, but it throws away a run that
      // money was already spent on.
      arms: true,
      key: "⇧R",
      phrases: phrasesFor({ kind: "restart" }),
    });
  }

  if (context.canReplay) {
    items.push({
      id: "replay",
      group: "Replay",
      label: "Replay a recorded run…",
      detail: "nothing is spent",
      cost: "0 calls",
      paid: false,
      danger: false,
      arms: false,
      key: "⌘⇧R",
      phrases: [],
    });
  }

  return items;
}

export interface CommandMatch {
  item: CommandItem;
  /** Where the query matched the label, for the accent highlight; null when it matched elsewhere. */
  range: [number, number] | null;
}

/**
 * How well a query matched, lowest first. The order is the one a person
 * expects: the thing whose name starts with what they typed, then a word
 * inside it, then anywhere in the name, then the spoken phrases, then the
 * detail line.
 */
function score(item: CommandItem, query: string): { rank: number; range: [number, number] | null } | null {
  const label = item.label.toLowerCase();
  if (label.startsWith(query)) return { rank: 0, range: [0, query.length] };
  for (let i = 0; i < label.length; i++) {
    if (i > 0 && /[a-z0-9]/.test(label[i - 1])) continue;
    if (label.startsWith(query, i)) return { rank: 1, range: [i, i + query.length] };
  }
  const at = label.indexOf(query);
  if (at >= 0) return { rank: 2, range: [at, at + query.length] };
  if (item.phrases.some((phrase) => phrase.includes(query))) return { rank: 3, range: null };
  if ((item.detail ?? "").toLowerCase().includes(query)) return { rank: 4, range: null };
  return null;
}

/**
 * The list filtered by what was typed. An empty query is everything, in the
 * order `commandItems` built it; otherwise best match first, ties broken by
 * that same order so the list never reshuffles under a stationary cursor.
 */
export function filterCommands(
  items: readonly CommandItem[],
  query: string
): CommandMatch[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return items.map((item) => ({ item, range: null }));
  const scored: { match: CommandMatch; rank: number; index: number }[] = [];
  items.forEach((item, index) => {
    const hit = score(item, needle);
    if (!hit) return;
    scored.push({ match: { item, range: hit.range }, rank: hit.rank, index });
  });
  scored.sort((a, b) => a.rank - b.rank || a.index - b.index);
  return scored.map((entry) => entry.match);
}

/** The vocabulary size, so a test can prove the menu covers all of it. */
export const VOCABULARY_SIZE = COMMAND_VOCABULARY.length;

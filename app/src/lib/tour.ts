// The 60-second tour, Apple "Tips" style. Pure state; `Tour.tsx` draws it.
//
// Three parts, each honest about where it can point:
//   1. one caption in the strip itself, pushed through the overlay's existing
//      `commandNote` path over the storage bridge (`TOUR_CAPTION_KEY`). The
//      strip is a 58 px non-activating panel — a coach mark cannot be drawn
//      over it from the dashboard, so the strip says its own sentence;
//   2. at most two Tips cards in the dashboard (`TOUR_STOPS`), each with a
//      *declarative* target — a `data-testid` plus identity attrs, never an
//      index — resolved at render time. A target that is missing or has no
//      box degrades to a caption-only card rather than a spotlight on nothing.
//
// State lives in localStorage under `TOUR_STORAGE_KEY` so both webviews read
// the same answer; the writer is told through a same-window listener
// (`storage` only fires elsewhere), the other window through the event.

import { safeLocalStorage } from "@/lib/storage/helper";

export const TOUR_STORAGE_KEY = "silkscreen_tour";
/** The storage-bridge key the strip reads for its one caption. */
export const TOUR_CAPTION_KEY = "tour.caption";
export const TOUR_CAPTION = "This is the field. Type a board, press Run. Steps land in KiCad.";

export type TourReason = "first-run" | "settings";
export type TourEnd = "completed" | "skipped" | "dismissed";

export type TourState =
  | { kind: "idle" }
  | { kind: "offered"; reason: TourReason }
  | { kind: "active"; index: number; reason: TourReason }
  | { kind: "done"; end: TourEnd };

export interface TourTarget {
  testid: string;
  /** Identity attributes (`data-ref`, `data-id`), never a position. */
  attrs?: Record<string, string>;
  /** A testid the target must sit inside. */
  within?: string;
}

export interface TourStop {
  id: string;
  /** `null` is a caption-only card by design, not a missing target. */
  target: TourTarget | null;
  /** Where the card lives; the tour navigates there when it is elsewhere. */
  route?: string;
  title: string;
  body: string;
}

export const TOUR_STOPS: readonly TourStop[] = [
  {
    id: "steps",
    target: null,
    route: "/workbench",
    title: "Steps land in KiCad",
    body:
      "Each step Hardy finishes — place, route, review — is written into the open KiCad project. Press a step in the strip and look at KiCad, not here.",
  },
  {
    id: "integrations",
    target: { testid: "integration-group" },
    route: "/integrations",
    title: "Integrations",
    body:
      "Google, Microsoft and Stripe are set up here. Ready means configured, not proven against a live account.",
  },
];

const IDLE: TourState = { kind: "idle" };

const listeners = new Set<(state: TourState) => void>();

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parse(raw: string | null): TourState {
  if (!raw) return IDLE;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value)) return IDLE;
    switch (value.kind) {
      case "offered":
        return { kind: "offered", reason: value.reason === "settings" ? "settings" : "first-run" };
      case "active": {
        const index = Number(value.index);
        if (!Number.isInteger(index) || index < 0 || index >= TOUR_STOPS.length) return IDLE;
        return {
          kind: "active",
          index,
          reason: value.reason === "settings" ? "settings" : "first-run",
        };
      }
      case "done":
        return {
          kind: "done",
          end:
            value.end === "completed" || value.end === "skipped" || value.end === "dismissed"
              ? value.end
              : "dismissed",
        };
      default:
        return IDLE;
    }
  } catch {
    return IDLE;
  }
}

export function readTour(): TourState {
  return parse(safeLocalStorage.getItem(TOUR_STORAGE_KEY));
}

function write(state: TourState): TourState {
  safeLocalStorage.setItem(TOUR_STORAGE_KEY, JSON.stringify(state));
  for (const cb of listeners) cb(state);
  return state;
}

/** Hear every change, from this window or the other one. Returns unsubscribe. */
export function subscribeTour(cb: (state: TourState) => void): () => void {
  listeners.add(cb);
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === TOUR_STORAGE_KEY) cb(readTour());
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

/** The consent gate: nothing is drawn until this is answered. */
export function offerTour(reason: TourReason = "settings"): TourState {
  return write({ kind: "offered", reason });
}

export function acceptTour(reason: TourReason = "first-run"): TourState {
  return write({ kind: "active", index: 0, reason });
}

/**
 * "Take the tour": the strip's caption goes out first, then the first Tips
 * card. Pressing the button *is* the consent, so this skips `offered`.
 */
export function startTour(reason: TourReason = "first-run"): TourState {
  safeLocalStorage.setItem(TOUR_CAPTION_KEY, TOUR_CAPTION);
  return acceptTour(reason);
}

export function nextStop(): TourState {
  const state = readTour();
  if (state.kind !== "active") return state;
  const index = state.index + 1;
  if (index >= TOUR_STOPS.length) return finishTour("completed");
  return write({ kind: "active", index, reason: state.reason });
}

export function finishTour(end: TourEnd): TourState {
  safeLocalStorage.removeItem(TOUR_CAPTION_KEY);
  return write({ kind: "done", end });
}

export function dismissTour(): TourState {
  return finishTour("dismissed");
}

export function skipTour(): TourState {
  return finishTour("skipped");
}

/** Forget the tour ran, so Settings can offer it again. */
export function resetTour(): TourState {
  safeLocalStorage.removeItem(TOUR_CAPTION_KEY);
  safeLocalStorage.removeItem(TOUR_STORAGE_KEY);
  for (const cb of listeners) cb(IDLE);
  return IDLE;
}

export function currentStop(state: TourState): TourStop | null {
  return state.kind === "active" ? (TOUR_STOPS[state.index] ?? null) : null;
}

/** Quote a value for an attribute selector: backslashes and quotes escaped. */
function quoteAttr(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

/**
 * The CSS selector for a target. Identity attributes only — a selector
 * carrying `:nth-child` would encode where a row happened to render, and the
 * tour would then highlight whatever moved into that slot.
 */
export function targetSelector(target: TourTarget): string {
  const attrs = Object.entries(target.attrs ?? {})
    .map(([key, value]) => `[${key}=${quoteAttr(value)}]`)
    .join("");
  const self = `[data-testid=${quoteAttr(target.testid)}]${attrs}`;
  return target.within ? `[data-testid=${quoteAttr(target.within)}] ${self}` : self;
}

export interface TargetRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

/**
 * Where a target is on screen, or `null` when it is missing or has no box.
 * `query` is injected so the resolver is testable without a document — and
 * so a caller can scope it to a portal or a shadow root later.
 */
export function resolveTarget(
  target: TourTarget | null,
  query: (selector: string) => { getBoundingClientRect(): TargetRect } | null,
): TargetRect | null {
  if (!target) return null;
  const el = query(targetSelector(target));
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  if (!(rect.width > 0) || !(rect.height > 0)) return null;
  return { top: rect.top, left: rect.left, width: rect.width, height: rect.height };
}

/**
 * The spoken Ada path, as data: local wake → command clip → /transcribe →
 * this router → action or caption.
 *
 * The one honesty rule everything here bends around: a deictic sentence
 * ("what's this", "I don't like that") is never a board intent. The old
 * enrich path prepended a fake "[desk: … screenshot captured]" note and
 * wakeAction treated that as `start`, which POSTed /generate. That path
 * is closed here. wake-flow.ts stays the board/command router; deixis
 * is decided first.
 */

import { needsDeskContext, type DeskSnapshot } from "./desk-context";
import { wakeAction, type WakeAction, type WakeFlowInput } from "./wake-flow";

/** Leftover annotation from the retired text-only enrich. */
const DESK_PREFIX = /^\[desk:[^\]]*\]\s*/i;

export const NO_DESK_CAPTION =
  "I heard you point at something, but I don’t have a real screenshot. Enable Screen Recording for Hardy, or name the part.";

export const DESK_FAILED_CAPTION =
  "I can see you pointed, but I couldn’t read the desk. Try the mic button or type it.";

export type AdaDecision =
  | WakeAction
  /** Deictic + a real snap: caller POSTs /desk/resolve, never /generate. */
  | { kind: "desk"; utterance: string; snap: DeskSnapshot }
  /** Deictic without a usable snap, or a resolve that abstained. */
  | {
      kind: "caption";
      text: string;
      abstain?: boolean;
      target?: DeskResolveResult["target"];
    };

export interface AdaPathInput extends WakeFlowInput {
  /** Capture from Rust. Ignored unless the sentence is deictic. */
  snap?: DeskSnapshot | null;
}

/** Drop a forged "[desk: …]" prefix so it can never become an intent. */
export function stripDeskAnnotation(text: string): string {
  return text.replace(DESK_PREFIX, "").trim();
}

export function isDeicticUtterance(text: string): boolean {
  const raw = text.trim();
  if (!raw) return false;
  if (DESK_PREFIX.test(raw)) return true;
  return needsDeskContext(stripDeskAnnotation(raw));
}

/** A snap the resolve route can actually look at — PNG bytes, not a claim. */
export function usableDeskSnap(
  snap: DeskSnapshot | null | undefined
): snap is DeskSnapshot {
  return Boolean(
    snap &&
      !snap.error &&
      snap.width > 0 &&
      snap.height > 0 &&
      snap.png_base64.trim()
  );
}

export function wouldStartBoard(decision: AdaDecision): boolean {
  return decision.kind === "start";
}

/**
 * Route one transcribed utterance.
 *
 * Deixis is decided before wakeAction, so a pointed "fix this" cannot become
 * a paid board. Non-deictic speech is wakeAction's, unchanged.
 */
export function routeSpokenUtterance(input: AdaPathInput): AdaDecision {
  const utterance = stripDeskAnnotation(input.utterance);
  if (isDeicticUtterance(input.utterance) || isDeicticUtterance(utterance)) {
    if (input.busy || input.stepsStatus === "running") {
      return { kind: "ignore", reason: "a run is in flight" };
    }
    if (usableDeskSnap(input.snap)) {
      return { kind: "desk", utterance, snap: input.snap };
    }
    return { kind: "caption", text: NO_DESK_CAPTION, abstain: true };
  }
  return wakeAction({
    utterance,
    busy: input.busy,
    stepsStatus: input.stepsStatus,
  });
}

export interface DeskResolveResult {
  caption: string;
  abstain: boolean;
  target: { testid: string; attrs?: Record<string, string>; tab?: string } | null;
}

export interface FulfillAdaDeps {
  resolveDesk?: (
    utterance: string,
    snap: DeskSnapshot
  ) => Promise<DeskResolveResult>;
}

/**
 * Turn a `desk` decision into a caption (or keep other kinds).
 *
 * Resolve failures and abstentions stay captions. Nothing here can become
 * `start` — that is the /generate poison this module exists to stop.
 */
export async function fulfillAdaDecision(
  decision: AdaDecision,
  deps: FulfillAdaDeps = {}
): Promise<AdaDecision> {
  if (decision.kind !== "desk") return decision;
  if (!deps.resolveDesk) {
    return { kind: "caption", text: DESK_FAILED_CAPTION, abstain: true };
  }
  try {
    const result = await deps.resolveDesk(decision.utterance, decision.snap);
    const text = (result.caption || "").trim() || DESK_FAILED_CAPTION;
    const abstain = result.abstain === true;
    return {
      kind: "caption",
      text,
      abstain,
      target: abstain ? null : result.target ?? null,
    };
  } catch {
    return { kind: "caption", text: DESK_FAILED_CAPTION, abstain: true };
  }
}

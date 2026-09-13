// Delivering a finished step run to Google Workspace, as data the panel renders.
//
// Everything here is pure: it reads what the engine answered (`DeliverConfig`,
// `DeliverResponse`, the step history) and produces the words the panel shows
// and the addresses it sends. The panel that draws it then has nothing to
// test but layout. The voice is the engineer reporting back — what was sent,
// where, and what was not, with the reason — never a cheerful assistant.

import { reviewFailure, reviewSkipped } from "./steps";
import type {
  DeliverConfig,
  DeliverResponse,
  GenerateRequest,
  SpecReviewBlock,
  StepResponse,
  SummaryMode,
} from "./types";

export type Destination = "chat" | "email" | "calendar" | "spec_review";

/**
 * A text box worth of addresses: commas, semicolons or whitespace between
 * them, blanks dropped, duplicates collapsed, order kept.
 */
export function parseAddresses(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const address = raw.trim();
    if (!address || seen.has(address)) continue;
    seen.add(address);
    out.push(address);
  }
  return out;
}

/**
 * The same shape the engine's `addresses.py` accepts: one `@`, a dotted
 * domain, none of the characters that would break a MIME header. This is a
 * courtesy check so the button can stay off while the field is half-typed;
 * the engine is the authority and refuses with a 400 either way.
 */
const FORBIDDEN = "\\s@,;:<>\"'()\\[\\]\\\\";
const ADDRESS = new RegExp(`^[^${FORBIDDEN}]+@[^${FORBIDDEN}.][^${FORBIDDEN}]*\\.[^${FORBIDDEN}]+$`);

export function looksLikeAddress(address: string): boolean {
  return ADDRESS.test(address);
}

/** The addresses that would not survive the engine's check, for the field's hint. */
export function badAddresses(addresses: readonly string[]): string[] {
  return addresses.filter((a) => !looksLikeAddress(a));
}

/**
 * True once the run has copper: the board file exists only after `route`.
 *
 * "At some point", not "most recently". The engine's own precondition is
 * `"routed" in _reached(session.stage)` (`service/deliver.py:_snapshot`), and
 * a run only ever moves forward — so the client asks the same question of the
 * whole history rather than of whichever step happened to finish last. Reading
 * only the last entry happens to give the same answer today, because
 * `service/steps.py` never advances `stage` past `routed` and every later
 * envelope repeats it; that is the engine's implementation detail, not the
 * rule, and gating the entire Send-to-Google panel on it made the client
 * stricter than the engine for no reason it could state.
 */
export function deliverable(history: readonly StepResponse[]): boolean {
  return history.some((response) => response.stage === "routed");
}

/** True when the engine has an OAuth client but no usable Google token yet. */
export function needsGoogleSignIn(
  config: DeliverConfig | null,
  /** Ada can run `python -m googleapps auth` even when the service lacks env. */
  cliGoogleapps = false
): boolean {
  if (!config?.available) return false;
  if (config.signed_in) return false;
  if (config.oauth_client === true) return true;
  if (cliGoogleapps) return true;
  // Older engines omit the flag; fall back to the hint text.
  return config.hints.some((h) => /googleapps auth|sign in with Google|(Hardy|Ada)'s Send panel/i.test(h));
}

/**
 * What the review said, if it ran. `reviewed` false means "blockers unknown",
 * which is different from zero — the calendar row has to say which. `failed`
 * is the third state, and it reads like the first: a critic that answered
 * nothing (`review.status` of `failed`, or `skipped`) left zero blockers
 * behind, and that zero is not a finding. `failure` carries the sentence.
 *
 * Searched from the end, like `specReviewFrom`: the question is "did review
 * run at any point", and if a session ever carries two review entries the
 * newer one is the one that stands.
 */
export function reviewState(history: readonly StepResponse[]): {
  reviewed: boolean;
  blockers: number;
  failed: boolean;
  failure: string | null;
} {
  for (let i = history.length - 1; i >= 0; i -= 1) {
    const review = history[i];
    if (review?.step !== "review") continue;
    const failure = reviewFailure(review.review) ?? reviewSkipped(review.review);
    return {
      reviewed: true,
      blockers: failure ? 0 : (review.blockers?.length ?? 0),
      failed: failure !== null,
      failure,
    };
  }
  return { reviewed: false, blockers: 0, failed: false, failure: null };
}

/**
 * The calendar row's sentence: what the review said about blockers, or why
 * nothing can be said. A failed review is the third case and gets the failed
 * sentence, never "found no blockers" — the engine books off blockers, and a
 * critic that answered nothing found none for the wrong reason.
 */
export function scheduleNote(history: readonly StepResponse[]): string {
  const review = reviewState(history);
  if (!review.reviewed) {
    return "The review has not run yet, so I cannot say whether there is anything to book.";
  }
  if (review.failure) return `The ${review.failure}, so nothing can be booked.`;
  if (review.blockers === 0) return "The review found no blockers, so there is nothing to book.";
  return `The review found ${review.blockers} blocker${review.blockers === 1 ? "" : "s"}; this books a half hour tomorrow with a Meet link.`;
}

/**
 * Why a destination is off, in the engine's own words — or null when it is
 * ready. Before the config has arrived nothing is known, so nothing is off
 * for a reason; the panel disables on `config === null` separately.
 */
export function hintFor(config: DeliverConfig | null, destination: Destination): string | null {
  if (!config) return null;
  if (!config.available) {
    return config.hints[0] ?? "The engine has no googleapps package beside it.";
  }
  const ready =
    destination === "chat" ? config.chat : destination === "email" ? config.gmail : config.calendar;
  if (ready) return null;
  const mine = config.hints.filter((h) =>
    destination === "chat" ? /chat/i.test(h) : /gmail|calendar|token/i.test(h)
  );
  if (mine.length) return mine.join(" ");
  return destination === "chat"
    ? "Chat is not configured on the engine."
    : "Gmail and Calendar are not signed in on the engine.";
}

/** One sentence per destination the engine reported on. */
export function describeChat(response: DeliverResponse): string | null {
  const block = response.chat;
  if (!block) return null;
  return block.ok ? "Posted the run card to Chat." : `Chat refused it: ${block.error ?? "no reason given"}`;
}

export function describeEmail(response: DeliverResponse): string | null {
  const block = response.email;
  if (!block) return null;
  const to = (block.to ?? []).join(", ");
  if (!block.ok) return `Gmail refused it${to ? ` (to ${to})` : ""}: ${block.error ?? "no reason given"}`;
  const id = block.message_id ? ` (message id ${block.message_id})` : "";
  return `Sent the board to ${to || "the recipients"}${id}.`;
}

export function describeCalendar(response: DeliverResponse): string | null {
  const block = response.calendar;
  if (!block) return null;
  if (block.skipped_reason) return `Nothing booked: ${block.skipped_reason}.`;
  if (!block.ok) return `Calendar refused it: ${block.error ?? "no reason given"}`;
  const n = block.blockers ?? 0;
  const head = `Booked the review for tomorrow, ${n} blocker${n === 1 ? "" : "s"} on the agenda.`;
  return block.meet_uri ? `${head} Meet: ${block.meet_uri}` : head;
}

export function describeSpecReview(response: DeliverResponse): string | null {
  const block = response.spec_review;
  if (!block) return null;
  // A deliberate skip is the engine's answer, not a failure: "no blockers" and
  // "the review has not run" are both real outcomes and are shown as such.
  if (block.skipped_reason) return `No spec review booked: ${block.skipped_reason}.`;
  if (!block.ok) return `The spec review was refused: ${block.error ?? "no reason given"}`;
  const parts = ["Booked the spec review."];
  if (block.meet_uri) parts.push(`Meet: ${block.meet_uri}`);
  if (block.html_link) parts.push(`Invite: ${block.html_link}`);
  return parts.join(" ");
}

export function describeDelivery(response: DeliverResponse): Partial<Record<Destination, string>> {
  const out: Partial<Record<Destination, string>> = {};
  const chat = describeChat(response);
  const email = describeEmail(response);
  const calendar = describeCalendar(response);
  const specReview = describeSpecReview(response);
  if (chat) out.chat = chat;
  if (email) out.email = email;
  if (calendar) out.calendar = calendar;
  if (specReview) out.spec_review = specReview;
  return out;
}

// ------------------------------------------------------------- spec review

/**
 * The agenda the engine last proposed, or null when it proposed none.
 *
 * The panel never assembles one of its own: an agenda invented here would be
 * this app's opinion presented as the engine's, and the whole point of showing
 * it before the send is that the engineer approves what will actually go out.
 */
export function specReviewFrom(history: readonly StepResponse[]): SpecReviewBlock | null {
  for (let i = history.length - 1; i >= 0; i -= 1) {
    const block = history[i]?.spec_review;
    if (block) return block;
  }
  return null;
}

/**
 * How long the meeting is. The engine's own total wins; the sum of the items
 * is the fallback for an engine that does not send one, and never overrides it
 * — a disagreement between the two is the engine's to resolve, not ours.
 */
export function agendaMinutes(review: SpecReviewBlock): number {
  if (typeof review.total_minutes === "number") return review.total_minutes;
  return review.items.reduce((total, item) => total + (item.minutes || 0), 0);
}

/** The items that stop the board, which are the reason a meeting is asked for. */
export function blockingItems(review: SpecReviewBlock): SpecReviewBlock["items"] {
  return review.items.filter((item) => item.blocking);
}

/**
 * Whether a spec review is worth offering, and what to say when it is not.
 *
 * The "not offerable" answers are results the engineer should read, not
 * silence: the review has not run, or it ran and found nothing that needs a
 * meeting. The button stays live for those — the engine is the authority on
 * what it will book, and it answers with its own `skipped_reason`. The one
 * exception is `refused`: a review that failed. Nothing is known about the
 * board, so an agenda — even one the engine drafted from routing and the
 * kernel before the critic gave up — is not offered over it, and the button
 * is off with the failed sentence, the same refusal the service makes.
 */
export function specReviewOffer(
  history: readonly StepResponse[]
): { review: SpecReviewBlock | null; note: string; refused: boolean } {
  const state = reviewState(history);
  if (state.failure) {
    return { review: null, note: `The ${state.failure}, so no spec review can be booked.`, refused: true };
  }
  const review = specReviewFrom(history);
  if (!review) {
    return {
      review: null,
      note: state.reviewed
        ? "The review ran but proposed no agenda, so there is nothing to put in a meeting."
        : "The review step has not run, so there is no agenda yet.",
      refused: false,
    };
  }
  if (blockingItems(review).length === 0) {
    return { review, note: "No blockers — nothing needs a meeting.", refused: false };
  }
  return { review, note: "", refused: false };
}

// ------------------------------------------------------------ when to meet

/**
 * The parts of a `datetime-local` value, or null when it is not one.
 * `YYYY-MM-DDTHH:MM` with optional `:SS`, which is all that control emits.
 */
function localParts(value: string): [number, number, number, number, number, number] | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!match) return null;
  const parts: [number, number, number, number, number, number] = [
    Number(match[1]),
    Number(match[2]),
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
    Number(match[6] ?? "0"),
  ];
  // `Date` rolls an impossible field over rather than refusing it (month 13
  // becomes January of the next year), which would turn a typo into a real
  // booking. The shape check above is not enough; the calendar has to agree.
  const [year, month, day, hour, minute, second] = parts;
  const moment = new Date(year, month - 1, day, hour, minute, second);
  const agrees =
    moment.getFullYear() === year &&
    moment.getMonth() === month - 1 &&
    moment.getDate() === day &&
    moment.getHours() === hour &&
    moment.getMinutes() === minute &&
    moment.getSeconds() === second;
  return agrees ? parts : null;
}

function two(n: number): string {
  return String(Math.abs(n)).padStart(2, "0");
}

/**
 * A `<input type="datetime-local">` value as the RFC 3339 timestamp the
 * engine's `parse_when` reads — the wall-clock the engineer typed, carrying
 * this machine's UTC offset for that instant (so a meeting typed across a
 * DST change still lands in the hour typed).
 *
 * Empty means "no preference": null, which the engine reads as its default
 * slot. Anything else that is not a `datetime-local` value is passed through
 * *verbatim* rather than smoothed to null — the engine refuses it with a 400
 * naming the value, and a typo that quietly became "tomorrow at the default
 * time" is exactly the meeting-in-the-wrong-hour `parse_when` exists to stop.
 */
export function whenToRfc3339(local: string): string | null {
  const trimmed = local.trim();
  if (!trimmed) return null;
  const parts = localParts(trimmed);
  if (!parts) return trimmed;
  const [year, month, day, hour, minute, second] = parts;
  const moment = new Date(year, month - 1, day, hour, minute, second);
  if (Number.isNaN(moment.getTime())) return trimmed;
  // Minutes *behind* UTC per the DOM, so New York (+300) is `-05:00`.
  const behind = moment.getTimezoneOffset();
  const sign = behind > 0 ? "-" : "+";
  const offset = `${sign}${two(Math.floor(Math.abs(behind) / 60))}:${two(Math.abs(behind) % 60)}`;
  return (
    `${moment.getFullYear()}-${two(moment.getMonth() + 1)}-${two(moment.getDate())}` +
    `T${two(moment.getHours())}:${two(moment.getMinutes())}:${two(moment.getSeconds())}${offset}`
  );
}

/**
 * A courtesy for the field's note: true when the typed time has already
 * passed. The engine is the authority (`'when' is in the past: …`, a 400)
 * and the button stays live either way; this only saves a round trip that
 * was certain to be refused.
 */
export function whenInPast(local: string, now: Date = new Date()): boolean {
  const parts = localParts(local);
  if (!parts) return false;
  const [year, month, day, hour, minute, second] = parts;
  const moment = new Date(year, month - 1, day, hour, minute, second);
  return !Number.isNaN(moment.getTime()) && moment.getTime() <= now.getTime();
}

// ------------------------------------------------------- how a run is said

const SUMMARY_MODE_KEY = "kaleo.summaryMode";
const DEFAULT_SUMMARY_MODE: SummaryMode = "prose";

function isSummaryMode(value: unknown): value is SummaryMode {
  return value === "structured" || value === "prose";
}

/**
 * The remembered choice of how a finished run is summarised.
 *
 * Per-viewer convenience, so `localStorage` is the right home and every access
 * is guarded: a webview with site data blocked throws on the accessor itself,
 * and the honest answer to "I could not read it" is the default, not a crash.
 */
export function readSummaryMode(): SummaryMode {
  try {
    const stored = window.localStorage.getItem(SUMMARY_MODE_KEY);
    return isSummaryMode(stored) ? stored : DEFAULT_SUMMARY_MODE;
  } catch {
    return DEFAULT_SUMMARY_MODE;
  }
}

export function writeSummaryMode(mode: SummaryMode): void {
  try {
    window.localStorage.setItem(SUMMARY_MODE_KEY, mode);
  } catch {
    /* per-viewer convenience only; the toggle still works for this session */
  }
}

/**
 * What the choice adds to a run request. A separate function so the submit
 * path folds one object in rather than spreading the rule across callers.
 */
export function summaryFields(mode: SummaryMode): Pick<GenerateRequest, "summary"> {
  return { summary: mode };
}

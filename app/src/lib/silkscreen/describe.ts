// NDJSON frames in, the two things the UI shows during a run out: one plain
// sentence per frame, and a folded progress state for the stage checklist.
//
// Pure and DOM-free. The transport belongs to `client.ts` and the rendering to
// the components, so this half stays testable without a server or a browser.
//
// Two rules run through the whole file:
//   * An unknown event name is expected, not exceptional — the engine may emit
//     stages this build has never heard of. Unknown frames are dropped: no
//     sentence, no state change, and never a raw JSON blob on screen.
//   * Progress ticks only from frames actually received. Nothing here infers
//     that an earlier stage finished because a later one started.

import type { ReviewBlock, RunResult, StreamFrame } from "./types";
import { REVIEW_CLEAN_LINE, reviewFailure, reviewSkipped } from "./steps";
import {
  PIPELINE_STAGES,
  planStages,
  stageForEvent,
  type RunPlan,
  type StageDescriptor,
  type StageId,
} from "./stages";

const MAX_ERROR_CHARS = 160;
const MAX_FEED_LINES = 500;

/* ------------------------------------------------------------------ describe */

/**
 * One frame, one short sentence — or `null` when there is nothing honest to
 * say about it (an unknown event, or a frame that is not an event at all).
 *
 * `t_s` is deliberately not in the sentence; the feed renders its own time.
 */
export function describeFrame(frame: unknown): string | null {
  const e = asFrame(frame);
  if (!e) return null;
  const describe = DESCRIBERS[e.event];
  if (!describe) return null;
  const sentence = describe(e);
  return sentence ? sentence : null;
}

type Describer = (e: StreamFrame) => string | null;

// A plain object literal would let an event named `constructor` or `toString`
// reach Object.prototype and return a function; a null-prototype map cannot.
const DESCRIBERS: Record<string, Describer | undefined> = Object.assign(
  Object.create(null) as Record<string, Describer>,
  {
    "run.accepted": () => "Run accepted, the pipeline is starting.",

    "stage.start": (e: StreamFrame) => {
      const stage = str(e.stage);
      if (!stage) return null;
      const start = STAGE_START[stage];
      // A stage name we do not know is still real data from the engine, so it
      // gets a generic sentence rather than being swallowed.
      return start ? start(e) : `Starting ${stage}.`;
    },

    // The critic did not answer. Its own sentence, in the words every other
    // surface uses for this outcome; never a line that reads as clean.
    "review.failed": (e: StreamFrame) =>
      sentence(reviewFailure({ status: "failed", ran: true, detail: clip(str(e.detail), MAX_ERROR_CHARS) || null, note: "" })!),

    "stage.done": (e: StreamFrame) => {
      const stage = str(e.stage);
      if (!stage) return null;
      const done = STAGE_DONE[stage];
      return done ? done(e) : `Finished ${stage}.`;
    },

    "read.part": (e: StreamFrame) => {
      const part = str(e.part) || "a datasheet";
      const counter = counterOf(e.index, e.total);
      if (e.cached) return `${part}: facts already cached${counter}.`;
      return `Reading ${part}${counter}.`;
    },

    "propose.round": (e: StreamFrame) => {
      const round = num(e.round);
      const which = round === null ? "Proposal" : `Proposal round ${round}`;
      const errors = count(e.errors);
      const first = clip(str(e.first_error), MAX_ERROR_CHARS);
      const why = first ? ` (first: ${first})` : "";
      return `${which} rejected: ${plural(errors, "validation error")}${why}.`;
    },

    "model.call": (e: StreamFrame) => {
      const where = whereOf(e.stage);
      const took = seconds(e.elapsed_s);
      // Same reading as the reducer: absent `ok` is not a failure. The
      // engine always sends it, so this only matters for a frame this build
      // half-recognises — and then the two must agree.
      if (e.ok === false) {
        return `Model call${where} failed${took ? ` after ${took} s` : ""}.`;
      }
      // Either name identifies the call; with neither, the sentence still has
      // to read as a sentence.
      const who = str(e.model) || str(e.provider);
      const chars = num(e.chars);
      const size = chars === null ? "" : `, ${group(chars)} characters`;
      return `${who || "The model"} answered${where}${took ? ` in ${took} s` : ""}${size}.`;
    },

    // Debug runs only. The answer itself belongs in the debug console, so the
    // sentence reports its size and whether there was more of it.
    "model.response": (e: StreamFrame) => {
      const counted = num(e.chars);
      const chars = counted === null ? str(e.text).length : counted;
      const clipped = e.truncated ? " (truncated)" : "";
      return `Response${whereOf(e.stage)}: ${group(chars)} characters${clipped}.`;
    },

    "model.retry": (e: StreamFrame) => {
      const who = str(e.provider);
      const why = clip(str(e.error), MAX_ERROR_CHARS);
      const took = seconds(e.elapsed_s);
      return (
        `Provider${who ? ` ${who}` : ""} failed${why ? ` (${why})` : ""}` +
        `${took ? ` after ${took} s` : ""}, trying the next one.`
      );
    },

    "ground.part": (e: StreamFrame) => {
      const part = str(e.part);
      const how = e.cached ? "cached pages" : "freshly read pages";
      return `Grounding${part ? ` ${part}` : ""} against ${how}.`;
    },

    "run.done": () => "Run complete.",

    "run.error": (e: StreamFrame) => {
      const status = num(e.status);
      const why = clip(str(e.error), MAX_ERROR_CHARS);
      return `Run failed${status === null ? "" : ` (${status})`}${why ? `: ${why}` : ""}`;
    },
  }
);

const STAGE_START: Record<string, Describer | undefined> = Object.assign(
  Object.create(null) as Record<string, Describer>,
  {
    read: () => "Reading the datasheets.",
    propose: () => "Proposing a circuit.",
    place: (e: StreamFrame) => {
      const budget = num(e.time_limit_s);
      return `Placing parts${budget === null ? "" : ` (${trim(budget)} s solver budget)`}.`;
    },
    schematic: () => "Drawing the schematic.",
    route: () => "Routing the copper.",
    review: () => "Reviewing the design.",
  }
);

const STAGE_DONE: Record<string, Describer | undefined> = Object.assign(
  Object.create(null) as Record<string, Describer>,
  {
    read: (e: StreamFrame) =>
      `Facts for ${plural(count(e.parts), "part")} (read or cached): ${plural(count(e.pins), "pin")}, ` +
      `${plural(count(e.requirements), "requirement")}.`,

    propose: (e: StreamFrame) => {
      const rounds = count(e.repair_rounds);
      const after = rounds > 0 ? ` after ${plural(rounds, "repair round")}` : "";
      return `Proposed ${plural(count(e.parts), "part")} across ${plural(
        count(e.nets),
        "net"
      )}${after}.`;
    },

    place: (e: StreamFrame) => {
      const warnings = count(e.warnings);
      const clauses = [
        boardOf(e.board_mm),
        str(e.solver_status),
        wireOf(e.wirelength_mm),
        warnings > 0 ? plural(warnings, "warning") : "",
      ].filter(Boolean);
      return clauses.length ? `Placed on ${clauses.join(", ")}.` : "Placement solved.";
    },

    schematic: (e: StreamFrame) => {
      const warnings = count(e.warnings);
      const extra = warnings > 0 ? `, ${plural(warnings, "warning")}` : "";
      return `Drew ${plural(count(e.symbols), "symbol")}${extra}.`;
    },

    route: (e: StreamFrame) => {
      const routed = count(e.routed_nets);
      const unrouted = count(e.unrouted_nets);
      const clauses: string[] = [];
      if (count(e.tracks) > 0) clauses.push(plural(count(e.tracks), "track"));
      if (count(e.vias) > 0) clauses.push(plural(count(e.vias), "via"));
      // Say the unfinished count out loud. A net left as ratsnest is invisible
      // until fabrication, and "routed" with nothing after it reads as done.
      if (unrouted > 0) clauses.push(`${unrouted} left unrouted`);
      const tail = clauses.length ? `: ${clauses.join(", ")}` : "";
      return `Routed ${routed} of ${routed + unrouted} nets${tail}.`;
    },

    review: (e: StreamFrame) => {
      const findings = count(e.findings);
      const blockers = count(e.blockers);
      // What became of the critic decides what an empty list means. The
      // engine's `stage.done` carries `status` ("ok" / "failed"); a block
      // under `review` is read the same way. Neither present is an older
      // engine, whose empty list keeps the reading it always had.
      const review = reviewFrameBlock(e);
      const unreviewed = reviewFailure(review) ?? reviewSkipped(review);
      if (unreviewed) return sentence(unreviewed);
      if (findings === 0) return review ? REVIEW_CLEAN_LINE : "Review finished with no findings.";
      const extra = blockers > 0 ? `, ${plural(blockers, "blocker")}` : "";
      return `Review raised ${plural(findings, "finding")}${extra}.`;
    },
  }
);

/**
 * The critic's outcome as a frame carries it: a `review` block verbatim, or
 * the `status` string (plus `detail`, on `review.failed`) the engine puts on
 * the review frames. Undefined when the frame says nothing about it.
 */
function reviewFrameBlock(e: StreamFrame): ReviewBlock | undefined {
  const raw = e as Record<string, unknown>;
  const block = raw.review;
  if (block && typeof block === "object" && typeof (block as ReviewBlock).status === "string") {
    return block as ReviewBlock;
  }
  const status = raw.status;
  if (typeof status !== "string" || !status) return undefined;
  return { status, ran: status !== "skipped", detail: clip(str(raw.detail), MAX_ERROR_CHARS) || null, note: "" };
}

/** A feed line: first letter up, one full stop. */
function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return trimmed;
  const capped = `${trimmed[0].toUpperCase()}${trimmed.slice(1)}`;
  return /[.!?]$/.test(capped) ? capped : `${capped}.`;
}

/* -------------------------------------------------------------------- reduce */

export type StageStatus = "pending" | "running" | "done" | "skipped" | "unreported";

export interface StageState {
  id: StageId;
  descriptor: StageDescriptor;
  status: StageStatus;
  /** Engine clock, seconds since the run started. Null until the frame arrives. */
  startedAtS: number | null;
  finishedAtS: number | null;
  /** Only set when both timestamps arrived; never guessed from anything else. */
  durationS: number | null;
  /** The `stage.done` sentence, so the checklist row can carry its own result. */
  summary: string | null;
  /** Why a `skipped` or `unreported` row is not going to tick. */
  note: string | null;
}

export interface ModelCall {
  stage: string | null;
  provider: string | null;
  model: string | null;
  ok: boolean;
  elapsedS: number | null;
  chars: number | null;
}

export interface FeedLine {
  /** Monotonic within one run; safe as a React key. */
  seq: number;
  event: string;
  /** Engine clock in seconds, when the frame carried one. */
  tS: number | null;
  text: string;
}

export type RunStatus = "idle" | "accepted" | "running" | "done" | "error";

export interface RunProgress {
  status: RunStatus;
  stages: StageState[];
  /** The stage with a `stage.start` and no `stage.done` yet. */
  currentStage: StageId | null;
  /** Highest `t_s` seen. The engine's clock, not a wall clock — the UI owns that. */
  elapsedS: number;
  modelCalls: number;
  modelFailures: number;
  /** Provider failovers reported by `model.retry`. */
  modelRetries: number;
  /** Summed `elapsed_s` of every model round-trip that reported one. */
  modelSecondsTotal: number;
  /** Newest last, capped; the whole log of round-trips is not worth unbounded memory. */
  modelCallLog: ModelCall[];
  /** Repair rounds seen as `propose.round` frames. */
  repairRounds: number;
  feed: FeedLine[];
  /** Frames whose event name this build does not know. Dropped, but counted, so it is diagnosable. */
  unknownFrames: number;
  result: RunResult | null;
  error: { status: number | null; message: string } | null;
}

const MAX_MODEL_CALLS = 200;

/**
 * A fresh progress state.
 *
 * Stages the plan rules out start as `skipped` with their reason attached —
 * that is a fact about the engine's control flow (see `planStages`), not an
 * optimistic guess. Everything else starts `pending` and moves only on frames.
 */
export function initialRunProgress(plan: RunPlan = {}): RunProgress {
  const running = new Set(planStages(plan).map((s) => s.id));
  return {
    status: "idle",
    stages: PIPELINE_STAGES.map((descriptor) => ({
      id: descriptor.id,
      descriptor,
      status: running.has(descriptor.id) ? "pending" : "skipped",
      startedAtS: null,
      finishedAtS: null,
      durationS: null,
      summary: null,
      note: running.has(descriptor.id) ? null : descriptor.skipReason,
    })),
    currentStage: null,
    elapsedS: 0,
    modelCalls: 0,
    modelFailures: 0,
    modelRetries: 0,
    modelSecondsTotal: 0,
    modelCallLog: [],
    repairRounds: 0,
    feed: [],
    unknownFrames: 0,
    result: null,
    error: null,
  };
}

/**
 * Fold one frame into the run state, returning a new object.
 *
 * Never throws. A frame that is not an object, has no `event`, or names an
 * event this build does not know leaves the state alone but for
 * `unknownFrames`.
 */
export function reduceFrame(state: RunProgress, frame: unknown): RunProgress {
  const e = asFrame(frame);
  if (!e) return { ...state, unknownFrames: state.unknownFrames + 1 };

  const sentence = describeFrame(e);
  if (!sentence && !KNOWN_EVENTS.has(e.event)) {
    return { ...state, unknownFrames: state.unknownFrames + 1 };
  }

  const t = num(e.t_s);
  let next: RunProgress = {
    ...state,
    elapsedS: t === null ? state.elapsedS : Math.max(state.elapsedS, t),
  };

  // done/error are terminal. A replayed or duplicated frame after the run
  // ended must not resurrect it — status only ever moves forward.
  const terminal = state.status === "done" || state.status === "error";

  switch (e.event) {
    case "run.accepted":
      if (!terminal) next.status = "accepted";
      break;

    case "stage.start":
      if (!terminal) next = startStage(next, e, t);
      break;

    case "stage.done":
      next = finishStage(next, e, t, sentence);
      break;

    case "propose.round":
      next.repairRounds = state.repairRounds + 1;
      // The repair loop lives inside `propose` and has no stage events of its
      // own, so its round frames are the only thing this row can tick from.
      next = patchStage(next, "validate", (s) => ({
        ...s,
        status: s.status === "done" ? s.status : "running",
        startedAtS: s.startedAtS ?? t,
      }));
      break;

    case "model.call": {
      const call: ModelCall = {
        stage: str(e.stage) || null,
        provider: str(e.provider) || null,
        model: str(e.model) || null,
        ok: e.ok !== false,
        elapsedS: num(e.elapsed_s),
        chars: num(e.chars),
      };
      next.modelCalls = state.modelCalls + 1;
      if (!call.ok) next.modelFailures = state.modelFailures + 1;
      if (call.elapsedS !== null) {
        next.modelSecondsTotal = round3(state.modelSecondsTotal + call.elapsedS);
      }
      next.modelCallLog = cap([...state.modelCallLog, call], MAX_MODEL_CALLS);
      break;
    }

    case "model.retry":
      next.modelRetries = state.modelRetries + 1;
      break;

    case "run.done":
      next.status = "done";
      next.result = (isObject(e.result) ? (e.result as RunResult) : {}) as RunResult;
      next = settle(next);
      break;

    case "run.error":
      next.status = "error";
      next.error = {
        status: num(e.status),
        message: str(e.error) || "The run failed.",
      };
      next = settle(next);
      break;

    default:
      break;
  }

  if (next.status === "accepted" && next.currentStage !== null) next.status = "running";
  if (sentence) {
    // Derived from the previous line, not the array length: the cap drops the
    // oldest lines, and a length-based key would then repeat itself.
    const seq = state.feed.length ? state.feed[state.feed.length - 1].seq + 1 : 0;
    next.feed = cap([...state.feed, { seq, event: e.event, tS: t, text: sentence }], MAX_FEED_LINES);
  }
  return next;
}

/** Convenience for replaying a batch — same rules, same immutability. */
export function reduceFrames(state: RunProgress, frames: readonly unknown[]): RunProgress {
  return frames.reduce<RunProgress>(reduceFrame, state);
}

/** Every event name the reducer acts on, so a known-but-silent event is not counted as unknown. */
const KNOWN_EVENTS = new Set([
  "run.accepted",
  "stage.start",
  "stage.done",
  "read.part",
  "propose.round",
  "model.call",
  // Emitted per call in debug mode, which the app itself turns on.
  "model.request",
  "model.response",
  "model.retry",
  "ground.part",
  "run.done",
  "run.error",
]);

function startStage(state: RunProgress, e: StreamFrame, t: number | null): RunProgress {
  const descriptor = stageForEvent(e.stage);
  // An unrecognised stage name is dropped from the checklist rather than
  // appended to it: the list is the pipeline's shape, not a running log.
  if (!descriptor) return state;
  const next = patchStage(state, descriptor.id, (s) => ({
    ...s,
    status: "running",
    startedAtS: t,
    note: null,
  }));
  next.currentStage = descriptor.id;
  return next;
}

function finishStage(
  state: RunProgress,
  e: StreamFrame,
  t: number | null,
  summary: string | null
): RunProgress {
  const descriptor = stageForEvent(e.stage);
  if (!descriptor) return state;
  let next = patchStage(state, descriptor.id, (s) => ({
    ...s,
    status: "done",
    finishedAtS: t,
    durationS: s.startedAtS !== null && t !== null ? round3(t - s.startedAtS) : null,
    summary,
    note: null,
  }));
  if (descriptor.id === "propose") {
    // `propose_circuit` returns only a spec that parsed and validated, so a
    // `stage.done` for propose is direct evidence that validation passed —
    // this is the one place the engine's control flow licenses an inference,
    // and it is about the same frame, not about an earlier stage.
    const rounds = next.repairRounds;
    next = patchStage(next, "validate", (s) => ({
      ...s,
      status: s.status === "skipped" ? s.status : "done",
      startedAtS: s.startedAtS ?? t,
      finishedAtS: t,
      durationS: s.startedAtS !== null && t !== null ? round3(t - s.startedAtS) : null,
      summary:
        rounds === 0
          ? "The first proposal validated."
          : `Validated after ${plural(rounds, "repair round")}.`,
      note: null,
    }));
  }
  if (next.currentStage === descriptor.id) next.currentStage = null;
  return next;
}

/**
 * Close the checklist out when the run ends.
 *
 * A stage still `pending` never started and now never will — that is observed,
 * not assumed. A stage still `running` is genuinely unaccounted for and says
 * so rather than being quietly ticked.
 */
function settle(state: RunProgress): RunProgress {
  return {
    ...state,
    currentStage: null,
    stages: state.stages.map((s): StageState => {
      if (s.status === "pending") {
        return { ...s, status: "skipped", note: s.descriptor.skipReason ?? "did not run" };
      }
      if (s.status === "running") {
        return { ...s, status: "unreported", note: "the run ended before this stage reported back" };
      }
      return s;
    }),
  };
}

function patchStage(
  state: RunProgress,
  id: StageId,
  patch: (stage: StageState) => StageState
): RunProgress {
  return { ...state, stages: state.stages.map((s) => (s.id === id ? patch(s) : s)) };
}

/* ------------------------------------------------------------------- helpers */

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A frame we are willing to look at: an object with a string `event`. */
function asFrame(value: unknown): StreamFrame | null {
  if (!isObject(value)) return null;
  return typeof value.event === "string" ? (value as StreamFrame) : null;
}

function str(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null || typeof value === "symbol" || typeof value === "object") return "";
  return String(value);
}

function clip(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

/** `null` for an absent number, so a clause can be dropped whole rather than reporting a confident zero. */
function num(value: unknown): number | null {
  if (value == null || value === "" || typeof value === "boolean") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Counts are different: a missing count reads as zero, which is what the caller means by it. */
function count(value: unknown): number {
  const n = num(value);
  return n === null ? 0 : Math.max(0, Math.round(n));
}

function plural(n: number, noun: string): string {
  return `${group(n)} ${noun}${n === 1 ? "" : "s"}`;
}

function group(n: number): string {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function trim(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function round3(n: number): number {
  return Math.round(n * 1000) / 1000;
}

function seconds(value: unknown): string {
  const n = num(value);
  return n === null ? "" : n.toFixed(1);
}

function counterOf(index: unknown, total: unknown): string {
  const i = num(index);
  const n = num(total);
  return i === null || n === null ? "" : ` (${i} of ${n})`;
}

function whereOf(stage: unknown): string {
  const name = str(stage);
  return name ? ` (${name})` : "";
}

function boardOf(value: unknown): string {
  if (!Array.isArray(value) || value.length < 2) return "";
  const w = num(value[0]);
  const h = num(value[1]);
  return w === null || h === null ? "" : `a ${w.toFixed(1)} × ${h.toFixed(1)} mm board`;
}

function wireOf(value: unknown): string {
  const n = num(value);
  return n === null ? "" : `${group(n)} mm of wire`;
}

function cap<T>(items: T[], max: number): T[] {
  return items.length > max ? items.slice(items.length - max) : items;
}

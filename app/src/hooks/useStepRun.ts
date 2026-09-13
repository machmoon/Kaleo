import { useCallback, useEffect, useRef, useState } from "react";
import {
  advanceStep,
  amendStep,
  cancelStep,
  showBoard3d,
  startSteps,
  stepStatus,
  SilkscreenError,
  type View3dResponse,
} from "@/lib/silkscreen/client";
import {
  STEP_DESCRIPTORS,
  availableSteps,
  backgroundFailure,
  backgroundJobs,
  isUnreceivedStep,
  mergeBackgroundOutcomes,
  placeDetails,
  reconcileHistory,
  reviewDetails,
  reviewFailure,
  reviewSkipped,
  routeDetails,
  type BackgroundJob,
} from "@/lib/silkscreen/steps";
import { notifyMilestone } from "@/lib/notify/notify";
import type {
  Amendment,
  AmendResponse,
  BackgroundOutcomes,
  CancelResponse,
  RunResult,
  StepName,
  StepRequest,
  StepResponse,
  SummaryMode,
} from "@/lib/silkscreen/types";
import { logEvent, logServer } from "@/lib/silkscreen/log";
import {
  describeFrame,
  initialRunProgress,
  reduceFrame,
  type RunProgress,
} from "@/lib/silkscreen/describe";
import { publishRun } from "@/lib/silkscreen/bridge";
import {
  DEFAULT_TIME_LIMIT_S,
  type RunHistoryEntry,
  type RunRequestDraft,
} from "@/hooks/useSilkscreenRun";

export type StepRunStatus = "idle" | "running" | "waiting" | "done" | "error";

export interface StepRun {
  status: StepRunStatus;
  /** The engine's session id once a run has started. */
  session: string | null;
  /**
   * Every step response in arrival order. A step the engine finished after
   * this client cancelled its request appears as a marker (see
   * `unreceivedStep`): done, with no result to show.
   */
  history: StepResponse[];
  /** The step in flight, while `status` is `running`. */
  running: StepName | null;
  /**
   * The step that was in flight when the run failed, while `status` is
   * `error`. `running` is cleared as the failure lands — nothing is in flight
   * any more — so without this the rail could not say which stage failed and
   * had to draw the error nowhere. Null after an abort: a cancelled step is
   * not a failed one, and the engine may well have finished it.
   */
  failedStep: StepName | null;
  /**
   * What may be approved now. From the latest response, or — after a cancel
   * or a 409 — from the engine's own `GET /steps/<id>`, which is the only
   * source that knows a cancelled step still ran.
   */
  available: StepName[];
  error: SilkscreenError | null;
  /** Wall-clock seconds the current step has been running. */
  elapsedS: number;
  /**
   * The id this session's board was published to the dashboard under, or null
   * while no step has produced one. It is set once — at the first step that
   * carried a board — and does not change as later steps update that entry,
   * so a caller can use it as the "there is now a board to look at" edge
   * without being woken by every subsequent step.
   *
   * Optional on the interface, not on the hook, which always supplies it: a
   * hand-written `StepRun` double in another component's test predates this
   * field and is not wrong for lacking it.
   */
  publishedId?: string | null;
  /**
   * The jobs the engine started without a press (`case`, `sourcing` — from
   * `place` on), each running, settled or failed. Running comes from the
   * engine's `background` list on every envelope and from a poll of
   * `GET /steps/<id>` while the engineer is deciding; finished and failed
   * from the engine's `background_outcome` the moment a job leaves that
   * list; on an older engine that sends no outcome, failed only from the
   * engine's own warning once a collecting step has answered, and settled is
   * everything else that has left the list — finished, outcome unreported.
   * Optional on the interface for the same reason `publishedId` is.
   */
  background?: BackgroundJob[];
  /**
   * Everything typed at the strip during this run, **as the engine last sent
   * it** on a step envelope or `GET /steps/<id>`. The client keeps no model of
   * its own: a note the `case` step consumed flips to `status: "applied"` on
   * the engine, and a locally-maintained copy would go on calling it pending.
   * Optional on the interface for the same reason `background` is.
   */
  amendments?: Amendment[];
  /** The session is closed to further steps. Also the engine's word, not ours. */
  cancelled?: boolean;
  start: (request: StepRequest) => void;
  approve: (step: StepName, payload?: Record<string, unknown>) => void;
  /**
   * Stop waiting on the in-flight request. Local only — it aborts this
   * client's fetch and nothing else; the engine finishes the step. Not to be
   * confused with `cancelRun`, which tells the engine.
   */
  cancel: () => void;
  /**
   * Ask the engine to close the session. Resolves with its report, which
   * names what could not be stopped; it is the caller's job to render that
   * rather than to claim the run halted.
   */
  cancelRun?: () => Promise<CancelResponse>;
  /**
   * Record a sentence against this run. Spends nothing. `step` may only name
   * a step that reads free text (today `case`); anything else is a 400 by
   * design, so the UI cannot offer a door the engine does not have.
   */
  amend?: (text: string, step?: StepName | null) => Promise<AmendResponse>;
  /**
   * Open KiCad's own 3D viewer on this run's routed board. Spends nothing and
   * changes nothing, so it may be pressed repeatedly.
   *
   * Resolves with the engine's verdict rather than throwing on a KiCad
   * problem: `opened: false` with a `detail` naming the fix is the useful
   * answer, and it is the caller's job to show it. Optional on the interface
   * for the same reason `amend` is — hand-written doubles in other
   * components' tests predate it.
   */
  show3d?: () => Promise<View3dResponse>;
  reset: () => void;
}

export interface UseStepRunOptions {
  baseUrl: string;
  token: string;
  /**
   * How a finished run reports back, from the page's own state — the same
   * value `summaryFields` folds into a one-shot request. The engine reads it
   * on exactly one step (see `summaryPayload`), so the hook, rather than each
   * button, decides which approval carries it: two components asking for an
   * agenda in two places is how one of them silently stops asking.
   */
  summary?: SummaryMode;
}

/**
 * What the summary choice adds to one step's approval, which is nothing at
 * all except on `review`.
 *
 * `review` is the only step that reads it — `service/steps.py::_wants_agenda`
 * turns `{"summary": "structured"}` into a spec-review agenda beside the
 * findings — and sending it anywhere else would be a field on a request that
 * has no use for it, which an older engine is entitled to refuse.
 *
 * Prose sends no `summary` key at all rather than `{"summary": "prose"}`: it
 * is the default on both sides, so the quiet request stays byte-for-byte what
 * it was before this existed. A bad value is not smoothed over here — the
 * engine answers 400 and the step fails, which is the honest outcome for "you
 * asked for an agenda and did not get one".
 */
export function summaryPayload(
  step: StepName,
  summary: SummaryMode | undefined
): Record<string, unknown> {
  return step === "review" && summary === "structured" ? { summary } : {};
}

/**
 * How often `GET /steps/<id>` is asked whether a background job is still
 * running, while the run waits on a press. A GET runs no stage and costs
 * nothing; without it the row would say "designing in the background" until
 * the next approval, however long the engineer took to decide.
 */
export const BACKGROUND_POLL_MS = 3000;

// ----------------------------------------------------------- the hand-off
//
// A step run has to reach the dashboard the same way a one-shot run does, or
// the default configuration (step mode is ON by default — see
// `pages/kaleo/index.tsx`) leaves the whole review window empty forever:
// board, schematic, findings and artifacts are live code nobody can see.
// `useSilkscreenRun` publishes once, at `done`. A step run has no single end —
// the engineer may stop after `route`, or carry on through review, sourcing,
// order and case — so this file decides three things, deliberately:
//
//   WHEN: from the first step that actually produced a board, and again after
//   every later successful step. Placement is where a board first exists
//   (`place` answers with `kicad_pcb` and `placements`), so waiting for the
//   last step would hide a real board for most runs, and publishing at
//   `propose` would announce a run the board tabs cannot draw.
//
//   WHAT IDENTITY: one entry per engine session (`steps-<session>`), so a
//   later step UPDATES the published run rather than adding a second copy of
//   the same board to the dashboard's history.
//
//   WHAT SHAPE: exactly the fields the step responses actually carry, mapped
//   onto the one-shot `RunResult` where the two vocabularies already agree.
//   Nothing is invented to fill a gap: `nets` is a count on a step response
//   and a list of names on `RunResult`, so it does not cross at all, and the
//   `sourcing`/`enclosure` blocks have no `RunResult` field to cross into.
//   Every field the workbench reads is optional there and has its own empty
//   state, which is why an absent one shows as "not yet" rather than a crash.

/** The published-run id for one engine session. Stable across its steps. */
export function stepRunId(session: string): string {
  return `steps-${session}`;
}

/**
 * The step responses folded into one `RunResult`, latest answer per field.
 *
 * A marker for a step whose response never arrived (see `unreceivedStep`)
 * carries no stage fields at all, so it contributes nothing here — which is
 * right: this client cannot publish a board it never received.
 */
export function stepRunResult(history: readonly StepResponse[]): RunResult {
  const result: RunResult = {};
  const warnings: string[] = [];
  let duration = 0;
  for (const response of history) {
    duration += response.duration_s ?? 0;
    if (response.intent) result.intent = response.intent;
    if (typeof response.kicad_pcb === "string") result.kicad_pcb = response.kicad_pcb;
    if (response.board_mm) result.board_mm = response.board_mm;
    if (typeof response.status === "string") result.status = response.status;
    if (response.placements) result.placements = response.placements;
    if (response.wirelength_mm !== undefined) result.wirelength_mm = response.wirelength_mm;
    if (response.schematic) result.schematic = response.schematic;
    if (typeof response.repair_rounds === "number") result.repair_rounds = response.repair_rounds;
    // `parts` is a count on the propose step and the list on later ones; only
    // the list is what `RunResult.parts` means.
    if (Array.isArray(response.parts)) result.parts = response.parts;
    if (response.routing) result.routing = response.routing;
    if (response.findings) result.findings = response.findings;
    if (response.blockers) result.blockers = response.blockers;
    // The critic's own verdict travels with its findings: a failed review's
    // empty list must reach the dashboard as "nothing known", not "clean".
    if (response.review) result.review = response.review;
    // The step `order` block is the same wire shape as the one-shot one
    // (`OrderBlock`: manifest, issues, verdict, files inline), so `OrderPanel`
    // reads it as written rather than as something it has to guess at.
    if (response.order) result.order = response.order;
    for (const warning of response.warnings ?? []) {
      if (!warnings.includes(warning)) warnings.push(warning);
    }
  }
  if (warnings.length) result.warnings = warnings;
  result.duration_s = duration;
  return result;
}

/**
 * The request as the dashboard needs it. Only the two flags it renders are
 * worth care:
 *
 * `review` says whether a review pass ran, taken from the history rather than
 * from the request — a step run never submits the flag, and the panel's
 * "review was skipped" line must reflect what actually happened. `ground`,
 * `debug` and `summary` are copied from the request as sent, and
 * `time_limit_s` falls back to the same default the form starts at rather
 * than to a zero that a later re-run would send to the engine as a real
 * (and impossible) solver budget.
 */
function stepRunRequest(
  request: StepRequest | null,
  history: readonly StepResponse[],
  intent: string
): RunRequestDraft {
  return {
    intent: request?.intent?.trim() || intent,
    datasheets: { ...(request?.datasheets ?? {}) },
    time_limit_s: request?.time_limit_s ?? DEFAULT_TIME_LIMIT_S,
    review: history.some((response) => response.step === "review"),
    ground: request?.ground ?? false,
    debug: request?.debug ?? false,
    ...(request?.summary ? { summary: request.summary } : {}),
  };
}

/**
 * The stage rows, reduced from the frames the engine actually sent with each
 * step — not guessed from which steps have been pressed. `output: true` is a
 * fact about this route and not an optimism: `/steps` writes a project to
 * disk (`files.schematic`), so unlike `/generate` its schematic stage really
 * does run and its row must not start out marked as skipped.
 */
function stepRunProgress(
  history: readonly StepResponse[],
  request: StepRequest | null
): RunProgress {
  let progress = initialRunProgress({
    datasheets: Object.values(request?.datasheets ?? {}).some((url) => url.trim()),
    review: true,
    route: true,
    output: true,
  });
  for (const response of history) {
    for (const frame of response.events) progress = reduceFrame(progress, frame);
  }
  return progress;
}

/**
 * One published run built from everything the session has answered so far, or
 * null while there is nothing a dashboard could draw.
 *
 * The board is the gate: without `kicad_pcb` there is no file to save, no
 * placements to draw and no copper to report, and announcing a run in that
 * state would replace whatever the dashboard is showing with an empty one.
 */
export function stepRunEntry(
  history: readonly StepResponse[],
  request: StepRequest | null,
  startedAt: number,
  now: number
): RunHistoryEntry | null {
  const last = history[history.length - 1];
  if (!last) return null;
  const result = stepRunResult(history);
  if (typeof result.kicad_pcb !== "string") return null;
  return {
    id: stepRunId(last.session),
    intent: last.intent,
    at: now,
    request: stepRunRequest(request, history, last.intent),
    result,
    // Frames never cross the bridge anyway; the progress below is where the
    // engine's own events survive the trip.
    frames: [],
    progress: stepRunProgress(history, request),
    startedAt,
    finishedAt: now,
    // Wall clock for the whole session, which for a step run includes the
    // time the engineer spent deciding. `result.duration_s` is the engine's
    // own total and stays separate, exactly as it does for a one-shot run.
    elapsedS: (now - startedAt) / 1000,
  };
}

/**
 * The banners one settled step earns, from its own response and nothing
 * else. Placement, routing and review each announce their headline number;
 * a background failure is announced here and only here, when the collecting
 * step's `warnings` carry the engine's sentence (`backgroundFailure` over
 * this one response, so an old warning cannot fire twice). Exported for the
 * hook's tests; `notifyMilestone` itself is a no-op outside the strip.
 */
export function announce(
  response: StepResponse,
  /**
   * Background failures already announced from `background_outcome`, so the
   * collecting step's warning about the same job does not ring twice.
   */
  announcedFailures: ReadonlySet<StepName> = new Set()
): void {
  const placed = placeDetails(response);
  if (placed) {
    notifyMilestone({
      kind: "placed",
      parts: placed.parts.length,
      boardMm: response.board_mm ?? null,
    });
  }
  const routed = routeDetails(response);
  if (routed) {
    notifyMilestone({
      kind: "routed",
      routed: routed.routed,
      total: routed.total,
      unrouted: routed.unrouted.length,
    });
  }
  const reviewed = reviewDetails(response);
  // A critic that answered nothing has no count to announce: "0 findings"
  // is the clean-board reading this outcome exists to refuse.
  if (reviewed && reviewed.status === "ok") {
    notifyMilestone({
      kind: "reviewed",
      findings: reviewed.findings.length,
      blockers: reviewed.blockers,
    });
  }
  for (const step of ["case", "sourcing"] as const) {
    if (announcedFailures.has(step)) continue;
    const warning = backgroundFailure(step, [response]);
    if (warning) notifyMilestone({ kind: `${step}_failed`, warning });
  }
}

/**
 * A fresh idempotency key.
 *
 * Shaped like stripe-python's `_generate_idempotency_key`
 * (`stripe/_api_requestor.py`), which formats 16 random bytes as a uuid: the
 * value carries no meaning and only has to be unique. `crypto.randomUUID` is
 * the same 16 bytes where the webview offers it.
 */
function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  if (typeof c?.getRandomValues === "function") {
    const bytes = c.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

/**
 * The key this start goes out under.
 *
 * A new press of a *different* request is a different run and gets its own
 * key. A press of the same request while the last start is still unanswered
 * reuses the key it was sent under, and that is the whole point: cancelling a
 * start aborts this client's fetch and nothing else — the engine has no
 * session to be told about yet, so it reads the datasheets, plans and proposes
 * to the end regardless (`fail` says so out loud). Without the reuse, starting
 * again is a second read, plan and propose for one board. With it, the engine
 * refuses the repeat with a 409 while the first is still going, and replays
 * the first run's envelope once it lands — which also hands back a session id
 * this client had otherwise lost. `service/steps.py::start_once` is the other
 * half, and the design is Stripe's.
 */
function startKeyFor(
  ref: { current: { key: string; request: string } | null },
  request: StepRequest
): string {
  const fingerprint = JSON.stringify(request);
  const kept = ref.current;
  const key = kept && kept.request === fingerprint ? kept.key : newIdempotencyKey();
  ref.current = { key, request: fingerprint };
  return key;
}

/**
 * The approval-gated run: one request per step, each one the engineer asked for.
 *
 * Same money rule as `useSilkscreenRun`: a single in-flight guard and a single
 * AbortController, so a double click cannot become two paid stages. The two
 * hooks never run together — the page routes a submit to one or the other.
 *
 * Cancelling aborts the fetch, not the step: the engine finishes it under the
 * session lock and records it as done. So after an abort, and after any 409
 * (the same fact, learned the hard way), the hook re-reads the session's
 * status and reconciles: steps done on the engine that never answered here
 * get a marker in `history`, and `available` becomes the engine's `next`.
 * Without that, the stale list offers the step that just ran, every press of
 * it is a 409, and the run is stranded with money already spent.
 */
export function useStepRun({ baseUrl, token, summary }: UseStepRunOptions): StepRun {
  const [status, setStatus] = useState<StepRunStatus>("idle");
  const [session, setSession] = useState<string | null>(null);
  const [history, setHistory] = useState<StepResponse[]>([]);
  const [available, setAvailable] = useState<StepName[]>([]);
  const [running, setRunning] = useState<StepName | null>(null);
  const [failedStep, setFailedStep] = useState<StepName | null>(null);
  const [error, setError] = useState<SilkscreenError | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [publishedId, setPublishedId] = useState<string | null>(null);
  const [background, setBackground] = useState<BackgroundJob[]>([]);
  // The engine's own view of what was typed at this run, and whether it is
  // closed. Both are copied straight off whichever envelope arrived last and
  // are never edited here — `case` consuming a note is a fact only the engine
  // knows, and a client that maintained this list would keep calling a
  // consumed note pending. Absent on an older engine leaves them at their
  // empty defaults, which is the honest reading of "this engine never said".
  const [amendments, setAmendments] = useState<Amendment[]>([]);
  const [cancelled, setCancelled] = useState(false);

  /** Take the lifecycle fields off any envelope that carries them. */
  const applyLifecycle = useCallback(
    (source: { amendments?: Amendment[]; cancelled?: boolean }) => {
      if (Array.isArray(source.amendments)) setAmendments(source.amendments);
      if (typeof source.cancelled === "boolean") setCancelled(source.cancelled);
    },
    []
  );
  // Every step that has ever been listed as running in the background this
  // session. A step that leaves the engine's list has settled, and the only
  // way to know it was ever there is to have remembered it.
  const seenBackgroundRef = useRef<Set<StepName>>(new Set());
  // What each background job was last seen doing, so `applyBackground` can
  // announce the running→settled edge exactly once per job per session.
  const backgroundStateRef = useRef<Map<StepName, BackgroundJob["state"]>>(new Map());
  // Every `background_outcome` the engine has sent this session, merged: a
  // job's outcome arrives once and must survive the envelopes that follow.
  const backgroundOutcomeRef = useRef<BackgroundOutcomes>({});
  // Failures announced from an outcome, so the collecting step's own warning
  // about the same job is not a second banner.
  const announcedFailuresRef = useRef<Set<StepName>>(new Set());

  const inFlightRef = useRef(false);
  // The request the run was started with, and when. Both are what the
  // published entry is built from, and both must survive every render
  // between `start()` and the last approval.
  const requestRef = useRef<StepRequest | null>(null);
  const runStartedAtRef = useRef<number>(0);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  // Mirrors of state that the async tails (settle, fail, resync) must read
  // after a render they did not see: the session a cancelled step belongs
  // to, and the history a resync reconciles against.
  const sessionRef = useRef<string | null>(null);
  const historyRef = useRef<StepResponse[]>([]);
  // A resync belongs to one run; `start`/`reset` bump this so a status reply
  // that lands after the run it described was thrown away is dropped.
  const epochRef = useRef(0);
  const syncRef = useRef<AbortController | null>(null);
  // The `Idempotency-Key` the current start is being attempted under, with
  // the request it belongs to. Kept across an abandoned start and dropped the
  // moment one answers — see `startKeyFor`.
  const startKeyRef = useRef<{ key: string; request: string } | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
      syncRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (status !== "running" || startedAt === null) return;
    const id = window.setInterval(() => setElapsedMs(Date.now() - startedAt), 500);
    return () => window.clearInterval(id);
  }, [status, startedAt]);

  const commitHistory = useCallback((next: StepResponse[]) => {
    historyRef.current = next;
    setHistory(next);
  }, []);

  const commitSession = useCallback((next: string | null) => {
    sessionRef.current = next;
    setSession(next);
  }, []);

  /**
   * Fold the engine's latest `background` list in. `running` undefined means
   * the reply carried no such field (an older engine), which says nothing
   * about the jobs and changes nothing.
   */
  const applyBackground = useCallback(
    (
      running: readonly StepName[] | undefined,
      history: readonly StepResponse[],
      outcome?: BackgroundOutcomes
    ) => {
      backgroundOutcomeRef.current = mergeBackgroundOutcomes(backgroundOutcomeRef.current, outcome);
      if (running === undefined && outcome === undefined) return;
      for (const step of running ?? []) seenBackgroundRef.current.add(step);
      const jobs = backgroundJobs(
        seenBackgroundRef.current,
        running ?? [],
        history,
        backgroundOutcomeRef.current
      );
      // Banners fire on the running→done edge, once per job per session:
      // "finished, press to collect" when the engine said `ok` (or, on an
      // older engine, when the job merely left the list), and the engine's
      // own sentence when it said it failed. A failure an older engine
      // reports only through the collecting step's warning is announced by
      // `settle`, not here.
      for (const job of jobs) {
        const previous = backgroundStateRef.current.get(job.step);
        backgroundStateRef.current.set(job.step, job.state);
        if (previous === job.state) continue;
        if (job.step !== "case" && job.step !== "sourcing") continue;
        if (job.state === "settled" || job.state === "finished") {
          if (previous === "running") notifyMilestone({ kind: `${job.step}_done`, state: "settled" });
        } else if (job.state === "failed" && job.warning && !announcedFailuresRef.current.has(job.step)) {
          announcedFailuresRef.current.add(job.step);
          notifyMilestone({ kind: `${job.step}_failed`, warning: job.warning });
        }
      }
      setBackground(jobs);
    },
    []
  );

  // While a job runs and nothing is pressed, ask the engine whether it is
  // still running. Stops on its own once no job is running — every started
  // job has an outcome in `background_outcome`, has left the list (an older
  // engine), or has been collected — the moment a step is in flight (its
  // envelope carries the list), and on unmount.
  const polling = status === "waiting" && background.some((job) => job.state === "running");
  useEffect(() => {
    if (!polling) return;
    const sessionId = sessionRef.current;
    if (!sessionId) return;
    const epoch = epochRef.current;
    const controller = new AbortController();
    const id = window.setInterval(() => {
      void stepStatus(baseUrl, sessionId, controller.signal, token)
        .then((result) => {
          if (!mountedRef.current || epoch !== epochRef.current || controller.signal.aborted)
            return;
          applyBackground(result.background, historyRef.current, result.background_outcome);
          applyLifecycle(result);
        })
        .catch(() => {
          // A poll that failed is not a failed run: the jobs are still
          // wherever they were, and the next envelope says where.
        });
    }, BACKGROUND_POLL_MS);
    return () => {
      window.clearInterval(id);
      controller.abort();
    };
  }, [polling, baseUrl, token, applyBackground]);

  /**
   * Ask the engine where the session stands and fold the answer in. Never
   * changes `status` except to notice that nothing is left to approve; the
   * caller has already put the hook in the state the cancel or 409 deserves.
   */
  const resync = useCallback(
    (sessionId: string) => {
      const epoch = epochRef.current;
      syncRef.current?.abort();
      const controller = new AbortController();
      syncRef.current = controller;
      void (async () => {
        let result;
        try {
          result = await stepStatus(baseUrl, sessionId, controller.signal, token);
        } catch (caught) {
          if (!mountedRef.current || epoch !== epochRef.current || controller.signal.aborted)
            return;
          setError(
            caught instanceof SilkscreenError
              ? caught
              : new SilkscreenError("server", (caught as Error)?.message ?? "status failed")
          );
          setStatus("error");
          return;
        }
        if (!mountedRef.current || epoch !== epochRef.current || controller.signal.aborted)
          return;
        const reconciled = reconcileHistory(historyRef.current, result);
        for (const marker of reconciled.history) {
          if (isUnreceivedStep(marker) && !historyRef.current.includes(marker)) {
            logEvent(
              `step.${marker.step}`,
              `${marker.step} finished on the engine after the request was cancelled; its result was not received.`
            );
          }
        }
        // A bridge failure that settled after the step answered: the engine's
        // status carries the reason, and the row that claimed KiCad has it
        // must stop claiming so.
        let nextHistory = reconciled.history;
        const last = nextHistory[nextHistory.length - 1];
        if (result.shown_detail && last && last.shown_in_kicad && !last.shown_detail) {
          nextHistory = [
            ...nextHistory.slice(0, -1),
            { ...last, shown_in_kicad: false, shown_detail: result.shown_detail },
          ];
        }
        commitHistory(nextHistory);
        applyBackground(result.background, nextHistory, result.background_outcome);
        applyLifecycle(result);
        setAvailable(reconciled.available);
        setStatus((previous) => {
          if (previous !== "waiting") return previous;
          return reconciled.available.length || reconciled.history.length === 0
            ? "waiting"
            : "done";
        });
      })();
    },
    [baseUrl, token, commitHistory, applyBackground]
  );

  /**
   * Hand the board to the other window, if there is one yet.
   *
   * Called only from `settle`, never from a resync: a reconciled marker means
   * the engine ran a step whose result this client never received, and there
   * is nothing new to publish from it. Failure is swallowed for the same
   * reason the bridge swallows its own — the step already succeeded and was
   * already paid for, and a failed announcement must not read as a failed run.
   */
  const publish = useCallback((history: StepResponse[]) => {
    try {
      const entry = stepRunEntry(history, requestRef.current, runStartedAtRef.current, Date.now());
      if (!entry) return;
      publishRun(entry);
      // Same value on every later step, so this is one state change per
      // session rather than one per approval.
      setPublishedId(entry.id);
    } catch (error) {
      console.warn("[kaleo steps] could not publish the board to the dashboard:", error);
    }
  }, []);

  const settle = useCallback(
    (response: StepResponse) => {
      if (!mountedRef.current) return;
      // The start was answered, so its key has done its job. Dropping it here
      // is what keeps "run that same intent again" a second board rather than
      // a replay of the first one.
      if (response.step === "propose") startKeyRef.current = null;
      const nextHistory = [...historyRef.current, response];
      commitHistory(nextHistory);
      commitSession(response.session);
      applyBackground(response.background, nextHistory, response.background_outcome);
      applyLifecycle(response);
      publish(nextHistory);
      setRunning(null);
      for (const frame of response.events) {
        logServer(frame.event, describeFrame(frame) ?? "", frame);
      }
      const next = availableSteps([response]);
      setAvailable(next);
      setStatus(next.length ? "waiting" : "done");
      logEvent(
        `step.${response.step}`,
        `${response.step} finished in ${response.duration_s.toFixed(1)} s` +
          (response.shown_in_kicad ? " and was shown in KiCad." : ".")
      );
      const unreviewed = reviewFailure(response.review) ?? reviewSkipped(response.review);
      if (response.step === "review" && unreviewed) logEvent("step.review", unreviewed);
      announce(response, announcedFailuresRef.current);
    },
    [commitHistory, commitSession, publish, applyBackground]
  );

  const fail = useCallback(
    (caught: unknown, controller: AbortController, step: StepName) => {
      if (!mountedRef.current) return;
      if (abortRef.current !== controller && controller.signal.aborted) return;
      if (controller.signal.aborted) {
        setRunning(null);
        const sessionId = sessionRef.current;
        if (!sessionId) {
          // The first step was cancelled: no session ever reached this
          // client, so there is nothing to ask the engine about and nothing
          // to show. Back to the blank prompt, not a run with no history.
          setStatus("idle");
          return;
        }
        setStatus((previous) => (previous === "running" ? "waiting" : previous));
        resync(sessionId);
        return;
      }
      const err =
        caught instanceof SilkscreenError
          ? caught
          : new SilkscreenError("server", (caught as Error)?.message ?? "step failed");
      setError(err);
      setRunning(null);
      setFailedStep(step);
      setStatus("error");
      notifyMilestone({
        kind: "run_failed",
        step: STEP_DESCRIPTORS[step]?.label ?? step,
        message: err.message,
      });
      // Out of order means the engine's picture and ours differ; only the
      // engine's is true. Any other failure leaves `available` as the last
      // good response said, which is still what may be retried.
      if (err.status === 409 && sessionRef.current) resync(sessionRef.current);
    },
    [resync]
  );

  const run = useCallback(
    (step: StepName, call: (signal: AbortSignal) => Promise<StepResponse>) => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      // A status reply from before this step could re-offer it once it has
      // run; the step's own response is the authority from here.
      syncRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setRunning(step);
      setFailedStep(null);
      setError(null);
      setStatus("running");
      setStartedAt(Date.now());
      setElapsedMs(0);
      void (async () => {
        try {
          settle(await call(controller.signal));
        } catch (caught) {
          fail(caught, controller, step);
        } finally {
          if (abortRef.current === controller) inFlightRef.current = false;
        }
      })();
    },
    [settle, fail]
  );

  const start = useCallback(
    (request: StepRequest) => {
      if (inFlightRef.current) return;
      if (!request.intent.trim()) {
        setError(
          new SilkscreenError("request", "Describe the board you want before starting a run.")
        );
        setStatus("error");
        return;
      }
      epochRef.current += 1;
      syncRef.current?.abort();
      commitHistory([]);
      commitSession(null);
      setAvailable([]);
      seenBackgroundRef.current = new Set();
      backgroundStateRef.current = new Map();
      backgroundOutcomeRef.current = {};
      announcedFailuresRef.current = new Set();
      setBackground([]);
      setAmendments([]);
      setCancelled(false);
      // A new session publishes under a new id, so the previous board stays in
      // the dashboard's history rather than being overwritten by this one.
      setPublishedId(null);
      requestRef.current = request;
      runStartedAtRef.current = Date.now();
      const key = startKeyFor(startKeyRef, request);
      run("propose", (signal) => startSteps(baseUrl, request, signal, token, key));
    },
    [baseUrl, token, run, commitHistory, commitSession]
  );

  const approve = useCallback(
    (step: StepName, payload: Record<string, unknown> = {}) => {
      if (!session) return;
      const body = { ...summaryPayload(step, summary), ...payload };
      run(step, (signal) => advanceStep(baseUrl, session, step, body, signal, token));
    },
    [baseUrl, token, session, run, summary]
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    inFlightRef.current = false;
  }, []);

  /**
   * Tell the engine to close this session, then stop waiting locally.
   *
   * The order matters: the POST goes first so the flag is set before the
   * running step reports its next event (that is the only seam the engine has
   * to abandon a stage), and the local abort follows so the strip stops
   * claiming to wait on a request whose answer is now moot. What the abort
   * does *not* do is stop the work — the response says which parts of it are
   * still going, and the caller renders that rather than improving on it.
   */
  const cancelRun = useCallback(async (): Promise<CancelResponse> => {
    const sessionId = sessionRef.current;
    if (!sessionId) {
      // The first phase (`POST /steps`) has no session id until it returns,
      // so there is nothing to address. The UI must not offer this then; if
      // it somehow does, saying so is better than a silent no-op.
      throw new SilkscreenError(
        "request",
        "This run has no session yet — the first stage cannot be cancelled."
      );
    }
    const result = await cancelStep(baseUrl, sessionId, undefined, token);
    setCancelled(true);
    cancel();
    return result;
  }, [baseUrl, token, cancel]);

  /** Record a sentence against this run. Spends nothing; see `amendStep`. */
  const amend = useCallback(
    async (text: string, step: StepName | null = null): Promise<AmendResponse> => {
      const sessionId = sessionRef.current;
      if (!sessionId) {
        throw new SilkscreenError(
          "request",
          "This run has no session yet — there is nothing to attach a note to."
        );
      }
      const result = await amendStep(baseUrl, sessionId, text, step, undefined, token);
      // The receipt, not the state: the authoritative list arrives on the next
      // envelope. Appending to a local list here is exactly the client-side
      // copy that would go on calling a consumed note pending.
      if (typeof result.cancelled === "boolean") setCancelled(result.cancelled);
      return result;
    },
    [baseUrl, token]
  );

  const show3d = useCallback(async (): Promise<View3dResponse> => {
    const sessionId = sessionRef.current;
    if (!sessionId) {
      throw new SilkscreenError(
        "request",
        "This run has no session yet — there is no board to show."
      );
    }
    return showBoard3d(baseUrl, sessionId, undefined, token);
  }, [baseUrl, token]);

  const reset = useCallback(() => {
    cancel();
    epochRef.current += 1;
    syncRef.current?.abort();
    setStatus("idle");
    commitSession(null);
    commitHistory([]);
    setAvailable([]);
    setRunning(null);
    setFailedStep(null);
    setError(null);
    setStartedAt(null);
    setElapsedMs(0);
    seenBackgroundRef.current = new Set();
    backgroundStateRef.current = new Map();
    backgroundOutcomeRef.current = {};
    announcedFailuresRef.current = new Set();
    setBackground([]);
    setAmendments([]);
    setCancelled(false);
    // Only this hook's memory of the hand-off is cleared. What was already
    // published stays published: the board on the dashboard is a real board,
    // and clearing the overlay is not a reason to take it off the bench.
    setPublishedId(null);
    requestRef.current = null;
  }, [cancel, commitHistory, commitSession]);

  return {
    status,
    session,
    history,
    running,
    failedStep,
    available,
    error,
    elapsedS: elapsedMs / 1000,
    publishedId,
    background,
    amendments,
    cancelled,
    start,
    approve,
    cancel,
    cancelRun,
    amend,
    show3d,
    reset,
  };
}

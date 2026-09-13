import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DEFAULT_BASE_URL,
  MAX_TIME_LIMIT_S,
  MIN_TIME_LIMIT_S,
  SilkscreenError,
  cancelRun as cancelRunOnEngine,
  generateStream,
  newRunId,
  runStatus,
} from "@/lib/silkscreen/client";
import type {
  GenerateRequest,
  RunResult,
  StreamFrame,
  SummaryMode,
} from "@/lib/silkscreen/types";
import {
  describeFrame,
  initialRunProgress,
  reduceFrame,
  type FeedLine,
  type RunProgress,
  type StageState,
} from "@/lib/silkscreen/describe";
import type { RunPlan } from "@/lib/silkscreen/stages";
import { useEngineHealth, type EngineHealth } from "@/hooks/useEngineHealth";
import { logError, logEvent, logServer } from "@/lib/silkscreen/log";
import { notifyMilestone } from "@/lib/notify/notify";
import {
  publishRun,
  readPublishedRun,
  subscribePublishedRun,
} from "@/lib/silkscreen/bridge";

/**
 * `cancelled` is deliberately its own status and not an error: the user asked
 * for it, so nothing went wrong and nothing should be dressed up as a failure.
 *
 * Distinct from `RunProgress["status"]` in `describe.ts`, which tracks what the
 * *engine* has said about itself. This one is what the *app* is doing, and only
 * this one knows about cancelling.
 */
export type RunStatus =
  | "idle"
  /** Asked for, not yet acknowledged. `run.accepted` has not arrived. */
  | "starting"
  | "running"
  | "done"
  | "error"
  | "cancelled";

/**
 * What one run cost, as the engine reports it (`service/metering.py`).
 *
 * `enabled: false` is a real answer with a `reason` attached, not an absence —
 * the module's `off_block()` returns a dict rather than `None` for exactly
 * that reason. Every field but `enabled` is optional here because an engine
 * that predates metering sends no block at all, and that third case ("we were
 * never told") must not read as either of the other two.
 */
export interface MeteringBlock {
  enabled: boolean;
  /** `committed` | `released` | `refused` | `off` | `unrecorded`. */
  state?: string;
  reason?: string;
  account?: string;
  reserved_mkcu?: number;
  charged_mkcu?: number;
  cost_cents?: number;
}

/**
 * How far a cancel got. Three states, never collapsed into one.
 *
 * The taxonomy is Argo Workflows' — `NodePhase` in
 * `pkg/apis/workflow/v1alpha1/workflow_types.go` keeps `Skipped` (deliberately
 * not run), `Failed` (ran and came back bad) and `Error` ("had an error other
 * than a non-0 exit code", i.e. we could not even get a verdict) as three
 * separate words rather than folding the last into the second. The same three
 * apply to asking a run to stop:
 *
 * - `asked` — the POST is out, nobody has answered. Not "cancelled".
 * - `settled` — the engine answered *and* a later poll saw it reach a terminal
 *   state. This is the only state that may say what the run cost.
 * - `unknown` — the ask failed, or it succeeded and the run was still going
 *   when we stopped looking. The run may still be burning model calls under
 *   `runId`, and saying so is the whole point.
 */
export type CancellationState = "asked" | "settled" | "unknown";

export interface Cancellation {
  state: CancellationState;
  /** The handle `GET /runs/<id>` and `POST /runs/<id>/cancel` take. */
  runId: string;
  /**
   * The engine's own sentence (`service/runs.py::cancel`'s `headline`).
   * Rendered verbatim; this app does not improve on it.
   */
  headline: string | null;
  /** `not_stoppable` — what a cancel cannot interrupt, in the engine's words. */
  notStoppable: string | null;
  /** `aborts_at`, only meaningful while something was actually in flight. */
  abortsAt: string | null;
  /** `state` from the last poll: `running` | `done` | `failed` | `cancelled`. */
  runState: string | null;
  /** The cost block, once the run has settled. Null while it has not. */
  metering: MeteringBlock | null;
  /** Why we have no verdict, when `state` is `unknown`. */
  detail: string | null;
}

/** A poll budget, not a timeout: the engine stops at its next pipeline event,
 *  and a solve can be seconds away from one. Bounded so a run that never
 *  speaks reads as "still running", which is the truth, rather than hanging
 *  the card on a spinner. */
const CANCEL_POLL_TRIES = 8;
const CANCEL_POLL_INTERVAL_MS = 1000;

function readMetering(value: unknown): MeteringBlock | null {
  if (!value || typeof value !== "object") return null;
  const block = value as Record<string, unknown>;
  if (typeof block.enabled !== "boolean") return null;
  return block as unknown as MeteringBlock;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/**
 * The form the user fills in. Mirrors `GenerateRequest`; every field the form
 * has always had is required, and only the additive `summary` is optional.
 */
export interface RunRequestDraft {
  intent: string;
  /** part ref -> datasheet URL. Half-filled rows are dropped by the client. */
  datasheets: Record<string, string>;
  time_limit_s: number;
  review: boolean;
  /** Only meaningful with at least one datasheet — retrieval needs a source. */
  ground: boolean;
  /** Asks the service for `model.response` frames, for the debug console. */
  debug: boolean;
  /**
   * How the finished run summarises itself. The page owns the remembered
   * choice (see `readSummaryMode`) and folds it in at `start()`; it lives in
   * the draft so a run records the mode it was actually asked for, the way it
   * records `review`. Prose is the default because it is what every run before
   * this field produced.
   *
   * The one optional field on the draft: a draft assembled before this field
   * existed is still a valid draft, and an absent mode is the same request the
   * engine answered yesterday rather than a missing answer.
   */
  summary?: SummaryMode;
}

export interface RunHistoryEntry {
  id: string;
  /** `request.intent`, lifted so a history row can label itself. */
  intent: string;
  /** When it finished, epoch ms — what a "2 minutes ago" label wants. */
  at: number;
  /** The request as submitted, not the draft as it stands now. */
  request: RunRequestDraft;
  result: RunResult;
  /** Every frame, in arrival order, for anything that wants the raw log. */
  frames: StreamFrame[];
  progress: RunProgress;
  startedAt: number;
  finishedAt: number;
  /** Wall clock, this app's measurement. `progress.elapsedS` is the engine's. */
  elapsedS: number;
}

export interface SilkscreenRun {
  /** Where the engine lives, and the poll behind the status dot. */
  baseUrl: string;
  setBaseUrl: (url: string) => void;
  /** The optional bearer token, empty when none is configured. */
  token: string;
  setToken: (token: string) => void;
  engine: EngineHealth;

  /** The editable request. */
  request: RunRequestDraft;
  updateRequest: (patch: Partial<RunRequestDraft>) => void;
  setDatasheet: (part: string, url: string) => void;
  removeDatasheet: (part: string) => void;
  /** Copy a past run's request back into the draft so it can be re-run. */
  restoreRequest: (id: string) => void;
  canStart: boolean;

  status: RunStatus;
  /** True while this app is showing a past run rather than the current one. */
  viewingHistory: boolean;
  /** The request that produced what is on screen, or null before the first run. */
  submitted: RunRequestDraft | null;

  /** Everything the frames have said so far. Never guesses ahead of them. */
  progress: RunProgress;
  /** `progress.stages`, lifted because most callers want only this. */
  stages: StageState[];
  /** `progress.feed`, the describer's sentences in arrival order. */
  lines: FeedLine[];
  frames: StreamFrame[];

  /** Wall-clock seconds, ticking from a real clock so a stall reads as a stall. */
  elapsedS: number;
  /** The same clock in milliseconds, for callers that format their own. */
  elapsedMs: number;
  startedAt: number | null;
  result: RunResult | null;
  error: SilkscreenError | null;

  /**
   * Run the draft, or the request passed in — for callers that keep the form
   * in their own state. Returns void so nothing can await, retry, or chain it.
   */
  start: (request?: Partial<GenerateRequest>) => void;
  /**
   * Stop the run — on the engine, not only in this window.
   *
   * Returns void so nothing can await or chain it, exactly like `start`. The
   * POST and the poll that follows it are fire-and-forget; what they learn
   * lands on `cancellation`, which is what a caller renders.
   */
  cancel: () => void;
  /** How far the last cancel got, or null if none was asked for. */
  cancellation: Cancellation | null;
  reset: () => void;

  history: RunHistoryEntry[];
  historyLimit: number;
  viewingId: string | null;
  selectRun: (id: string | null) => void;
  clearHistory: () => void;
}

export interface UseSilkscreenRunOptions {
  baseUrl?: string;
  /** Bearer token for a token-gated engine; empty or absent means no gate. */
  token?: string;
  /** How many finished runs stay in memory. Boards are large; keep it small. */
  historyLimit?: number;
  healthIntervalMs?: number;
}

export const DEFAULT_HISTORY_LIMIT = 8;
export const DEFAULT_TIME_LIMIT_S = 20;
/** A stream is a few hundred frames; this only guards against a runaway one. */
const FRAME_LOG_LIMIT = 2000;

export const EMPTY_REQUEST: RunRequestDraft = {
  intent: "",
  datasheets: {},
  time_limit_s: DEFAULT_TIME_LIMIT_S,
  review: true,
  ground: false,
  debug: false,
  summary: "prose",
};

let seq = 0;
function newId(prefix: string): string {
  seq += 1;
  return `${prefix}-${Date.now().toString(36)}-${seq}`;
}

/** Fold an explicit request over the draft, so a caller may pass all or none. */
function toDraft(
  base: RunRequestDraft,
  request?: Partial<GenerateRequest>
): RunRequestDraft {
  return {
    intent: request?.intent ?? base.intent,
    datasheets: { ...(request?.datasheets ?? base.datasheets) },
    time_limit_s: request?.time_limit_s ?? base.time_limit_s,
    review: request?.review ?? base.review,
    ground: request?.ground ?? base.ground,
    debug: request?.debug ?? base.debug,
    summary: request?.summary ?? base.summary,
  };
}

function hasDatasheet(draft: RunRequestDraft): boolean {
  return Object.entries(draft.datasheets).some(
    ([part, url]) => part.trim() && url.trim()
  );
}

/**
 * Which stages this request can emit at all.
 *
 * `output: false` is not a guess — nothing over HTTP passes an output path, so
 * `schematic_stage` provably does no work and its row must not sit pending
 * forever. `route` is the engine's own default and the service does not
 * override it.
 */
function planFor(draft: RunRequestDraft): RunPlan {
  return {
    datasheets: hasDatasheet(draft),
    review: draft.review,
    route: true,
    output: false,
  };
}

/**
 * Anything that is not already a `SilkscreenError` still has to reach the UI
 * with a `kind`, because that is the only thing the UI switches on.
 */
function toSilkscreenError(error: unknown): SilkscreenError {
  if (error instanceof SilkscreenError) return error;
  const err = error as Error | undefined;
  if (err?.name === "TimeoutError") {
    return new SilkscreenError("timeout", "The engine did not answer in time.", {
      detail: err?.message ?? "",
    });
  }
  return new SilkscreenError(
    "server",
    err?.message || "The run failed for an unknown reason."
  );
}

/**
 * The whole state machine for one board run.
 *
 * Call this ONCE, from `RunProvider`. Everything else reads it through
 * `useSilkscreenRun()` in `@/contexts/run.context`.
 *
 * The money rule shapes most of what follows: every `start()` is at most one
 * request to the engine, which spends real Gemini credit. There is no retry
 * anywhere in here — not on error, not on a dead stream, not on a failed health
 * check — and the client's single fallback (a 404, meaning the stream route
 * does not exist and so no run was ever started) is left exactly as it is.
 */
export function useSilkscreenRunState(
  options: UseSilkscreenRunOptions = {}
): SilkscreenRun {
  const historyLimit = options.historyLimit ?? DEFAULT_HISTORY_LIMIT;

  const [baseUrl, setBaseUrl] = useState(options.baseUrl ?? DEFAULT_BASE_URL);
  const [token, setToken] = useState(options.token ?? "");
  // The recovery banner: `notifyMilestone` fires only from the strip's
  // webview, so the dashboard running this same provider adds no second one.
  const engine = useEngineHealth(baseUrl, options.healthIntervalMs, token, {
    onRecovered: (url) => notifyMilestone({ kind: "engine_up", baseUrl: url }),
  });

  const [request, setRequest] = useState<RunRequestDraft>(EMPTY_REQUEST);
  const [submitted, setSubmitted] = useState<RunRequestDraft | null>(null);

  const [status, setStatus] = useState<RunStatus>("idle");
  const [progress, setProgress] = useState<RunProgress>(() =>
    initialRunProgress()
  );
  const [frames, setFrames] = useState<StreamFrame[]>([]);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<SilkscreenError | null>(null);

  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);

  const [history, setHistory] = useState<RunHistoryEntry[]>([]);
  const [viewingId, setViewingId] = useState<string | null>(null);

  const [cancellation, setCancellation] = useState<Cancellation | null>(null);

  const mountedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);
  // Set synchronously, unlike `status`, so a double-click cannot start two runs.
  const inFlightRef = useRef(false);
  // The run's name on the engine. Minted before the request leaves so a run
  // whose response never arrives is still addressable, and overwritten by the
  // engine's own id the moment a frame carries one — an older service, or a
  // proxy that dropped the header, mints its own and that one is authoritative.
  const runIdRef = useRef<string | null>(null);
  // The cost block off the terminal frame. Read where it is asked for — after
  // a cancel — and deliberately nowhere else; the strip is not a meter.
  const meteringRef = useRef<MeteringBlock | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // A dropped provider must not leave a paid run streaming into nothing.
      abortRef.current?.abort();
      abortRef.current = null;
      inFlightRef.current = false;
    };
  }, []);

  // The internal status, readable from long-lived closures. `status` itself is
  // render state; the bridge subscription below outlives any one render.
  const statusRef = useRef<RunStatus>("idle");
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  // A run finished in ANOTHER window (the overlay, usually) arrives over the
  // storage bridge and joins this provider's history — this is what lets the
  // dashboard show a board it never generated. A live run in this window
  // always wins: adoption never interrupts one, and an entry already in
  // history (same id) is left alone. On mount, a run published before this
  // window existed is restored into history only; a live event additionally
  // lands on screen when this window is sitting idle.
  useEffect(() => {
    const adopt = (entry: RunHistoryEntry, show: boolean) => {
      if (inFlightRef.current) return;
      setHistory((previous) => {
        const at = previous.findIndex((h) => h.id === entry.id);
        if (at === -1) return [entry, ...previous].slice(0, historyLimit);
        // Same id, newer publication: one run said more about itself. That is
        // an approval-gated run growing a step at a time (see
        // `useStepRun.stepRunEntry`, which publishes one entry per engine
        // session), so the row is REPLACED — a second copy of the same board
        // in the history rail would be the same run pretending to be two.
        // A stale or repeated announcement (`at` not newer) is left alone.
        if (previous[at].at >= entry.at) return previous;
        const next = [...previous];
        next[at] = entry;
        return next;
      });
      if (show && statusRef.current === "idle") {
        setViewingId(entry.id);
      }
    };
    const existing = readPublishedRun();
    if (existing) adopt(existing, false);
    return subscribePublishedRun((entry) => adopt(entry, true));
  }, [historyLimit]);

  // The clock is independent of the event stream on purpose: if the engine goes
  // quiet for ninety seconds, the user should see ninety seconds pass, not a
  // frozen number that reads as "finished".
  useEffect(() => {
    if ((status !== "running" && status !== "starting") || startedAt === null)
      return;
    setElapsedMs(Date.now() - startedAt);
    const timer = window.setInterval(() => {
      setElapsedMs(Date.now() - startedAt);
    }, 200);
    return () => window.clearInterval(timer);
  }, [status, startedAt]);

  const updateRequest = useCallback((patch: Partial<RunRequestDraft>) => {
    setRequest((previous) => {
      const next = { ...previous, ...patch };
      if (patch.time_limit_s !== undefined) {
        const n = Number(patch.time_limit_s);
        next.time_limit_s = Number.isFinite(n)
          ? Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, Math.round(n)))
          : previous.time_limit_s;
      }
      return next;
    });
  }, []);

  const setDatasheet = useCallback((part: string, url: string) => {
    setRequest((previous) => ({
      ...previous,
      datasheets: { ...previous.datasheets, [part]: url },
    }));
  }, []);

  const removeDatasheet = useCallback((part: string) => {
    setRequest((previous) => {
      const datasheets = { ...previous.datasheets };
      delete datasheets[part];
      // Grounding with nothing to ground on says nothing; drop the flag too.
      const stillHasOne = Object.values(datasheets).some((u) => u.trim());
      return {
        ...previous,
        datasheets,
        ground: stillHasOne ? previous.ground : false,
      };
    });
  }, []);

  const restoreRequest = useCallback(
    (id: string) => {
      const entry = history.find((h) => h.id === id);
      if (entry) {
        setRequest({ ...entry.request, datasheets: { ...entry.request.datasheets } });
      }
    },
    [history]
  );

  const start = useCallback(
    (override?: Partial<GenerateRequest>) => {
    if (inFlightRef.current) return;

    const draft = toDraft(request, override);
    if (!draft.intent.trim()) {
      setError(
        new SilkscreenError(
          "request",
          "Describe the board you want before starting a run."
        )
      );
      setStatus("error");
      return;
    }

    inFlightRef.current = true;
    const controller = new AbortController();
    abortRef.current = controller;
    const runId = newRunId();
    runIdRef.current = runId;
    meteringRef.current = null;
    setCancellation(null);

    const began = Date.now();
    // Accumulate into values owned by this run rather than reading state back
    // out: the history entry needs the finished log, and a state updater is not
    // a place to do work.
    let collectedFrames: StreamFrame[] = [];
    let collectedProgress = initialRunProgress(planFor(draft));

    setSubmitted(draft);
    setViewingId(null);
    // Not "running" yet: nothing has come back, and saying otherwise would be
    // the UI asserting something the engine has not confirmed.
    setStatus("starting");
    setFrames(collectedFrames);
    setProgress(collectedProgress);
    setResult(null);
    setError(null);
    setStartedAt(began);
    setElapsedMs(0);

    const onFrame = (frame: StreamFrame) => {
      // Read before the abort guard: the engine's own name for this run and
      // the cost it settled on arrive on frames, and a client that hung up
      // mid-run still needs both to ask what became of it.
      const named = text(frame.run_id);
      if (named) runIdRef.current = named;
      const metered = readMetering(frame.metering);
      if (metered) meteringRef.current = metered;
      if (!mountedRef.current || controller.signal.aborted) return;
      collectedFrames =
        collectedFrames.length >= FRAME_LOG_LIMIT
          ? [...collectedFrames.slice(1), frame]
          : [...collectedFrames, frame];
      // `reduceFrame` is documented never to throw and to count, not crash on,
      // an event name this build has never heard of.
      collectedProgress = reduceFrame(collectedProgress, frame);
      // Mirror into the debug console. `logServer` is the sanctioned path: it
      // scrubs credentials and reduces `result.kicad_pcb` to a length marker.
      logServer(frame.event, describeFrame(frame) ?? "", frame);
      setFrames(collectedFrames);
      setProgress(collectedProgress);
      // The first frame is the engine acknowledging the run.
      setStatus((previous) => (previous === "starting" ? "running" : previous));
    };

    // Deliberately fire-and-forget: `start()` returns void so no caller can
    // await it, retry it, or chain a second request onto it.
    void (async () => {
      try {
        const runResult = await generateStream(
          baseUrl,
          {
            intent: draft.intent,
            datasheets: draft.datasheets,
            time_limit_s: draft.time_limit_s,
            review: draft.review,
            ...(draft.ground ? { ground: true } : {}),
            ...(draft.debug ? { debug: true } : {}),
            ...(draft.summary ? { summary: draft.summary } : {}),
          },
          onFrame,
          controller.signal,
          token,
          runId
        );
        if (!mountedRef.current) return;
        if (controller.signal.aborted) return; // cancel() already set the state

        const finished = Date.now();
        setResult(runResult);
        setStatus("done");
        setElapsedMs(finished - began);

        // The entry holds the same object references the live view is showing,
        // so keeping a run in history costs nothing beyond the array slot.
        const entry: RunHistoryEntry = {
          id: newId("run"),
          intent: draft.intent,
          at: finished,
          request: draft,
          result: runResult,
          frames: collectedFrames,
          progress: collectedProgress,
          startedAt: began,
          finishedAt: finished,
          elapsedS: (finished - began) / 1000,
        };
        setHistory((previous) => [entry, ...previous].slice(0, historyLimit));
        // Announce across windows: the dashboard adopts this entry and shows
        // the board without this window having to push anything else.
        publishRun(entry);
        logEvent("run.finished", `Run finished in ${entry.elapsedS.toFixed(1)} s.`);
        // Total is routed + unrouted, the router's own honesty contract: a
        // refusal names every net it covers, so 0/0 cannot read as complete.
        const routedNets = runResult.routing?.routed?.length ?? 0;
        const unroutedNets = Object.keys(runResult.routing?.unrouted ?? {}).length;
        notifyMilestone({
          kind: "run_done",
          routed: routedNets,
          total: routedNets + unroutedNets,
          unrouted: unroutedNets,
          blockers: runResult.blockers?.length ?? 0,
        });
      } catch (caught) {
        if (!mountedRef.current) return;
        // A stale run — one that was cancelled and then superseded by a new
        // start() — must not write anything: its late rejection would clobber
        // the live run's status. Only the run that still owns abortRef may
        // report a terminal state.
        if (abortRef.current !== controller && controller.signal.aborted) {
          return;
        }
        // Check the signal first: an abort during the opening fetch surfaces
        // from the client as an `offline` SilkscreenError, which would
        // otherwise read as "the engine is down" when it plainly is not.
        if (controller.signal.aborted) {
          setStatus("cancelled");
          setElapsedMs(Date.now() - began);
          return;
        }
        const failure = toSilkscreenError(caught);
        logError("run.failed", failure.message, {
          kind: failure.kind,
          status: failure.status,
          errorId: failure.errorId,
        });
        setError(failure);
        setStatus("error");
        setElapsedMs(Date.now() - began);
        notifyMilestone({ kind: "run_failed", step: "Run", message: failure.message });
      } finally {
        // The in-flight guard, like the abort handle, belongs to the CURRENT
        // run: a stale run releasing it would let a new start() fire while
        // another run is still streaming — a second paid run for one action.
        if (abortRef.current === controller) {
          abortRef.current = null;
          inFlightRef.current = false;
        }
      }
    })();
    },
    [baseUrl, token, historyLimit, request]
  );

  /**
   * Ask the engine to stop, then find out whether it did.
   *
   * Until this existed the button aborted a socket and nothing else: the
   * pipeline went on solving and went on spending, and the strip said
   * "cancelled". `service/runs.py` gives the run a name and a cancel route,
   * and `client.ts` has had `cancelRun`/`runStatus` for it, unused.
   *
   * The two halves are deliberately separate, because the engine's own
   * docstring is careful about it: `runs.cancel` "says *requested*, not
   * *stopped*", the flag being read only when the pipeline next emits an
   * event — Celery's `Control.revoke`, which the docstring cites. So the POST
   * answers "we asked", and the poll that follows answers "and this is where
   * it got to". Neither is allowed to speak for the other.
   */
  const askEngineToStop = useCallback(
    (runId: string) => {
      setCancellation({
        state: "asked",
        runId,
        headline: null,
        notStoppable: null,
        abortsAt: null,
        runState: null,
        metering: null,
        detail: null,
      });
      void (async () => {
        let answer: Record<string, unknown>;
        try {
          answer = await cancelRunOnEngine(baseUrl, runId, undefined, token);
        } catch (caught) {
          if (!mountedRef.current) return;
          // We could not even ask. The run is not stopped, and calling it
          // cancelled here would be this window asserting something no
          // engine confirmed.
          setCancellation({
            state: "unknown",
            runId,
            headline: null,
            notStoppable: null,
            abortsAt: null,
            runState: null,
            metering: meteringRef.current,
            detail: toSilkscreenError(caught).message,
          });
          return;
        }
        const base: Cancellation = {
          state: "asked",
          runId,
          headline: text(answer.headline),
          notStoppable: text(answer.not_stoppable),
          abortsAt: answer.in_flight === true ? text(answer.aborts_at) : null,
          runState: text(answer.state),
          metering: readMetering(answer.metering) ?? meteringRef.current,
          detail: null,
        };
        if (mountedRef.current) setCancellation(base);
        // Already over when the ask landed: no poll can add anything.
        if (base.runState && base.runState !== "running") {
          if (mountedRef.current) setCancellation({ ...base, state: "settled" });
          return;
        }
        for (let tries = 0; tries < CANCEL_POLL_TRIES; tries += 1) {
          await new Promise((done) =>
            setTimeout(done, CANCEL_POLL_INTERVAL_MS)
          );
          if (!mountedRef.current) return;
          // A newer run owns the hook now; this one's late verdict must not
          // land on top of it. Same rule the run's own catch follows.
          if (runIdRef.current !== runId) return;
          let poll: Record<string, unknown> | null;
          try {
            poll = await runStatus(baseUrl, runId, undefined, token);
          } catch {
            continue; // One unreachable poll is not a verdict. Try again.
          }
          if (!mountedRef.current || runIdRef.current !== runId) return;
          // 404: the engine forgot it or restarted. A real answer, and not
          // one that says the run stopped.
          if (poll === null) {
            setCancellation({
              ...base,
              state: "unknown",
              detail:
                "The engine no longer has a record of this run — it was " +
                "forgotten or the service restarted.",
            });
            return;
          }
          const state = text(poll.state);
          if (state && state !== "running") {
            setCancellation({
              ...base,
              state: "settled",
              runState: state,
              metering: readMetering(poll.metering) ?? meteringRef.current,
            });
            return;
          }
        }
        if (!mountedRef.current || runIdRef.current !== runId) return;
        // Still going. The honest word is that we stopped looking, not that
        // it stopped: it is inside a solve or a model call, and that is
        // billed whether or not anyone is watching.
        setCancellation({
          ...base,
          state: "unknown",
          runState: "running",
          metering: meteringRef.current,
          detail:
            "It had not reached its next pipeline event while this window was " +
            "watching, so it is still going and still being billed.",
        });
      })();
    },
    [baseUrl, token]
  );

  const cancel = useCallback(() => {
    const controller = abortRef.current;
    // The engine first, then this window's ears. The POST goes out even when
    // the stream has already been dropped — the run outlives the socket, and
    // the whole failure this closes is a strip that stopped listening to a
    // pipeline that never stopped working.
    const runId = runIdRef.current;
    if (runId) askEngineToStop(runId);
    if (!controller) return;
    controller.abort();
    // Set it here as well as in the catch: the reader may take a moment to
    // notice, and the button must stop saying "running" the instant it is hit.
    setStatus("cancelled");
    // Release the guard for the same reason. The status says idle immediately,
    // so the next submission is enabled immediately, and a guard still held by
    // a run the reader has not noticed yet would discard it in silence.
    // `abortRef` deliberately stays put: it is what tells this run's own catch
    // and finally that a newer run has taken over, and clearing it here would
    // cost the cancelled run its elapsed time.
    inFlightRef.current = false;
  }, [askEngineToStop]);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    inFlightRef.current = false;
    runIdRef.current = null;
    meteringRef.current = null;
    setCancellation(null);
    setStatus("idle");
    setSubmitted(null);
    setFrames([]);
    setProgress(initialRunProgress());
    setResult(null);
    setError(null);
    setStartedAt(null);
    setElapsedMs(0);
    setViewingId(null);
  }, []);

  const selectRun = useCallback(
    (id: string | null) => {
      // Flipping the view away from a live run would hide the thing the user is
      // paying for; make them cancel first.
      if (status === "running" || status === "starting") return;
      setViewingId(id);
    },
    [status]
  );

  const clearHistory = useCallback(() => {
    setHistory([]);
    setViewingId(null);
  }, []);

  const viewed = useMemo(
    () => (viewingId ? history.find((h) => h.id === viewingId) ?? null : null),
    [history, viewingId]
  );

  const shownProgress = viewed ? viewed.progress : progress;
  const busy = status === "running" || status === "starting";
  const canStart = !busy && request.intent.trim().length > 0;

  return {
    baseUrl,
    setBaseUrl,
    token,
    setToken,
    engine,

    request,
    updateRequest,
    setDatasheet,
    removeDatasheet,
    restoreRequest,
    canStart,

    status: viewed ? "done" : status,
    viewingHistory: viewed !== null,
    submitted: viewed ? viewed.request : submitted,

    progress: shownProgress,
    stages: shownProgress.stages,
    lines: shownProgress.feed,
    frames: viewed ? viewed.frames : frames,

    elapsedS: viewed ? viewed.elapsedS : elapsedMs / 1000,
    elapsedMs: viewed ? viewed.finishedAt - viewed.startedAt : elapsedMs,
    startedAt: viewed ? viewed.startedAt : startedAt,
    result: viewed ? viewed.result : result,
    error: viewed ? null : error,

    start,
    cancel,
    cancellation,
    reset,

    history,
    historyLimit,
    viewingId,
    selectRun,
    clearHistory,
  };
}

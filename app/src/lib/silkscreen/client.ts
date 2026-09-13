// The only place Kaleo talks to the silkscreen engine.
//
// The boundary is HTTP and it is deliberate. This directory is GPL-3.0 and the
// Python side is MIT; they stay separable because they are two programs that
// communicate, never one program in two languages (see `app/NOTICE.md`). So
// everything here goes through the documented `/healthz`, `/generate` and
// `/generate/stream` surface, and nothing here imports, embeds, or vendors
// engine source.
//
// `fetch` is Tauri's, not the webview's: the app origin is `tauri://localhost`
// and the service is `http://127.0.0.1:PORT`, which is cross-origin. The
// service ships no CORS headers on purpose and must not grow any, so the
// request goes through Rust instead, where the same-origin policy does not
// apply. That is also why `http:default` in `src-tauri/capabilities` allows
// the loopback origins the engine may live on.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import type {
  DeliverConfig,
  DeliverRequest,
  DeliverResponse,
  GenerateRequest,
  RunError,
  RunResult,
  StepName,
  StepRequest,
  StepResponse,
  StepStatusResponse,
  AmendResponse,
  CancelResponse,
  StreamFrame,
} from "./types";

export const DEFAULT_BASE_URL = "http://127.0.0.1:8081";

/** A solve can legitimately run for minutes; 300 s is this client's own ceiling. */
export const REQUEST_TIMEOUT_MS = 300_000;
export const MIN_TIME_LIMIT_S = 5;
export const MAX_TIME_LIMIT_S = 60;

/**
 * What went wrong, in the terms the UI switches on.
 *
 * `kind` is the whole point: "the engine is not running" and "the engine ran
 * and refused your prompt" are different conversations with the user, and a
 * bare status code cannot tell them apart.
 */
export type ErrorKind =
  | "offline" // nothing is listening; the engine was never reached
  | "setup" // reached it, but it has no GOOGLE_API_KEY
  | "auth" // 401 — the engine has a token gate and ours is missing or wrong
  | "request" // 400/413 — the prompt or its options were rejected
  | "upstream" // 502/503 — the model provider failed
  | "server" // 500 — a bug on the engine side, carries an error_id
  | "timeout"
  | "cancelled";

export class SilkscreenError extends Error {
  kind: ErrorKind;
  status: number;
  errorId: string;
  detail: string;
  /**
   * The engine's name for the run this failed on, when there is one.
   *
   * The point of carrying it on the error rather than only on the result: a
   * stream that dies is exactly the case where the run is still going and
   * still being paid for, and this is the handle `runStatus` and `cancelRun`
   * take. Empty when the failure happened before a run existed.
   */
  runId: string;

  constructor(
    kind: ErrorKind,
    message: string,
    { status = 0, errorId = "", detail = "", runId = "" } = {}
  ) {
    super(message);
    this.name = "SilkscreenError";
    this.kind = kind;
    this.status = status;
    this.errorId = errorId;
    this.detail = detail;
    this.runId = runId;
  }
}

/**
 * A name for a run, chosen before the request is sent.
 *
 * Same shape as `useStepRun`'s `newIdempotencyKey`, and deliberately so: both
 * are 16 random bytes the way stripe-python's `_generate_idempotency_key`
 * mints one. What it buys is different, though — an idempotency key stops a
 * second run, a run id addresses the first. A client that chose the id knows
 * the run's name before the response exists, which is the only way a request
 * that never answers at all is still pollable and cancellable. LiteLLM's
 * proxy accepts a caller's id the same way, on `x-litellm-call-id`
 * (`litellm/proxy/common_request_processing.py`,
 * `ProxyBaseLLMRequestProcessing.common_processing_pre_call_logic`).
 */
export function newRunId(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return `run_${c.randomUUID()}`;
  if (typeof c?.getRandomValues === "function") {
    const bytes = c.getRandomValues(new Uint8Array(16));
    return `run_${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
  }
  return `run_${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

/**
 * The caller's abort signal combined with this client's own ceiling. Passing
 * a signal must not silently remove the timeout — the app always passes one,
 * so `signal ?? timeout` would leave every real request without a deadline.
 *
 * The ceiling's own leg is kept visible: when the deadline (not the caller)
 * is what killed the request, the plugin surfaces a bare cancellation with no
 * usable name, so the only way to report an honest `timeout` kind is to ask
 * this signal afterwards.
 */
function withTimeout(signal?: AbortSignal): {
  signal: AbortSignal;
  timedOut: () => boolean;
} {
  const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  return {
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    timedOut: () => timeout.aborted,
  };
}

function timeoutError(): SilkscreenError {
  return new SilkscreenError(
    "timeout",
    `The engine did not finish within this app's ${Math.round(
      REQUEST_TIMEOUT_MS / 60_000
    )}-minute ceiling.`
  );
}

function clampTimeLimit(value: number | undefined): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return MIN_TIME_LIMIT_S;
  return Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, Math.round(n)));
}

/** Drop half-filled datasheet rows and clamp the solver budget to this app's 5–60 s range. */
export function normalizeRequest(request: GenerateRequest): GenerateRequest {
  const datasheets: Record<string, string> = {};
  for (const [part, url] of Object.entries(request.datasheets ?? {})) {
    const p = String(part).trim();
    const u = String(url).trim();
    if (p && u) datasheets[p] = u;
  }
  return {
    intent: String(request.intent ?? "").trim(),
    datasheets,
    time_limit_s: clampTimeLimit(request.time_limit_s),
    review: request.review !== false,
    // Grounding is opt-in and only sent when asked for: an absent flag is the
    // service's default, so a stray `ground: false` would say nothing. And
    // never after normalization emptied `datasheets` — the service 400s a
    // ground request with nothing to ground on.
    ...(request.ground === true && Object.keys(datasheets).length > 0
      ? { ground: true }
      : {}),
    ...(request.debug === true ? { debug: true } : {}),
    // Additive, and sent explicitly in both modes: it says how the finished run
    // is summarised, so "the user chose prose" and "nobody said" are the same
    // request to an engine that already defaults to prose, and an engine that
    // has never heard of the field ignores an unknown key rather than failing.
    // Anything that is not one of the two modes is dropped rather than passed
    // through — a request must never carry a value the engine cannot read.
    ...(request.summary === "structured" || request.summary === "prose"
      ? { summary: request.summary }
      : {}),
  };
}

/**
 * A missing API key is a setup problem, not an outage.
 *
 * The service answers it as a 502 like any other upstream failure, so the only
 * thing separating "go export your key" from "Gemini is down" is the message
 * text. Getting this wrong sends the user to a status page over a five-second
 * fix.
 */
function kindForStatus(status: number, body: Partial<RunError>): ErrorKind {
  const text = `${body.error ?? ""} ${body.detail ?? ""}`;
  if (status === 502 || status === 503) {
    return /GOOGLE_API_KEY|api key/i.test(text) ? "setup" : "upstream";
  }
  if (status === 401) return "auth";
  if (status === 400 || status === 413) return "request";
  if (status >= 500) return "server";
  return "server";
}

function errorFromBody(status: number, body: Partial<RunError>): SilkscreenError {
  return new SilkscreenError(
    kindForStatus(status, body),
    body.error || `The engine answered ${status}.`,
    { status, errorId: body.error_id ?? "", detail: body.detail ?? "" }
  );
}

/**
 * The `Authorization` header, when a token is configured.
 *
 * A deployed engine can sit behind a bearer-token gate; locally with no token
 * there is no gate. An empty or whitespace token sends nothing at all — a
 * bare `Bearer ` header would turn "no token configured" into a 401.
 */
export function authHeaders(token?: string): Record<string, string> {
  const trimmed = (token ?? "").trim();
  return trimmed ? { Authorization: `Bearer ${trimmed}` } : {};
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * Is the engine up?
 *
 * Returns the reason rather than throwing: this runs on a timer behind a
 * status dot, and an unreachable engine is an ordinary state for this app to
 * be in, not an exception.
 */
export async function health(
  baseUrl: string,
  token?: string
): Promise<{ ok: boolean; detail: string }> {
  try {
    const response = await tauriFetch(`${baseUrl}/healthz`, {
      method: "GET",
      headers: authHeaders(token),
      signal: AbortSignal.timeout(4000),
    });
    if (!response.ok) return { ok: false, detail: `answered ${response.status}` };
    const body = await readJson(response);
    return body?.ok === true
      ? { ok: true, detail: "" }
      : { ok: false, detail: "answered without ok:true" };
  } catch (error) {
    return { ok: false, detail: (error as Error)?.message ?? "unreachable" };
  }
}

/** One-shot generation. Used as the fallback when the stream never begins. */
export async function generate(
  baseUrl: string,
  request: GenerateRequest,
  signal?: AbortSignal,
  token?: string
): Promise<RunResult> {
  const deadline = withTimeout(signal);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders(token) },
      body: JSON.stringify(normalizeRequest(request)),
      signal: deadline.signal,
    });
  } catch (error) {
    if (deadline.timedOut() && !signal?.aborted) throw timeoutError();
    throw error;
  }
  const body = await readJson(response);
  if (!response.ok) throw errorFromBody(response.status, body as Partial<RunError>);
  return body as RunResult;
}

/**
 * Streaming generation: NDJSON frames, one per engine event.
 *
 * `onFrame` is called for every parsed frame in arrival order and the promise
 * resolves with the run's result, taken from the terminal `run.done`.
 *
 * The fallback to `generate()` fires **only** when the stream never began — a
 * 404 from a service too old to have the route. Every other failure mode
 * happens after a 200, by which point the engine has already started a paid
 * run, and quietly re-running it would bill the user twice for one prompt.
 */
export async function generateStream(
  baseUrl: string,
  request: GenerateRequest,
  onFrame: (frame: StreamFrame) => void,
  signal?: AbortSignal,
  token?: string,
  runId: string = newRunId()
): Promise<RunResult> {
  const payload = normalizeRequest(request);
  const deadline = withTimeout(signal);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/generate/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(token),
        // Named before the request leaves, so a run whose response never
        // arrives is still pollable and cancellable by this client. An engine
        // that predates the header ignores it and mints its own, which the
        // frames and `X-Kaleo-Run-Id` still carry back.
        "X-Kaleo-Run-Id": runId,
      },
      body: JSON.stringify(payload),
      signal: deadline.signal,
    });
  } catch (error) {
    if (deadline.timedOut() && !signal?.aborted) throw timeoutError();
    throw new SilkscreenError(
      "offline",
      "Could not reach the silkscreen engine.",
      { detail: (error as Error)?.message ?? "", runId }
    );
  }

  // The one safe fallback: the route does not exist, so no run has started.
  if (response.status === 404) return generate(baseUrl, request, signal, token);

  if (!response.ok) {
    // Pre-stream validation (400/413) still answers plain JSON.
    throw errorFromBody(response.status, (await readJson(response)) as Partial<RunError>);
  }

  // The engine's answer wins over ours: an older service, or one behind a
  // proxy that dropped the request header, mints its own.
  const liveRunId = response.headers?.get?.("x-kaleo-run-id") || runId;

  const body = response.body;
  if (!body) {
    throw new SilkscreenError(
      "server",
      "The engine accepted the run but sent no stream to follow. " +
        `It is still running as ${liveRunId}; it was not started again.`,
      { status: response.status, runId: liveRunId }
    );
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let result: RunResult | null = null;
  let failure: SilkscreenError | null = null;

  const handle = (line: string) => {
    const frame = parseFrame(line);
    if (!frame) return;
    onFrame(frame);
    if (frame.event === "run.done") result = (frame.result ?? {}) as RunResult;
    if (frame.event === "run.error") {
      failure = errorFromBody(
        Number(frame.status ?? 500),
        frame as unknown as Partial<RunError>
      );
      failure.runId = String(frame.run_id ?? liveRunId);
    }
    if (frame.event === "run.cancelled") {
      // The engine stopped because somebody asked it to. `cancelled` rather
      // than `server`, so the UI does not report a deliberate stop as a bug.
      failure = new SilkscreenError(
        "cancelled",
        String(frame.detail ?? "The run was cancelled."),
        { status: 409, runId: String(frame.run_id ?? liveRunId) }
      );
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, { stream: true });
      let newline = buffered.indexOf("\n");
      while (newline !== -1) {
        handle(buffered.slice(0, newline));
        buffered = buffered.slice(newline + 1);
        newline = buffered.indexOf("\n");
      }
    }
  } catch (error) {
    // A stream killed by this client's own ceiling rejects with a bare,
    // nameless cancellation from the http plugin; asking the deadline is the
    // only way to keep it from reading as "the run failed for an unknown
    // reason". A caller abort is left for the caller's own signal check.
    if (deadline.timedOut() && !signal?.aborted) throw timeoutError();
    // A terminal frame already arrived, so the run finished on the engine and
    // the board is in hand; the connection dying on the next read does not
    // un-finish it. Reporting a completed board as failed would also drop it
    // from history, which is the one copy the user has.
    if (result === null && failure === null) throw error;
  }
  handle(buffered);

  if (failure) throw failure;
  if (!result) {
    // The connection closed before `run.done`. Something did happen on the
    // engine side, so this must not silently re-run — but it is no longer
    // invisible either: the run has a name, and `runStatus`/`cancelRun` take
    // it. That is the §6 gap in docs/paid-run-safety.md, closed.
    throw new SilkscreenError(
      "server",
      "The engine closed the stream before the run finished. It may still be " +
        `running as ${liveRunId} — check it rather than starting again.`,
      { runId: liveRunId }
    );
  }
  return result;
}

/**
 * `GET /runs/<id>` — what became of a run whose stream this client lost.
 *
 * Deliberately not a way to recover the board: the engine keeps no result for
 * a streamed run (see `service/runs.py`), so this answers "did it finish, and
 * what did it cost" and nothing more. A 404 means the engine has forgotten it
 * or restarted, which is a real answer and not an error to retry.
 */
export async function runStatus(
  baseUrl: string,
  runId: string,
  signal?: AbortSignal,
  token?: string
): Promise<Record<string, unknown> | null> {
  let response: Response;
  try {
    response = await tauriFetch(
      `${baseUrl}/runs/${encodeURIComponent(runId)}`,
      { headers: authHeaders(token), signal }
    );
  } catch (error) {
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
      runId,
    });
  }
  if (response.status === 404) return null;
  const body = await readJson(response);
  if (!response.ok) throw errorFromBody(response.status, body as Partial<RunError>);
  return body as Record<string, unknown>;
}

/**
 * `POST /runs/<id>/cancel` — ask a streamed run to stop.
 *
 * The same honesty rules as `cancelStep`: the answer's `aborts_at` and
 * `not_stoppable` say what is still going to finish and be billed. Render
 * those; do not improve on them.
 */
export async function cancelRun(
  baseUrl: string,
  runId: string,
  signal?: AbortSignal,
  token?: string
): Promise<Record<string, unknown>> {
  return stepPost<Record<string, unknown>>(
    baseUrl,
    `/runs/${encodeURIComponent(runId)}/cancel`,
    {},
    signal,
    token
  );
}

/**
 * Parse one NDJSON line, never throwing.
 *
 * A malformed frame must not take down a run that is otherwise fine, so this
 * returns null and the caller skips it. Blank lines are ordinary: the service
 * flushes per event and the last chunk usually ends in a newline.
 */
export function parseFrame(line: string): StreamFrame | null {
  const trimmed = line.trim();
  if (!trimmed) return null;
  try {
    const parsed = JSON.parse(trimmed);
    if (!parsed || typeof parsed !== "object") return null;
    if (typeof parsed.event !== "string") return null;
    return parsed as StreamFrame;
  } catch {
    return null;
  }
}

/**
 * Approval-gated steps: `POST /steps` proposes the circuit and holds the run
 * on the engine; each later `POST /steps/<id>/<step>` runs one more stage
 * only when the engineer says so. Every call is one paid stage at most, and
 * nothing here retries — a 409 (out of order) or 404 (the engine forgot the
 * session, usually a restart) surfaces as a `request` error to act on.
 */
export async function startSteps(
  baseUrl: string,
  request: StepRequest,
  signal?: AbortSignal,
  token?: string,
  idempotencyKey?: string
): Promise<StepResponse> {
  const { intent, datasheets, time_limit_s, review: _review, ...rest } =
    normalizeRequest(request) as StepRequest;
  return stepPost(
    baseUrl,
    "/steps",
    { intent, datasheets, time_limit_s, ...rest, kicad_live: request.kicad_live ?? false },
    signal,
    token,
    idempotencyKey
  );
}

export async function advanceStep(
  baseUrl: string,
  session: string,
  step: StepName,
  payload: Record<string, unknown> = {},
  signal?: AbortSignal,
  token?: string
): Promise<StepResponse> {
  return stepPost(
    baseUrl,
    `/steps/${encodeURIComponent(session)}/${step}`,
    payload,
    signal,
    token
  );
}

/** What `showBoard3d` answers: opened or not, and why not in words. */
export interface View3dResponse {
  opened: boolean;
  detail: string | null;
}

/**
 * Open KiCad's own 3D viewer on this run's routed board.
 *
 * Not a step: it produces nothing, changes no state and spends no model call,
 * so it may be pressed any number of times and the engine never answers
 * "already ran". The board it shows is the routed `.kicad_pcb` the engine
 * wrote — the same file KiCad reads, with the component bodies the engine
 * named on every footprint — which is what "show me the board in 3D" means to
 * a hardware engineer.
 *
 * `opened: false` always carries a `detail` naming the fix (KiCad's API server
 * is off, no Accessibility grant, no board yet). A silent false would send an
 * engineer looking at the board when the problem is a preference.
 */
export async function showBoard3d(
  baseUrl: string,
  session: string,
  signal?: AbortSignal,
  token?: string
): Promise<View3dResponse> {
  return stepPost<View3dResponse>(
    baseUrl,
    `/steps/${encodeURIComponent(session)}/view3d`,
    {},
    signal,
    token
  );
}

/**
 * Record what was typed while the run was going. Spends nothing.
 *
 * The service routes this (and `cancelStep`) *before* it builds a model, so
 * both work with no API key — which is the point: a cancel that needed a
 * configured key would fail exactly when a run is going wrong.
 *
 * `step` may only ever be a step that genuinely reads free text. Today that
 * is `"case"` and nothing else, and the service answers 400 for any other
 * value on purpose, so a UI cannot promise a door that does not exist. Leave
 * it null to keep the note for a restart.
 */
export async function amendStep(
  baseUrl: string,
  session: string,
  text: string,
  step: StepName | null = null,
  signal?: AbortSignal,
  token?: string
): Promise<AmendResponse> {
  return stepPost<AmendResponse>(
    baseUrl,
    `/steps/${encodeURIComponent(session)}/amend`,
    step ? { text, step } : { text },
    signal,
    token
  );
}

/**
 * Close the session to further steps.
 *
 * What this buys is the steps that now never run. It does **not** reach into
 * work already started — the solve, a model request in flight, the background
 * case and parts jobs — and the response says so in `not_stoppable`,
 * `aborts_at` and `still_running`. Render those; do not improve on them.
 */
export async function cancelStep(
  baseUrl: string,
  session: string,
  signal?: AbortSignal,
  token?: string
): Promise<CancelResponse> {
  return stepPost<CancelResponse>(
    baseUrl,
    `/steps/${encodeURIComponent(session)}/cancel`,
    {},
    signal,
    token
  );
}

async function stepPost<T = StepResponse>(
  baseUrl: string,
  path: string,
  payload: Record<string, unknown>,
  signal?: AbortSignal,
  token?: string,
  idempotencyKey?: string
): Promise<T> {
  const deadline = withTimeout(signal);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(token),
        ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      },
      body: JSON.stringify(payload),
      signal: deadline.signal,
    });
  } catch (error) {
    if (deadline.timedOut() && !signal?.aborted) throw timeoutError();
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }
  const body = await readJson(response);
  if (!response.ok) {
    if (response.status === 404 || response.status === 409) {
      throw new SilkscreenError(
        "request",
        String(body.error ?? `step refused (${response.status})`),
        { status: response.status }
      );
    }
    throw errorFromBody(response.status, body as Partial<RunError>);
  }
  return body as unknown as T;
}

/**
 * What the engine could deliver a finished run to (`GET /deliver/config`).
 *
 * Never a secret: booleans, the token's state, and the hints that name the
 * fix. A 404 means the engine predates the route, and reads the same as
 * "nothing configured" — the panel shows the hint either way.
 */
export async function deliverConfig(
  baseUrl: string,
  token?: string,
  signal?: AbortSignal
): Promise<DeliverConfig> {
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/deliver/config`, {
      method: "GET",
      headers: authHeaders(token),
      signal: signal ?? AbortSignal.timeout(8000),
    });
  } catch (error) {
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }
  if (response.status === 404) {
    return {
      available: false,
      chat: false,
      gmail: false,
      calendar: false,
      oauth_client: false,
      signed_in: false,
      token: "missing",
      hints: ["This engine has no /deliver route; update the service."],
    };
  }
  const body = await readJson(response);
  if (!response.ok) throw errorFromBody(response.status, body as Partial<RunError>);
  return body as unknown as DeliverConfig;
}

/**
 * Open Google's OAuth consent page via the engine, then wait for the redirect.
 *
 * Ada opens the URL with Tauri `openUrl` (the service often cannot open a
 * browser from a worker thread). Needs `GOOGLEAPPS_CLIENT_ID` + `SECRET` on
 * the service process.
 */
export async function deliverAuth(
  baseUrl: string,
  token?: string,
  signal?: AbortSignal
): Promise<DeliverConfig> {
  let startResponse: Response;
  try {
    startResponse = await tauriFetch(`${baseUrl}/deliver/auth/start`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(token),
      },
      body: "{}",
      signal: signal ?? AbortSignal.timeout(20_000),
    });
  } catch (error) {
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }
  const startBody = await readJson(startResponse);
  if (!startResponse.ok) {
    throw new SilkscreenError(
      "request",
      String((startBody as { error?: string }).error ?? `sign-in refused (${startResponse.status})`),
      { status: startResponse.status }
    );
  }
  const authUrl = String((startBody as { auth_url?: string }).auth_url ?? "");
  if (!/^https:\/\//i.test(authUrl)) {
    throw new SilkscreenError("server", "The engine did not return a Google consent URL.");
  }

  try {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(authUrl);
  } catch (error) {
    throw new SilkscreenError(
      "request",
      `Could not open the Google consent page: ${(error as Error)?.message ?? "unknown"}. Open this URL yourself: ${authUrl}`
    );
  }

  let finishResponse: Response;
  try {
    finishResponse = await tauriFetch(`${baseUrl}/deliver/auth`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(token),
      },
      body: JSON.stringify({ client_opens: true }),
      signal: signal ?? AbortSignal.timeout(320_000),
    });
  } catch (error) {
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }
  const finishBody = await readJson(finishResponse);
  if (!finishResponse.ok) {
    throw new SilkscreenError(
      "request",
      String(
        (finishBody as { error?: string }).error ?? `sign-in refused (${finishResponse.status})`
      ),
      { status: finishResponse.status }
    );
  }
  return finishBody as unknown as DeliverConfig;
}

/**
 * Send a routed step session to Google Workspace (`POST /steps/<id>/deliver`).
 *
 * The engine answers 200 even when one destination failed — each block in
 * the response says for itself — so only a refused request (bad address,
 * nothing asked for), an unrouted run (409) or a forgotten session (404)
 * throws. Nothing here retries: a retry is a second email.
 */
export async function deliverRun(
  baseUrl: string,
  session: string,
  payload: DeliverRequest,
  signal?: AbortSignal,
  token?: string
): Promise<DeliverResponse> {
  return stepPost<DeliverResponse>(
    baseUrl,
    `/steps/${encodeURIComponent(session)}/deliver`,
    payload as Record<string, unknown>,
    signal,
    token
  );
}

/**
 * Where a step session stands on the engine: `GET /steps/<id>`.
 *
 * Aborting a step's fetch does not abort the step — the engine finishes it
 * under the session lock and adds it to `done` — so after a cancel, or after
 * a 409, this is the only way for the client to learn which stage is legal
 * now. A GET never runs a stage and never costs anything.
 */
export async function stepStatus(
  baseUrl: string,
  session: string,
  signal?: AbortSignal,
  token?: string
): Promise<StepStatusResponse> {
  const deadline = withTimeout(signal);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/steps/${encodeURIComponent(session)}`, {
      method: "GET",
      headers: authHeaders(token),
      signal: deadline.signal,
    });
  } catch (error) {
    if (deadline.timedOut() && !signal?.aborted) throw timeoutError();
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }
  const body = await readJson(response);
  if (!response.ok) {
    if (response.status === 404) {
      throw new SilkscreenError(
        "request",
        String(body.error ?? "the engine no longer has this session"),
        { status: 404 }
      );
    }
    throw errorFromBody(response.status, body as Partial<RunError>);
  }
  return body as unknown as StepStatusResponse;
}

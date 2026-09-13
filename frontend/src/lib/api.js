// The only request boundary in the app. Browser paths remain same-origin; the
// desktop shell injects its loopback sidecar origin before this module loads.

import { fetch as nativeFetch } from '@tauri-apps/plugin-http'
import { logError, logEvent, logWarn } from './log.js'
import { parseNdjson } from './stream.js'
import { normalizeConstraintManifest } from './constraints.js'
import { createTransport } from './transport.js'

const ENDPOINT = '/generate'
const STREAM_ENDPOINT = '/generate/stream'
const PLACEMENT_ENDPOINT = '/placement/repair'
const CHAT_STREAM_ENDPOINT = '/chat/stream'
const MODELS_ENDPOINT = '/models'
const CONFIG_STATUS_ENDPOINT = '/config/status'
const NDJSON_TYPE = 'application/x-ndjson'
const TIMEOUT_MESSAGE = 'The run passed the 300 second budget and was cancelled.'

const transportRequest = createTransport({
  baseUrl: globalThis.__SILKSCREEN_BASE__ ?? '',
  // Resolve this at call time so tests and browser instrumentation can replace
  // the global fetch implementation after the module has loaded.
  webFetch: (...args) => globalThis.fetch(...args),
  nativeFetch,
})

export const REQUEST_TIMEOUT_MS = 300000
export const MAX_REQUEST_BYTES = 1024 * 1024
export const MIN_TIME_LIMIT_S = 5
export const MAX_TIME_LIMIT_S = 120
// service/app.py's MAX_ENCLOSURE_STYLE_CHARS: a longer style is a plain 400.
export const MAX_ENCLOSURE_STYLE_CHARS = 500

/** kind is what the UI switches on; status is kept for the error panel's footer. */
export class ApiError extends Error {
  constructor(kind, message, { status = 0, errorId = '', runId = '' } = {}) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.errorId = errorId
    /** The service's name for the run this failed on, when there is one.

        Carried on the error because the errors that matter here are exactly
        the ones where the run is still going and still being paid for — a 200
        that cannot be read, a stream that dies. `runStatus` and `cancelRun`
        take this handle. Empty when the failure predates any run. */
    this.runId = runId
  }
}

/** A name for a run, chosen before the request is sent.

    Sixteen random bytes, shaped like stripe-python's
    `_generate_idempotency_key` — but it buys something different: an
    idempotency key stops a *second* run, a run id addresses the *first*.
    Choosing it here means this client knows the run's name before the service
    has answered anything, so a response that never arrives is still pollable
    and cancellable. LiteLLM's proxy accepts a caller's id the same way, on
    `x-litellm-call-id` (`litellm/proxy/common_request_processing.py`,
    `ProxyBaseLLMRequestProcessing.common_processing_pre_call_logic`:
    `request.headers.get("x-litellm-call-id", str(uuid.uuid4()))`). */
export function newRunId() {
  const c = globalThis.crypto
  if (typeof c?.randomUUID === 'function') return `run_${c.randomUUID()}`
  if (typeof c?.getRandomValues === 'function') {
    const bytes = c.getRandomValues(new Uint8Array(16))
    return `run_${Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`
  }
  return `run_${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`
}

function clampTimeLimit(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return MIN_TIME_LIMIT_S
  return Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, Math.round(n)))
}

/** Drop half-filled datasheet rows and clamp the solver budget to what the service accepts. */
export function normalizeRequest(request) {
  const datasheets = {}
  for (const [part, url] of Object.entries(request.datasheets || {})) {
    const p = String(part).trim()
    const u = String(url).trim()
    if (p && u) datasheets[p] = u
  }
  const placementEnabled = request.placement_enabled !== false
  const experimentalPlacement = request.experimental_placement === true
  const placementPolicies = new Set(['fast', 'deterministic', 'gemini', 'ollama', 'tinker', 'hybrid'])
  const placementPolicy = placementPolicies.has(String(request.placement_policy || 'deterministic'))
    ? String(request.placement_policy || 'deterministic')
    : 'deterministic'
  return {
    intent: String(request.intent ?? '').trim(),
    datasheets,
    time_limit_s: clampTimeLimit(request.time_limit_s),
    review: request.review !== false,
    // Unlimited is the UI default. Keep an explicit false in the normalized
    // request so edit/retry/session restore cannot silently turn it back on.
    no_solver_budget:
      request.no_solver_budget === undefined ? true : request.no_solver_budget === true,
    // Grounding is opt-in and only sent when it was asked for: an absent flag
    // is the service's default, so a stray `ground: false` would say nothing.
    ...(request.ground === true ? { ground: true } : {}),
    // The case is opt-in the same way grounding is: an absent flag is the
    // service's default, so `enclosure: false` is never sent. The style rides
    // along only when a case was asked for and the text says something, capped
    // at the 500 characters service/app.py accepts.
    ...(request.enclosure === true
      ? {
          enclosure: true,
          // Rigorous fit checking is a second opt-in on top of the case itself:
          // absent means the service's fast default, so false is never sent.
          ...(request.enclosure_rigorous === true ? { enclosure_rigorous: true } : {}),
          ...(String(request.enclosure_style ?? '').trim()
            ? {
                enclosure_style: String(request.enclosure_style ?? '')
                  .trim()
                  .slice(0, MAX_ENCLOSURE_STYLE_CHARS),
              }
            : {}),
        }
      : {}),
    // Sourcing is opt-in exactly the way the case is: one more model call plus
    // a datasheet probe per part, so an absent flag is the service's default
    // and `sourcing: false` is never sent.
    ...(request.sourcing === true ? { sourcing: true } : {}),
    ...(request.constraints && typeof request.constraints === 'object' && !Array.isArray(request.constraints)
      ? { constraints: normalizeConstraintManifest(request.constraints) }
      : {}),
    ...(placementEnabled
      ? {
          placement_profile: String(request.placement_profile || 'compact-control'),
          placement_policy: placementPolicy,
          experimental_placement: experimentalPlacement,
          ...(experimentalPlacement && request.record_trace === true
            ? { record_trace: true }
            : {}),
          ...(request.placement_feedback && typeof request.placement_feedback === 'object'
            ? { placement_feedback: request.placement_feedback }
            : {}),
        }
      : {}),
  }
}

export function normalizePlacementRequest(request = {}) {
  const policies = new Set(['fast', 'deterministic', 'gemini', 'ollama', 'tinker', 'hybrid'])
  const requestedPolicy = String(request.policy || 'deterministic')
  const profile =
    request.profile && typeof request.profile === 'object'
      ? request.profile
      : String(request.profile || 'compact-control')
  const normalized = {
    profile,
    policy: policies.has(requestedPolicy) ? requestedPolicy : 'deterministic',
    experimental_placement: request.experimental_placement === true,
  }
  if (request.board && typeof request.board === 'object') normalized.board = request.board
  if (request.feedback && typeof request.feedback === 'object') {
    normalized.feedback = request.feedback
  }
  if (request.experimental_placement === true && request.record_trace === true) {
    normalized.record_trace = true
  }
  if (request.quota_rpm !== undefined) normalized.quota_rpm = request.quota_rpm
  return normalized
}

export async function repairPlacement(request) {
  const body = JSON.stringify(normalizePlacementRequest(request))
  guardSize(body)
  let response
  try {
    response = await transportRequest(PLACEMENT_ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
    })
  } catch (err) {
    throw new ApiError('network', String(err && err.message ? err.message : err))
  }
  let data = {}
  try {
    data = await response.json()
  } catch {
    throw new ApiError('internal', 'The placement service returned invalid JSON.', {
      status: response.status,
    })
  }
  if (!response.ok) throw errorFor(response.status, data)
  return data
}

export function requestBytes(request) {
  return new TextEncoder().encode(JSON.stringify(request)).length
}

function asArray(value) {
  return Array.isArray(value) ? value : []
}

/** Synthesize finding cards from the flattened blocker strings older responses return. */
function findingsFrom(data) {
  if (Array.isArray(data.findings)) return data.findings
  return asArray(data.blockers).map((text) => ({
    severity: 'blocker',
    title: String(text),
    detail: '',
    parts: [],
    citation: '',
    suggested_fix: '',
  }))
}

function normalizeResponse(data) {
  return {
    ...data,
    board_mm: asArray(data.board_mm),
    parts: asArray(data.parts),
    blockers: asArray(data.blockers),
    warnings: asArray(data.warnings),
    nets: asArray(data.nets),
    datasheets: asArray(data.datasheets),
    findings: findingsFrom(data),
    repair_rounds: Number(data.repair_rounds ?? 0),
    duration_s: Number(data.duration_s ?? 0),
  }
}

function errorFor(status, body) {
  const message = String(body.error || `request failed with status ${status}`)
  const errorId = String(body.error_id || '')
  if (status === 400) return new ApiError('validation', message, { status })
  if (status === 404) return new ApiError('not-found', message, { status })
  if (status === 413) return new ApiError('too-large', message, { status })
  if (status === 502) {
    // An unkeyed clone is the most common first run: it is configuration, not an outage.
    const kind = message.includes('GOOGLE_API_KEY') ? 'no-api-key' : 'upstream'
    return new ApiError(kind, message, { status })
  }
  if (status >= 500) return new ApiError('internal', message, { status, errorId })
  return new ApiError('upstream', message, { status })
}

/** The one line a response leaves behind, whichever way a request exits. A
    body that would not parse is a failed run even under a 200, so it warns.
    `response` is duck-typed: the streaming path passes the status its terminal
    error frame carried, which is the status the one-shot would have answered. */
function recordOutcome(path, response, ms, parsed, extra = {}) {
  const outcome = { status: response.status, ok: response.ok, ms, parsed, ...extra }
  const line = `POST ${path} returned ${response.status} in ${ms} ms`
  if (response.ok && parsed) logEvent('api.response', line, outcome)
  else logWarn('api.response', line, outcome)
}

/** The pre-flight the network never sees. Shared, so both entry points refuse
    the same oversized request with the same error. */
function guardSize(body) {
  const bytes = new TextEncoder().encode(body).length
  if (bytes <= MAX_REQUEST_BYTES) return
  logWarn('api.too-large', `Request of ${bytes} bytes refused before the network`, {
    bytes,
    limit: MAX_REQUEST_BYTES,
  })
  throw new ApiError(
    'too-large',
    `The request is ${bytes} bytes; the service accepts at most ${MAX_REQUEST_BYTES}.`,
  )
}

function requestTimer(controller, request) {
  if (request?.no_solver_budget === true) return null
  return setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
}

function clearRequestTimer(timer) {
  if (timer !== null) clearTimeout(timer)
}

export async function generate(request) {
  const body = JSON.stringify(request)
  guardSize(body)

  const controller = new AbortController()
  const timer = requestTimer(controller, request)

  const startedAt = Date.now()
  let response
  try {
    response = await transportRequest(ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
      signal: controller.signal,
    })
  } catch (err) {
    logError('api.failed', 'POST /generate never reached a response', {
      ms: Date.now() - startedAt,
      aborted: controller.signal.aborted,
    })
    if (controller.signal.aborted) {
      throw new ApiError('timeout', TIMEOUT_MESSAGE)
    }
    throw new ApiError('network', String(err && err.message ? err.message : err))
  } finally {
    clearRequestTimer(timer)
  }

  let data = {}
  let parsed = true
  try {
    data = await response.json()
  } catch {
    parsed = false
    if (response.ok) {
      // Written before the throw, which is the one exit that would otherwise
      // skip the line below -- and a 200 whose body will not parse is exactly
      // the response worth having a record of.
      recordOutcome(ENDPOINT, response, Date.now() - startedAt, false)
      throw new ApiError('internal', 'The service returned a body that is not JSON.', {
        status: response.status,
      })
    }
  }

  recordOutcome(ENDPOINT, response, Date.now() - startedAt, parsed)

  if (!response.ok) throw errorFor(response.status, data || {})
  return normalizeResponse(data || {})
}

// ------------------------------------------------------------------ stream

/** The declared type, read defensively: a proxy, a stub, or an error page need
    not carry Headers at all, and a missing one is not a reason to throw. */
function headerOf(response, name) {
  try {
    return String(response.headers?.get?.(name) ?? '')
  } catch {
    return ''
  }
}

function contentTypeOf(response) {
  return headerOf(response, 'content-type')
}

function isNdjson(type) {
  return type.toLowerCase().includes(NDJSON_TYPE)
}

/** One listener call that cannot take the read down with it: a bug in the feed
    must not cost the run that is still arriving. */
function emit(onEvent, evt) {
  if (typeof onEvent !== 'function') return
  try {
    onEvent(evt)
  } catch (err) {
    logWarn('api.stream-listener', 'A stream listener threw; the read continues', {
      event: String(evt?.event ?? ''),
      message: String(err && err.message ? err.message : err),
    })
  }
}

/** The one-shot endpoint, taken over from a stream the service never accepted.
    Reached on a 404 and on nothing else — see generateStream.

    The rule this encodes is every HTTP client's: a request may only be repeated
    when the server cannot have acted on it. urllib3 leaves POST out of
    `Retry.DEFAULT_ALLOWED_METHODS` (`urllib3/util/retry.py:197`, "we only retry
    on methods which are considered to be idempotent"), openai-python's
    `_should_retry` (`src/openai/_base_client.py`) returns True only for 408,
    409, 429 and 5xx and falls through to False for everything else including
    every 2xx, and stripe-python's `_should_retry` (`stripe/_http_client.py`)
    retries a POST only for 409/5xx and only because an Idempotency-Key rides on
    every POST (`_api_requestor.request_headers`). We have no idempotency key,
    so 404 — the route is absent, so no run exists — is the whole allowlist. */
function fallbackToOneShot(request, why, data) {
  logEvent('api.stream-fallback', `Falling back to POST ${ENDPOINT}: ${why}`, data)
  return generate(request)
}

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

/** The two frames that end a run, mapped onto exactly what the one-shot path
    would have produced for the same outcome. Anything else is progress. */
function terminalOf(evt) {
  const name = String(evt?.event ?? '')
  if (name === 'run.done') return { result: normalizeResponse(objectOf(evt.result)) }
  if (name === 'run.error') {
    // A frame with no status is malformed rather than classifiable; 500 is the
    // honest reading, and it is the status errorFor treats as internal.
    return { error: errorFor(Number(evt.status) || 500, evt) }
  }
  return null
}

/** A 200 whose frames can never be read: the run is running and its outcome is
    unknowable from here. Reported, never retried — the same reading generate()
    gives a 200 whose body will not parse. The message says the run started,
    because "try again" is exactly the wrong advice for it. */
function startedButUnreadable(response, type, why, runId) {
  logError('api.stream-unreadable', `POST ${STREAM_ENDPOINT} answered 200 but ${why}`, {
    status: response.status,
    content_type: type,
    run_id: runId,
  })
  return new ApiError(
    'internal',
    `The service accepted the run and then answered with something this app cannot read (${why}). ` +
      'The run has started; it was not sent again, because a second request would be a second run.' +
      (runId ? ` It is running as ${runId} — poll or cancel it rather than starting again.` : ''),
    { status: response.status, runId },
  )
}

/** A stream that opened and then stopped without saying how the run ended. The
    pipeline may well have finished server-side, so this is reported, never
    retried: a second run is a second set of model calls. */
function streamFailure(controller, ms, events, why, runId = '') {
  logError('api.failed', `POST ${STREAM_ENDPOINT} stopped after ${events} events`, {
    ms,
    events,
    aborted: controller.signal.aborted,
    why,
    run_id: runId,
  })
  if (controller.signal.aborted) return new ApiError('timeout', TIMEOUT_MESSAGE, { runId })
  return new ApiError('network', why, { runId })
}

/** Best-effort release of a body we are done with; a reader that rejects on
    cancel has nothing left to tell us. */
function releaseReader(reader) {
  try {
    const cancelled = reader.cancel?.()
    if (cancelled && typeof cancelled.catch === 'function') cancelled.catch(() => {})
  } catch {
    // Deliberately swallowed: the run's outcome is already decided.
  }
}

/** The streaming twin of generate(): same request shaping, same budget, same
    result, same error taxonomy — the only addition is that `onEvent` sees each
    pipeline event as it lands.

    It falls back to generate() in exactly one case, the only one that means no
    run exists to duplicate: a 404, a service built before this endpoint
    existed. Every other outcome is this request's outcome. A 200 whose type was
    rewritten or whose body cannot be read is a run that started and cannot be
    watched, so it is reported as a failure rather than sent again, and a
    connection that dies mid-stream is a network failure rather than a second
    attempt — the pipeline either of them was reporting on costs real model
    calls. */
export async function generateStream(request, onEvent, runId = newRunId()) {
  // Streaming is the debugging surface — the feed is where a raw answer can
  // actually be read — so this route, and only this route, asks for the
  // model's own text. The one-shot body stays exactly what the caller shaped.
  const body = JSON.stringify({ ...request, debug: true })
  guardSize(body)

  const controller = new AbortController()
  const timer = requestTimer(controller, request)
  const startedAt = Date.now()

  try {
    let response
    try {
      response = await transportRequest(STREAM_ENDPOINT, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          accept: NDJSON_TYPE,
          // Named before the request leaves. A service that predates the
          // header ignores it and mints its own, which comes back on the
          // response header and on every frame.
          'x-kaleo-run-id': runId,
        },
        body,
        signal: controller.signal,
      })
    } catch (err) {
      logError('api.failed', `POST ${STREAM_ENDPOINT} never reached a response`, {
        ms: Date.now() - startedAt,
        aborted: controller.signal.aborted,
        run_id: runId,
      })
      // A request that never reached a response may still have started a run
      // on the far side, so the id is carried out even here.
      if (controller.signal.aborted) throw new ApiError('timeout', TIMEOUT_MESSAGE, { runId })
      throw new ApiError('network', String(err && err.message ? err.message : err), { runId })
    }

    if (!response.ok) {
      if (response.status === 404) {
        return await fallbackToOneShot(request, 'the service has no streaming endpoint', {
          status: 404,
        })
      }
      // Every other status is an answer about this request, so it is classified
      // exactly as the one-shot path classifies it.
      let data = {}
      let parsed = true
      try {
        data = await response.json()
      } catch {
        parsed = false
      }
      recordOutcome(STREAM_ENDPOINT, response, Date.now() - startedAt, parsed, { stream: true })
      throw errorFor(response.status, data || {})
    }

    // Past this point the service has answered 200, which it sends from
    // _generate_stream *before* it starts the pipeline (service/app.py:2692) —
    // so a paid run is already under way. Neither a rewritten content type nor
    // an unreadable body is a reason to send the request again; both are
    // reported as this run's failure, and the run itself is left to finish
    // server-side unwatched. Falling back here duplicated every model call in
    // it (PR #9 review, P2).
    // The service's own name for the run wins over ours: an older build, or a
    // proxy that dropped the request header, minted its own, and polling ours
    // would ask about a run that does not exist.
    const liveRunId = headerOf(response, 'x-kaleo-run-id') || runId

    const type = contentTypeOf(response)
    if (!isNdjson(type)) {
      throw startedButUnreadable(response, type, `the response is ${type || 'untyped'}`, liveRunId)
    }
    if (!response.body || typeof response.body.getReader !== 'function') {
      throw startedButUnreadable(
        response,
        type,
        'the response body cannot be read as a stream',
        liveRunId,
      )
    }

    logEvent('api.stream', `POST ${STREAM_ENDPOINT} opened ${response.status}`, {
      status: response.status,
      ms: Date.now() - startedAt,
      content_type: type,
    })

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let carry = ''
    let events = 0
    let outcome = null
    let closed = false

    try {
      while (!outcome && !closed) {
        let chunk
        try {
          chunk = await reader.read()
        } catch (err) {
          throw streamFailure(
            controller,
            Date.now() - startedAt,
            events,
            String(err && err.message ? err.message : err),
            liveRunId,
          )
        }
        closed = Boolean(chunk.done)
        // On close the decoder is flushed and a newline appended, so a server
        // that ends without one still yields its last frame.
        const text = closed
          ? `${decoder.decode()}\n`
          : decoder.decode(chunk.value, { stream: true })
        const step = parseNdjson(carry, text)
        carry = step.carry
        for (const evt of step.events) {
          events += 1
          emit(onEvent, evt)
          if (!outcome) outcome = terminalOf(evt)
        }
      }
    } finally {
      releaseReader(reader)
    }

    const ms = Date.now() - startedAt
    if (outcome?.error) {
      const error = outcome.error
      recordOutcome(STREAM_ENDPOINT, { status: error.status, ok: false }, ms, true, {
        stream: true,
        events,
      })
      throw error
    }
    if (outcome) {
      recordOutcome(STREAM_ENDPOINT, response, ms, true, { stream: true, events })
      return outcome.result
    }
    throw streamFailure(
      controller,
      ms,
      events,
      'The stream closed before the run finished. It may still be running; ' +
        `ask GET /runs/${liveRunId} rather than starting again.`,
      liveRunId,
    )
  } finally {
    clearRequestTimer(timer)
  }
}

/** `GET /runs/<id>` — what became of a run whose stream this client lost.

    Not a way to recover the board: the service keeps no result for a streamed
    run (`service/runs.py` says why), so this answers "did it finish, and what
    did it cost". `null` for a 404, which means the service has forgotten it or
    restarted — a real answer, not something to retry. */
export async function runStatus(runId) {
  let response
  try {
    response = await transportRequest(`/runs/${encodeURIComponent(runId)}`, { method: 'GET' })
  } catch (err) {
    throw new ApiError('network', String(err && err.message ? err.message : err), { runId })
  }
  if (response.status === 404) return null
  let data = {}
  try {
    data = await response.json()
  } catch {
    throw new ApiError('internal', 'The run status is not JSON.', {
      status: response.status,
      runId,
    })
  }
  if (!response.ok) throw errorFor(response.status, data || {})
  return data
}

/** `POST /runs/<id>/cancel` — ask a run to stop.

    The answer's `aborts_at` and `not_stoppable` state what still finishes and
    is still billed. Render those; do not improve on them. */
export async function cancelRun(runId) {
  let response
  try {
    response = await transportRequest(`/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
    })
  } catch (err) {
    throw new ApiError('network', String(err && err.message ? err.message : err), { runId })
  }
  let data = {}
  try {
    data = await response.json()
  } catch {
    throw new ApiError('internal', 'The cancel answer is not JSON.', {
      status: response.status,
      runId,
    })
  }
  if (!response.ok) throw errorFor(response.status, data || {})
  return data
}

// --------------------------------------------------------------------- chat

function chatTerminalOf(evt) {
  const name = String(evt?.event ?? '')
  if (name === 'chat.done') {
    return {
      outcome: {
        assistant: String(evt.assistant ?? ''),
        needsClarification: evt.needs_clarification === true,
        model: String(evt.model ?? ''),
        thinkingLevel: String(evt.thinking_level ?? 'auto'),
        quotaRpm: String(evt.quota_rpm ?? 'auto'),
        result: evt.result ? normalizeResponse(objectOf(evt.result)) : null,
      },
    }
  }
  if (name === 'chat.error') {
    return { error: errorFor(Number(evt.status) || 500, evt) }
  }
  return null
}

function chatFailure(controller, ms, events, why) {
  logError('api.failed', `POST ${CHAT_STREAM_ENDPOINT} stopped after ${events} events`, {
    ms,
    events,
    aborted: controller.signal.aborted,
    why,
  })
  if (controller.signal.aborted) return new ApiError('timeout', TIMEOUT_MESSAGE)
  return new ApiError('network', why)
}

/** One ADK orchestrator turn. Unlike generateStream this has no one-shot
    fallback: replaying an agent turn can duplicate a paid tool invocation. */
export async function chatStream(request, onEvent) {
  const body = JSON.stringify({ ...request, debug: true })
  guardSize(body)

  const controller = new AbortController()
  const timer = requestTimer(controller, request)
  const startedAt = Date.now()
  try {
    let response
    try {
      response = await transportRequest(CHAT_STREAM_ENDPOINT, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: NDJSON_TYPE },
        body,
        signal: controller.signal,
      })
    } catch (err) {
      logError('api.failed', `POST ${CHAT_STREAM_ENDPOINT} never reached a response`, {
        ms: Date.now() - startedAt,
        aborted: controller.signal.aborted,
      })
      if (controller.signal.aborted) throw new ApiError('timeout', TIMEOUT_MESSAGE)
      throw new ApiError('network', String(err && err.message ? err.message : err))
    }

    if (!response.ok) {
      let data = {}
      let parsed = true
      try {
        data = await response.json()
      } catch {
        parsed = false
      }
      recordOutcome(CHAT_STREAM_ENDPOINT, response, Date.now() - startedAt, parsed, {
        stream: true,
      })
      throw errorFor(response.status, data || {})
    }

    const type = contentTypeOf(response)
    if (!isNdjson(type) || !response.body || typeof response.body.getReader !== 'function') {
      throw new ApiError('internal', 'The chat service did not return a readable event stream.', {
        status: response.status,
      })
    }

    logEvent('api.stream', `POST ${CHAT_STREAM_ENDPOINT} opened ${response.status}`, {
      status: response.status,
      ms: Date.now() - startedAt,
      content_type: type,
    })

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let carry = ''
    let events = 0
    let outcome = null
    let closed = false
    try {
      while (!outcome && !closed) {
        let chunk
        try {
          chunk = await reader.read()
        } catch (err) {
          throw chatFailure(
            controller,
            Date.now() - startedAt,
            events,
            String(err && err.message ? err.message : err),
          )
        }
        closed = Boolean(chunk.done)
        const text = closed ? `${decoder.decode()}\n` : decoder.decode(chunk.value, { stream: true })
        const step = parseNdjson(carry, text)
        carry = step.carry
        for (const evt of step.events) {
          events += 1
          emit(onEvent, evt)
          if (!outcome) outcome = chatTerminalOf(evt)
        }
      }
    } finally {
      releaseReader(reader)
    }

    const ms = Date.now() - startedAt
    if (outcome?.error) {
      const error = outcome.error
      recordOutcome(CHAT_STREAM_ENDPOINT, { status: error.status, ok: false }, ms, true, {
        stream: true,
        events,
      })
      throw error
    }
    if (outcome) {
      recordOutcome(CHAT_STREAM_ENDPOINT, response, ms, true, { stream: true, events })
      return outcome.outcome
    }
    throw chatFailure(controller, ms, events, 'The stream closed before the turn finished.')
  } finally {
    clearRequestTimer(timer)
  }
}

/** Server-filtered Gemini models. Auto remains the normal UI choice. */
export async function listModels() {
  let response
  try {
    response = await transportRequest(MODELS_ENDPOINT, { headers: { accept: 'application/json' } })
  } catch (err) {
    throw new ApiError('network', String(err && err.message ? err.message : err))
  }
  let data = {}
  try {
    data = await response.json()
  } catch {
    throw new ApiError('internal', 'The model catalog is not JSON.', { status: response.status })
  }
  if (!response.ok) throw errorFor(response.status, data || {})
  return {
    default: String(data.default ?? 'auto'),
    auto_model: String(data.auto_model ?? ''),
    source: String(data.source ?? ''),
    warning: String(data.warning ?? ''),
    models: asArray(data.models)
      .map((model) => ({
        id: String(model?.id ?? ''),
        name: String(model?.name ?? model?.id ?? ''),
        description: String(model?.description ?? ''),
        input_token_limit: model?.input_token_limit ?? null,
        output_token_limit: model?.output_token_limit ?? null,
        thinking: model?.thinking ?? null,
      }))
      .filter((model) => model.id),
    placement: {
      experimental_enabled: data.placement?.experimental_enabled === true,
      profiles: asArray(data.placement?.profiles).map(String),
      policies:
        data.placement?.policies && typeof data.placement.policies === 'object'
          ? data.placement.policies
          : {},
    },
  }
}

/** Secret-safe backend and .env readiness for the live side-rail monitor. */
export async function getConfigurationStatus({ signal } = {}) {
  let response
  try {
    response = await transportRequest(CONFIG_STATUS_ENDPOINT, {
      headers: { accept: 'application/json' },
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    })
  } catch (err) {
    throw new ApiError('network', String(err && err.message ? err.message : err))
  }
  let data = {}
  try {
    data = await response.json()
  } catch {
    throw new ApiError('internal', 'The configuration status is not JSON.', {
      status: response.status,
    })
  }
  if (!response.ok) throw errorFor(response.status, data || {})

  const states = new Set(['ready', 'off', 'warning', 'error', 'restart'])
  const state = (value) => (states.has(String(value)) ? String(value) : 'warning')
  return {
    version: Number(data.version) || 1,
    dotenv: {
      present: data.dotenv?.present === true,
      state: state(data.dotenv?.state),
      summary: String(data.dotenv?.summary ?? ''),
      reload_required: data.dotenv?.reload_required === true,
      changed_since_start: data.dotenv?.changed_since_start === true,
      pending: asArray(data.dotenv?.pending).map(String),
    },
    features: asArray(data.features)
      .map((feature) => ({
        id: String(feature?.id ?? ''),
        label: String(feature?.label ?? ''),
        state: state(feature?.state),
        summary: String(feature?.summary ?? ''),
        variables: asArray(feature?.variables).map(String),
      }))
      .filter((feature) => feature.id && feature.label),
  }
}

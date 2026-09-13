import { derived, get, writable } from 'svelte/store'
import { countOf } from './format.js'
import { logError, logEvent, logWarn, nextRunId, record, suspectEngineBug } from './log.js'
import { readRouting, runOutcomeText } from './routing.js'
import { describeStageEvent } from './stream.js'

/** Longest feed a run keeps. A pipeline emits tens of events, not hundreds, so
    this is a bound against a pathological server rather than a display limit. */
export const MAX_FEED = 500

const IDLE = {
  phase: 'idle',
  request: null,
  result: null,
  error: null,
  startedAt: 0,
  id: '',
  // Keyed by backend stage name, plus the synthetic `validate` — see stageEvent.
  stages: {},
  feed: [],
  sessionId: '',
  entries: [],
  needsClarification: false,
  actualModel: '',
  orchestratorModel: 'gemini-3.7-flash',
  thinkingLevel: 'auto',
  actualThinkingLevel: '',
  quotaRpm: 'auto',
}

/** phase: idle | running | done | error */
export const run = writable(IDLE)

export function startRun(request, options = {}) {
  const id = nextRunId()
  const intent = String(request?.intent ?? '')
  const previous = get(run)
  const preserve = options.preserve === true
  const sessionId = preserve && previous.sessionId ? previous.sessionId : newSessionId()
  const entries = preserve ? [...(previous.entries || [])] : []
  const message = String(options.message ?? intent)
  const orchestratorModel = String(
    options.model || (preserve ? previous.orchestratorModel : '') || 'gemini-3.7-flash',
  )
  const thinkingLevel = String(
    options.thinkingLevel || (preserve ? previous.thinkingLevel : '') || 'auto',
  )
  const quotaRpm = String(options.quotaRpm || (preserve ? previous.quotaRpm : '') || 'auto')
  if (message) {
    entries.push({ id: `${id}-user`, type: 'message', role: 'user', text: message })
  }
  entries.push({
    id,
    type: 'activity',
    phase: 'running',
    request,
    stages: {},
    feed: [],
    result: null,
    error: null,
  })
  logEvent('run.start', `run ${id} started`, {
    id,
    intent,
    intent_chars: intent.length,
    datasheets: Object.keys(request?.datasheets || {}).length,
    time_limit_s: request?.time_limit_s ?? null,
    no_solver_budget: request?.no_solver_budget === true,
    review: request?.review !== false,
    // normalizeRequest omits `ground` unless it was asked for, so absent is false.
    ground: request?.ground === true,
    orchestrator_model: orchestratorModel,
    thinking_level: thinkingLevel,
    quota_rpm: quotaRpm,
  })
  run.set({
    phase: 'running',
    request,
    result: null,
    error: null,
    startedAt: Date.now(),
    id,
    stages: {},
    feed: [],
    sessionId,
    entries,
    needsClarification: false,
    actualModel: '',
    orchestratorModel,
    thinkingLevel,
    actualThinkingLevel: '',
    quotaRpm,
  })
}

export function finishRun(result) {
  logDone(get(run), result)
  run.update((state) => {
    const activity = updateActivity(state.entries, state.id, {
      phase: 'done',
      result,
      error: null,
    })
    return {
      ...state,
      phase: 'done',
      result,
      error: null,
      needsClarification: false,
      entries: attachResult(activity, result),
    }
  })
}

export function finishClarification(outcome) {
  run.update((state) => ({
    ...state,
    phase: 'clarification',
    result: null,
    error: null,
    needsClarification: true,
    actualModel: String(outcome?.model ?? state.actualModel ?? ''),
    entries: updateActivity(state.entries, state.id, {
      phase: 'clarification',
      result: null,
      error: null,
    }),
  }))
}

export function failRun(error) {
  logFailure(get(run), error)
  run.update((state) => ({
    ...state,
    phase: 'error',
    result: null,
    error,
    entries: updateActivity(state.entries, state.id, {
      phase: 'error',
      result: null,
      error: plainError(error),
    }),
  }))
}

export function resetRun() {
  const { id } = get(run)
  // log.js has no debug helper: this line is noise unless you are chasing a
  // state machine, which is exactly what the level is for.
  record({ level: 'debug', src: 'app', event: 'run.reset', msg: 'run cleared', data: { id } })
  // Fresh containers rather than IDLE's own: nothing may end up sharing the
  // holder every future run starts from.
  run.update((state) => ({
    ...IDLE,
    stages: {},
    feed: [],
    entries: [],
    request: state.request,
    orchestratorModel: state.orchestratorModel,
    thinkingLevel: state.thinkingLevel,
    quotaRpm: state.quotaRpm,
  }))
}

/** Restore a previously validated session snapshot. */
export function restoreRun(snapshot) {
  run.set({
    ...IDLE,
    ...snapshot,
    phase: snapshot.phase === 'running' ? 'error' : snapshot.phase,
    stages: snapshot.stages || {},
    feed: snapshot.feed || [],
    entries: Array.isArray(snapshot.entries) ? snapshot.entries : [],
    startedAt: 0,
  })
}

// ------------------------------------------------------- the live pipeline

/** What each finished stage keeps: enough to label the row, never the payload
    itself. The feed already carries the sentence, and the debug log the frame. */
const DONE_SUMMARY = {
  read: (e) => ({
    parts: countOf(e.parts),
    pins: countOf(e.pins),
    requirements: countOf(e.requirements),
  }),
  propose: (e) => ({
    parts: countOf(e.parts),
    nets: countOf(e.nets),
    repair_rounds: countOf(e.repair_rounds),
  }),
  place: (e) => ({
    solver_status: String(e.solver_status ?? ''),
    board_mm: listOf(e.board_mm),
    wirelength_mm: finiteOrNull(e.wirelength_mm),
    warnings: countOf(e.warnings),
  }),
  placement_repair: (e) => ({
    profile: String(e.profile ?? ''),
    policy: String(e.policy ?? ''),
    moves: countOf(e.moves),
    hard_before: finiteOrNull(e.hard_before),
    hard_after: finiteOrNull(e.hard_after),
    applied: e.applied === true,
  }),
  route: (e) => ({
    tracks: countOf(e.tracks),
    vias: countOf(e.vias),
    routed_nets: countOf(e.routed_nets),
    unrouted_nets: countOf(e.unrouted_nets),
  }),
  enclosure: (e) => ({
    cutouts: countOf(e.cutouts),
    lid: String(e.lid ?? ''),
    wall_mm: finiteOrNull(e.wall_mm),
    repair_rounds: countOf(e.repair_rounds),
    rendered: e.rendered === true,
  }),
  sourcing: (e) => ({
    verified: countOf(e.verified),
    proposed: countOf(e.proposed),
    unresolved: countOf(e.unresolved),
  }),
  review: (e) => ({ findings: countOf(e.findings), blockers: countOf(e.blockers) }),
}

/** The solver budget is the one thing a starting stage reports. */
const START_SUMMARY = {
  place: (e) => ({ time_limit_s: finiteOrNull(e.time_limit_s) }),
}

/** Events that are not, in themselves, bad news default to info. */
const LEVEL_OF = {
  'run.error': 'error',
  'chat.error': 'error',
  'tool.error': 'error',
  'model.retry': 'warn',
  'client.badframe': 'warn',
  // The run continues without a case — degraded, not dead, so warn not error.
  'enclosure.failed': 'warn',
  // Same shape: the board ships with a deterministic BOM instead of a sourced one.
  'sourcing.failed': 'warn',
}

let feedSeq = 0

/** One event from the pipeline stream: it moves the stage list, appends the
    sentence to the feed, and leaves the raw frame in the debug log. The whole
    transition is a merge, so an event that names nothing it knows changes
    nothing but the feed. */
export function stageEvent(evt) {
  const event = evt && typeof evt === 'object' ? evt : {}
  const name = String(event.event ?? '')
  const text = describeStageEvent(event)

  // record() rather than logEvent(): the level helpers all stamp src 'app', and
  // this line came from the server, which is the distinction worth keeping.
  record({
    level: own(LEVEL_OF, name) || 'info',
    src: 'server',
    event: `pipeline.${name || 'unknown'}`,
    msg: text,
    data: event,
  })

  run.update((state) => {
    const stages = nextStages(state.stages || {}, name, event)
    const eventModel = String(event.model ?? '')
    const actualModel =
      eventModel && (event.layer === 'orchestrator' || name.startsWith('chat.'))
        ? eventModel
        : state.actualModel
    const eventThinkingLevel = String(event.thinking_level ?? '')
    const actualThinkingLevel =
      eventThinkingLevel && (event.layer === 'orchestrator' || name.startsWith('chat.'))
        ? eventThinkingLevel
        : state.actualThinkingLevel
    const eventQuotaRpm = String(event.quota_rpm ?? '')
    const quotaRpm =
      eventQuotaRpm && name.startsWith('chat.') ? eventQuotaRpm : state.quotaRpm
    const hidden = name === 'assistant.message' || name === 'chat.done'
    const row = hidden
      ? null
      : {
          id: (feedSeq += 1),
          t_s: finiteOrNull(event.t_s),
          text,
          event: name,
          layer: String(event.layer ?? ''),
          callId: String(event.call_id ?? event.tool_call_id ?? ''),
          ...detailOf(name, event),
        }
    const feed = row ? appendFeed(state.feed || [], row) : state.feed || []
    let entries = updateActivity(state.entries, state.id, { stages, feed })
    if (name === 'assistant.message') {
      entries = [
        ...entries,
        {
          id: String(event.event_id || `${state.id}-assistant-${feedSeq += 1}`),
          type: 'message',
          role: 'assistant',
          text: String(event.text ?? ''),
          model: String(event.model ?? ''),
          needsClarification: event.needs_clarification === true,
        },
      ]
    }
    return {
      ...state,
      stages,
      feed,
      entries,
      actualModel,
      actualThinkingLevel,
      quotaRpm,
    }
  })
}

function detailOf(name, event) {
  if (name === 'model.response') {
    if (event.response) return { detail: pretty(event.response), detailLabel: 'raw response' }
    return { detail: String(event.text ?? ''), detailLabel: 'raw response' }
  }
  if (name === 'model.request') {
    return {
      detail: pretty({
        system: event.system ?? '',
        prompt: event.prompt ?? undefined,
        contents: event.contents ?? undefined,
        documents: event.documents ?? undefined,
        tools: event.tools ?? undefined,
        temperature: event.temperature ?? undefined,
        max_output_tokens: event.max_output_tokens ?? undefined,
      }),
      detailLabel: 'raw prompt',
    }
  }
  if (name === 'tool.start') {
    return { detail: pretty(event.args ?? {}), detailLabel: 'arguments' }
  }
  if (name === 'tool.done') {
    return { detail: pretty(event.result ?? {}), detailLabel: 'result' }
  }
  if (name === 'tool.error') {
    return { detail: String(event.error ?? ''), detailLabel: 'error' }
  }
  return {}
}

function pretty(value) {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value ?? '')
  }
}

function updateActivity(entries, id, patch) {
  return (entries || []).map((entry) =>
    entry.type === 'activity' && entry.id === id ? { ...entry, ...patch } : entry,
  )
}

function attachResult(entries, result) {
  const next = [...(entries || [])]
  for (let index = next.length - 1; index >= 0; index -= 1) {
    if (next[index].type === 'message' && next[index].role === 'assistant') {
      next[index] = { ...next[index], result }
      return next
    }
  }
  next.push({
    id: `result-${Date.now()}`,
    type: 'message',
    role: 'assistant',
    // Never a bare "completed": over a ratsnest the sentence names every
    // unrouted net, and with routing skipped it says so.
    text: runOutcomeText(result),
    result,
  })
  return next
}

function plainError(error) {
  return {
    kind: String(error?.kind ?? 'internal'),
    message: String(error?.message ?? error ?? ''),
    status: Number(error?.status ?? 0),
    errorId: String(error?.errorId ?? ''),
  }
}

let sessionSeq = 0
function newSessionId() {
  sessionSeq += 1
  return `session-${Date.now().toString(36)}-${sessionSeq}`
}

function nextStages(stages, name, evt) {
  const at = finiteOrNull(evt.t_s)
  const stage = String(evt.stage ?? '')

  if (name === 'stage.start') {
    if (!stage) return stages
    return {
      ...stages,
      [stage]: { state: 'running', t_s: at, ...summaryOf(START_SUMMARY, stage, evt) },
    }
  }

  if (name === 'stage.done') {
    if (!stage) return stages
    const next = {
      ...stages,
      [stage]: { state: 'done', t_s: at, ...summaryOf(DONE_SUMMARY, stage, evt) },
    }
    // Validation and repair have no stage of their own on the wire: they run
    // inside propose, so propose finishing is the only report that they
    // finished — and a proposal accepted on the first try was still validated.
    if (stage === 'propose') {
      next.validate = { state: 'done', t_s: at, rounds: roundsIn(stages) }
    }
    return next
  }

  // The enclosure degrades instead of failing the run, so no stage.done ever
  // arrives for it — this event is the stage row's only ending, and it has to
  // say "failed" rather than leaving the row running forever.
  if (name === 'enclosure.failed') {
    return { ...stages, enclosure: { state: 'failed', t_s: at, error: String(evt.error ?? '') } }
  }
  // Sourcing degrades the same way: the rows still arrive, unsourced, and the
  // row has to say so rather than run forever.
  if (name === 'sourcing.failed') {
    return { ...stages, sourcing: { state: 'failed', t_s: at, error: String(evt.error ?? '') } }
  }

  // A rejected proposal is the one thing that proves the repair loop is turning.
  if (name === 'propose.round') {
    return { ...stages, validate: { state: 'running', t_s: at, rounds: roundsIn(stages) + 1 } }
  }

  return stages
}

function roundsIn(stages) {
  const rounds = Number(stages?.validate?.rounds)
  return Number.isFinite(rounds) ? rounds : 0
}

function summaryOf(table, stage, evt) {
  return own(table, stage) ? table[stage](evt) : {}
}

/** Own keys only: a stage named `constructor` must not reach Object.prototype. */
function own(table, key) {
  return Object.hasOwn(table, key) ? table[key] : null
}

function appendFeed(feed, entry) {
  const next = [...feed, entry]
  return next.length > MAX_FEED ? next.slice(next.length - MAX_FEED) : next
}

/** null rather than 0 for an absent number: a stage that reported no time is
    not a stage that reported zero. */
function finiteOrNull(value) {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** The only wall-clock the client owns, on the same clock as `startedAt` so
    fake timers move both. Zero when no run was started to measure from. */
function clientMs(state) {
  return state.startedAt ? Date.now() - state.startedAt : 0
}

function numberOr(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function listOf(value) {
  return Array.isArray(value) ? value : []
}

/** Short part-number lists; which part came from cache is the whole signal. */
function cacheOf(result) {
  const cache = result?.cache
  if (!cache || typeof cache !== 'object') return null
  return { hit: listOf(cache.hit), read: listOf(cache.read), unusable: listOf(cache.unusable) }
}

// The one place `served_by` and `cache` surface at all: nothing in the UI
// renders them, and the pipeline logs nothing server-side.
function logDone(state, result) {
  const ms = clientMs(state)
  const serverS = numberOr(result?.duration_s, 0)
  const blockers = listOf(result?.blockers)
  const warnings = listOf(result?.warnings)
  const status = String(result?.status ?? '')
  const pcb = typeof result?.kicad_pcb === 'string' ? result.kicad_pcb : ''
  const routing = readRouting(result)
  const unrouted = routing ? routing.unrouted.length : 0
  const note = status === 'fallback' || blockers.length || unrouted ? logWarn : logEvent
  note('run.done', `run ${state.id} finished ${status || 'with no status'} in ${ms} ms`, {
    id: state.id,
    client_ms: ms,
    server_s: serverS,
    // duration_s starts inside the service's generate(), so the difference is
    // transport plus JSON, which is the number worth watching.
    overhead_ms: ms - Math.round(serverS * 1000),
    status,
    served_by: result?.served_by ?? null,
    cache: cacheOf(result),
    repair_rounds: numberOr(result?.repair_rounds, 0),
    parts: listOf(result?.parts).length,
    nets: listOf(result?.nets).length,
    findings: listOf(result?.findings).length,
    blockers: blockers.length,
    warnings: warnings.length,
    first_warning: warnings.length ? String(warnings[0]) : '',
    wirelength_mm: result?.wirelength_mm ?? null,
    // null when the run was not routed, so a skipped stage is not a clean one.
    routed_nets: routing ? routing.routed.length : null,
    unrouted_nets: routing ? unrouted : null,
    // The board text is by far the largest thing a response carries. Its size
    // is the useful part, so only the size is kept.
    has_pcb: pcb.length > 0,
    pcb_chars: pcb.length,
  })
}

function logFailure(state, error) {
  const kind = String(error?.kind ?? '')
  const status = numberOr(error?.status, 0)
  const message = String(error?.message ?? error ?? '')
  const suspect = suspectEngineBug(status, message)
  const msg = suspect
    ? `run ${state.id} failed (${kind}). This looks like known issue 10: an internal engine error surfaced as a 400.`
    : `run ${state.id} failed (${kind})`
  logError('run.error', msg, {
    id: state.id,
    client_ms: clientMs(state),
    kind,
    status,
    errorId: String(error?.errorId ?? ''),
    message,
    suspect_engine_bug: suspect,
  })
}

/** Milliseconds since the run started; the interval exists only while running. */
export const elapsed = derived(
  run,
  ($run, set) => {
    if ($run.phase !== 'running') {
      set(0)
      return
    }
    set(Date.now() - $run.startedAt)
    const id = setInterval(() => set(Date.now() - $run.startedAt), 250)
    return () => clearInterval(id)
  },
  0,
)

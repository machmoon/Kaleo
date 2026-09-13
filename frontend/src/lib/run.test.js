import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import { clearLog, log } from './log.js'
import {
  MAX_FEED,
  elapsed,
  failRun,
  finishClarification,
  finishRun,
  resetRun,
  run,
  stageEvent,
  startRun,
} from './run.js'

// The store is a module singleton, so the idle value has to be captured at import
// time, before any test mutates it. Every test then starts from this exact object.
const IDLE = get(run)

/** Collects every value the elapsed store pushes, and cleans itself up. */
function watchElapsed() {
  const seen = []
  const unsubscribe = elapsed.subscribe((value) => seen.push(value))
  return { seen, unsubscribe, last: () => seen[seen.length - 1] }
}

beforeEach(() => {
  run.set(IDLE)
  clearLog()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the run store', () => {
  it('starts idle with nothing to show', () => {
    expect(IDLE).toEqual({
      phase: 'idle',
      request: null,
      result: null,
      error: null,
      startedAt: 0,
      id: '',
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
    })
  })
})

// Every entry the store writes goes through log.js's buffer, whose appends are
// batched onto a microtask; nothing is readable until that queue drains.
async function flushed() {
  await Promise.resolve()
  return get(log).entries
}

function lastEvent(entries, event) {
  return entries.filter((entry) => entry.event === event).pop()
}

describe('what the run store logs', () => {
  // Run ids come from a module-level counter that no API resets, so this is the
  // one test that can name an id: it is the first in the file to start a run.
  it('records the request shape under run.start, and mints r1 for the first run', async () => {
    startRun({
      intent: 'a 3v3 regulator',
      datasheets: { U1: 'https://example.com/u1.pdf' },
      time_limit_s: 20,
      no_solver_budget: false,
      review: true,
      ground: true,
    })

    const entry = lastEvent(await flushed(), 'run.start')

    expect(entry.level).toBe('info')
    expect(entry.data).toEqual({
      id: 'r1',
      intent: 'a 3v3 regulator',
      intent_chars: 15,
      datasheets: 1,
      time_limit_s: 20,
      no_solver_budget: false,
      review: true,
      ground: true,
      orchestrator_model: 'gemini-3.7-flash',
      thinking_level: 'auto',
      quota_rpm: 'auto',
    })
    expect(get(run).id).toBe('r1')
  })

  it('reads an absent ground flag as not grounded, the way the service does', async () => {
    startRun({ intent: 'x', datasheets: {}, time_limit_s: 5, review: false })

    expect(lastEvent(await flushed(), 'run.start').data).toMatchObject({
      review: false,
      ground: false,
    })
  })

  it('records the fields no other surface shows under run.done', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_000)
    startRun({ intent: 'x' })
    vi.setSystemTime(1_700_000_004_000)

    finishRun({
      status: 'feasible',
      served_by: 'gemini-3.7-flash',
      cache: { hit: ['STM32F103'], read: [], unusable: [] },
      repair_rounds: 2,
      duration_s: 3.5,
      parts: [{ ref: 'U1' }, { ref: 'C1' }],
      nets: ['vcc'],
      findings: [{ title: 'a' }],
      blockers: [],
      warnings: ['no decoupling on U1'],
      wirelength_mm: 41.2,
    })

    const entry = lastEvent(await flushed(), 'run.done')

    expect(entry.level).toBe('info')
    expect(entry.data).toMatchObject({
      id: get(run).id,
      client_ms: 4000,
      server_s: 3.5,
      overhead_ms: 500,
      status: 'feasible',
      served_by: 'gemini-3.7-flash',
      cache: { hit: ['STM32F103'], read: [], unusable: [] },
      repair_rounds: 2,
      parts: 2,
      nets: 1,
      findings: 1,
      blockers: 0,
      warnings: 1,
      first_warning: 'no decoupling on U1',
      wirelength_mm: 41.2,
    })
  })

  it('warns rather than informs when the placer fell back', async () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'fallback', blockers: [] })

    expect(lastEvent(await flushed(), 'run.done').level).toBe('warn')
  })

  it('warns when the review found a blocker', async () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible', blockers: ['U1 has no decoupling capacitor'] })

    const entry = lastEvent(await flushed(), 'run.done')

    expect(entry.level).toBe('warn')
    expect(entry.data.blockers).toBe(1)
  })

  it('keeps the done entry small when the response carries a 200 KB board', async () => {
    const kicad_pcb = 'x'.repeat(200_000)
    startRun({ intent: 'x' })

    finishRun({ status: 'feasible', kicad_pcb })

    const entry = lastEvent(await flushed(), 'run.done')

    expect(entry.data).toMatchObject({ has_pcb: true, pcb_chars: 200_000 })
    expect(JSON.stringify(entry)).not.toContain('xxxxx')
    expect(JSON.stringify(entry).length).toBeLessThan(1024)
  })

  it('reports no board when the response carries none', async () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })

    expect(lastEvent(await flushed(), 'run.done').data).toMatchObject({
      has_pcb: false,
      pcb_chars: 0,
    })
  })

  it('records the kind, status and error id under run.error', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_000)
    startRun({ intent: 'x' })
    vi.setSystemTime(1_700_000_000_250)

    failRun({ name: 'ApiError', kind: 'internal', status: 500, errorId: 'e-42', message: 'boom' })

    const entry = lastEvent(await flushed(), 'run.error')

    expect(entry.level).toBe('error')
    expect(entry.data).toEqual({
      id: get(run).id,
      client_ms: 250,
      kind: 'internal',
      status: 500,
      errorId: 'e-42',
      message: 'boom',
      suspect_engine_bug: false,
    })
  })

  it('names known issue 10 when a 400 mentions no request field', async () => {
    startRun({ intent: 'x' })

    failRun({ kind: 'validation', status: 400, message: 'not enough values to unpack' })

    const entry = lastEvent(await flushed(), 'run.error')

    expect(entry.data.suspect_engine_bug).toBe(true)
    expect(entry.msg).toContain('known issue 10')
  })

  it('leaves a plain field-validation 400 unflagged', async () => {
    startRun({ intent: 'x' })

    failRun({ kind: 'validation', status: 400, message: 'intent must be a string' })

    const entry = lastEvent(await flushed(), 'run.error')

    expect(entry.data.suspect_engine_bug).toBe(false)
    expect(entry.msg).not.toContain('known issue 10')
  })

  it('records run.reset at debug level, naming the run it cleared', async () => {
    startRun({ intent: 'x' })
    const { id } = get(run)

    resetRun()

    const entry = lastEvent(await flushed(), 'run.reset')

    expect(entry.level).toBe('debug')
    expect(entry.data).toEqual({ id })
  })
})

describe('startRun', () => {
  it('enters the running phase and holds on to the request', () => {
    const request = { intent: 'a 3v3 regulator' }

    startRun(request)

    expect(get(run).phase).toBe('running')
    expect(get(run).request).toBe(request)
  })

  it('stamps the start time so the ticker has an origin', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_000)

    startRun({ intent: 'x' })

    expect(get(run).startedAt).toBe(1_700_000_000_000)
  })

  it('clears the previous result and error, so a retry shows a clean slate', () => {
    startRun({ intent: 'first' })
    failRun(new Error('boom'))

    startRun({ intent: 'second' })

    expect(get(run).result).toBeNull()
    expect(get(run).error).toBeNull()
  })

  it('starts a transcript with the user message and a live activity entry', () => {
    startRun({ intent: 'a regulator' })

    expect(get(run).entries).toMatchObject([
      { type: 'message', role: 'user', text: 'a regulator' },
      { type: 'activity', phase: 'running', feed: [], stages: {} },
    ])
    expect(get(run).sessionId).toMatch(/^session-/)
  })

  it('keeps earlier transcript entries for a clarification answer', () => {
    startRun({ intent: 'a regulator' })
    finishClarification({ model: 'gemini-test' })
    const sessionId = get(run).sessionId

    startRun(
      { intent: 'a regulator' },
      { preserve: true, message: '5 V input' },
    )

    expect(get(run).sessionId).toBe(sessionId)
    expect(get(run).entries.filter((entry) => entry.role === 'user')).toHaveLength(2)
  })
})

describe('finishRun', () => {
  it('names every unrouted net in the client-side completion message', () => {
    startRun({ intent: 'x' })

    finishRun({
      status: 'feasible',
      routing: { routed: ['GND'], unrouted: { SW: 'blocked', FB: 'no node' } },
    })

    const assistant = get(run).entries.filter((entry) => entry.role === 'assistant')
    expect(assistant).toHaveLength(1)
    expect(assistant[0].text).toBe(
      'The board run completed with 2 of 3 nets left unrouted: FB, SW.',
    )
  })

  it('says a run with no routing block was not routed rather than completed', () => {
    startRun({ intent: 'x' })

    finishRun({ status: 'feasible' })

    const assistant = get(run).entries.filter((entry) => entry.role === 'assistant')
    expect(assistant[0].text).toBe('The board run completed; the board was not routed.')
  })

  it('moves a running run to done and carries the result', () => {
    const result = { status: 'feasible' }
    startRun({ intent: 'x' })

    finishRun(result)

    expect(get(run).phase).toBe('done')
    expect(get(run).result).toBe(result)
    expect(get(run).error).toBeNull()
  })

  it('keeps the request, which the chrome reads to label the run', () => {
    const request = { intent: 'x', review: false }
    startRun(request)

    finishRun({ status: 'feasible' })

    expect(get(run).request).toBe(request)
  })

  it('keeps the start time', () => {
    startRun({ intent: 'x' })
    const { startedAt } = get(run)

    finishRun({ status: 'feasible' })

    expect(get(run).startedAt).toBe(startedAt)
  })

  it('walks idle to running to done', () => {
    const phases = []
    const unsubscribe = run.subscribe((state) => phases.push(state.phase))

    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })
    unsubscribe()

    expect(phases).toEqual(['idle', 'running', 'done'])
  })
})

describe('failRun', () => {
  it('moves a running run to error and carries the error', () => {
    const error = new Error('upstream is down')
    startRun({ intent: 'x' })

    failRun(error)

    expect(get(run).phase).toBe('error')
    expect(get(run).error).toBe(error)
    expect(get(run).result).toBeNull()
  })

  it('keeps the request, which the error panel retries with', () => {
    const request = { intent: 'x' }
    startRun(request)

    failRun(new Error('boom'))

    expect(get(run).request).toBe(request)
  })

  it('walks idle to running to error', () => {
    const phases = []
    const unsubscribe = run.subscribe((state) => phases.push(state.phase))

    startRun({ intent: 'x' })
    failRun(new Error('boom'))
    unsubscribe()

    expect(phases).toEqual(['idle', 'running', 'error'])
  })

  it('drops a result left over from an earlier success', () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })

    failRun(new Error('the retry failed'))

    expect(get(run).result).toBeNull()
  })
})

describe('resetRun', () => {
  it('returns to idle', () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })

    resetRun()

    expect(get(run).phase).toBe('idle')
    expect(get(run).result).toBeNull()
    expect(get(run).error).toBeNull()
    expect(get(run).startedAt).toBe(0)
  })

  it('keeps the request, so dismissing an error leaves the form filled in', () => {
    const request = { intent: 'a 3v3 regulator' }
    startRun(request)
    failRun(new Error('boom'))

    resetRun()

    expect(get(run).request).toBe(request)
  })
})

describe('the elapsed ticker', () => {
  it('reads zero while idle', () => {
    const watcher = watchElapsed()

    expect(watcher.last()).toBe(0)

    watcher.unsubscribe()
  })

  it('runs no interval while idle', () => {
    vi.useFakeTimers()

    const watcher = watchElapsed()

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('publishes zero the moment a run starts, before the first tick', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()

    startRun({ intent: 'x' })

    expect(watcher.last()).toBe(0)

    watcher.unsubscribe()
  })

  it('ticks every 250 ms while running', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })

    vi.advanceTimersByTime(250)
    expect(watcher.last()).toBe(250)

    vi.advanceTimersByTime(250)
    expect(watcher.last()).toBe(500)

    vi.advanceTimersByTime(750)
    expect(watcher.last()).toBe(1250)

    watcher.unsubscribe()
  })

  it('measures from the start of the run, not from the first subscription', () => {
    vi.useFakeTimers()
    startRun({ intent: 'x' })
    vi.advanceTimersByTime(4000)

    const watcher = watchElapsed()

    expect(watcher.last()).toBe(4000)

    watcher.unsubscribe()
  })

  it('starts exactly one interval no matter how many subscribers there are', () => {
    vi.useFakeTimers()
    const first = watchElapsed()
    const second = watchElapsed()

    startRun({ intent: 'x' })

    expect(vi.getTimerCount()).toBe(1)

    first.unsubscribe()
    second.unsubscribe()
  })

  it('clears the interval when the run finishes', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    expect(vi.getTimerCount()).toBe(1)

    finishRun({ status: 'feasible' })

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('clears the interval when the run fails', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    expect(vi.getTimerCount()).toBe(1)

    failRun(new Error('boom'))

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('clears the interval on reset', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })

    resetRun()

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('stops publishing once the phase leaves running', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    vi.advanceTimersByTime(1000)

    finishRun({ status: 'feasible' })
    const settled = watcher.seen.length
    vi.advanceTimersByTime(10_000)

    expect(watcher.seen).toHaveLength(settled)
    expect(watcher.last()).toBe(0)

    watcher.unsubscribe()
  })

  it('clears the interval when the last subscriber goes away mid-run', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    expect(vi.getTimerCount()).toBe(1)

    watcher.unsubscribe()

    expect(vi.getTimerCount()).toBe(0)
  })

  it('leaks no timer across a full run and a second run after it', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()

    startRun({ intent: 'first' })
    vi.advanceTimersByTime(500)
    finishRun({ status: 'feasible' })
    startRun({ intent: 'second' })
    vi.advanceTimersByTime(500)
    finishRun({ status: 'feasible' })

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('stageEvent: the stage list', () => {
  it('moves a stage from pending, through running, to done', () => {
    startRun({ intent: 'x' })
    expect(get(run).stages.place).toBeUndefined()

    stageEvent({ event: 'stage.start', stage: 'place', t_s: 0.2, time_limit_s: 20 })
    expect(get(run).stages.place).toEqual({ state: 'running', t_s: 0.2, time_limit_s: 20 })

    stageEvent({
      event: 'stage.done',
      stage: 'place',
      t_s: 3.1,
      solver_status: 'FEASIBLE',
      board_mm: [24, 18],
      wirelength_mm: 312.5,
      warnings: ['one'],
    })
    expect(get(run).stages.place).toEqual({
      state: 'done',
      t_s: 3.1,
      solver_status: 'FEASIBLE',
      board_mm: [24, 18],
      wirelength_mm: 312.5,
      warnings: 1,
    })
  })

  it('leaves a stage the pipeline skipped with no entry at all', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'propose', t_s: 0.1 })
    stageEvent({ event: 'stage.done', stage: 'propose', t_s: 2 })

    // No datasheets were sent, so no read stage ever ran; nothing may tick it.
    expect(get(run).stages.read).toBeUndefined()
    expect(get(run).stages.review).toBeUndefined()
  })

  it('summarises a finished read as counts, whether it sent lists or numbers', () => {
    startRun({ intent: 'x' })

    stageEvent({
      event: 'stage.done',
      stage: 'read',
      t_s: 4,
      parts: ['STM32F103'],
      pins: 48,
      requirements: ['a 100n decoupling cap per rail'],
    })

    expect(get(run).stages.read).toEqual({
      state: 'done',
      t_s: 4,
      parts: 1,
      pins: 48,
      requirements: 1,
    })
  })

  it('summarises a finished review as counts', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.done', stage: 'review', t_s: 9, findings: [1, 2, 3], blockers: [1] })

    expect(get(run).stages.review).toEqual({ state: 'done', t_s: 9, findings: 3, blockers: 1 })
  })

  it('summarises a finished enclosure with the frozen stage.done fields', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'enclosure', t_s: 10 })
    expect(get(run).stages.enclosure).toEqual({ state: 'running', t_s: 10 })

    stageEvent({
      event: 'stage.done',
      stage: 'enclosure',
      t_s: 14,
      cutouts: 2,
      lid: 'friction',
      wall_mm: 2.0,
      repair_rounds: 1,
      rendered: false,
    })
    expect(get(run).stages.enclosure).toEqual({
      state: 'done',
      t_s: 14,
      cutouts: 2,
      lid: 'friction',
      wall_mm: 2.0,
      repair_rounds: 1,
      rendered: false,
    })
  })

  it('marks the enclosure failed on enclosure.failed — the row must not run forever', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'enclosure', t_s: 10 })
    stageEvent({ event: 'enclosure.failed', t_s: 12, error: 'repair budget exhausted' })

    expect(get(run).stages.enclosure).toEqual({
      state: 'failed',
      t_s: 12,
      error: 'repair budget exhausted',
    })
  })

  it('summarises finished sourcing with the frozen stage.done counts', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'sourcing', t_s: 10 })
    expect(get(run).stages.sourcing).toEqual({ state: 'running', t_s: 10 })

    stageEvent({ event: 'stage.done', stage: 'sourcing', t_s: 15, verified: 4, proposed: 5, unresolved: 2 })
    expect(get(run).stages.sourcing).toEqual({
      state: 'done',
      t_s: 15,
      verified: 4,
      proposed: 5,
      unresolved: 2,
    })
  })

  it('marks sourcing failed on sourcing.failed — the row must not run forever', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'sourcing', t_s: 10 })
    stageEvent({ event: 'sourcing.failed', t_s: 12, error: 'model answered nothing usable' })

    expect(get(run).stages.sourcing).toEqual({
      state: 'failed',
      t_s: 12,
      error: 'model answered nothing usable',
    })
  })

  it('reports an absent time as nothing rather than as zero', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'propose' })

    expect(get(run).stages.propose.t_s).toBeNull()
  })

  it('ignores a stage event that names no stage', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', t_s: 1 })

    expect(get(run).stages).toEqual({})
  })
})

describe('stageEvent: the synthetic validate row', () => {
  it('starts running on the first rejected proposal and counts the rounds', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'propose.round', round: 1, errors: 2, first_error: 'C1.1 is not a pin' })
    expect(get(run).stages.validate).toEqual({ state: 'running', t_s: null, rounds: 1 })

    stageEvent({ event: 'propose.round', round: 2, errors: 1, t_s: 6 })
    expect(get(run).stages.validate).toEqual({ state: 'running', t_s: 6, rounds: 2 })
  })

  it('finishes when propose finishes, keeping the rounds it counted', () => {
    startRun({ intent: 'x' })
    stageEvent({ event: 'propose.round', round: 1, errors: 2 })

    stageEvent({ event: 'stage.done', stage: 'propose', t_s: 8, parts: [1, 2], nets: [1] })

    expect(get(run).stages.validate).toEqual({ state: 'done', t_s: 8, rounds: 1 })
    expect(get(run).stages.propose).toMatchObject({ state: 'done', parts: 2, nets: 1 })
  })

  it('finishes even when no proposal was ever rejected, because it still validated', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.done', stage: 'propose', t_s: 5, parts: [1], nets: [] })

    expect(get(run).stages.validate).toEqual({ state: 'done', t_s: 5, rounds: 0 })
  })

  it('is untouched by a propose stage that has only started', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'propose', t_s: 0.1 })

    expect(get(run).stages.validate).toBeUndefined()
  })
})

describe('stageEvent: the feed', () => {
  it('keeps the route stage tally on its own row', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.start', stage: 'route', t_s: 4.0 })
    expect(get(run).stages.route).toEqual({ state: 'running', t_s: 4.0 })

    stageEvent({
      event: 'stage.done',
      stage: 'route',
      t_s: 4.6,
      tracks: 28,
      vias: 5,
      routed_nets: 5,
      unrouted_nets: 1,
      copper_mm: 59.0,
    })
    expect(get(run).stages.route).toEqual({
      state: 'done',
      t_s: 4.6,
      tracks: 28,
      vias: 5,
      routed_nets: 5,
      unrouted_nets: 1,
    })
  })

  it('appends the sentence, the time and the event name', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'run.accepted', t_s: 0 })
    stageEvent({ event: 'stage.start', stage: 'place', t_s: 0.4, time_limit_s: 20 })

    const feed = get(run).feed
    expect(feed).toHaveLength(2)
    expect(feed[0]).toMatchObject({ t_s: 0, text: 'request accepted, pipeline starting', event: 'run.accepted' })
    expect(feed[1].text).toContain('placing with CP-SAT')
    expect(feed[1].event).toBe('stage.start')
  })

  it('gives every row an id of its own, since the list is keyed by it', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'run.accepted', t_s: 0 })
    stageEvent({ event: 'run.accepted', t_s: 0 })

    const ids = get(run).feed.map((row) => row.id)
    expect(new Set(ids).size).toBe(2)
  })

  it('keeps a row for an event nothing else understands', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'ground.part', part: 'STM32F103', cached: true, t_s: 2 })

    expect(get(run).feed[0].text).toBe('grounding STM32F103 (cached pages)')
  })

  it('keeps the raw answer on the row that carried it, and only on that row', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'model.call', stage: 'propose', provider: 'gemini', ok: true })
    stageEvent({
      event: 'model.response',
      stage: 'propose',
      chars: 15,
      truncated: false,
      text: '{"devices": {}}',
      t_s: 4,
    })

    const [call, response] = get(run).feed
    expect(call.detail).toBeUndefined()
    expect(response).toMatchObject({ event: 'model.response', detail: '{"devices": {}}' })
    expect(response.text).toBe('response (propose): 15 chars')
  })

  it('keeps observable model prompts expandable and correlated by call id', () => {
    startRun({ intent: 'x' })

    stageEvent({
      event: 'model.request',
      layer: 'orchestrator',
      call_id: 'orchestrator-1',
      system: 'system instruction',
      contents: [{ role: 'user', text: 'make a board' }],
    })

    const [request] = get(run).feed
    expect(request).toMatchObject({
      event: 'model.request',
      layer: 'orchestrator',
      callId: 'orchestrator-1',
      detailLabel: 'raw prompt',
    })
    expect(request.detail).toContain('system instruction')
    expect(request.detail).toContain('make a board')
  })

  it('keeps the session model attributed to the orchestrator across worker calls', () => {
    startRun(
      { intent: 'board', datasheets: {} },
      { model: 'gemini-3.1-pro-preview', thinkingLevel: 'high', quotaRpm: '6' },
    )
    stageEvent({
      event: 'chat.accepted',
      layer: 'orchestrator',
      model: 'gemini-root',
      thinking_level: 'high',
      quota_rpm: 6,
    })
    stageEvent({
      event: 'model.call',
      layer: 'worker',
      model: 'gemini-worker',
      stage: 'propose',
      ok: true,
    })

    expect(get(run).actualModel).toBe('gemini-root')
    expect(get(run)).toMatchObject({
      orchestratorModel: 'gemini-3.1-pro-preview',
      thinkingLevel: 'high',
      actualThinkingLevel: 'high',
      quotaRpm: '6',
    })
  })

  it('moves the orchestrator answer into the transcript rather than the activity feed', () => {
    startRun({ intent: 'x' })

    stageEvent({
      event: 'assistant.message',
      event_id: 'answer-1',
      text: 'Which input voltage?',
      needs_clarification: true,
    })

    expect(get(run).feed).toEqual([])
    expect(get(run).entries.at(-1)).toMatchObject({
      id: 'answer-1',
      type: 'message',
      role: 'assistant',
      text: 'Which input voltage?',
      needsClarification: true,
    })
  })

  it('carries an empty detail rather than nothing for a response with no text', () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'model.response', stage: 'propose', chars: 0 })

    expect(get(run).feed[0].detail).toBe('')
  })

  it('keeps only the last MAX_FEED rows', () => {
    startRun({ intent: 'x' })

    for (let i = 0; i < MAX_FEED + 5; i += 1) {
      stageEvent({ event: 'model.call', stage: 'propose', provider: `p${i}`, ok: true })
    }

    const feed = get(run).feed
    expect(feed).toHaveLength(MAX_FEED)
    expect(feed[feed.length - 1].text).toContain(`p${MAX_FEED + 4}`)
    expect(feed[0].text).toContain('p5')
  })

  it('bounds a debug feed too, where every row carries a raw answer', () => {
    startRun({ intent: 'x' })

    for (let i = 0; i < MAX_FEED + 5; i += 1) {
      stageEvent({ event: 'model.response', stage: 'propose', chars: i, text: `answer ${i}` })
    }

    const feed = get(run).feed
    expect(feed).toHaveLength(MAX_FEED)
    expect(feed[feed.length - 1].detail).toBe(`answer ${MAX_FEED + 4}`)
    expect(feed[0].detail).toBe('answer 5')
  })
})

describe('stageEvent: what it writes to the debug log', () => {
  it('records one server-sourced line per event, under a pipeline name', async () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'stage.done', stage: 'review', t_s: 9, findings: [], blockers: [] })

    const entry = lastEvent(await flushed(), 'pipeline.stage.done')
    expect(entry).toMatchObject({ level: 'info', src: 'server', msg: 'review: no findings' })
    expect(entry.data).toMatchObject({ event: 'stage.done', stage: 'review', t_s: 9 })
  })

  it('records a failed run at error level and a provider retry at warn', async () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'model.retry', provider: 'gemini', error: '429', elapsed_s: 1.2 })
    stageEvent({ event: 'run.error', status: 502, error: 'gemini returned 429' })

    const entries = await flushed()
    expect(lastEvent(entries, 'pipeline.model.retry').level).toBe('warn')
    expect(lastEvent(entries, 'pipeline.run.error').level).toBe('error')
  })

  it('records a degraded enclosure at warn, not error — the run continues', async () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'enclosure.failed', t_s: 12, error: 'repair budget exhausted' })

    expect(lastEvent(await flushed(), 'pipeline.enclosure.failed').level).toBe('warn')
  })

  it('records failed sourcing at warn, not error — the board still ships with its rows', async () => {
    startRun({ intent: 'x' })

    stageEvent({ event: 'sourcing.failed', t_s: 12, error: 'model answered nothing usable' })

    expect(lastEvent(await flushed(), 'pipeline.sourcing.failed').level).toBe('warn')
  })

  it('names an event carrying no name rather than writing a bare prefix', async () => {
    startRun({ intent: 'x' })

    stageEvent({})

    expect(lastEvent(await flushed(), 'pipeline.unknown')).toBeTruthy()
  })

  it('survives an event that is not an object at all', () => {
    startRun({ intent: 'x' })

    expect(() => stageEvent(null)).not.toThrow()
    expect(get(run).feed).toHaveLength(1)
  })
})

describe('stageEvent: what clears it', () => {
  it('starts every run with an empty stage list and feed', () => {
    startRun({ intent: 'first' })
    stageEvent({ event: 'stage.done', stage: 'place', t_s: 1 })

    startRun({ intent: 'second' })

    expect(get(run).stages).toEqual({})
    expect(get(run).feed).toEqual([])
  })

  it('clears both on reset', () => {
    startRun({ intent: 'x' })
    stageEvent({ event: 'stage.start', stage: 'place', t_s: 1 })
    finishRun({ status: 'feasible' })

    resetRun()

    expect(get(run).stages).toEqual({})
    expect(get(run).feed).toEqual([])
  })

  it('never writes through to the idle holder, so the next run starts empty', () => {
    startRun({ intent: 'x' })
    stageEvent({ event: 'stage.start', stage: 'place', t_s: 1 })
    resetRun()

    expect(IDLE.stages).toEqual({})
    expect(IDLE.feed).toEqual([])
  })
})

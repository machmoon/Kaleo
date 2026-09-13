import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import {
  ApiError,
  cancelRun,
  newRunId,
  runStatus,
  MAX_REQUEST_BYTES,
  MAX_TIME_LIMIT_S,
  MIN_TIME_LIMIT_S,
  REQUEST_TIMEOUT_MS,
  chatStream,
  generate,
  generateStream,
  getConfigurationStatus,
  listModels,
  normalizePlacementRequest,
  normalizeRequest,
  requestBytes,
} from './api.js'
import { clearLog, log } from './log.js'

/** A fetch Response is duck-typed here: generate() only reads ok, status and json(). */
function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

function unparseableResponse(status) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected token in JSON at position 0')
    },
  }
}

function stubFetch(response) {
  const fetch = vi.fn(async () => response)
  vi.stubGlobal('fetch', fetch)
  return fetch
}

function stubFetchRejecting(error) {
  const fetch = vi.fn(async () => {
    throw error
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

/** Answers per path, which is the only way to watch a fallback: the stream
    endpoint and the one-shot endpoint have to reply differently in one test. */
function stubFetchByPath(responses) {
  const fetch = vi.fn(async (url) => {
    const response = responses[url]
    if (!response) throw new TypeError(`no stub for ${url}`)
    return response
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

const ENCODER = new TextEncoder()

/** A duck-typed streaming Response. `cut` makes the reader throw once the
    chunks run out, which is a connection dying mid-run. */
function streamResponse(chunks, { status = 200, type = 'application/x-ndjson', cut = false } = {}) {
  const queue = chunks.map((chunk) => ENCODER.encode(chunk))
  let sent = 0
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (String(name).toLowerCase() === 'content-type' ? type : null) },
    json: async () => ({}),
    body: {
      getReader: () => ({
        read: async () => {
          if (sent < queue.length) return { value: queue[sent++], done: false }
          if (cut) throw new TypeError('network error')
          return { value: undefined, done: true }
        },
        cancel: async () => {},
      }),
    },
  }
}

/** The frames a plain successful run sends, with one object deliberately split
    across two chunks — a reader loop that reassembles nothing would fail here. */
function happyFrames(result = OK_BODY) {
  return [
    '{"event":"run.accepted","t_s":0.0}\n{"event":"stage.start","stage":"pl',
    'ace","t_s":0.2,"time_limit_s":20}\n',
    '{"event":"stage.done","stage":"place","t_s":3.1,"solver_status":"FEASIBLE"}\n',
    `{"event":"run.done","result":${JSON.stringify(result)}}\n`,
  ]
}

/** The minimum a successful response needs; every field is optional to the normalizer. */
const OK_BODY = { status: 'feasible', board_mm: [20, 30], parts: [{ ref: 'U1' }] }

function happyChatFrames(result = OK_BODY) {
  return [
    '{"event":"chat.accepted","model":"gemini-auto","thinking_level":"high","quota_rpm":6}\n',
    '{"event":"assistant.message","text":"Ready.","needs_clarification":false}\n',
    `{"event":"chat.done","assistant":"Ready.","model":"gemini-auto","thinking_level":"high","quota_rpm":6,"needs_clarification":false,"result":${JSON.stringify(result)}}\n`,
  ]
}

// The log buffer is a module singleton and appends are batched through a
// microtask, so a read waits one turn before looking.
async function recorded(event) {
  await Promise.resolve()
  return get(log).entries.filter((entry) => entry.event === event)
}

beforeEach(() => {
  clearLog()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('normalizeRequest', () => {
  it('trims the intent', () => {
    expect(normalizeRequest({ intent: '  a 3v3 regulator  ' }).intent).toBe('a 3v3 regulator')
  })

  it('treats a missing intent as empty rather than the string "undefined"', () => {
    expect(normalizeRequest({}).intent).toBe('')
    expect(normalizeRequest({ intent: null }).intent).toBe('')
  })

  it('keeps a datasheet row that has both a part and a url', () => {
    const request = normalizeRequest({ datasheets: { STM32F103: 'https://example.com/ds.pdf' } })

    expect(request.datasheets).toEqual({ STM32F103: 'https://example.com/ds.pdf' })
  })

  it('trims both sides of a datasheet row', () => {
    const request = normalizeRequest({ datasheets: { '  U1  ': '  https://e.com/a.pdf  ' } })

    expect(request.datasheets).toEqual({ U1: 'https://e.com/a.pdf' })
  })

  it('drops a half-filled datasheet row', () => {
    const request = normalizeRequest({
      datasheets: { KeptPart: 'https://e.com/a.pdf', NoUrl: '', '': 'https://e.com/b.pdf' },
    })

    expect(request.datasheets).toEqual({ KeptPart: 'https://e.com/a.pdf' })
  })

  it('drops a row whose halves are only whitespace', () => {
    expect(normalizeRequest({ datasheets: { '   ': '   ' } }).datasheets).toEqual({})
  })

  it('produces an empty datasheet map when none were given', () => {
    expect(normalizeRequest({}).datasheets).toEqual({})
    expect(normalizeRequest({ datasheets: null }).datasheets).toEqual({})
  })

  it('leaves a solver budget inside the accepted range alone', () => {
    expect(normalizeRequest({ time_limit_s: 30 }).time_limit_s).toBe(30)
  })

  it('keeps both ends of the accepted range', () => {
    expect(normalizeRequest({ time_limit_s: MIN_TIME_LIMIT_S }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: MAX_TIME_LIMIT_S }).time_limit_s).toBe(120)
  })

  it('clamps a solver budget below the floor up to five seconds', () => {
    expect(normalizeRequest({ time_limit_s: 4 }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: 0 }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: -100 }).time_limit_s).toBe(5)
  })

  it('clamps a solver budget above the ceiling down to 120 seconds', () => {
    expect(normalizeRequest({ time_limit_s: 121 }).time_limit_s).toBe(120)
    expect(normalizeRequest({ time_limit_s: 100000 }).time_limit_s).toBe(120)
  })

  it('rounds a fractional solver budget to a whole second', () => {
    expect(normalizeRequest({ time_limit_s: 12.4 }).time_limit_s).toBe(12)
    expect(normalizeRequest({ time_limit_s: 12.6 }).time_limit_s).toBe(13)
  })

  it('accepts a numeric string, since a number input yields one', () => {
    expect(normalizeRequest({ time_limit_s: '20' }).time_limit_s).toBe(20)
  })

  it('falls back to the floor when the solver budget is missing or unparseable', () => {
    expect(normalizeRequest({}).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: 'soon' }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: Infinity }).time_limit_s).toBe(5)
  })

  it('runs the review unless it was explicitly switched off', () => {
    expect(normalizeRequest({}).review).toBe(true)
    expect(normalizeRequest({ review: true }).review).toBe(true)
    expect(normalizeRequest({ review: undefined }).review).toBe(true)
  })

  it('honours an explicit false for the review', () => {
    expect(normalizeRequest({ review: false }).review).toBe(false)
  })

  it('defaults no-solver-budget on and preserves an explicit off choice', () => {
    expect(normalizeRequest({}).no_solver_budget).toBe(true)
    expect(normalizeRequest({ no_solver_budget: true }).no_solver_budget).toBe(true)
    expect(normalizeRequest({ no_solver_budget: false }).no_solver_budget).toBe(false)
    expect(normalizeRequest({ no_solver_budget: 'yes' }).no_solver_budget).toBe(false)
  })

  it('emits the stable board and placement fields, dropping anything else', () => {
    const request = normalizeRequest({ intent: 'x', nonsense: 'drop me' })

    expect(Object.keys(request).sort()).toEqual([
      'datasheets',
      'experimental_placement',
      'intent',
      'no_solver_budget',
      'placement_policy',
      'placement_profile',
      'review',
      'time_limit_s',
    ])
  })

  it('passes grounding through when it was asked for', () => {
    expect(normalizeRequest({ ground: true }).ground).toBe(true)
  })

  it('passes the enclosure opt-in through when it was asked for', () => {
    const request = normalizeRequest({ enclosure: true })

    expect(request.enclosure).toBe(true)
    expect(request).not.toHaveProperty('enclosure_style')
  })

  it('omits the enclosure entirely rather than sending false, which claims nothing', () => {
    expect(normalizeRequest({})).not.toHaveProperty('enclosure')
    expect(normalizeRequest({ enclosure: false })).not.toHaveProperty('enclosure')
    expect(normalizeRequest({ enclosure: 'yes' })).not.toHaveProperty('enclosure')
  })

  it('trims the enclosure style and sends it only alongside the opt-in', () => {
    const request = normalizeRequest({ enclosure: true, enclosure_style: '  vented lid  ' })

    expect(request.enclosure_style).toBe('vented lid')
    expect(normalizeRequest({ enclosure_style: 'vented lid' })).not.toHaveProperty(
      'enclosure_style',
    )
  })

  it('drops a whitespace-only enclosure style rather than sending an empty string', () => {
    expect(normalizeRequest({ enclosure: true, enclosure_style: '   ' })).not.toHaveProperty(
      'enclosure_style',
    )
  })

  it('caps the enclosure style at the 500 characters the service accepts', () => {
    const request = normalizeRequest({ enclosure: true, enclosure_style: 'x'.repeat(600) })

    expect(request.enclosure_style).toHaveLength(500)
  })

  it('forwards rigorous fit checks only alongside the enclosure opt-in', () => {
    const request = normalizeRequest({ enclosure: true, enclosure_rigorous: true })

    expect(request.enclosure_rigorous).toBe(true)
    expect(normalizeRequest({ enclosure_rigorous: true })).not.toHaveProperty(
      'enclosure_rigorous',
    )
    expect(
      normalizeRequest({ enclosure: false, enclosure_rigorous: true }),
    ).not.toHaveProperty('enclosure_rigorous')
  })

  it('omits rigorous fit checks entirely rather than sending false, which claims nothing', () => {
    expect(normalizeRequest({ enclosure: true })).not.toHaveProperty('enclosure_rigorous')
    expect(
      normalizeRequest({ enclosure: true, enclosure_rigorous: false }),
    ).not.toHaveProperty('enclosure_rigorous')
    expect(
      normalizeRequest({ enclosure: true, enclosure_rigorous: 'yes' }),
    ).not.toHaveProperty('enclosure_rigorous')
  })

  it('keeps rigorous fit checks stable through renormalization, so retry and restore cannot drop them', () => {
    const first = normalizeRequest({ intent: 'a board', enclosure: true, enclosure_rigorous: true })
    const again = normalizeRequest(first)

    expect(again.enclosure_rigorous).toBe(true)
  })

  it('keeps the enclosure opt-in stable through renormalization, so retry and restore cannot drop it', () => {
    const first = normalizeRequest({
      intent: 'a board',
      enclosure: true,
      enclosure_style: '  snap-fit  ',
    })
    const again = normalizeRequest(first)

    expect(again.enclosure).toBe(true)
    expect(again.enclosure_style).toBe('snap-fit')
  })

  it('passes the sourcing opt-in through when it was asked for', () => {
    expect(normalizeRequest({ sourcing: true }).sourcing).toBe(true)
  })

  it('omits sourcing entirely rather than sending false, which claims nothing', () => {
    expect(normalizeRequest({})).not.toHaveProperty('sourcing')
    expect(normalizeRequest({ sourcing: false })).not.toHaveProperty('sourcing')
    expect(normalizeRequest({ sourcing: 'yes' })).not.toHaveProperty('sourcing')
    expect(normalizeRequest({ sourcing: 1 })).not.toHaveProperty('sourcing')
  })

  it('sends sourcing independently of the case — either opt-in stands alone', () => {
    expect(normalizeRequest({ sourcing: true })).not.toHaveProperty('enclosure')
    expect(normalizeRequest({ enclosure: true })).not.toHaveProperty('sourcing')
  })

  it('keeps the sourcing opt-in stable through renormalization, so retry and restore cannot drop it', () => {
    const first = normalizeRequest({ intent: 'a board', sourcing: true })
    expect(normalizeRequest(first).sourcing).toBe(true)
  })

  it('omits grounding entirely rather than sending false, which claims nothing', () => {
    expect(normalizeRequest({}).ground).toBeUndefined()
    expect(normalizeRequest({ ground: false }).ground).toBeUndefined()
    expect(normalizeRequest({ ground: undefined }).ground).toBeUndefined()
    expect('ground' in normalizeRequest({ ground: false })).toBe(false)
  })

  it('sends grounding only for a literal true, not for anything merely truthy', () => {
    expect(normalizeRequest({ ground: 'yes' }).ground).toBeUndefined()
    expect(normalizeRequest({ ground: 1 }).ground).toBeUndefined()
  })

  it('adds the grounding field to the emitted set only when it is on', () => {
    expect(Object.keys(normalizeRequest({ intent: 'x', ground: true })).sort()).toEqual([
      'datasheets',
      'experimental_placement',
      'ground',
      'intent',
      'no_solver_budget',
      'placement_policy',
      'placement_profile',
      'review',
      'time_limit_s',
    ])
  })

  it('runs deterministic placement verification by default and can turn it off', () => {
    expect(normalizeRequest({})).toMatchObject({
      placement_profile: 'compact-control',
      placement_policy: 'deterministic',
      experimental_placement: false,
    })
    expect(normalizeRequest({ placement_enabled: false }).placement_profile).toBeUndefined()
  })

  it('only forwards trace consent with the experimental switch on', () => {
    expect(normalizeRequest({ record_trace: true }).record_trace).toBeUndefined()
    expect(
      normalizeRequest({ experimental_placement: true, record_trace: true }).record_trace,
    ).toBe(true)
  })

  it('omits constraints by default so prompt-only demos remain valid', () => {
    expect(normalizeRequest({ intent: 'a regulator' }).constraints).toBeUndefined()
  })

  it('normalizes an enabled manifest, drops unknown keys, and invalidates approval', () => {
    const constraints = normalizeRequest({
      constraints: {
        version: 2,
        approved: true,
        board_layers: 2,
        unexpected: 'drop me',
        net_classes: [{
          name: 'Signals',
          kind: 'signal',
          nets: ['LED'],
          allowed_layers: ['F.Cu'],
          max_layer_transitions: 0,
          max_vias_per_net: 0,
          unknown_limit: 99,
        }],
      },
    }).constraints

    expect(constraints).toMatchObject({
      version: 2,
      approved: false,
      board_layers: 2,
      net_classes: [{ name: 'Signals', nets: ['LED'], allowed_layers: ['F.Cu'] }],
    })
    expect(constraints.unexpected).toBeUndefined()
    expect(constraints.net_classes[0].unknown_limit).toBeUndefined()
  })

  it('migrates restored version-one constraints and invalidates their approval', () => {
    const constraints = normalizeRequest({
      constraints: {
        version: 1,
        approved: true,
        board_layers: 2,
        net_classes: [{
          name: 'Signals',
          kind: 'signal',
          nets: ['LED'],
          allowed_layers: ['F.Cu'],
          max_layer_transitions: 0,
          max_vias_per_net: 0,
        }],
      },
    }).constraints

    expect(constraints.version).toBe(2)
    expect(constraints.approved).toBe(false)
  })
})

describe('normalizePlacementRequest', () => {
  it('defaults to the reproducible placement demo', () => {
    expect(normalizePlacementRequest()).toEqual({
      profile: 'compact-control',
      policy: 'deterministic',
      experimental_placement: false,
    })
  })

  it('keeps structured feedback and normalizes the policy', () => {
    expect(
      normalizePlacementRequest({
        profile: 'thermal-first',
        policy: 'gemini',
        feedback: { fixed_refs_add: ['C1'] },
      }),
    ).toEqual({
      profile: 'thermal-first',
      policy: 'gemini',
      experimental_placement: false,
      feedback: { fixed_refs_add: ['C1'] },
    })
  })

  it('does not forward caller-selected profile memory ids', () => {
    expect(
      normalizePlacementRequest({ profile_id: 'shared-team' }).profile_id,
    ).toBeUndefined()
  })

  it.each(['fast', 'deterministic', 'gemini', 'ollama', 'tinker', 'hybrid'])(
    'preserves the supported %s policy',
    (policy) => {
      expect(normalizePlacementRequest({ policy }).policy).toBe(policy)
    },
  )

  it('falls back when the placement policy is unknown', () => {
    expect(normalizePlacementRequest({ policy: 'invented' }).policy).toBe('deterministic')
  })
})

describe('requestBytes', () => {
  it('measures the serialised request', () => {
    expect(requestBytes({ intent: 'ab' })).toBe(JSON.stringify({ intent: 'ab' }).length)
  })

  it('counts utf-8 bytes, not characters, so a multibyte intent is not undercounted', () => {
    const request = { intent: 'µ' }

    // The micro sign is one character but two bytes, so the byte count runs ahead.
    expect(JSON.stringify(request)).toHaveLength(14)
    expect(requestBytes(request)).toBe(15)
  })
})

describe('generate: the request it sends', () => {
  it('posts JSON to the same-origin generate endpoint under an abort signal', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))
    const request = { intent: 'a 3v3 regulator', time_limit_s: 20 }

    await generate(request)

    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/generate')
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual({ 'content-type': 'application/json' })
    expect(init.body).toBe(JSON.stringify(request))
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(init.signal.aborted).toBe(false)
  })

  it('sends the request as given, leaving normalisation to the caller', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))

    await generate({ time_limit_s: 9999 })

    expect(JSON.parse(fetch.mock.calls[0][1].body).time_limit_s).toBe(9999)
  })
})

describe('generate: the 1 MiB client-side guard', () => {
  it('rejects an oversized request without troubling the network', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES) }

    await expect(generate(request)).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'too-large',
      status: 0,
    })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('names the actual and the permitted size, so the user can tell how far over they are', async () => {
    stubFetch(jsonResponse(200, OK_BODY))
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES) }
    const bytes = requestBytes(request)

    await expect(generate(request)).rejects.toThrow(
      `The request is ${bytes} bytes; the service accepts at most ${MAX_REQUEST_BYTES}.`,
    )
  })

  it('lets a request that is exactly at the limit through', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))
    const overhead = requestBytes({ intent: '' })
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES - overhead) }
    expect(requestBytes(request)).toBe(MAX_REQUEST_BYTES)

    await generate(request)

    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

describe('generate: a successful response', () => {
  it('returns the body with the fields the UI indexes into', async () => {
    stubFetch(
      jsonResponse(200, {
        status: 'feasible',
        board_mm: [20.5, 30.5],
        parts: [{ ref: 'U1' }, { ref: 'C1' }],
        duration_s: 12.5,
        repair_rounds: 2,
      }),
    )

    const result = await generate({ intent: 'x' })

    expect(result.status).toBe('feasible')
    expect(result.board_mm).toEqual([20.5, 30.5])
    expect(result.parts).toHaveLength(2)
    expect(result.duration_s).toBe(12.5)
    expect(result.repair_rounds).toBe(2)
  })

  it('substitutes an empty array for every list the response omits', async () => {
    stubFetch(jsonResponse(200, { status: 'feasible' }))

    const result = await generate({ intent: 'x' })

    expect(result.board_mm).toEqual([])
    expect(result.parts).toEqual([])
    expect(result.blockers).toEqual([])
    expect(result.warnings).toEqual([])
    expect(result.nets).toEqual([])
    expect(result.datasheets).toEqual([])
    expect(result.findings).toEqual([])
  })

  it('replaces a non-array list with an empty array rather than letting .length throw', async () => {
    stubFetch(jsonResponse(200, { parts: 'eleven', warnings: null, nets: { a: 1 } }))

    const result = await generate({ intent: 'x' })

    expect(result.parts).toEqual([])
    expect(result.warnings).toEqual([])
    expect(result.nets).toEqual([])
  })

  it('defaults the two numeric fields to zero when absent', async () => {
    stubFetch(jsonResponse(200, {}))

    const result = await generate({ intent: 'x' })

    expect(result.repair_rounds).toBe(0)
    expect(result.duration_s).toBe(0)
  })

  it('preserves fields it does not know about', async () => {
    stubFetch(jsonResponse(200, { pcb_path: '/tmp/out.kicad_pcb' }))

    const result = await generate({ intent: 'x' })

    expect(result.pcb_path).toBe('/tmp/out.kicad_pcb')
  })

  it('clears the timeout timer once the response lands', async () => {
    vi.useFakeTimers()
    stubFetch(jsonResponse(200, OK_BODY))

    await generate({ intent: 'x' })

    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('generate: findings synthesized from blockers', () => {
  it('passes a real findings array through untouched', async () => {
    const findings = [{ severity: 'blocker', title: 'VDD tied to ground', detail: 'd' }]
    stubFetch(jsonResponse(200, { findings, blockers: ['ignored'] }))

    const result = await generate({ intent: 'x' })

    expect(result.findings).toEqual(findings)
  })

  it('prefers an empty findings array over synthesizing from blockers', async () => {
    stubFetch(jsonResponse(200, { findings: [], blockers: ['a blocker'] }))

    const result = await generate({ intent: 'x' })

    expect(result.findings).toEqual([])
  })

  it('builds a blocker-severity card per blocker string when findings are absent', async () => {
    stubFetch(jsonResponse(200, { blockers: ['VDD tied to ground', 'no decoupling on U1'] }))

    const result = await generate({ intent: 'x' })

    expect(result.findings).toEqual([
      {
        severity: 'blocker',
        title: 'VDD tied to ground',
        detail: '',
        parts: [],
        citation: '',
        suggested_fix: '',
      },
      {
        severity: 'blocker',
        title: 'no decoupling on U1',
        detail: '',
        parts: [],
        citation: '',
        suggested_fix: '',
      },
    ])
  })

  it('leaves the flattened blockers in place alongside the synthesized cards', async () => {
    stubFetch(jsonResponse(200, { blockers: ['VDD tied to ground'] }))

    const result = await generate({ intent: 'x' })

    expect(result.blockers).toEqual(['VDD tied to ground'])
  })

  it('gives every synthesized card the empty part list a finding card expects', async () => {
    stubFetch(jsonResponse(200, { blockers: ['one'] }))

    const [finding] = (await generate({ intent: 'x' })).findings

    expect(finding.parts).toEqual([])
    expect(finding.severity).toBe('blocker')
  })

  it('stringifies a non-string blocker rather than rendering an object', async () => {
    stubFetch(jsonResponse(200, { blockers: [42] }))

    const [finding] = (await generate({ intent: 'x' })).findings

    expect(finding.title).toBe('42')
  })

  it('synthesizes nothing when neither findings nor blockers are present', async () => {
    stubFetch(jsonResponse(200, { status: 'feasible' }))

    expect((await generate({ intent: 'x' })).findings).toEqual([])
  })

  it('synthesizes nothing when findings is absent and blockers is not an array', async () => {
    stubFetch(jsonResponse(200, { blockers: 'one big problem' }))

    expect((await generate({ intent: 'x' })).findings).toEqual([])
  })
})

describe('generate: ApiError kind classification', () => {
  it('classifies 400 as a validation failure', async () => {
    stubFetch(jsonResponse(400, { error: 'intent must be a non-empty string' }))

    await expect(generate({ intent: '' })).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'validation',
      status: 400,
      message: 'intent must be a non-empty string',
      errorId: '',
    })
  })

  it('classifies 404 as not-found', async () => {
    stubFetch(jsonResponse(404, { error: 'not found' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'not-found',
      status: 404,
    })
  })

  it('classifies 413 as too-large, matching the client-side guard', async () => {
    stubFetch(jsonResponse(413, { error: 'request body too large' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'too-large',
      status: 413,
    })
  })

  it('classifies a 502 that names GOOGLE_API_KEY as configuration, not an outage', async () => {
    stubFetch(jsonResponse(502, { error: 'GOOGLE_API_KEY is not set' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'no-api-key',
      status: 502,
      message: 'GOOGLE_API_KEY is not set',
    })
  })

  it('spots GOOGLE_API_KEY anywhere in the 502 message, not only at the start', async () => {
    stubFetch(jsonResponse(502, { error: 'upstream call failed: GOOGLE_API_KEY missing in env' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({ kind: 'no-api-key' })
  })

  it('classifies any other 502 as an upstream failure', async () => {
    stubFetch(jsonResponse(502, { error: 'gemini returned 429' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'upstream',
      status: 502,
    })
  })

  it('classifies 500 as internal and carries the error id for the panel footer', async () => {
    stubFetch(jsonResponse(500, { error: 'unhandled', error_id: 'a1b2c3d4' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'internal',
      status: 500,
      errorId: 'a1b2c3d4',
    })
  })

  it('leaves the error id empty when a 500 does not supply one', async () => {
    stubFetch(jsonResponse(500, { error: 'unhandled' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({ kind: 'internal', errorId: '' })
  })

  it('classifies other 5xx statuses as internal too', async () => {
    stubFetch(jsonResponse(503, { error: 'service unavailable', error_id: 'z9' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'internal',
      status: 503,
      errorId: 'z9',
    })
  })

  it('falls back to upstream for an unhandled 4xx', async () => {
    stubFetch(jsonResponse(429, { error: 'slow down' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'upstream',
      status: 429,
    })
  })

  it('writes a message from the status when the body carries no error field', async () => {
    stubFetch(jsonResponse(400, {}))

    await expect(generate({ intent: 'x' })).rejects.toThrow('request failed with status 400')
  })

  it('still classifies a failure whose body is not JSON at all', async () => {
    stubFetch(unparseableResponse(502))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'upstream',
      status: 502,
      message: 'request failed with status 502',
    })
  })

  it('reports a 200 whose body is not JSON as internal', async () => {
    stubFetch(unparseableResponse(200))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'internal',
      status: 200,
      message: 'The service returned a body that is not JSON.',
    })
  })

  it('throws an ApiError instance, which App.svelte checks with instanceof', async () => {
    stubFetch(jsonResponse(400, { error: 'bad' }))

    await expect(generate({ intent: 'x' })).rejects.toBeInstanceOf(ApiError)
  })
})

describe('generate: transport failures', () => {
  it('classifies the TypeError a dead connection produces as a network failure', async () => {
    stubFetchRejecting(new TypeError('Failed to fetch'))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'network',
      status: 0,
      message: 'Failed to fetch',
    })
  })

  it('stringifies a thrown non-error rather than reporting "undefined"', async () => {
    stubFetchRejecting('connection reset')

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'network',
      message: 'connection reset',
    })
  })

  it('classifies the abort its own budget timer fires as a timeout', async () => {
    vi.useFakeTimers()
    const fetch = vi.fn(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => {
            reject(new DOMException('This operation was aborted', 'AbortError'))
          })
        }),
    )
    vi.stubGlobal('fetch', fetch)

    const pending = generate({ intent: 'x' })
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'timeout',
      status: 0,
      message: 'The run passed the 300 second budget and was cancelled.',
    })

    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS)

    await assertion
  })

  it('does not abort a request that is still inside the budget', async () => {
    vi.useFakeTimers()
    let signal
    const fetch = vi.fn((_url, init) => {
      signal = init.signal
      return new Promise(() => {})
    })
    vi.stubGlobal('fetch', fetch)

    generate({ intent: 'x' })
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS - 1)

    expect(signal.aborted).toBe(false)
  })

  it('does not install the client timer when no solver budget was requested', async () => {
    vi.useFakeTimers()
    let signal
    vi.stubGlobal(
      'fetch',
      vi.fn((_url, init) => {
        signal = init.signal
        return new Promise(() => {})
      }),
    )

    generate({ intent: 'x', no_solver_budget: true })
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS * 2)

    expect(signal.aborted).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('clears the budget timer after a transport failure', async () => {
    vi.useFakeTimers()
    stubFetchRejecting(new TypeError('Failed to fetch'))

    await expect(generate({ intent: 'x' })).rejects.toThrow()

    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('generate: what it writes to the debug log', () => {
  it('records a response that arrived and parsed', async () => {
    stubFetch(jsonResponse(200, OK_BODY))

    await generate({ intent: 'x' })

    const [entry] = await recorded('api.response')
    expect(entry).toMatchObject({ level: 'info', src: 'app' })
    expect(entry.data).toMatchObject({ status: 200, ok: true, parsed: true })
    expect(typeof entry.data.ms).toBe('number')
    expect(entry.data.ms).toBeGreaterThanOrEqual(0)
  })

  it('records the response before throwing, so a failed run still says what came back', async () => {
    stubFetch(jsonResponse(500, { error: 'unhandled', error_id: 'a1b2c3d4' }))

    await expect(generate({ intent: 'x' })).rejects.toThrow()

    const [entry] = await recorded('api.response')
    expect(entry.level).toBe('warn')
    expect(entry.data).toMatchObject({ status: 500, ok: false, parsed: true })
  })

  it('reports a body that would not parse, which is otherwise invisible', async () => {
    stubFetch(unparseableResponse(502))

    await expect(generate({ intent: 'x' })).rejects.toThrow()

    const [entry] = await recorded('api.response')
    expect(entry.data).toMatchObject({ status: 502, ok: false, parsed: false })
  })

  it('reports a 200 whose body would not parse, on the path that throws first', async () => {
    stubFetch(unparseableResponse(200))

    await expect(generate({ intent: 'x' })).rejects.toThrow()

    const [entry] = await recorded('api.response')
    // A 200 the client could not read is a failed run, whatever the status said.
    expect(entry.level).toBe('warn')
    expect(entry.data).toMatchObject({ status: 200, ok: true, parsed: false })
    expect(typeof entry.data.ms).toBe('number')
  })

  it('records a transport failure that no abort of ours caused', async () => {
    stubFetchRejecting(new TypeError('Failed to fetch'))

    await expect(generate({ intent: 'x' })).rejects.toThrow()

    const [entry] = await recorded('api.failed')
    expect(entry.level).toBe('error')
    expect(entry.data).toMatchObject({ aborted: false })
    expect(typeof entry.data.ms).toBe('number')
    expect(await recorded('api.response')).toEqual([])
  })

  it('records the budget timer firing as an abort, which is a different bug', async () => {
    vi.useFakeTimers()
    const fetch = vi.fn(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => {
            reject(new DOMException('This operation was aborted', 'AbortError'))
          })
        }),
    )
    vi.stubGlobal('fetch', fetch)

    const pending = generate({ intent: 'x' })
    const assertion = expect(pending).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS)
    await assertion

    const [entry] = await recorded('api.failed')
    expect(entry.data.aborted).toBe(true)
    expect(typeof entry.data.ms).toBe('number')
  })

  it('records the pre-flight refusal, with no response to report', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES) }

    await expect(generate(request)).rejects.toThrow()

    const [entry] = await recorded('api.too-large')
    expect(entry.level).toBe('warn')
    expect(entry.data).toEqual({ bytes: requestBytes(request), limit: MAX_REQUEST_BYTES })
    expect(fetch).not.toHaveBeenCalled()
    expect(await recorded('api.response')).toEqual([])
  })
})

describe('generateStream: the happy stream', () => {
  it('posts to the streaming endpoint under the same budget and shaping', async () => {
    const fetch = stubFetch(streamResponse(happyFrames()))
    const request = { intent: 'a 3v3 regulator', time_limit_s: 20 }

    await generateStream(request, () => {})

    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/generate/stream')
    expect(init.method).toBe('POST')
    expect(init.headers['content-type']).toBe('application/json')
    expect(init.body).toBe(JSON.stringify({ ...request, debug: true }))
    expect(init.signal).toBeInstanceOf(AbortSignal)
  })

  it('asks the service for the raw model responses, which only this route gets', async () => {
    const fetch = stubFetch(streamResponse(happyFrames()))

    await generateStream({ intent: 'x' }, () => {})

    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ intent: 'x', debug: true })
  })

  it('leaves the one-shot body alone, since nothing there could show the text', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))

    await generate({ intent: 'x' })

    const body = JSON.parse(fetch.mock.calls[0][1].body)
    expect(body).toEqual({ intent: 'x' })
    expect(body.debug).toBeUndefined()
  })

  it('hands every event to the listener, in order, reassembling a split frame', async () => {
    stubFetch(streamResponse(happyFrames()))
    const seen = []

    await generateStream({ intent: 'x' }, (evt) => seen.push(evt))

    expect(seen.map((evt) => evt.event)).toEqual([
      'run.accepted',
      'stage.start',
      'stage.done',
      'run.done',
    ])
    expect(seen[1]).toEqual({ event: 'stage.start', stage: 'place', t_s: 0.2, time_limit_s: 20 })
  })

  it('resolves with exactly what the one-shot path would have returned', async () => {
    stubFetch(jsonResponse(200, OK_BODY))
    const oneShot = await generate({ intent: 'x' })

    stubFetch(streamResponse(happyFrames()))
    const streamed = await generateStream({ intent: 'x' }, () => {})

    expect(streamed).toEqual(oneShot)
  })

  it('reads a last frame that arrives without a trailing newline', async () => {
    stubFetch(streamResponse(['{"event":"run.done","result":{"status":"feasible"}}']))

    expect(await generateStream({ intent: 'x' }, () => {})).toMatchObject({ status: 'feasible' })
  })

  it('keeps reading when a listener throws, since the run is still arriving', async () => {
    stubFetch(streamResponse(happyFrames()))
    const seen = []

    const result = await generateStream({ intent: 'x' }, (evt) => {
      seen.push(evt.event)
      if (evt.event === 'run.accepted') throw new Error('a bug in the feed')
    })

    expect(seen).toHaveLength(4)
    expect(result).toMatchObject({ status: 'feasible' })
  })

  it('clears the budget timer once the stream finishes', async () => {
    vi.useFakeTimers()
    stubFetch(streamResponse(happyFrames()))

    await generateStream({ intent: 'x' }, () => {})

    expect(vi.getTimerCount()).toBe(0)
  })

  it('refuses an oversized request before the network, exactly as generate does', async () => {
    const fetch = stubFetch(streamResponse(happyFrames()))
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES) }

    await expect(generateStream(request, () => {})).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'too-large',
    })
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe('generateStream: falling back to the one-shot endpoint', () => {
  it('delegates on a 404, which is a service built before the endpoint existed', async () => {
    const fetch = stubFetchByPath({
      '/generate/stream': jsonResponse(404, { error: 'not found' }),
      '/generate': jsonResponse(200, OK_BODY),
    })

    const result = await generateStream({ intent: 'x' }, () => {})

    expect(result).toMatchObject({ status: 'feasible' })
    expect(fetch.mock.calls.map((call) => call[0])).toEqual(['/generate/stream', '/generate'])
    const [entry] = await recorded('api.stream-fallback')
    expect(entry).toMatchObject({ level: 'info', src: 'app' })
    expect(entry.data).toMatchObject({ status: 404 })
  })

  // The property these two pin is not the error kind, it is the request count:
  // the service sends its 200 before the pipeline starts, so a second request
  // after one is a second paid run. urllib3 keeps POST out of
  // Retry.DEFAULT_ALLOWED_METHODS for this reason, and neither openai-python's
  // nor stripe-python's _should_retry retries any 2xx.
  it('does not send a second request when a 200 arrives as something other than NDJSON', async () => {
    const fetch = stubFetchByPath({
      '/generate/stream': streamResponse(happyFrames(), { type: 'text/html' }),
      '/generate': jsonResponse(200, OK_BODY),
    })

    await expect(generateStream({ intent: 'x' }, () => {})).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'internal',
      status: 200,
    })

    expect(fetch.mock.calls.map((call) => call[0])).toEqual(['/generate/stream'])
    expect(await recorded('api.stream-fallback')).toHaveLength(0)
    expect((await recorded('api.stream-unreadable'))[0].data).toMatchObject({
      content_type: 'text/html',
    })
  })

  it('does not send a second request when the response carries no readable body', async () => {
    const noBody = { ...streamResponse([]), body: null }
    const fetch = stubFetchByPath({
      '/generate/stream': noBody,
      '/generate': jsonResponse(200, OK_BODY),
    })

    await expect(generateStream({ intent: 'x' }, () => {})).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'internal',
    })

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(await recorded('api.stream-fallback')).toHaveLength(0)
    expect(await recorded('api.stream-unreadable')).toHaveLength(1)
  })

  it('does not fall back on a pre-stream 400, which is an answer about this request', async () => {
    const fetch = stubFetchByPath({
      '/generate/stream': jsonResponse(400, { error: 'intent must be a non-empty string' }),
      '/generate': jsonResponse(200, OK_BODY),
    })

    await expect(generateStream({ intent: '' }, () => {})).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'validation',
      status: 400,
      message: 'intent must be a non-empty string',
    })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(await recorded('api.stream-fallback')).toHaveLength(0)
  })

  it('classifies a pre-stream 413 the way the one-shot path does', async () => {
    stubFetch(jsonResponse(413, { error: 'request body too large' }))

    await expect(generateStream({ intent: 'x' }, () => {})).rejects.toMatchObject({
      kind: 'too-large',
      status: 413,
    })
  })
})

describe('generateStream: a run that fails inside the stream', () => {
  it('throws the same error a 502 naming GOOGLE_API_KEY would have thrown', async () => {
    const fetch = stubFetchByPath({
      '/generate/stream': streamResponse([
        '{"event":"run.accepted","t_s":0.0}\n',
        '{"event":"run.error","status":502,"error":"GOOGLE_API_KEY is not set"}\n',
      ]),
      '/generate': jsonResponse(200, OK_BODY),
    })

    await expect(generateStream({ intent: 'x' }, () => {})).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'no-api-key',
      status: 502,
      message: 'GOOGLE_API_KEY is not set',
    })
    // The pipeline already ran and failed; running it again would cost a second
    // set of model calls to reach the same answer.
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('carries the error id off a 500 frame, the way the error panel needs it', async () => {
    stubFetch(
      streamResponse([
        '{"event":"run.error","status":500,"error":"unhandled","error_id":"a1b2c3d4"}\n',
      ]),
    )

    await expect(generateStream({ intent: 'x' }, () => {})).rejects.toMatchObject({
      kind: 'internal',
      status: 500,
      errorId: 'a1b2c3d4',
    })
  })

  it('shows the failure to the listener before throwing it', async () => {
    stubFetch(streamResponse(['{"event":"run.error","status":400,"error":"bad intent"}\n']))
    const seen = []

    await expect(generateStream({ intent: 'x' }, (evt) => seen.push(evt.event))).rejects.toThrow()

    expect(seen).toEqual(['run.error'])
  })
})

describe('generateStream: a stream that stops mid-run', () => {
  it('reports a connection that died after frames as a network failure, and never reruns', async () => {
    const fetch = stubFetchByPath({
      '/generate/stream': streamResponse(['{"event":"run.accepted","t_s":0.0}\n'], { cut: true }),
      '/generate': jsonResponse(200, OK_BODY),
    })
    const seen = []

    await expect(generateStream({ intent: 'x' }, (evt) => seen.push(evt.event))).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'network',
      message: 'network error',
    })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(seen).toEqual(['run.accepted'])
  })

  it('reports a clean close that never said how the run ended', async () => {
    const fetch = stubFetchByPath({
      '/generate/stream': streamResponse([
        '{"event":"run.accepted","t_s":0.0}\n{"event":"stage.start","stage":"place","t_s":0.1}\n',
      ]),
      '/generate': jsonResponse(200, OK_BODY),
    })

    // The message now names the run, because the run is still going: telling
    // the user it stopped without saying what to ask about was the §6 gap.
    const error = await generateStream({ intent: 'x' }, () => {}, 'run_mine').catch((e) => e)
    expect(error.kind).toBe('network')
    expect(error.message).toMatch(/The stream closed before the run finished\./)
    expect(error.message).toMatch(/run_mine/)
    expect(error.runId).toBe('run_mine')
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('classifies a stream its own budget timer aborted as a timeout', async () => {
    vi.useFakeTimers()
    let signal = null
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url, init) => {
        signal = init.signal
        return {
          ok: true,
          status: 200,
          headers: { get: () => 'application/x-ndjson' },
          body: {
            getReader: () => ({
              read: () =>
                new Promise((_resolve, reject) => {
                  signal.addEventListener('abort', () => reject(new Error('aborted')))
                }),
              cancel: async () => {},
            }),
          },
        }
      }),
    )

    const settled = generateStream({ intent: 'x' }, () => {})
    await Promise.resolve()
    vi.advanceTimersByTime(REQUEST_TIMEOUT_MS)

    await expect(settled).rejects.toMatchObject({ kind: 'timeout' })
  })
})

describe('generateStream: what it writes to the debug log', () => {
  it('records the stream opening', async () => {
    stubFetch(streamResponse(happyFrames()))

    await generateStream({ intent: 'x' }, () => {})

    const [entry] = await recorded('api.stream')
    expect(entry).toMatchObject({ level: 'info', src: 'app' })
    expect(entry.data).toMatchObject({ status: 200, content_type: 'application/x-ndjson' })
  })

  it('records the same api.response line the one-shot path leaves behind', async () => {
    stubFetch(streamResponse(happyFrames()))

    await generateStream({ intent: 'x' }, () => {})

    const [entry] = await recorded('api.response')
    expect(entry.level).toBe('info')
    expect(entry.data).toMatchObject({
      status: 200,
      ok: true,
      parsed: true,
      stream: true,
      events: 4,
    })
  })

  it('records a run that failed inside the stream under the status it carried', async () => {
    stubFetch(
      streamResponse(['{"event":"run.error","status":502,"error":"gemini returned 429"}\n']),
    )

    await expect(generateStream({ intent: 'x' }, () => {})).rejects.toThrow()

    const [entry] = await recorded('api.response')
    expect(entry.level).toBe('warn')
    expect(entry.data).toMatchObject({ status: 502, ok: false, stream: true, events: 1 })
  })

  it('records a stream that stopped, with how many events had arrived', async () => {
    stubFetch(streamResponse(['{"event":"run.accepted","t_s":0.0}\n'], { cut: true }))

    await expect(generateStream({ intent: 'x' }, () => {})).rejects.toThrow()

    const [entry] = await recorded('api.failed')
    expect(entry.level).toBe('error')
    expect(entry.data).toMatchObject({ events: 1, aborted: false })
  })
})

describe('chatStream', () => {
  it('returns the orchestrator outcome and forwards every frame', async () => {
    const events = []
    const fetch = stubFetch(streamResponse(happyChatFrames()))

    const outcome = await chatStream(
      { intent: 'a regulator', session_id: 's1', model: 'gemini-3.1-pro-preview', thinking_level: 'high', quota_rpm: '6' },
      (event) => events.push(event),
    )

    expect(fetch).toHaveBeenCalledWith(
      '/chat/stream',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({
      intent: 'a regulator',
      session_id: 's1',
      model: 'gemini-3.1-pro-preview',
      thinking_level: 'high',
      quota_rpm: '6',
      debug: true,
    })
    expect(events.map((event) => event.event)).toEqual([
      'chat.accepted',
      'assistant.message',
      'chat.done',
    ])
    expect(outcome).toMatchObject({
      assistant: 'Ready.',
      needsClarification: false,
      model: 'gemini-auto',
      thinkingLevel: 'high',
      quotaRpm: '6',
      result: { status: 'feasible', parts: [{ ref: 'U1' }] },
    })
  })

  it('returns a clarification without inventing a board result', async () => {
    stubFetch(
      streamResponse([
        '{"event":"assistant.message","text":"Which input voltage?","needs_clarification":true}\n',
        '{"event":"chat.done","assistant":"Which input voltage?","needs_clarification":true,"model":"gemini-auto","result":null}\n',
      ]),
    )

    await expect(chatStream({ intent: 'a regulator' }, () => {})).resolves.toEqual({
      assistant: 'Which input voltage?',
      needsClarification: true,
      model: 'gemini-auto',
      thinkingLevel: 'auto',
      quotaRpm: 'auto',
      result: null,
    })
  })

  it('never falls back to another paid endpoint when its stream is malformed', async () => {
    const fetch = stubFetch(streamResponse([], { type: 'text/html' }))

    await expect(chatStream({ intent: 'x' }, () => {})).rejects.toMatchObject({
      kind: 'internal',
    })
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

describe('listModels', () => {
  it('normalizes the server-filtered model catalog', async () => {
    stubFetch(
      jsonResponse(200, {
        default: 'auto',
        auto_model: 'gemini-auto',
        source: 'gemini',
        models: [
          {
            id: 'gemini-debug',
            name: 'Gemini Debug',
            input_token_limit: 1000000,
            thinking: true,
          },
        ],
      }),
    )

    await expect(listModels()).resolves.toEqual({
      default: 'auto',
      auto_model: 'gemini-auto',
      source: 'gemini',
      warning: '',
      models: [
        {
          id: 'gemini-debug',
          name: 'Gemini Debug',
          description: '',
          input_token_limit: 1000000,
          output_token_limit: null,
          thinking: true,
        },
      ],
      placement: {
        experimental_enabled: false,
        profiles: [],
        policies: {},
      },
    })
  })
})

describe('getConfigurationStatus', () => {
  it('normalizes the secret-safe backend readiness response', async () => {
    const fetch = stubFetch(
      jsonResponse(200, {
        version: 1,
        dotenv: {
          present: true,
          state: 'restart',
          summary: 'Restart to apply one setting.',
          reload_required: true,
          changed_since_start: true,
          pending: ['GOOGLE_API_KEY'],
        },
        features: [
          {
            id: 'gemini',
            label: 'Gemini agents',
            state: 'ready',
            summary: 'API access verified.',
            variables: ['GOOGLE_API_KEY'],
          },
        ],
      }),
    )

    await expect(getConfigurationStatus()).resolves.toEqual({
      version: 1,
      dotenv: {
        present: true,
        state: 'restart',
        summary: 'Restart to apply one setting.',
        reload_required: true,
        changed_since_start: true,
        pending: ['GOOGLE_API_KEY'],
      },
      features: [
        {
          id: 'gemini',
          label: 'Gemini agents',
          state: 'ready',
          summary: 'API access verified.',
          variables: ['GOOGLE_API_KEY'],
        },
      ],
    })
    expect(fetch).toHaveBeenCalledWith('/config/status', {
      headers: { accept: 'application/json' },
      cache: 'no-store',
    })
  })

  it('downgrades unknown states instead of trusting server markup', async () => {
    stubFetch(
      jsonResponse(200, {
        dotenv: { state: 'surprise' },
        features: [{ id: 'x', label: 'X', state: '<script>' }],
      }),
    )

    const status = await getConfigurationStatus()
    expect(status.dotenv.state).toBe('warning')
    expect(status.features[0].state).toBe('warning')
  })
})


// ------------------------------------------------------- an addressable run
//
// docs/paid-run-safety.md §6: a run started by POST /generate/stream used to
// be unnameable, so a client that lost the stream could not rejoin it, poll it
// or cancel it. §1 stopped the double spend and left the single spend
// invisible. These are the tests that it is no longer invisible.

describe('an addressable run', () => {
  beforeEach(() => {
    clearLog()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends the run id it chose, before the service has answered anything', async () => {
    const fetch = stubFetchByPath({ '/generate/stream': streamResponse(happyFrames()) })
    await generateStream({ intent: 'x' }, () => {}, 'run_chosen')
    const [, init] = fetch.mock.calls[0]
    expect(init.headers['x-kaleo-run-id']).toBe('run_chosen')
  })

  it('carries the run id out on a 200 whose body it cannot read', async () => {
    // The §1 case: the run is under way and must not be sent again. Until the
    // id existed there was nothing else to say about it either.
    const fetch = stubFetchByPath({
      '/generate/stream': streamResponse(happyFrames(), { type: 'text/html' }),
      '/generate': jsonResponse(200, OK_BODY),
    })
    const error = await generateStream({ intent: 'x' }, () => {}, 'run_mine').catch((e) => e)
    expect(error.runId).toBe('run_mine')
    expect(error.message).toMatch(/run_mine/)
    // Still exactly one request: naming the run is not a licence to repeat it.
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it("prefers the service's own run id over the one this client proposed", async () => {
    // An older service, or a proxy that dropped the request header, minted its
    // own; polling ours would ask about a run that does not exist.
    const response = streamResponse(happyFrames(), { type: 'text/html' })
    response.headers = {
      get: (name) =>
        String(name).toLowerCase() === 'x-kaleo-run-id' ? 'run_theirs' : 'text/html',
    }
    stubFetchByPath({ '/generate/stream': response })
    const error = await generateStream({ intent: 'x' }, () => {}, 'run_ours').catch((e) => e)
    expect(error.runId).toBe('run_theirs')
  })

  it('polls a run by id and reads a 404 as "forgotten", not as an error', async () => {
    stubFetchByPath({ '/runs/run_a': jsonResponse(200, { run_id: 'run_a', state: 'done' }) })
    expect(await runStatus('run_a')).toMatchObject({ state: 'done' })

    stubFetchByPath({ '/runs/run_b': jsonResponse(404, { error: 'no run' }) })
    expect(await runStatus('run_b')).toBeNull()
  })

  it('cancels a run by id and hands back the answer verbatim', async () => {
    const answer = {
      cancelled: true,
      in_flight: true,
      aborts_at: 'the next event the pipeline reports',
      not_stoppable: 'A model call already under way runs to completion.',
    }
    const fetch = stubFetchByPath({ '/runs/run_c/cancel': jsonResponse(200, answer) })
    expect(await cancelRun('run_c')).toEqual(answer)
    expect(fetch.mock.calls[0][1].method).toBe('POST')
  })

  it('mints a distinct run id per call', () => {
    expect(newRunId()).not.toBe(newRunId())
    expect(newRunId().startsWith('run_')).toBe(true)
  })
})

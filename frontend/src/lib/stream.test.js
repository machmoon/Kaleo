import { describe, expect, it } from 'vitest'
import { describeStageEvent, parseNdjson } from './stream.js'

// Spelled as codepoints, like format.test.js, so a silent substitution with an
// ASCII "..." or a letter x fails here rather than on screen.
const ELLIPSIS = '…'
const TIMES = '×'

/** Feeds chunks through the parser the way a reader loop does. */
function drain(chunks) {
  let carry = ''
  const events = []
  for (const chunk of chunks) {
    const step = parseNdjson(carry, chunk)
    events.push(...step.events)
    carry = step.carry
  }
  return { events, carry }
}

describe('parseNdjson', () => {
  it('returns the completed events and holds the unfinished tail', () => {
    const step = parseNdjson('', '{"event":"run.accepted"}\n{"event":"run.d')

    expect(step.events).toEqual([{ event: 'run.accepted' }])
    expect(step.carry).toBe('{"event":"run.d')
  })

  it('carries nothing when the chunk ends on a newline', () => {
    expect(parseNdjson('', '{"event":"run.done"}\n').carry).toBe('')
  })

  it('reassembles one object split across three chunks', () => {
    const drained = drain(['{"event":"stage.', 'done","stage":"pl', 'ace","warnings":0}\n'])

    expect(drained.events).toEqual([{ event: 'stage.done', stage: 'place', warnings: 0 }])
    expect(drained.carry).toBe('')
  })

  it('reads several events out of a single chunk', () => {
    const step = parseNdjson('', '{"event":"run.accepted"}\n{"event":"run.done"}\n')

    expect(step.events).toEqual([{ event: 'run.accepted' }, { event: 'run.done' }])
  })

  it('joins the carry to the next chunk before splitting', () => {
    const step = parseNdjson('{"event":"run.', 'done"}\n')

    expect(step.events).toEqual([{ event: 'run.done' }])
  })

  it('skips empty and whitespace-only lines', () => {
    const step = parseNdjson('', '\n\n   \n\t\n{"event":"run.done"}\n\n')

    expect(step.events).toEqual([{ event: 'run.done' }])
  })

  it('tolerates carriage returns, since each line is trimmed before parsing', () => {
    const step = parseNdjson('', '{"event":"run.done"}\r\n')

    expect(step.events).toEqual([{ event: 'run.done' }])
  })

  it('turns an unparseable line into a badframe event carrying the raw line', () => {
    const step = parseNdjson('', '{"event":"run.done"\n')

    expect(step.events).toEqual([{ event: 'client.badframe', raw: '{"event":"run.done"' }])
  })

  it('truncates the raw line to 200 characters', () => {
    const step = parseNdjson('', `${'x'.repeat(500)}\n`)

    expect(step.events[0].event).toBe('client.badframe')
    expect(step.events[0].raw).toBe('x'.repeat(200))
  })

  it('keeps a raw line shorter than the limit whole', () => {
    expect(parseNdjson('', 'nope\n').events[0].raw).toBe('nope')
  })

  it('rejects a line of well-formed JSON that is not an object', () => {
    const step = parseNdjson('', '42\n"text"\nnull\ntrue\n[1,2]\n')

    expect(step.events.map((e) => e.event)).toEqual(Array(5).fill('client.badframe'))
    expect(step.events.map((e) => e.raw)).toEqual(['42', '"text"', 'null', 'true', '[1,2]'])
  })

  it('keeps parsing after a bad line rather than abandoning the stream', () => {
    const step = parseNdjson('', 'nope\n{"event":"run.done"}\n')

    expect(step.events.map((e) => e.event)).toEqual(['client.badframe', 'run.done'])
  })

  it('finishes a carried line when the stream ends, given a trailing newline', () => {
    const held = parseNdjson('', '{"event":"run.done"}')

    expect(held.events).toEqual([])

    const flushed = parseNdjson(held.carry, '\n')

    expect(flushed.events).toEqual([{ event: 'run.done' }])
    expect(flushed.carry).toBe('')
  })

  it('splits a single object arriving one character at a time', () => {
    const drained = drain([...'{"event":"run.done"}\n'])

    expect(drained.events).toEqual([{ event: 'run.done' }])
  })

  it('never throws, whatever the chunk holds', () => {
    for (const chunk of ['', '\n', '{{{\n', ' \n', 'ok', undefined, null, 42]) {
      expect(() => parseNdjson('', chunk)).not.toThrow()
    }

    expect(() => parseNdjson(undefined, '{}\n')).not.toThrow()
    expect(parseNdjson(undefined, '{}\n').events).toEqual([{}])
  })
})

describe('describeStageEvent', () => {
  it('announces an accepted run', () => {
    expect(describeStageEvent({ event: 'run.accepted', t_s: 0 })).toBe(
      'request accepted, pipeline starting',
    )
  })

  it('never spells the timestamp into the sentence, since the feed renders it', () => {
    expect(describeStageEvent({ event: 'run.accepted', t_s: 12.5 })).not.toContain('12.5')
  })

  it('names each stage as it starts', () => {
    expect(describeStageEvent({ event: 'stage.start', stage: 'read' })).toBe(
      `reading datasheets${ELLIPSIS}`,
    )
    expect(describeStageEvent({ event: 'stage.start', stage: 'propose' })).toBe(
      `proposing a circuit${ELLIPSIS}`,
    )
    expect(describeStageEvent({ event: 'stage.start', stage: 'review' })).toBe(
      `adversarial review${ELLIPSIS}`,
    )
  })

  it('quotes the solver budget when the place stage starts', () => {
    expect(describeStageEvent({ event: 'stage.start', stage: 'place', time_limit_s: 20 })).toBe(
      `placing with CP-SAT (20 s solver budget)${ELLIPSIS}`,
    )
  })

  it('distinguishes an unlimited solver from a legacy event with no budget field', () => {
    expect(describeStageEvent({ event: 'stage.start', stage: 'place' })).toBe(
      `placing with CP-SAT${ELLIPSIS}`,
    )
    expect(describeStageEvent({ event: 'stage.start', stage: 'place', time_limit_s: null })).toBe(
      `placing with CP-SAT (no solver budget)${ELLIPSIS}`,
    )
  })

  it('names a stage it has no copy for rather than saying nothing useful', () => {
    expect(describeStageEvent({ event: 'stage.start', stage: 'validate' })).toBe(
      `starting validate${ELLIPSIS}`,
    )
    expect(describeStageEvent({ event: 'stage.done', stage: 'validate' })).toBe('validate finished')
  })

  it('falls back to the generic sentence when the stage is missing entirely', () => {
    expect(describeStageEvent({ event: 'stage.start' })).toBe('pipeline event: stage.start')
  })

  it('reports each datasheet as it is read', () => {
    expect(describeStageEvent({ event: 'read.part', part: 'AMS1117-3.3', index: 2, total: 3 })).toBe(
      `reading AMS1117-3.3 (2 of 3)${ELLIPSIS}`,
    )
  })

  it('says a cached part was not read at all', () => {
    expect(
      describeStageEvent({
        event: 'read.part',
        part: 'AMS1117-3.3',
        index: 2,
        total: 3,
        cached: true,
      }),
    ).toBe('AMS1117-3.3: facts already cached (2 of 3)')
  })

  it('drops the counter when the event carries no position', () => {
    expect(describeStageEvent({ event: 'read.part', part: 'AMS1117-3.3' })).toBe(
      `reading AMS1117-3.3${ELLIPSIS}`,
    )
    expect(describeStageEvent({ event: 'read.part', part: 'AMS1117-3.3', index: 2 })).toBe(
      `reading AMS1117-3.3${ELLIPSIS}`,
    )
  })

  it('reports the datasheet download separately from the read', () => {
    // The download is the one step that waits on someone else's web server, so
    // it gets its own line: without it a slow host is indistinguishable from a
    // hung run.
    expect(
      describeStageEvent({ event: 'read.fetch', part: 'AMS1117-3.3', bytes: 95151 }),
    ).toBe('AMS1117-3.3: downloaded 93 kB')
  })

  it('still describes a download that reports no size', () => {
    expect(describeStageEvent({ event: 'read.fetch', part: 'AMS1117-3.3' })).toBe(
      'AMS1117-3.3: downloaded the PDF',
    )
    expect(describeStageEvent({ event: 'read.fetch', bytes: 2048 })).toBe('downloaded 2 kB')
  })

  it('still reads as a sentence when the part is missing', () => {
    expect(describeStageEvent({ event: 'read.part', part: null, index: 1, total: 2 })).toBe(
      `reading a datasheet (1 of 2)${ELLIPSIS}`,
    )
    expect(describeStageEvent({ event: 'read.part', cached: true })).toBe('facts already cached')
  })

  it('tallies what the read stage found', () => {
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'read', parts: 3, pins: 24, requirements: 7 }),
    ).toBe('datasheets read: 3 parts, 24 pins, 7 requirements')
  })

  it('uses the singular for a one-of-each read', () => {
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'read', parts: 1, pins: 1, requirements: 1 }),
    ).toBe('datasheets read: 1 part, 1 pin, 1 requirement')
  })

  it('counts a missing tally as zero rather than omitting it', () => {
    expect(describeStageEvent({ event: 'stage.done', stage: 'read' })).toBe(
      'datasheets read: 0 parts, 0 pins, 0 requirements',
    )
  })

  it('reports a rejected proposal round with the first error', () => {
    expect(
      describeStageEvent({
        event: 'propose.round',
        round: 2,
        errors: 3,
        first_error: 'C1.1 names no pin on C1',
      }),
    ).toBe('proposal round 2 rejected: 3 validation errors (first: C1.1 names no pin on C1)')
  })

  it('drops the parenthetical when no first error came through', () => {
    expect(describeStageEvent({ event: 'propose.round', round: 1, errors: 1 })).toBe(
      'proposal round 1 rejected: 1 validation error',
    )
  })

  it('truncates a long first error, which is model output and can run to pages', () => {
    expect(
      describeStageEvent({
        event: 'propose.round',
        round: 1,
        errors: 1,
        first_error: 'e'.repeat(400),
      }),
    ).toBe(`proposal round 1 rejected: 1 validation error (first: ${'e'.repeat(160)})`)
  })

  it('reports a proposed circuit', () => {
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'propose',
        parts: 9,
        nets: 12,
        repair_rounds: 0,
      }),
    ).toBe('circuit proposed: 9 parts, 12 nets')
  })

  it('mentions repair rounds only when there were any', () => {
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'propose',
        parts: 9,
        nets: 12,
        repair_rounds: 2,
      }),
    ).toBe('circuit proposed: 9 parts, 12 nets, after 2 repair rounds')
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'propose',
        parts: 9,
        nets: 12,
        repair_rounds: 1,
      }),
    ).toBe('circuit proposed: 9 parts, 12 nets, after 1 repair round')
  })

  it('reports the placement with its status, size, and wirelength', () => {
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'place',
        solver_status: 'FEASIBLE',
        board_mm: [34, 28],
        wirelength_mm: 412,
        warnings: 0,
      }),
    ).toBe(`placed: FEASIBLE, 34.0 ${TIMES} 28.0 mm, 412 mm wire`)
  })

  it('appends the warning count only when the placer warned', () => {
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'place',
        solver_status: 'FALLBACK',
        board_mm: [34, 28],
        wirelength_mm: 412,
        warnings: 2,
      }),
    ).toBe(`placed: FALLBACK, 34.0 ${TIMES} 28.0 mm, 412 mm wire, 2 warnings`)
  })

  it('drops the wire clause when no wirelength was reported', () => {
    for (const wirelength_mm of [null, undefined]) {
      expect(
        describeStageEvent({
          event: 'stage.done',
          stage: 'place',
          solver_status: 'FEASIBLE',
          board_mm: [34, 28],
          wirelength_mm,
        }),
      ).toBe(`placed: FEASIBLE, 34.0 ${TIMES} 28.0 mm`)
    }
  })

  it('still says something when the place event carries no numbers at all', () => {
    expect(describeStageEvent({ event: 'stage.done', stage: 'place' })).toBe('placed')
  })

  it('reports the routing tally, and names what is still ratsnest', () => {
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'route',
        tracks: 28,
        vias: 5,
        routed_nets: 5,
        unrouted_nets: 0,
      }),
    ).toBe('routed: 5/5 nets, 28 tracks, 5 vias')
  })

  it('says how many nets were left unrouted rather than only what worked', () => {
    // A net left as ratsnest is invisible until fabrication, so the count that
    // matters is the one that did not finish.
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'route',
        tracks: 12,
        vias: 0,
        routed_nets: 6,
        unrouted_nets: 44,
      }),
    ).toBe('routed: 6/50 nets, 12 tracks, 44 left unrouted')
  })

  it('describes a starting route stage', () => {
    expect(describeStageEvent({ event: 'stage.start', stage: 'route' })).toBe(
      'routing the copper…',
    )
  })

  it('reports the review tally', () => {
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'review', findings: 2, blockers: 1 }),
    ).toBe('review: 2 findings, 1 blocker')
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'review', findings: 3, blockers: 2 }),
    ).toBe('review: 3 findings, 2 blockers')
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'review', findings: 1, blockers: 0 }),
    ).toBe('review: 1 finding')
  })

  it('says "no findings" rather than "0 findings" for a clean review', () => {
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'review', findings: 0, blockers: 0 }),
    ).toBe('review: no findings')
  })

  it('describes a starting enclosure stage', () => {
    expect(describeStageEvent({ event: 'stage.start', stage: 'enclosure' })).toBe(
      'drawing the printable case…',
    )
  })

  it('summarises a finished enclosure from the frozen stage.done fields', () => {
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'enclosure',
        cutouts: 3,
        lid: 'sliding',
        wall_mm: 2,
        repair_rounds: 1,
        rendered: true,
      }),
    ).toBe('printable case generated: 3 cutouts, sliding lid, 2 mm walls, after 1 repair round, rendered')
  })

  it('drops the enclosure clauses that were not reported rather than inventing zeros', () => {
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'enclosure', cutouts: 1, repair_rounds: 0 }),
    ).toBe('printable case generated: 1 cutout')
  })

  it('reports an enclosure repair round with its error tally', () => {
    expect(
      describeStageEvent({
        event: 'enclosure.round',
        round: 2,
        errors: 3,
        first_error: 'cutout off the board edge',
      }),
    ).toBe('case proposal round 2 rejected: 3 validation errors (first: cutout off the board edge)')
    expect(describeStageEvent({ event: 'enclosure.round', round: 1, errors: 1 })).toBe(
      'case proposal round 1 rejected: 1 validation error',
    )
  })

  it('describes an enclosure failure with its error and the delivered board', () => {
    expect(
      describeStageEvent({ event: 'enclosure.failed', error: 'repair budget exhausted' }),
    ).toBe('case generation failed: repair budget exhausted — the board is still delivered')
    expect(describeStageEvent({ event: 'enclosure.failed' })).toBe(
      'case generation failed — the board is still delivered',
    )
  })

  it('describes a starting sourcing stage as a lookup plus probes, not a purchase', () => {
    expect(describeStageEvent({ event: 'stage.start', stage: 'sourcing' })).toBe(
      'looking up parts and probing datasheets…',
    )
  })

  it('summarises finished sourcing from the frozen counts, saying "proposed" and never "found"', () => {
    expect(
      describeStageEvent({
        event: 'stage.done',
        stage: 'sourcing',
        verified: 4,
        proposed: 5,
        unresolved: 2,
      }),
    ).toBe('parts sourced: 5/7 parts proposed, 4 datasheets verified, 2 unresolved')
    expect(
      describeStageEvent({ event: 'stage.done', stage: 'sourcing', verified: 1, proposed: 1, unresolved: 0 }),
    ).toBe('parts sourced: 1/1 parts proposed, 1 datasheet verified')
  })

  it('reports each sourced part with both statuses in plain words', () => {
    expect(
      describeStageEvent({
        event: 'sourcing.part',
        ref: 'U1',
        mpn_status: 'proposed',
        datasheet_status: 'verified',
      }),
    ).toBe('U1: MPN proposed, datasheet verified')
    expect(
      describeStageEvent({ event: 'sourcing.part', ref: 'C1', mpn_status: 'none', datasheet_status: 'none' }),
    ).toBe('C1: no MPN, no datasheet')
    expect(
      describeStageEvent({ event: 'sourcing.part', ref: 'D1', mpn_status: 'proposed', datasheet_status: 'not_pdf' }),
    ).toBe('D1: MPN proposed, datasheet is not a PDF')
    expect(
      describeStageEvent({ event: 'sourcing.part', ref: 'Y1', mpn_status: 'proposed', datasheet_status: 'unreachable' }),
    ).toBe('Y1: MPN proposed, datasheet unreachable')
    expect(describeStageEvent({ event: 'sourcing.part', ref: 'R1', datasheet_status: 'toString' })).toBe(
      'R1: no MPN, no datasheet',
    )
  })

  it('describes a sourcing failure with its error and the delivered board', () => {
    expect(describeStageEvent({ event: 'sourcing.failed', error: 'repair gave up' })).toBe(
      'sourcing failed: repair gave up — the board is still delivered, unsourced',
    )
    expect(describeStageEvent({ event: 'sourcing.failed' })).toBe(
      'sourcing failed — the board is still delivered, unsourced',
    )
  })

  it('reports a model call that answered', () => {
    expect(
      describeStageEvent({
        event: 'model.call',
        stage: 'propose',
        provider: 'gemini',
        model: 'gemini-3.7-flash',
        elapsed_s: 4.24,
        ok: true,
        chars: 1842,
      }),
    ).toBe('model call (propose): gemini-3.7-flash answered in 4.2 s, 1,842 chars')
  })

  it('falls back to the provider name when the model is null', () => {
    expect(
      describeStageEvent({
        event: 'model.call',
        stage: 'propose',
        provider: 'gemini',
        model: null,
        elapsed_s: 4.2,
        ok: true,
        chars: 1234567,
      }),
    ).toBe('model call (propose): gemini answered in 4.2 s, 1,234,567 chars')
  })

  it('names nobody, and still reads as a sentence, when both names are null', () => {
    expect(
      describeStageEvent({
        event: 'model.call',
        stage: 'propose',
        provider: null,
        model: null,
        elapsed_s: 4.2,
        ok: true,
      }),
    ).toBe('model call (propose): answered in 4.2 s')
  })

  it('reports a model call that did not answer', () => {
    expect(
      describeStageEvent({
        event: 'model.call',
        stage: 'propose',
        provider: 'gemini',
        model: 'gemini-3.7-flash',
        elapsed_s: 4.2,
        ok: false,
      }),
    ).toBe('model call (propose) failed after 4.2 s')
  })

  it('drops the stage parenthetical when the call names no stage', () => {
    expect(describeStageEvent({ event: 'model.call', ok: true, elapsed_s: 1 })).toBe(
      'model call: answered in 1.0 s',
    )
  })

  it('names orchestrator and worker prompts before their calls', () => {
    expect(
      describeStageEvent({
        event: 'model.request',
        layer: 'orchestrator',
        model: 'gemini-auto',
      }),
    ).toBe('orchestrator prompt prepared for gemini-auto')
    expect(
      describeStageEvent({ event: 'model.request', layer: 'worker', stage: 'review' }),
    ).toBe('worker prompt prepared (review)')
  })

  it('renders the effort receipt the engine wrote, not a second sentence', () => {
    // `effort.selected` is the first frame of every run since the effort
    // slider landed, and no consumer knew the name -- it read as
    // "pipeline event: effort.selected". The engine already writes a sentence
    // for a person (EffortReceipt.headline) and it is the same sentence the
    // result carries, so this renders that rather than composing a rival.
    expect(
      describeStageEvent({
        event: 'effort.selected',
        level: 'fast',
        headline: 'effort fast: 5 s solver budget, 1 repair round',
      }),
    ).toBe('effort fast: 5 s solver budget, 1 repair round')
    // A receipt with no headline still names the level rather than falling
    // through to the unknown-event line.
    expect(describeStageEvent({ event: 'effort.selected', level: 'thorough' })).toBe(
      'effort thorough',
    )
  })

  it('says what the critic threw away, in words', () => {
    expect(
      describeStageEvent({ event: 'review.dropped', detail: 'names R47, not in the spec' }),
    ).toBe('review discarded a finding: names R47, not in the spec')
    expect(describeStageEvent({ event: 'review.merged', detail: 'two on C1' })).toBe(
      'review merged two findings: two on C1',
    )
  })

  it('names the orchestrator reasoning effort when a chat starts', () => {
    expect(
      describeStageEvent({
        event: 'chat.accepted',
        model: 'gemini-3.1-pro-preview',
        thinking_level: 'high',
        quota_rpm: 6,
      }),
    ).toBe('orchestrator started with gemini-3.1-pro-preview · high thinking · 6 RPM pace')
  })

  it('explains an app-side quota wait', () => {
    expect(
      describeStageEvent({
        event: 'quota.wait',
        layer: 'worker',
        quota_rpm: 6,
        delay_s: 10,
      }),
    ).toBe('worker waiting for quota pace: 10.0 s at 6 RPM')
  })

  it('describes orchestrator tool boundaries and clarification completion', () => {
    expect(describeStageEvent({ event: 'tool.start', tool: 'generate_board' })).toBe(
      'orchestrator called generate_board...',
    )
    expect(describeStageEvent({ event: 'tool.done', tool: 'generate_board' })).toBe(
      'generate_board finished',
    )
    expect(describeStageEvent({ event: 'chat.done', needs_clarification: true })).toBe(
      'waiting for one clarification',
    )
  })

  it('reports a raw model response by its size', () => {
    expect(
      describeStageEvent({
        event: 'model.response',
        stage: 'propose',
        provider: 'gemini',
        chars: 1842,
        truncated: false,
        text: '{"devices": {}}',
      }),
    ).toBe('response (propose): 1,842 chars')
  })

  it('says so when the response was clipped, and still quotes its real length', () => {
    expect(
      describeStageEvent({
        event: 'model.response',
        stage: 'review',
        chars: 16500,
        truncated: true,
        text: 'x'.repeat(16000),
      }),
    ).toBe('response (review): 16,500 chars (truncated)')
  })

  it('falls back to the length of the text it was given when no count came through', () => {
    expect(describeStageEvent({ event: 'model.response', text: 'a dozen ch' })).toBe(
      'response: 10 chars',
    )
  })

  it('still reads as a sentence when the response carries nothing at all', () => {
    expect(describeStageEvent({ event: 'model.response' })).toBe('response: 0 chars')
  })

  it('reports a provider failover', () => {
    expect(
      describeStageEvent({
        event: 'model.retry',
        stage: 'propose',
        provider: 'gemini-3.7-flash',
        error: 'ModelError',
        elapsed_s: 8.06,
      }),
    ).toBe('provider gemini-3.7-flash failed (ModelError) after 8.1 s, trying next')
  })

  it('still reports the failover when the provider and reason are missing', () => {
    expect(
      describeStageEvent({ event: 'model.retry', provider: null, error: null, elapsed_s: null }),
    ).toBe('provider failed, trying next')
  })

  it('says which pages grounding is working from', () => {
    expect(describeStageEvent({ event: 'ground.part', part: 'AMS1117-3.3', cached: true })).toBe(
      'grounding AMS1117-3.3 (cached pages)',
    )
    expect(describeStageEvent({ event: 'ground.part', part: 'AMS1117-3.3', cached: false })).toBe(
      'grounding AMS1117-3.3 (reading pages)',
    )
  })

  it('summarizes constraint eligibility without hiding a blocked artifact', () => {
    expect(
      describeStageEvent({
        event: 'constraints.verify',
        promotable: true,
        verified: 12,
        blockers: 0,
        artifact_available: true,
      }),
    ).toBe('constraints verified: 12 checks; production promotion eligible')
    expect(
      describeStageEvent({
        event: 'constraints.verify',
        promotable: false,
        blockers: 2,
        violated: 1,
        unresolved: 1,
        artifact_available: true,
      }),
    ).toBe(
      'constraints checked: 2 blockers (1 violated, 1 unresolved); artifact available; production promotion blocked',
    )
  })

  it('announces a finished run', () => {
    expect(describeStageEvent({ event: 'run.done', t_s: 41.2 })).toBe('run complete')
  })

  it('reports a failed run with its status and reason', () => {
    expect(
      describeStageEvent({ event: 'run.error', status: 502, error: 'all providers failed' }),
    ).toBe('run failed (502): all providers failed')
  })

  it('drops whichever half of the failure the event did not carry', () => {
    expect(describeStageEvent({ event: 'run.error', error: 'all providers failed' })).toBe(
      'run failed: all providers failed',
    )
    expect(describeStageEvent({ event: 'run.error', status: 500 })).toBe('run failed (500)')
  })

  it('truncates a long failure message to 160 characters', () => {
    expect(describeStageEvent({ event: 'run.error', status: 500, error: 'x'.repeat(400) })).toBe(
      `run failed (500): ${'x'.repeat(160)}`,
    )
  })

  it('describes the frame the parser could not read', () => {
    expect(describeStageEvent({ event: 'client.badframe', raw: '{{{' })).toBe(
      'unparseable frame from server',
    )
  })

  it('names an event it does not know', () => {
    expect(describeStageEvent({ event: 'some.event' })).toBe('pipeline event: some.event')
  })

  it('says the event is unknown when there is no event key at all', () => {
    expect(describeStageEvent({})).toBe('pipeline event: unknown')
    expect(describeStageEvent({ event: null })).toBe('pipeline event: unknown')
    expect(describeStageEvent(null)).toBe('pipeline event: unknown')
    expect(describeStageEvent(undefined)).toBe('pipeline event: unknown')
    expect(describeStageEvent('run.done')).toBe('pipeline event: unknown')
  })

  it('does not mistake an inherited property for a describer', () => {
    expect(describeStageEvent({ event: 'toString' })).toBe('pipeline event: toString')
    expect(describeStageEvent({ event: 'constructor' })).toBe('pipeline event: constructor')
    expect(describeStageEvent({ event: 'stage.done', stage: 'toString' })).toBe('toString finished')
  })

  it('never throws, and always returns a readable sentence, on a gutted event', () => {
    const events = [
      'chat.accepted',
      'run.accepted',
      'stage.start',
      'stage.done',
      'read.part',
      'propose.round',
      'enclosure.round',
      'enclosure.failed',
      'sourcing.part',
      'sourcing.failed',
      'model.call',
      'model.request',
      'model.response',
      'model.retry',
      'tool.start',
      'tool.done',
      'tool.error',
      'ground.part',
      'constraints.verify',
      'run.done',
      'run.error',
      'assistant.message',
      'chat.done',
      'chat.error',
      'client.badframe',
      'some.event',
    ]

    for (const event of events) {
      for (const stage of [undefined, 'read', 'propose', 'place', 'review', 'enclosure', 'sourcing']) {
        const gutted = {
          event,
          stage,
          part: null,
          index: null,
          total: null,
          parts: null,
          pins: null,
          nets: null,
          requirements: null,
          repair_rounds: null,
          solver_status: null,
          board_mm: null,
          wirelength_mm: null,
          warnings: null,
          findings: null,
          blockers: null,
          errors: null,
          round: null,
          first_error: null,
          time_limit_s: null,
          provider: null,
          model: null,
          elapsed_s: null,
          chars: null,
          status: null,
          error: null,
          ok: null,
          cached: null,
        }

        expect(() => describeStageEvent(gutted)).not.toThrow()
        expect(describeStageEvent(gutted).length).toBeGreaterThan(0)
        for (const leak of ['undefined', 'null', 'NaN']) {
          expect(describeStageEvent(gutted)).not.toContain(leak)
        }
      }
    }
  })

  it('describes whatever the parser hands it, badframes included', () => {
    const { events } = parseNdjson('', '{"event":"run.accepted"}\nnope\n{"event":"run.done"}\n')

    expect(events.map(describeStageEvent)).toEqual([
      'request accepted, pipeline starting',
      'unparseable frame from server',
      'run complete',
    ])
  })
})

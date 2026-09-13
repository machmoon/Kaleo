// The pipeline's NDJSON progress stream: bytes to events, events to sentences.
// Pure and DOM-free — the transport belongs to api.js and the rendering to the
// feed, so both halves stay testable without a server or a browser.

import { countOf, formatBoard, formatCount } from './format.js'

const MAX_RAW_CHARS = 200
const MAX_ERROR_CHARS = 160

/** One decoded chunk in; the events it completed plus the unfinished tail out.
    Feed the tail back as `carry` on the next call, starting from ''. Never
    throws: a line that is not a JSON object becomes a `client.badframe` event,
    so a malformed frame is something the feed can show rather than a dead run. */
export function parseNdjson(carry, chunk) {
  const segments = `${text(carry)}${text(chunk)}`.split('\n')
  const tail = segments.pop()
  const events = []
  for (const segment of segments) {
    const line = segment.trim()
    if (line) events.push(frameOf(line))
  }
  return { events, carry: tail }
}

function frameOf(line) {
  let frame
  try {
    frame = JSON.parse(line)
  } catch {
    return badFrame(line)
  }
  // A bare number, string, or array is well-formed JSON and still not an event.
  if (!frame || typeof frame !== 'object' || Array.isArray(frame)) return badFrame(line)
  return frame
}

function badFrame(line) {
  return { event: 'client.badframe', raw: clip(line, MAX_RAW_CHARS) }
}

const STAGE_START = {
  read: () => 'reading datasheets…',
  propose: () => 'proposing a circuit…',
  place: (e) => `placing with CP-SAT${budgetOf(e.time_limit_s)}…`,
  placement_repair: (e) =>
    `verifying placement${text(e.profile) ? ` for ${text(e.profile)}` : ''}…`,
  route: () => 'routing the copper…',
  review: () => 'adversarial review…',
  enclosure: () => 'drawing the printable case…',
  sourcing: () => 'looking up parts and probing datasheets…',
}

const STAGE_DONE = {
  read: (e) =>
    `datasheets read: ${formatCount(countOf(e.parts), 'part')}, ` +
    `${formatCount(countOf(e.pins), 'pin')}, ` +
    `${formatCount(countOf(e.requirements), 'requirement')}`,

  propose: (e) => {
    const rounds = countOf(e.repair_rounds)
    const repaired = rounds > 0 ? `, after ${formatCount(rounds, 'repair round')}` : ''
    return `circuit proposed: ${formatCount(countOf(e.parts), 'part')}, ${formatCount(countOf(e.nets), 'net')}${repaired}`
  },

  place: (e) => {
    const warnings = countOf(e.warnings)
    const clauses = [
      text(e.solver_status),
      formatBoard(e.board_mm),
      wireOf(e.wirelength_mm),
      warnings > 0 ? formatCount(warnings, 'warning') : '',
    ].filter(Boolean)
    return clauses.length ? `placed: ${clauses.join(', ')}` : 'placed'
  },

  placement_repair: (e) => {
    const moves = countOf(e.moves)
    const hardBefore = num(e.hard_before)
    const hardAfter = num(e.hard_after)
    const verified = e.applied === true ? 'verified and applied' : 'checked, not applied'
    const hard =
      hardBefore === null || hardAfter === null
        ? ''
        : `, hard faults ${hardBefore.toFixed(3)} → ${hardAfter.toFixed(3)} mm`
    return `placement ${verified}: ${formatCount(moves, 'accepted move')}${hard}`
  },

  route: (e) => {
    const routed = countOf(e.routed_nets)
    const unrouted = countOf(e.unrouted_nets)
    // Say the unfinished count out loud. A net left as ratsnest is invisible
    // until fabrication, and "routed" with nothing after it reads as done.
    const clauses = [`${routed}/${routed + unrouted} nets`]
    if (countOf(e.tracks) > 0) clauses.push(formatCount(countOf(e.tracks), 'track'))
    if (countOf(e.vias) > 0) clauses.push(formatCount(countOf(e.vias), 'via'))
    if (unrouted > 0) clauses.push(`${unrouted} left unrouted`)
    return `routed: ${clauses.join(', ')}`
  },

  enclosure: (e) => {
    const rounds = countOf(e.repair_rounds)
    const wall = num(e.wall_mm)
    const clauses = [
      formatCount(countOf(e.cutouts), 'cutout'),
      text(e.lid) ? `${text(e.lid)} lid` : '',
      wall === null ? '' : `${wall} mm walls`,
      rounds > 0 ? `after ${formatCount(rounds, 'repair round')}` : '',
      e.rendered === true ? 'rendered' : '',
    ].filter(Boolean)
    return `printable case generated: ${clauses.join(', ')}`
  },

  // "proposed", never "found": an MPN here is the model's suggestion, and the
  // only thing the engine checked is that the datasheet URL served a PDF.
  sourcing: (e) => {
    const proposed = countOf(e.proposed)
    const unresolved = countOf(e.unresolved)
    const clauses = [
      `${proposed}/${proposed + unresolved} parts proposed`,
      `${formatCount(countOf(e.verified), 'datasheet')} verified`,
    ]
    if (unresolved > 0) clauses.push(`${unresolved} unresolved`)
    return `parts sourced: ${clauses.join(', ')}`
  },

  review: (e) => {
    const findings = countOf(e.findings)
    const blockers = countOf(e.blockers)
    const clauses = [findings > 0 ? formatCount(findings, 'finding') : 'no findings']
    if (blockers > 0) clauses.push(formatCount(blockers, 'blocker'))
    return `review: ${clauses.join(', ')}`
  },
}

/** The probe's four answers, in the feed's words. */
const SHEET_WORDS = {
  verified: 'datasheet verified',
  not_pdf: 'datasheet is not a PDF',
  unreachable: 'datasheet unreachable',
  none: 'no datasheet',
}

const DESCRIBERS = {
  'chat.accepted': (e) =>
    `orchestrator started${text(e.model) ? ` with ${text(e.model)}` : ''}${text(e.thinking_level) ? ` · ${text(e.thinking_level)} thinking` : ''}${text(e.quota_rpm) && text(e.quota_rpm) !== 'auto' ? ` · ${text(e.quota_rpm)} RPM pace` : ''}`,

  'quota.wait': (e) => {
    const delay = seconds(e.delay_s)
    const layer = text(e.layer) || 'Gemini'
    const rpm = text(e.quota_rpm)
    return `${layer} waiting for quota pace${delay ? `: ${delay} s` : ''}${rpm ? ` at ${rpm} RPM` : ''}`
  },

  'run.accepted': () => 'request accepted, pipeline starting',

  // The first frame of every run since the effort slider landed. The engine
  // already writes the sentence (`EffortReceipt.headline`), and it is written
  // for a person, so this renders it rather than composing a second one that
  // could disagree with the receipt on the result.
  'effort.selected': (e) => text(e.headline) || `effort ${text(e.level) || 'unknown'}`,

  // A drop and a merge each hide a row the critic wrote, so they read as
  // sentences rather than as a difference between two counts nobody sees.
  'review.dropped': (e) =>
    `review discarded a finding${text(e.detail) ? `: ${text(e.detail)}` : ''}`,

  'review.merged': (e) =>
    `review merged two findings${text(e.detail) ? `: ${text(e.detail)}` : ''}`,

  'stage.start': (e) => stageSentence(STAGE_START, e, (stage) => `starting ${stage}…`),

  'stage.done': (e) => stageSentence(STAGE_DONE, e, (stage) => `${stage} finished`),

  'read.part': (e) => {
    const part = text(e.part)
    const counter = counterOf(e.index, e.total)
    if (e.cached) return `${part ? `${part}: ` : ''}facts already cached${counter}`
    return `reading ${part || 'a datasheet'}${counter}…`
  },

  // The download is the one step that can stall on someone else's web server,
  // so it reports separately from the read it belongs to.
  'read.fetch': (e) => {
    const part = text(e.part)
    const bytes = num(e.bytes)
    const size = bytes === null ? 'the PDF' : `${Math.round(bytes / 1024)} kB`
    return `${part ? `${part}: ` : ''}downloaded ${size}`
  },

  'propose.round': (e) => {
    const round = num(e.round)
    const which = round === null ? '' : ` round ${round}`
    const first = clip(text(e.first_error), MAX_ERROR_CHARS)
    return `proposal${which} rejected: ${formatCount(countOf(e.errors), 'validation error')}${first ? ` (first: ${first})` : ''}`
  },

  'enclosure.round': (e) => {
    const round = num(e.round)
    const which = round === null ? '' : ` round ${round}`
    const first = clip(text(e.first_error), MAX_ERROR_CHARS)
    return `case proposal${which} rejected: ${formatCount(countOf(e.errors), 'validation error')}${first ? ` (first: ${first})` : ''}`
  },

  // The run continues without a case; the sentence must say both halves, or a
  // delivered board reads as a failed run.
  'enclosure.failed': (e) => {
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `case generation failed${why ? `: ${why}` : ''} — the board is still delivered`
  },

  // One part at a time, with both statuses said plainly: a part whose MPN
  // came back null is news the engineer wants while the board is still open.
  'sourcing.part': (e) => {
    const ref = text(e.ref)
    const mpn = text(e.mpn_status) === 'proposed' ? 'MPN proposed' : 'no MPN'
    const sheet = lookup(SHEET_WORDS, text(e.datasheet_status)) || 'no datasheet'
    return `${ref ? `${ref}: ` : ''}${mpn}, ${sheet}`
  },

  // The run continues with the deterministic rows; the sentence must say both
  // halves, or a delivered board reads as a failed run.
  'sourcing.failed': (e) => {
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `sourcing failed${why ? `: ${why}` : ''} — the board is still delivered, unsourced`
  },

  'model.call': (e) => {
    const stage = whereOf(e.stage)
    const took = seconds(e.elapsed_s)
    if (!e.ok) return `model call${stage} failed${took ? ` after ${took} s` : ''}`
    // Either name identifies the call well enough; with neither, the sentence
    // still has to read as a sentence.
    const name = text(e.model) || text(e.provider)
    const chars = num(e.chars)
    const answered = `${name ? `${name} answered` : 'answered'}${took ? ` in ${took} s` : ''}`
    return `model call${stage}: ${answered}${chars === null ? '' : `, ${group(chars)} chars`}`
  },

  'model.request': (e) => {
    const layer = text(e.layer) || 'model'
    const model = text(e.model)
    return `${layer} prompt prepared${whereOf(e.stage)}${model ? ` for ${model}` : ''}`
  },

  // Only ever present on a debug run; the text itself belongs to the feed, so
  // the sentence reports its size and whether there was more of it.
  'model.response': (e) => {
    const counted = num(e.chars)
    // The event carries the answer, so its own length stands in when the count
    // is missing -- reporting nothing at all would read as a broken sentence.
    const chars = counted === null ? text(e.text).length : counted
    const clipped = e.truncated ? ' (truncated)' : ''
    return `response${whereOf(e.stage)}: ${group(chars)} chars${clipped}`
  },

  'model.retry': (e) => {
    const name = text(e.provider)
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    const took = seconds(e.elapsed_s)
    return `provider${name ? ` ${name}` : ''} failed${why ? ` (${why})` : ''}${took ? ` after ${took} s` : ''}, trying next`
  },

  'tool.start': (e) => `orchestrator called ${text(e.tool) || 'a tool'}...`,

  'tool.done': (e) => `${text(e.tool) || 'tool'} finished`,

  'tool.error': (e) => {
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `${text(e.tool) || 'tool'} failed${why ? `: ${why}` : ''}`
  },

  'ground.part': (e) => {
    const part = text(e.part)
    return `grounding${part ? ` ${part}` : ''} (${e.cached ? 'cached' : 'reading'} pages)`
  },

  'constraints.verify': (e) => {
    const verified = countOf(e.verified)
    if (e.promotable === true) {
      return `constraints verified: ${formatCount(verified, 'check')}; production promotion eligible`
    }
    const blockers = countOf(e.blockers)
    const details = []
    if (countOf(e.violated) > 0) details.push(`${countOf(e.violated)} violated`)
    if (countOf(e.unresolved) > 0) details.push(`${countOf(e.unresolved)} unresolved`)
    const why = details.length ? ` (${details.join(', ')})` : ''
    const artifact = e.artifact_available === true ? '; artifact available' : ''
    return `constraints checked: ${formatCount(blockers, 'blocker')}${why}${artifact}; production promotion blocked`
  },

  'run.done': () => 'run complete',

  'run.error': (e) => {
    const status = num(e.status)
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `run failed${status ? ` (${status})` : ''}${why ? `: ${why}` : ''}`
  },

  'assistant.message': () => 'orchestrator answered',

  'chat.done': (e) =>
    e.needs_clarification ? 'waiting for one clarification' : 'orchestrator turn complete',

  'chat.error': (e) => {
    const status = num(e.status)
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `orchestrator failed${status ? ` (${status})` : ''}${why ? `: ${why}` : ''}`
  },

  'client.badframe': () => 'unparseable frame from server',
}

/** One event, one plain sentence for the chat feed. `t_s` is deliberately not
    in it: the feed renders its own time column. An event this does not know
    still gets a sentence, and nothing here throws on a missing field. */
export function describeStageEvent(evt) {
  const e = evt && typeof evt === 'object' ? evt : {}
  const key = text(e.event)
  const describe = lookup(DESCRIBERS, key)
  return (describe && describe(e)) || `pipeline event: ${key || 'unknown'}`
}

function stageSentence(table, evt, fallback) {
  const stage = text(evt.stage)
  const describe = lookup(table, stage)
  if (describe) return describe(evt)
  return stage ? fallback(stage) : ''
}

/** Own keys only: an event named `toString` must not reach Object.prototype. */
function lookup(table, key) {
  return Object.hasOwn(table, key) ? table[key] : null
}

function text(value) {
  if (typeof value === 'string') return value
  if (value == null || typeof value === 'symbol') return ''
  return String(value)
}

function clip(value, max) {
  return value.length > max ? value.slice(0, max) : value
}

/** null rather than 0 for an absent number, so a clause can be dropped whole
    instead of reporting a confident zero the server never sent. */
function num(value) {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function seconds(value) {
  const n = num(value)
  return n === null ? '' : n.toFixed(1)
}

function group(n) {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function counterOf(index, total) {
  const i = num(index)
  const n = num(total)
  return i === null || n === null ? '' : ` (${i} of ${n})`
}

function budgetOf(value) {
  if (value === null) return ' (no solver budget)'
  const n = num(value)
  return n === null ? '' : ` (${n} s solver budget)`
}

function wireOf(value) {
  const n = num(value)
  return n === null ? '' : `${group(n)} mm wire`
}

function whereOf(stage) {
  const name = text(stage)
  return name ? ` (${name})` : ''
}

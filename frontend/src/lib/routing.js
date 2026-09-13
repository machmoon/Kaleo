// The SPA's read of the service's additive `routing` response key, which
// mirrors `RouteResult.as_dict()` in engine/silkscreen/routing.py: `routed`
// (net names), `unrouted` (net name -> reason), `tracks`, `vias`, `warnings`
// and `completion`. Pure and DOM-free; every surface that mentions copper
// renders what this accepts and nothing else.
//
// The honesty rule this module exists for: the grid router is not a competitive
// autorouter and does not pretend to be, so every net it could not finish is
// named here, verbatim, with the reason it gave. Nothing built on this may say
// "board ready" over a ratsnest, and an absent block is "not routed" — a run
// with routing switched off — never a routed board with nothing to report.

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function stringOf(value) {
  return typeof value === 'string' ? value : ''
}

function countOf(value) {
  const n = Number(value)
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : 0
}

function namesOf(value) {
  if (!Array.isArray(value)) return []
  return value.map((net) => stringOf(net)).filter(Boolean)
}

function stringsOf(value) {
  if (!Array.isArray(value)) return []
  return value.map((entry) => stringOf(entry)).filter(Boolean)
}

/** `unrouted` is a name -> reason map on the wire. The reason is what the
    router said and travels untouched; a net whose reason is not a string
    keeps its name and an empty reason, since dropping it would hide a
    ratsnest. Sorted by name so two renders of one result list the same order. */
function unroutedOf(value) {
  const map = objectOf(value)
  if (!map) return []
  return Object.keys(map)
    .filter((net) => net !== '')
    .sort()
    .map((net) => ({ net, reason: stringOf(map[net]) }))
}

/** The routing block on a response, or null when it carries none. `null`
    means the run was not routed (`route: false` / `--no-route`), and every
    caller renders that as "not routed" rather than as an empty success. A
    malformed block is treated as absent for the same reason a malformed
    enclosure is: nothing may draw a routing receipt it cannot vouch for. */
export function readRouting(result) {
  const routing = objectOf(result?.routing)
  if (!routing) return null
  const routed = namesOf(routing.routed)
  const unrouted = unroutedOf(routing.unrouted)
  const total = routed.length + unrouted.length
  return {
    tracks: countOf(routing.tracks),
    vias: countOf(routing.vias),
    routed,
    unrouted,
    warnings: stringsOf(routing.warnings),
    total,
    // The engine defines completion as routed / (routed + unrouted) and reads
    // 0/0 as 1.0. That 1.0 is what a refusal that named nothing would look
    // like, so the client never repeats it: with no routable nets there is no
    // fraction to show, and `null` is how that is said.
    completion: total === 0 ? null : routed.length / total,
  }
}

/** One of four words, never a fifth: `unrouted` (routing skipped: no block),
    `empty` (routed, but no net was routable), `partial` (some net left as
    ratsnest), `complete` (every routable net connected). */
export function routingStatus(routing) {
  if (!routing) return 'unrouted'
  if (routing.total === 0) return 'empty'
  return routing.unrouted.length ? 'partial' : 'complete'
}

/** The short form: "4 of 5 nets routed", "not routed", "no routable nets". */
export function routingSummary(routing) {
  const status = routingStatus(routing)
  if (status === 'unrouted') return 'not routed'
  if (status === 'empty') return 'no routable nets'
  const line = `${routing.routed.length} of ${routing.total} nets routed`
  return status === 'partial' ? `${line}, ${routing.unrouted.length} unrouted` : line
}

/** The completion as a percentage, only when there is one: the engine's
    100% on an empty result is deliberately not reproduced. */
export function routingPercent(routing) {
  if (!routing || routing.completion === null) return ''
  return `${Math.round(routing.completion * 100)}%`
}

/** What a finished run may say about itself, client-side. The board is the
    product either way, but over a ratsnest the sentence has to name every net
    left in it, and with routing skipped it has to say that instead of nothing. */
export function runOutcomeText(result) {
  const routing = readRouting(result)
  const status = routingStatus(routing)
  if (status === 'unrouted') return 'The board run completed; the board was not routed.'
  if (status === 'empty') return 'The board run completed; no net was routable.'
  if (status === 'complete') {
    return `The board run completed; ${routing.routed.length} of ${routing.total} nets routed.`
  }
  const names = routing.unrouted.map((entry) => entry.net).join(', ')
  return (
    `The board run completed with ${routing.unrouted.length} of ${routing.total} nets ` +
    `left unrouted: ${names}.`
  )
}

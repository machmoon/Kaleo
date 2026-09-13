// The effort slider: named levels over the solver budget the service already
// takes. The service's contract is unchanged -- a request still carries
// `time_limit_s` in seconds -- because the number was never the thing anyone
// wanted to choose. "How hard should it try" is the question; seconds were the
// only vocabulary the form had for it.
//
// Two honesty rules, both load-bearing:
//
// * every level names the seconds it spends, in the UI as well as here. A
//   slider that hides the budget behind an adjective would be claiming the run
//   is bounded by something other than a clock, and it is not.
// * the endpoints are exactly api.js's MIN_TIME_LIMIT_S and MAX_TIME_LIMIT_S,
//   so no slider position can ask for a budget the service would clamp. The
//   test pins that, since the two files would otherwise drift apart silently.

/** The stops, in slider order -- Claude Code's effort vocabulary (low,
    medium, high), because that is the dial everyone on this team already has
    a feel for. `seconds` is what goes on the wire. */
export const EFFORT_LEVELS = [
  { name: 'low', seconds: 5, blurb: 'a fast first pass' },
  { name: 'medium', seconds: 20, blurb: 'the default; feasible on real boards' },
  { name: 'high', seconds: 120, blurb: 'the most solver time the service accepts' },
]

export const DEFAULT_EFFORT_INDEX = EFFORT_LEVELS.findIndex((l) => l.name === 'medium')

/** The level at a slider position, clamped. An out-of-range index is a bug
    upstream, not a reason to hand back undefined and blank the label. */
export function levelAt(index) {
  const n = Number(index)
  if (!Number.isFinite(n)) return EFFORT_LEVELS[DEFAULT_EFFORT_INDEX]
  const i = Math.min(EFFORT_LEVELS.length - 1, Math.max(0, Math.round(n)))
  return EFFORT_LEVELS[i]
}

/** The slider position for a budget in seconds -- the nearest level, so a run
    seeded from a previous request (which carried a free-typed number) lands on
    a real stop rather than between two of them. Ties go to the lower level:
    the form is re-seeding a value the user already accepted, and rounding a
    tie upward would quietly spend more of their quota than last time. */
export function indexForSeconds(seconds) {
  // Number(null) is 0, which is finite -- a missing budget would otherwise
  // snap to the fastest level instead of the default.
  const n = seconds === null ? NaN : Number(seconds)
  if (!Number.isFinite(n)) return DEFAULT_EFFORT_INDEX
  let best = 0
  let bestDistance = Infinity
  for (let i = 0; i < EFFORT_LEVELS.length; i += 1) {
    const distance = Math.abs(EFFORT_LEVELS[i].seconds - n)
    if (distance < bestDistance) {
      best = i
      bestDistance = distance
    }
  }
  return best
}

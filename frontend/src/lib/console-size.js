// The drawer's height arithmetic, kept out of the component so it can be
// tested: vitest runs in node with no DOM and no Svelte plugin, so a viewport
// is a number passed in rather than a window this module reads.

export const MIN_HEIGHT = 160
/** Share of the viewport the drawer may take. Above it the body has no room
    left to show the run the console is describing. */
export const MAX_FRACTION = 0.8
/** One ArrowUp or ArrowDown. Coarse enough to cross the range in a few taps,
    fine enough to land on a size someone actually wanted. */
export const STEP = 24

/** The `clamp(160px, 34vh, 340px)` the stylesheet used before the drawer could
    be dragged, reproduced here so the default opening height is unchanged. */
const PREFERRED_FRACTION = 0.34
const PREFERRED_MAX = 340

/** Never below MIN_HEIGHT: on a viewport short enough that 80% of it is under
    the minimum, a max that honoured the fraction would invert the range. */
export function maxHeight(viewport) {
  const vh = Number(viewport)
  if (!Number.isFinite(vh) || vh <= 0) return MIN_HEIGHT
  return Math.max(MIN_HEIGHT, Math.round(vh * MAX_FRACTION))
}

function fit(height, viewport) {
  return Math.min(maxHeight(viewport), Math.max(MIN_HEIGHT, Math.round(height)))
}

export function defaultHeight(viewport) {
  const vh = Number(viewport)
  const preferred = Number.isFinite(vh) && vh > 0 ? vh * PREFERRED_FRACTION : PREFERRED_MAX
  return fit(Math.min(preferred, PREFERRED_MAX), viewport)
}

/** A proposed height, held inside the range. A height that is not a number at
    all -- an unset persisted value, an arithmetic slip mid-drag -- reopens the
    drawer at its default rather than collapsing it to the minimum. */
export function clampHeight(height, viewport) {
  const n = Number(height)
  if (!Number.isFinite(n)) return defaultHeight(viewport)
  return fit(n, viewport)
}

/** One keyboard step. `direction` is 1 for taller and -1 for shorter, matching
    ArrowUp and ArrowDown on a drawer that grows upward from the bottom. */
export function stepHeight(height, direction, viewport) {
  const from = clampHeight(height, viewport)
  return clampHeight(from + Math.sign(Number(direction) || 0) * STEP, viewport)
}

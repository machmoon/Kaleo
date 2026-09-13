// The Case tab's read of the service's additive `enclosure` response key
// (docs/ai-cad-plan.md): the STEP assembly text, the warnings, the repair
// rounds, the kernel's clause-by-clause receipt, and the files written beside
// the project. The v1 `scad`/`params`/`fit`/`engine` keys went away with the
// OpenSCAD emitter on 2026-09-08 — there is one engine now, and a response
// that carries none of those simply parses them to empty/null.
// Pure and DOM-free; the tab renders what this accepts and nothing else.

export const STEP_FILENAME = 'enclosure.step'
// Octet-stream for the same reason download.js's PCB_MIME is: the browser must
// save the file, not open a 40k-line text tab over the top of the app. The
// registered `model/step` would be more descriptive but is not what browsers
// treat as "save this"; the type here is only the Blob's hint, and the honest
// job is the download.
export const STEP_MIME = 'application/octet-stream'

/** The receipt's axis order — x and y around the board, z above it. */
export const MARGIN_AXES = ['x', 'y', 'z']

/** The engines the contract names; anything else parses as unknown. The
    build123d/OCCT kernel is the only one since the OpenSCAD emitter was
    removed from the product path, and the response no longer carries the key
    at all — so this normally reads null, which the tab says out loud. */
export const ENGINES = ['kernel']

/** The kernel's clause names, frozen with the response shape. Order is the
    verifier's own — topology before fit before printability. */
export const KERNEL_CLAUSES = [
  'valid_topology',
  'positive_volume',
  'solid_count',
  'bbox',
  'board_clash',
  'headroom',
  'underside',
  'standoff_concentric',
  'cutout_admits_plug',
  'lid_mates',
  'min_wall',
  'overhang',
  'vent_keepout',
]

/** The file slots the contract names, in the order the tab lists them. */
export const FILE_SLOTS = ['step', 'base_stl', 'lid_stl']

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

/** The enclosure on a response, or null when it carries none. `null` is the
    contract's honest degradation (stage skipped or failed, board still
    delivered), so callers must render an explicit empty state, never a blank
    tab. A malformed object is treated the same as an absent one — the tab
    must not draw a receipt it cannot vouch for. */
export function readEnclosure(result) {
  const enclosure = objectOf(result?.enclosure)
  if (!enclosure) return null
  const step = typeof enclosure.step === 'string' ? enclosure.step : ''
  if (!step.trim()) return null
  return {
    step,
    params: paramsOf(enclosure.params),
    margins: marginsOf(enclosure.fit),
    warnings: warningsOf(enclosure.warnings),
    repairRounds: roundsOf(enclosure.repair_rounds),
    engine: engineOf(enclosure.engine),
    kernel: kernelOf(enclosure.kernel),
    files: filesOf(enclosure.files),
    brief: typeof enclosure.brief === 'string' && enclosure.brief.trim() ? enclosure.brief : null,
  }
}

/** `kernel`, or null when the response did not say. An unknown engine is null
    rather than passed through: the tab must not describe a path it cannot
    vouch for, and it must not invent a name for one it does not know. */
export function engineOf(value) {
  return typeof value === 'string' && ENGINES.includes(value) ? value : null
}

/** The kernel report, or null when no kernel check ran. Defensive the way
    `marginsOf` is: a clause without a name is not a clause, `margin_mm` is a
    finite number or null, and a clause passed only when the engine said
    `true` — "probably passed" is exactly the quiet-zero bug class. Absent or
    malformed input is null, never an empty passing report. */
export function kernelOf(value) {
  const kernel = objectOf(value)
  if (!kernel) return null
  const clauses = (Array.isArray(kernel.clauses) ? kernel.clauses : [])
    .map(objectOf)
    .filter((clause) => clause && typeof clause.name === 'string' && clause.name.trim())
    .map((clause) => ({
      name: clause.name,
      passed: clause.passed === true,
      marginMm: finite(clause.margin_mm) ? clause.margin_mm : null,
      detail: typeof clause.detail === 'string' ? clause.detail : '',
    }))
  return { passed: kernel.passed === true, clauses, warnings: warningsOf(kernel.warnings) }
}

/** The files written beside the project: absolute paths on the engine's
    machine, or null per slot. Always an object, so a template can index it
    without guarding; `snapshots` is always an array. */
export function filesOf(value) {
  const files = objectOf(value) ?? {}
  const pathOf = (v) => (typeof v === 'string' && v.trim() ? v : null)
  return {
    step: pathOf(files.step),
    base_stl: pathOf(files.base_stl),
    lid_stl: pathOf(files.lid_stl),
    snapshots: Array.isArray(files.snapshots) ? files.snapshots.filter((s) => pathOf(s)) : [],
  }
}

/** True when a kernel report exists and did not pass — either its own
    verdict or any clause. No report is not a failure, and not a pass either:
    callers show "no kernel check" for that, never green. */
export function hasKernelFailure(kernel) {
  if (!kernel) return false
  return !kernel.passed || kernel.clauses.some((clause) => !clause.passed)
}

/** The clauses that failed, in the verifier's order. */
export function failedClauses(kernel) {
  return kernel ? kernel.clauses.filter((clause) => !clause.passed) : []
}

/** The emitted parameters as rows, in the server's own order — emit.py writes
    them board-first on purpose, and alphabetising would shuffle the story.
    Only finite numbers survive: a parameter is a millimetre value or it is
    not a parameter. */
function paramsOf(value) {
  const params = objectOf(value)
  if (!params) return []
  return Object.entries(params)
    .filter(([, mm]) => finite(mm))
    .map(([name, mm]) => ({ name, mm }))
}

/** The signed per-axis margins, or null when the receipt is absent or
    malformed. Whole-or-nothing: a receipt missing an axis is not a receipt,
    and inventing a zero for it is exactly the quiet-zero bug class the
    verifier exists to prevent. */
function marginsOf(fit) {
  const margins = objectOf(objectOf(fit)?.margins_mm)
  if (!margins || !MARGIN_AXES.every((axis) => finite(margins[axis]))) return null
  return { x: margins.x, y: margins.y, z: margins.z }
}

function warningsOf(value) {
  if (!Array.isArray(value)) return []
  return value.filter((warning) => typeof warning === 'string' && warning.trim())
}

function roundsOf(value) {
  const n = Number(value)
  return Number.isFinite(n) && n >= 0 ? Math.round(n) : 0
}

/** Any negative margin means the cavity collides with the board — the one
    number on the receipt that must never be softened. */
export function hasCollision(margins) {
  if (!margins) return false
  return MARGIN_AXES.some((axis) => margins[axis] < 0)
}

/** A millimetre value for display: up to three decimals, trailing zeros
    trimmed, but never bare — "2.0", not "2", so the column reads as mm. */
export function formatMm(value) {
  const trimmed = value.toFixed(3).replace(/0+$/, '')
  return trimmed.endsWith('.') ? `${trimmed}0` : trimmed
}

/** A signed margin: the receipt's whole point is the sign, so a positive
    clearance carries an explicit plus and a collision keeps its minus. */
export function formatMargin(value) {
  return `${value >= 0 ? '+' : ''}${formatMm(value)}`
}

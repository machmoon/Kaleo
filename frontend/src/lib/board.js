// Board geometry for the well, kept pure so it is testable without a DOM.
//
// The service resolves every placement — rotation included — in the solver's
// Y-up frame, so the only transform left to the client is the flip into SVG's
// Y-down frame. Per the repo's coordinate-frames rule that flip happens exactly
// once, in the group transform `flipTransform` builds; nothing downstream of it
// may flip anything again. `flipY` exists for the two things that sit outside
// that group — the pixel anchor of a tooltip and its own test.

export const SOLVER_FRAME = 'solver-y-up'

/** The board's colours, named rather than spelt: every value lives with the
    rest of the palette in styles/tokens.css, so nothing here is a colour that
    a theme cannot reach. These go straight into SVG paint attributes, which
    are CSS declarations and take a var() like any other. */
export const GRID_DOT = 'var(--board-grid-dot)'
export const GRID_DOT_OPACITY = 0.22
export const EDGE_CUTS = 'var(--board-edge-cuts)'
export const COURTYARD = 'var(--board-courtyard)'
export const SILKSCREEN = 'var(--board-silkscreen)'

const LAYERS = {
  top: { key: 'top', kicad: 'F.Cu', color: 'var(--board-copper-top)' },
  bottom: { key: 'bottom', kicad: 'B.Cu', color: 'var(--board-copper-bottom)' },
}

/** The contract has two layers. An unrecognised one is drawn as front copper
    rather than dropped — losing a part off the board would be the worse lie. */
export function layerInfo(raw) {
  const key = String(raw ?? '').toLowerCase()
  return LAYERS[key] || LAYERS.top
}

export function layerColor(raw) {
  return layerInfo(raw).color
}

/** The well's corner label, naming only the layers actually on the board. */
export function layerCaption(parts) {
  const present = new Set((parts || []).map((part) => layerInfo(part.layer).key))
  const names = ['top', 'bottom'].filter((key) => present.has(key)).map((key) => LAYERS[key].kicad)
  return names.length ? `Board · ${names.join(' + ')}` : 'Board'
}

function r3(n) {
  return Math.round(n * 1000) / 1000
}

/** null, not zero: an absent wirelength is a thing not reported, not a
    measurement of nothing, and Number(null) would quietly make it a zero. */
function numberOrNull(value) {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** An absolute [x, y, w, h] rect in mm, as both the courtyard and every pad carry. */
export function isRect(value) {
  return (
    Array.isArray(value) &&
    value.length === 4 &&
    value.every((n) => typeof n === 'number' && Number.isFinite(n)) &&
    value[2] >= 0 &&
    value[3] >= 0
  )
}

/** SVG attribute names for a contract rect, or null when it is malformed. */
export function rectAttrs(rect) {
  if (!isRect(rect)) return null
  const [x, y, width, height] = rect
  return { x, y, width, height }
}

export function centerOf(rect) {
  return [rect[0] + rect[2] / 2, rect[1] + rect[3] / 2]
}

/** Pads with a usable rect, each carrying the colour its layer is drawn in. */
export function padRects(part) {
  const color = layerColor(part.layer)
  return (Array.isArray(part.pads) ? part.pads : [])
    .filter((pad) => pad && isRect(pad.rect_mm))
    .map((pad) => ({
      number: String(pad.number ?? ''),
      net: pad.net == null ? null : String(pad.net),
      color,
      ...rectAttrs(pad.rect_mm),
    }))
}

function isPlacedPart(part) {
  return Boolean(part) && Boolean(part.ref) && isRect(part.courtyard_mm)
}

/** Read the placements block off a response, or null when it cannot be drawn
    honestly. A frame this renderer does not know is a refusal, not a guess:
    drawing an unknown frame with the Y-up flip would mirror the board. An
    absent frame is taken as the contract's `solver-y-up`. */
export function readPlacements(result) {
  const placements = result && result.placements
  if (!placements || typeof placements !== 'object') return null

  const frame = String(placements.frame ?? '')
  if (frame && frame !== SOLVER_FRAME) return null

  const [widthMm, heightMm] = Array.isArray(placements.board_mm) ? placements.board_mm : []
  if (!Number.isFinite(widthMm) || !Number.isFinite(heightMm)) return null
  if (widthMm <= 0 || heightMm <= 0) return null

  const parts = (Array.isArray(placements.parts) ? placements.parts : []).filter(isPlacedPart)
  if (!parts.length) return null

  return {
    widthMm,
    heightMm,
    parts,
    wirelengthMm: numberOrNull(result.wirelength_mm),
  }
}

/** The refs a finding points at that are actually on this board.

    A finding names parts the way the circuit spec does — `AMS1117-3.3`,
    `c_bulk_vin` — while the board labels the same parts `U1` and `C1`. The
    response carries both, so `refs` is what matches and `parts` is only a
    fallback for a server that predates it.

    Filtering against the board is the load-bearing part: the well dims every
    part that is not highlighted, so a list that matches nothing does not
    highlight nothing, it greys out the whole board. An empty result here is
    the caller's signal to highlight nobody and say so. */
export function highlightRefs(finding, placements) {
  if (!finding || !placements) return []
  const onBoard = new Set(placements.parts.map((part) => String(part.ref)))
  const named = Array.isArray(finding.refs) ? finding.refs : finding.parts
  const wanted = new Set((Array.isArray(named) ? named : []).filter(Boolean).map(String))
  return [...wanted].filter((ref) => onBoard.has(ref))
}

/** Breathing room around the edge cuts, in mm of board space. */
export const MARGIN_MM = 1.5

export function viewBoxOf(placements, margin = MARGIN_MM) {
  return {
    minX: -margin,
    minY: -margin,
    width: r3(placements.widthMm + 2 * margin),
    height: r3(placements.heightMm + 2 * margin),
  }
}

export function viewBoxString(box) {
  return `${box.minX} ${box.minY} ${box.width} ${box.height}`
}

/** Solver y (up from the bottom edge) to SVG y (down from the top edge). */
export function flipY(yMm, heightMm) {
  return r3(heightMm - yMm)
}

/** The one flip in the client, applied to the group that holds every element. */
export function flipTransform(heightMm) {
  return `translate(0 ${heightMm}) scale(1 -1)`
}

/** Labels live inside the flipped group, so each undoes the flip about its own
    centre; without this every reference designator reads mirrored. */
export function labelTransform(rect) {
  const [cx, cy] = centerOf(rect)
  return `translate(${r3(cx)} ${r3(cy)}) scale(1 -1)`
}

export const REF_MIN_MM = 0.4
export const REF_MAX_MM = 1.2

/** Font size in mm that keeps a reference designator inside its courtyard.
    0.6 em per character is Chivo Mono's advance width. */
export function refFontMm(ref, rect) {
  const chars = Math.max(1, String(ref ?? '').length)
  const byWidth = (rect[2] * 0.86) / (chars * 0.6)
  const byHeight = rect[3] * 0.62
  return r3(Math.min(REF_MAX_MM, Math.max(REF_MIN_MM, Math.min(byWidth, byHeight))))
}

export const MIN_PX_PER_MM = 5
export const MAX_PX_PER_MM = 48

/** Pixels per mm: fill the well when the board fits, floor the scale when it
    does not so a wide board scrolls inside the well instead of the page. */
export function fitScale(box, availableW, availableH) {
  const byWidth = availableW > 0 ? availableW / box.width : Infinity
  const byHeight = availableH > 0 ? availableH / box.height : Infinity
  const fit = Math.min(byWidth, byHeight)
  if (!Number.isFinite(fit)) return MIN_PX_PER_MM
  return Math.min(MAX_PX_PER_MM, Math.max(MIN_PX_PER_MM, fit))
}

export function stagePx(box, scale) {
  return { width: Math.round(box.width * scale), height: Math.round(box.height * scale) }
}

/** Where a tooltip hangs: the top-centre of a courtyard, in stage pixels.
    This is outside the flipped group, so it maps the frame itself. */
export function anchorPx(rect, box, scale, heightMm) {
  const [x, y, w, h] = rect
  return {
    left: r3((x + w / 2 - box.minX) * scale),
    top: r3((flipY(y + h, heightMm) - box.minY) * scale),
  }
}

/** Room a tooltip needs above a part before it has to hang below instead, and
    the margin that keeps it off the left and right walls of the well. */
export const TIP_HEADROOM_PX = 130
export const TIP_SIDE_PX = 80

/** Where the tooltip goes and which way it hangs. The well clips, so a part
    too near the top edge gets its tooltip hung under the courtyard instead of
    over it, and the horizontal anchor is kept off both walls. */
export function tipPlacement(rect, box, scale, heightMm, stageWidth) {
  const [x, y, , h] = rect
  const above = anchorPx(rect, box, scale, heightMm)
  const under = anchorPx([x, y, 0, 0], box, scale, heightMm).top
  const below = above.top < TIP_HEADROOM_PX
  const right = Math.max(TIP_SIDE_PX, stageWidth - TIP_SIDE_PX)
  const leftWall = Math.min(TIP_SIDE_PX, right)
  return {
    left: Math.min(right, Math.max(leftWall, above.left)),
    top: below ? under : above.top,
    below,
  }
}

function mm(value) {
  return `${Number(value).toFixed(2)} mm`
}

/** Tooltip rows. Value is dropped when the part has none rather than shown blank. */
export function partDetails(part) {
  const value = String(part.value ?? '').trim()
  return [
    { label: 'footprint', text: String(part.footprint ?? '') },
    ...(value ? [{ label: 'value', text: value }] : []),
    { label: 'x', text: mm(part.x_mm) },
    { label: 'y', text: mm(part.y_mm) },
    { label: 'layer', text: layerInfo(part.layer).kicad },
    { label: 'rotated', text: part.rotated ? 'yes' : 'no' },
  ]
}

/** Screen-reader name for a part, since the drawing itself says nothing. */
export function partLabel(part) {
  return `${part.ref}, ${layerInfo(part.layer).kicad}, at ${mm(part.x_mm)} by ${mm(part.y_mm)}`
}

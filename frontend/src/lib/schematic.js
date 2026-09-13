// Electrical topology for the schematic view.  The service owns what is
// connected; this module only validates that contract and lays it out on a
// deterministic drafting grid.  Keeping geometry pure makes a malformed net
// refuse to render rather than becoming a plausible-looking wrong diagram.

export const SCHEMATIC_VERSION = 1
export const SCHEMATIC_KINDS = new Set([
  'device',
  'resistor',
  'capacitor',
  'inductor',
  'diode',
  'crystal',
])

export const SHEET_PAD = 44
export const SHEET_HEADER = 52
export const CELL_WIDTH = 320
export const SYMBOL_WIDTH = 180
export const STUB_LENGTH = 58
export const ROW_GAP = 42
export const PIN_GAP = 24

function nonempty(value) {
  const text = String(value ?? '').trim()
  return text || null
}

function optionalText(value) {
  if (value == null) return null
  return nonempty(value)
}

function readPart(raw) {
  if (!raw || typeof raw !== 'object') return null
  const id = nonempty(raw.id)
  const ref = optionalText(raw.ref)
  const kind = nonempty(raw.kind)
  const value = nonempty(raw.value)
  const symbol = optionalText(raw.symbol)
  if (!id || !kind || !value || !SCHEMATIC_KINDS.has(kind)) return null
  if (!Array.isArray(raw.pins)) return null

  const names = new Set()
  const pins = []
  for (const rawPin of raw.pins) {
    if (!rawPin || typeof rawPin !== 'object') return null
    const name = nonempty(rawPin.name)
    const number = nonempty(rawPin.number)
    if (!name || !number || names.has(name)) return null
    names.add(name)
    pins.push({ name, number })
  }
  return { id, ref, kind, value, symbol, pins }
}

/** Return a normalized v1 schematic, or null when drawing it would require a guess. */
export function readSchematic(result) {
  const raw = result && result.schematic
  if (!raw || typeof raw !== 'object' || raw.version !== SCHEMATIC_VERSION) return null
  if (!Array.isArray(raw.parts) || !raw.parts.length || !Array.isArray(raw.nets)) return null

  const parts = []
  const byId = new Map()
  for (const rawPart of raw.parts) {
    const part = readPart(rawPart)
    if (!part || byId.has(part.id)) return null
    parts.push(part)
    byId.set(part.id, part)
  }

  const names = new Set()
  const nets = []
  for (const rawNet of raw.nets) {
    if (!rawNet || typeof rawNet !== 'object') return null
    const name = nonempty(rawNet.name)
    if (!name || names.has(name) || !Array.isArray(rawNet.endpoints) || rawNet.endpoints.length < 2) {
      return null
    }
    names.add(name)

    const endpoints = []
    for (const rawEndpoint of rawNet.endpoints) {
      if (!rawEndpoint || typeof rawEndpoint !== 'object') return null
      const partId = nonempty(rawEndpoint.part_id)
      const ref = optionalText(rawEndpoint.ref)
      const pin = nonempty(rawEndpoint.pin)
      const number = nonempty(rawEndpoint.number)
      const part = byId.get(partId)
      if (!part || !pin || !number) return null
      if (part.ref && ref !== part.ref) return null
      const declared = part.pins.find((candidate) => candidate.name === pin)
      if (!declared || declared.number !== number) return null
      endpoints.push({ partId, ref, pin, number })
    }
    nets.push({ name, endpoints })
  }

  return { version: SCHEMATIC_VERSION, parts, nets }
}

function natural(a, b) {
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' })
}

function orderedParts(parts) {
  return [...parts].sort((a, b) => {
    if (a.kind === 'device' && b.kind !== 'device') return -1
    if (a.kind !== 'device' && b.kind === 'device') return 1
    return natural(a.ref || a.id, b.ref || b.id)
  })
}

export function columnsFor(width, partCount) {
  const count = Math.max(1, Number(partCount) || 1)
  const available = Number(width) || 0
  if (available >= 1040 && count >= 3) return 3
  if (available >= 700 && count >= 2) return 2
  return 1
}

function partHeight(part) {
  if (part.kind !== 'device') return 100
  const rows = Math.max(1, Math.ceil(part.pins.length / 2))
  return Math.max(110, 68 + rows * PIN_GAP)
}

function netByPin(schematic) {
  const out = new Map()
  for (const net of schematic.nets) {
    for (const endpoint of net.endpoints) {
      out.set(`${endpoint.partId}\u0000${endpoint.pin}`, net.name)
    }
  }
  return out
}

function pinsFor(part, box, byPin) {
  const pins = []
  for (let i = 0; i < part.pins.length; i += 1) {
    const pin = part.pins[i]
    const side = part.kind === 'device' ? (i % 2 === 0 ? 'left' : 'right') : i === 0 ? 'left' : 'right'
    const sideRow = part.kind === 'device' ? Math.floor(i / 2) : 0
    const x = side === 'left' ? box.x : box.x + box.width
    const y = part.kind === 'device' ? box.y + 48 + sideRow * PIN_GAP : box.y + box.height / 2
    const wireX = x + (side === 'left' ? -STUB_LENGTH : STUB_LENGTH)
    pins.push({
      ...pin,
      side,
      x,
      y,
      wireX,
      net: byPin.get(`${part.id}\u0000${pin.name}`) || '',
    })
  }
  return pins
}

/** Stable SVG geometry. Layout changes only when the topology or column count changes. */
export function layoutSchematic(schematic, columns = 1) {
  if (!schematic || !Array.isArray(schematic.parts) || !schematic.parts.length) return null
  const parts = orderedParts(schematic.parts)
  const columnCount = Math.max(1, Math.min(3, Math.floor(Number(columns) || 1), parts.length))
  const byPin = netByPin(schematic)
  const laidOut = []
  let y = SHEET_HEADER + SHEET_PAD

  for (let start = 0; start < parts.length; start += columnCount) {
    const row = parts.slice(start, start + columnCount)
    const heights = row.map(partHeight)
    const rowHeight = Math.max(...heights)
    row.forEach((part, column) => {
      const height = heights[column]
      const box = {
        x: SHEET_PAD + column * CELL_WIDTH + (CELL_WIDTH - SYMBOL_WIDTH) / 2,
        y: y + (rowHeight - height) / 2,
        width: SYMBOL_WIDTH,
        height,
      }
      laidOut.push({ ...part, ...box, pins: pinsFor(part, box, byPin) })
    })
    y += rowHeight + ROW_GAP
  }

  return {
    width: SHEET_PAD * 2 + columnCount * CELL_WIDTH,
    height: y - ROW_GAP + SHEET_PAD,
    columns: columnCount,
    parts: laidOut,
    nets: schematic.nets,
  }
}

/** Spec IDs to highlight for a finding, checked against what this sheet contains. */
export function highlightSchematicParts(finding, schematic) {
  if (!finding || !schematic) return []
  const wanted = new Set(
    [...(Array.isArray(finding.refs) ? finding.refs : []), ...(Array.isArray(finding.parts) ? finding.parts : [])]
      .filter(Boolean)
      .map(String),
  )
  return schematic.parts
    .filter((part) => wanted.has(part.id) || (part.ref && wanted.has(part.ref)))
    .map((part) => part.id)
}

export function partCaption(part) {
  return [part.ref || part.id, part.value, part.kind === 'device' ? '' : part.kind]
    .filter(Boolean)
    .join(', ')
}

export function partRosterEntry(part) {
  const pins = part.pins
    .map((pin) => `${pin.name} pin ${pin.number}${pin.net ? ` on ${pin.net}` : ', unconnected'}`)
    .join('; ')
  return `${partCaption(part)}; ${pins || 'no pins declared'}`
}

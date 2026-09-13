// The Sourcing tab's read of the service's additive `sourcing` response key
// (the 2026-09-03 sourcing contract's frozen wire shape): one row per placed
// part carrying a proposed manufacturer and MPN, a probed datasheet URL, and
// the KiCad-library 3D model attached to the footprint. Pure and DOM-free; the
// tab renders what this accepts and nothing else.
//
// Two honesty rules the whole module is built around. An MPN is a proposal
// until a distributor (Mouser, when the engine has a key) lists it — only the
// engine may call one verified, never this module — and a datasheet counts as
// verified only when the engine's probe actually saw `%PDF-`, which is the
// one status a link may open on.

export const BOM_FILENAME = 'bom.csv'
// Octet-stream for the same reason download.js's PCB_MIME is: the browser must
// save the file, not open a text tab over the top of the app.
export const BOM_MIME = 'application/octet-stream'

/** The frozen vocabularies. Anything off-list degrades to `none`, the status
    that claims nothing, rather than being shown as whatever the server said. */
// Kept in step with `engine/silkscreen/sourcing.KINDS`, which is derived from
// `_PREFIX_KINDS` and is what the server actually sends. `switch` and
// `testpoint` arrived with the SW/TP reference prefixes; without them here a
// button and a bare test pad both degraded to `device` on this side, which is
// the wrong kind of quiet: the row still rendered, so nothing looked broken.
export const KINDS = [
  'resistor',
  'capacitor',
  'inductor',
  'diode',
  'crystal',
  'switch',
  'testpoint',
  'device',
]
export const MPN_STATUSES = ['verified', 'proposed', 'none']
export const DATASHEET_STATUSES = ['verified', 'not_pdf', 'unreachable', 'unprobed', 'none']

/** The datasheet badge text, by status. "verified" is the probe's word, and it
    means exactly one thing: the URL served a PDF when the engine fetched it. */
export const DATASHEET_LABELS = {
  verified: 'verified PDF',
  not_pdf: 'not a PDF',
  unreachable: 'unreachable',
  unprobed: 'not probed (budget)',
  none: 'no datasheet',
}

/** The engine's bom_csv column order, byte for byte: a CSV saved from the tab
    and one written beside the project must diff clean. */
export const CSV_COLUMNS = [
  'ref',
  'value',
  'kind',
  'package',
  'manufacturer',
  'mpn',
  'mpn_status',
  'datasheet_url',
  'datasheet_status',
  'model3d',
]

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function stringOf(value) {
  return typeof value === 'string' ? value : ''
}

/** A nullable text field: the wire sends null for "the model said nothing",
    and an empty string means the same thing here. */
function optional(value) {
  const s = stringOf(value).trim()
  return s ? s : null
}

function oneOf(list, value, fallback) {
  return list.includes(value) ? value : fallback
}

/** One entry off the wire, or null when it names no part. A row with no ref
    cannot be placed against the board or the schematic, so it is dropped
    rather than drawn as an anonymous line the engineer cannot act on. */
function rowOf(entry) {
  const e = objectOf(entry)
  if (!e) return null
  const ref = stringOf(e.ref).trim()
  if (!ref) return null
  const mpn = optional(e.mpn)
  const datasheetUrl = optional(e.datasheet_url)
  return {
    ref,
    value: stringOf(e.value).trim(),
    kind: oneOf(KINDS, e.kind, 'device'),
    package: stringOf(e.package).trim(),
    manufacturer: optional(e.manufacturer),
    mpn,
    // A status is only as good as the field it describes: "proposed" with no
    // MPN behind it is a contradiction, and `none` is the reading that claims
    // nothing.
    mpnStatus: mpn ? oneOf(MPN_STATUSES, e.mpn_status, 'none') : 'none',
    datasheetUrl,
    datasheetStatus: datasheetUrl ? oneOf(DATASHEET_STATUSES, e.datasheet_status, 'none') : 'none',
    model3d: optional(e.model3d),
    model3dNote: optional(e.model3d_note),
    note: optional(e.note),
  }
}

function warningsOf(value) {
  if (!Array.isArray(value)) return []
  return value.filter((warning) => typeof warning === 'string' && warning.trim())
}

/** The three headline counts, recomputed from the rows the tab will draw. The
    wire carries the engine's own tally too, but a table and a summary that
    disagree is worse than either alone, so the rows are the single source. */
export function countSourcing(rows) {
  let verified = 0
  let proposed = 0
  let unresolved = 0
  for (const row of rows) {
    if (row.datasheetStatus === 'verified') verified += 1
    if (row.mpnStatus === 'proposed' || row.mpnStatus === 'verified') proposed += 1
    else unresolved += 1
  }
  return { verified, proposed, unresolved }
}

/** The sourcing block on a response, or null when it carries none. `null` is
    the contract's honest degradation (stage skipped or failed, board still
    delivered), so callers must render an explicit empty state, never a blank
    tab. A malformed block is treated the same as an absent one; a block whose
    every entry was unusable is also null — a BOM with no lines is not a BOM. */
export function readSourcing(result) {
  const sourcing = objectOf(result?.sourcing)
  if (!sourcing || !Array.isArray(sourcing.parts)) return null
  const parts = sourcing.parts.map(rowOf).filter(Boolean)
  if (!parts.length) return null
  return {
    parts,
    ...countSourcing(parts),
    warnings: warningsOf(sourcing.warnings),
  }
}

/** Whether a datasheet link may open: the probe saw a PDF and the URL is one a
    browser can follow. A `verified` status on a `javascript:` URL is not a
    verified datasheet, however the server came to say so. */
export function canOpenDatasheet(row) {
  if (!row || row.datasheetStatus !== 'verified' || !row.datasheetUrl) return false
  let url
  try {
    url = new URL(row.datasheetUrl)
  } catch {
    return false
  }
  return url.protocol === 'https:' || url.protocol === 'http:'
}

export function datasheetLabel(status) {
  return DATASHEET_LABELS[oneOf(DATASHEET_STATUSES, status, 'none')]
}

/** The MPN cell's own caption. "verified" names the distributor that said
    so, because the word alone would read as this module's own claim. */
export function mpnLabel(row) {
  if (row?.mpnStatus === 'verified') return 'verified · Mouser'
  return row?.mpnStatus === 'proposed' ? 'proposed' : 'none'
}

/** The 3D model's file name, for the badge — the KiCad path is long and every
    entry shares the `${KISYS3DMOD}` prefix, so the tail is the only news. */
export function model3dName(path) {
  const s = stringOf(path)
  const tail = s.split('/').pop()
  return tail || s
}

/** The one-line summary the artifact card and the feed share. */
export function sourcingSummary(sourcing) {
  if (!sourcing) return ''
  const total = sourcing.parts.length
  return `${sourcing.proposed} of ${total} parts proposed · ${sourcing.verified} datasheet${sourcing.verified === 1 ? '' : 's'} verified`
}

/** RFC 4180 quoting, applied only where a field needs it, matching Python's
    csv module in its default dialect: the engine writes the same file. */
function csvField(value) {
  const s = value == null ? '' : String(value)
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** The BOM as CSV text: the engine's column order, "\n" line ends, and the
    same statuses the table shows — the download says nothing the screen did
    not. Notes stay off the sheet on purpose; a BOM column a purchaser reads
    into a spreadsheet is not the place for prose. */
export function bomCsv(sourcing) {
  const rows = sourcing?.parts ?? []
  const lines = [CSV_COLUMNS.join(',')]
  for (const row of rows) {
    lines.push(
      [
        row.ref,
        row.value,
        row.kind,
        row.package,
        row.manufacturer,
        row.mpn,
        row.mpnStatus,
        row.datasheetUrl,
        row.datasheetStatus,
        row.model3d,
      ]
        .map(csvField)
        .join(','),
    )
  }
  return `${lines.join('\n')}\n`
}

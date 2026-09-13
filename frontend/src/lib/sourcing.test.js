import { describe, expect, it } from 'vitest'
import {
  BOM_FILENAME,
  BOM_MIME,
  CSV_COLUMNS,
  DATASHEET_STATUSES,
  KINDS,
  MPN_STATUSES,
  bomCsv,
  canOpenDatasheet,
  countSourcing,
  datasheetLabel,
  model3dName,
  mpnLabel,
  readSourcing,
  sourcingSummary,
} from './sourcing.js'

const U1 = {
  ref: 'U1',
  value: 'AMS1117-3.3',
  kind: 'device',
  package: 'SOT-223-3_TabPin2',
  manufacturer: 'Advanced Monolithic Systems',
  mpn: 'AMS1117-3.3',
  mpn_status: 'proposed',
  datasheet_url: 'https://example.com/ds.pdf',
  datasheet_status: 'verified',
  model3d: '${KISYS3DMOD}/Package_TO_SOT_SMD.3dshapes/SOT-223.step',
  model3d_note: null,
  note: null,
}

const C1 = {
  ref: 'C1',
  value: '10uF',
  kind: 'capacitor',
  package: 'C_0805',
  manufacturer: null,
  mpn: null,
  mpn_status: 'none',
  datasheet_url: null,
  datasheet_status: 'none',
  model3d: null,
  model3d_note: 'no KiCad library model for a 2-pin 0805 capacitor',
  note: 'generic; any 10 µF X5R will do',
}

const FULL = {
  sourcing: {
    parts: [U1, C1],
    verified: 1,
    proposed: 1,
    unresolved: 1,
    warnings: ['C1: the model named no part'],
  },
}

describe('readSourcing', () => {
  it('reads the frozen response shape whole', () => {
    expect(readSourcing(FULL)).toEqual({
      parts: [
        {
          ref: 'U1',
          value: 'AMS1117-3.3',
          kind: 'device',
          package: 'SOT-223-3_TabPin2',
          manufacturer: 'Advanced Monolithic Systems',
          mpn: 'AMS1117-3.3',
          mpnStatus: 'proposed',
          datasheetUrl: 'https://example.com/ds.pdf',
          datasheetStatus: 'verified',
          model3d: '${KISYS3DMOD}/Package_TO_SOT_SMD.3dshapes/SOT-223.step',
          model3dNote: null,
          note: null,
        },
        {
          ref: 'C1',
          value: '10uF',
          kind: 'capacitor',
          package: 'C_0805',
          manufacturer: null,
          mpn: null,
          mpnStatus: 'none',
          datasheetUrl: null,
          datasheetStatus: 'none',
          model3d: null,
          model3dNote: 'no KiCad library model for a 2-pin 0805 capacitor',
          note: 'generic; any 10 µF X5R will do',
        },
      ],
      verified: 1,
      proposed: 1,
      unresolved: 1,
      warnings: ['C1: the model named no part'],
    })
  })

  it('keeps the board order rather than sorting', () => {
    const refs = readSourcing({ sourcing: { parts: [C1, U1] } }).parts.map((row) => row.ref)
    expect(refs).toEqual(['C1', 'U1'])
  })

  it('returns null for the contract\'s honest degradation', () => {
    expect(readSourcing({ sourcing: null })).toBe(null)
    expect(readSourcing({})).toBe(null)
    expect(readSourcing(null)).toBe(null)
    expect(readSourcing(undefined)).toBe(null)
  })

  it('treats a malformed block the same as an absent one', () => {
    expect(readSourcing({ sourcing: 'text' })).toBe(null)
    expect(readSourcing({ sourcing: [] })).toBe(null)
    expect(readSourcing({ sourcing: { parts: 'U1' } })).toBe(null)
    expect(readSourcing({ sourcing: { parts: [] } })).toBe(null)
    expect(readSourcing({ sourcing: { parts: [null, 'U1', { value: 'no ref' }] } })).toBe(null)
  })

  it('drops an entry without a ref rather than drawing an anonymous line', () => {
    const rows = readSourcing({ sourcing: { parts: [U1, { ...C1, ref: '   ' }] } }).parts
    expect(rows.map((row) => row.ref)).toEqual(['U1'])
  })

  it('counts from the rows it will draw, not from the server\'s tally', () => {
    const sourcing = readSourcing({
      sourcing: { parts: [U1, C1], verified: 9, proposed: 9, unresolved: 9 },
    })
    expect(sourcing.verified).toBe(1)
    expect(sourcing.proposed).toBe(1)
    expect(sourcing.unresolved).toBe(1)
  })

  it('degrades an off-list kind or status to the reading that claims nothing', () => {
    const [row] = readSourcing({
      sourcing: {
        parts: [{ ...U1, kind: 'ic', mpn_status: 'confirmed', datasheet_status: 'ok' }],
      },
    }).parts
    expect(row.kind).toBe('device')
    expect(row.mpnStatus).toBe('none')
    expect(row.datasheetStatus).toBe('none')
  })

  it('never reports a status for a field that is empty', () => {
    const [row] = readSourcing({
      sourcing: {
        parts: [{ ...U1, mpn: null, mpn_status: 'proposed', datasheet_url: '', datasheet_status: 'verified' }],
      },
    }).parts
    expect(row.mpn).toBe(null)
    expect(row.mpnStatus).toBe('none')
    expect(row.datasheetUrl).toBe(null)
    expect(row.datasheetStatus).toBe('none')
  })

  it('defaults warnings without inventing content', () => {
    expect(readSourcing({ sourcing: { parts: [U1] } }).warnings).toEqual([])
    expect(
      readSourcing({ sourcing: { parts: [U1], warnings: ['real', '', 42, null] } }).warnings,
    ).toEqual(['real'])
  })
})

describe('countSourcing', () => {
  it('counts every unproposed MPN as unresolved, so the three numbers add up', () => {
    const rows = readSourcing({ sourcing: { parts: [U1, C1, { ...C1, ref: 'C2' }] } }).parts
    expect(countSourcing(rows)).toEqual({ verified: 1, proposed: 1, unresolved: 2 })
    expect(countSourcing([])).toEqual({ verified: 0, proposed: 0, unresolved: 0 })
  })

  it('counts a distributor-verified MPN as proposed, not unresolved', () => {
    const rows = readSourcing({ sourcing: { parts: [{ ...U1, mpn_status: 'verified' }, C1] } }).parts
    expect(rows[0].mpnStatus).toBe('verified')
    expect(countSourcing(rows)).toEqual({ verified: 1, proposed: 1, unresolved: 1 })
  })
})

describe('canOpenDatasheet', () => {
  const verified = readSourcing({ sourcing: { parts: [U1] } }).parts[0]

  it('opens a verified https datasheet', () => {
    expect(canOpenDatasheet(verified)).toBe(true)
  })

  it('refuses every status but verified — a URL the probe could not read is not a link', () => {
    for (const status of ['not_pdf', 'unreachable', 'none']) {
      const [row] = readSourcing({
        sourcing: { parts: [{ ...U1, datasheet_status: status }] },
      }).parts
      expect(canOpenDatasheet(row)).toBe(false)
    }
    expect(canOpenDatasheet(null)).toBe(false)
  })

  it('refuses a verified status on a URL a browser must not follow', () => {
    expect(canOpenDatasheet({ ...verified, datasheetUrl: 'javascript:alert(1)' })).toBe(false)
    expect(canOpenDatasheet({ ...verified, datasheetUrl: 'not a url' })).toBe(false)
    expect(canOpenDatasheet({ ...verified, datasheetUrl: 'ftp://example.com/ds.pdf' })).toBe(false)
  })
})

describe('labels', () => {
  it('names each datasheet status, and calls an unknown one none', () => {
    expect(datasheetLabel('verified')).toBe('verified PDF')
    expect(datasheetLabel('not_pdf')).toBe('not a PDF')
    expect(datasheetLabel('unreachable')).toBe('unreachable')
    expect(datasheetLabel('unprobed')).toBe('not probed (budget)')
    expect(datasheetLabel('none')).toBe('no datasheet')
    expect(datasheetLabel('confirmed')).toBe('no datasheet')
  })

  it('calls an MPN verified only with the distributor that said so', () => {
    expect(mpnLabel({ mpnStatus: 'verified' })).toBe('verified · Mouser')
    expect(mpnLabel({ mpnStatus: 'proposed' })).toBe('proposed')
    expect(mpnLabel({ mpnStatus: 'none' })).toBe('none')
    expect(mpnLabel(null)).toBe('none')
    expect(MPN_STATUSES).toEqual(['verified', 'proposed', 'none'])
  })

  it('shows a 3D model by its file name', () => {
    expect(model3dName('${KISYS3DMOD}/Package_TO_SOT_SMD.3dshapes/SOT-223.step')).toBe('SOT-223.step')
    expect(model3dName('SOT-223.step')).toBe('SOT-223.step')
    expect(model3dName(null)).toBe('')
  })

  it('summarises the counts against the part total, with the plural right', () => {
    expect(sourcingSummary(readSourcing(FULL))).toBe(
      '1 of 2 parts proposed · 1 datasheet verified',
    )
    expect(sourcingSummary(readSourcing({ sourcing: { parts: [C1] } }))).toBe(
      '0 of 1 parts proposed · 0 datasheets verified',
    )
    expect(sourcingSummary(null)).toBe('')
  })
})

describe('bomCsv', () => {
  it('writes the engine\'s column order with "\\n" line ends and a trailing newline', () => {
    expect(bomCsv(readSourcing(FULL))).toBe(
      [
        CSV_COLUMNS.join(','),
        'U1,AMS1117-3.3,device,SOT-223-3_TabPin2,Advanced Monolithic Systems,AMS1117-3.3,proposed,https://example.com/ds.pdf,verified,${KISYS3DMOD}/Package_TO_SOT_SMD.3dshapes/SOT-223.step',
        'C1,10uF,capacitor,C_0805,,,none,,none,',
        '',
      ].join('\n'),
    )
  })

  it('quotes a field carrying a comma or a quote, doubling the quote', () => {
    const sourcing = readSourcing({
      sourcing: {
        parts: [{ ...U1, manufacturer: 'Texas Instruments, Inc.', value: '1k "precision"' }],
      },
    })
    const [, line] = bomCsv(sourcing).split('\n')
    expect(line).toBe(
      'U1,"1k ""precision""",device,SOT-223-3_TabPin2,"Texas Instruments, Inc.",AMS1117-3.3,proposed,https://example.com/ds.pdf,verified,${KISYS3DMOD}/Package_TO_SOT_SMD.3dshapes/SOT-223.step',
    )
  })

  it('writes the header alone for nothing', () => {
    expect(bomCsv(null)).toBe(`${CSV_COLUMNS.join(',')}\n`)
  })
})

describe('the kind vocabulary tracks the server', () => {
  it('keeps a switch and a test point rather than calling them devices', () => {
    // The join: `netlist.KINDS` gained switch/testpoint, `sourcing._PREFIX_KINDS`
    // gained SW/TP, and this list did not. `oneOf` degrades an off-list value
    // to 'device', so the row rendered and read as the same kind of thing as a
    // microcontroller.
    const rows = readSourcing({
      sourcing: {
        parts: [
          { ref: 'SW1', kind: 'switch' },
          { ref: 'TP3', kind: 'testpoint' },
          { ref: 'X9', kind: 'nonsense' },
        ],
      },
    }).parts
    expect(rows.map((r) => r.kind)).toEqual(['switch', 'testpoint', 'device'])
  })
})

describe('constants', () => {
  it('names the artifact and the frozen vocabularies', () => {
    expect(BOM_FILENAME).toBe('bom.csv')
    expect(BOM_MIME).toBe('application/octet-stream')
    // The server's list, in the server's order (engine/silkscreen/sourcing.py
    // `_PREFIX_KINDS` then `device`). `switch` and `testpoint` arrived with
    // the SW/TP reference prefixes and were missing here, so a button and a
    // bare test pad both degraded to `device` -- a row that still rendered,
    // and therefore a drop nobody would have noticed.
    expect(KINDS).toEqual([
      'resistor',
      'capacitor',
      'inductor',
      'diode',
      'crystal',
      'switch',
      'testpoint',
      'device',
    ])
    expect(MPN_STATUSES).toEqual(['verified', 'proposed', 'none'])
    expect(DATASHEET_STATUSES).toEqual(['verified', 'not_pdf', 'unreachable', 'unprobed', 'none'])
    expect(CSV_COLUMNS).toEqual([
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
    ])
  })
})

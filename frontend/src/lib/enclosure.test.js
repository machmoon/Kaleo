import { describe, expect, it } from 'vitest'
import {
  ENGINES,
  FILE_SLOTS,
  KERNEL_CLAUSES,
  MARGIN_AXES,
  STEP_FILENAME,
  STEP_MIME,
  engineOf,
  failedClauses,
  filesOf,
  formatMargin,
  formatMm,
  hasCollision,
  hasKernelFailure,
  kernelOf,
  readEnclosure,
} from './enclosure.js'

// A STEP assembly is ISO 10303-21 text; two lines stand in for the tens of
// thousands the kernel really exports.
const STEP_TEXT = "ISO-10303-21;\nHEADER;\nFILE_NAME('enclosure.step','',(''),(''),'','','');\nENDSEC;\n"

// `params` and `fit` are v1 keys the service stopped sending on 2026-09-08
// along with the OpenSCAD emitter. They stay in the fixture because the parse
// of them is still live code: a cached or replayed old response must degrade
// to rows and a receipt rather than throw.
const FULL = {
  enclosure: {
    step: STEP_TEXT,
    params: { board_x: 48.2, board_y: 30, wall: 2, clearance: 1, cavity_z: 8.55 },
    fit: { margins_mm: { x: 1, y: 1, z: 0.55 } },
    warnings: ['U1 height defaulted to 3.0 mm'],
    repair_rounds: 1,
  },
}

describe('readEnclosure', () => {
  it('reads the response shape whole, v1 keys included', () => {
    const enclosure = readEnclosure(FULL)
    expect(enclosure).toEqual({
      step: STEP_TEXT,
      params: [
        { name: 'board_x', mm: 48.2 },
        { name: 'board_y', mm: 30 },
        { name: 'wall', mm: 2 },
        { name: 'clearance', mm: 1 },
        { name: 'cavity_z', mm: 8.55 },
      ],
      margins: { x: 1, y: 1, z: 0.55 },
      warnings: ['U1 height defaulted to 3.0 mm'],
      repairRounds: 1,
      engine: null,
      kernel: null,
      files: { step: null, base_stl: null, lid_stl: null, snapshots: [] },
      brief: null,
    })
  })

  it('reads the v2 keys — engine, kernel receipt, files, brief — whole', () => {
    const enclosure = readEnclosure({
      enclosure: {
        ...FULL.enclosure,
        engine: 'kernel',
        kernel: {
          passed: false,
          clauses: [
            { name: 'valid_topology', passed: true, margin_mm: 0, detail: 'BRepCheck clean' },
            { name: 'lid_mates', passed: false, margin_mm: -0.2, detail: 'lip binds by 0.2 mm' },
          ],
          warnings: ['J1 plug envelope defaulted to USB-C'],
        },
        files: {
          step: '/out/enclosure.step',
          base_stl: '/out/enclosure-base.stl',
          lid_stl: null,
          snapshots: ['/out/snap-iso.png', '/out/snap-top.png'],
        },
        brief: 'Snap lid, USB-C on the left.',
      },
    })
    expect(enclosure.engine).toBe('kernel')
    expect(enclosure.kernel).toEqual({
      passed: false,
      clauses: [
        { name: 'valid_topology', passed: true, marginMm: 0, detail: 'BRepCheck clean' },
        { name: 'lid_mates', passed: false, marginMm: -0.2, detail: 'lip binds by 0.2 mm' },
      ],
      warnings: ['J1 plug envelope defaulted to USB-C'],
    })
    expect(enclosure.files).toEqual({
      step: '/out/enclosure.step',
      base_stl: '/out/enclosure-base.stl',
      lid_stl: null,
      snapshots: ['/out/snap-iso.png', '/out/snap-top.png'],
    })
    expect(enclosure.brief).toBe('Snap lid, USB-C on the left.')
  })

  it('parses malformed v2 keys to nulls rather than throwing', () => {
    const enclosure = readEnclosure({
      enclosure: {
        step: STEP_TEXT,
        engine: 'laser',
        kernel: 'yes',
        files: 'nope',
        brief: '   ',
      },
    })
    expect(enclosure.engine).toBe(null)
    expect(enclosure.kernel).toBe(null)
    expect(enclosure.files).toEqual({ step: null, base_stl: null, lid_stl: null, snapshots: [] })
    expect(enclosure.brief).toBe(null)
  })

  it('keeps the server\'s parameter order rather than sorting', () => {
    const names = readEnclosure(FULL).params.map((p) => p.name)
    expect(names).toEqual(['board_x', 'board_y', 'wall', 'clearance', 'cavity_z'])
  })

  it('returns null for the contract\'s honest degradation', () => {
    expect(readEnclosure({ enclosure: null })).toBe(null)
    expect(readEnclosure({})).toBe(null)
    expect(readEnclosure(null)).toBe(null)
    expect(readEnclosure(undefined)).toBe(null)
  })

  it('treats a malformed enclosure the same as an absent one', () => {
    expect(readEnclosure({ enclosure: 'text' })).toBe(null)
    expect(readEnclosure({ enclosure: [] })).toBe(null)
    expect(readEnclosure({ enclosure: { step: '' } })).toBe(null)
    expect(readEnclosure({ enclosure: { step: '   ' } })).toBe(null)
    expect(readEnclosure({ enclosure: { params: { wall: 2 } } })).toBe(null)
  })

  it('drops non-numeric parameters instead of rendering them as millimetres', () => {
    const enclosure = readEnclosure({
      enclosure: {
        step: STEP_TEXT,
        params: { wall: 2, lid: 'friction', bogus: NaN, missing: null },
      },
    })
    expect(enclosure.params).toEqual([{ name: 'wall', mm: 2 }])
  })

  it('refuses a partial fit receipt whole rather than inventing a zero axis', () => {
    const partial = readEnclosure({
      enclosure: { step: STEP_TEXT, fit: { margins_mm: { x: 1, y: 1 } } },
    })
    expect(partial.margins).toBe(null)
    const bogus = readEnclosure({
      enclosure: { step: STEP_TEXT, fit: { margins_mm: { x: 1, y: 1, z: 'ok' } } },
    })
    expect(bogus.margins).toBe(null)
    const absent = readEnclosure({ enclosure: { step: STEP_TEXT } })
    expect(absent.margins).toBe(null)
  })

  it('keeps negative margins signed — a collision must survive parsing', () => {
    const enclosure = readEnclosure({
      enclosure: { step: STEP_TEXT, fit: { margins_mm: { x: 1, y: -0.4, z: 0 } } },
    })
    expect(enclosure.margins).toEqual({ x: 1, y: -0.4, z: 0 })
    expect(hasCollision(enclosure.margins)).toBe(true)
  })

  it('defaults warnings and repair rounds without inventing content', () => {
    const enclosure = readEnclosure({ enclosure: { step: STEP_TEXT } })
    expect(enclosure.warnings).toEqual([])
    expect(enclosure.repairRounds).toBe(0)
    const noisy = readEnclosure({
      enclosure: {
        step: STEP_TEXT,
        warnings: ['real', '', 42, null],
        repair_rounds: 'three',
      },
    })
    expect(noisy.warnings).toEqual(['real'])
    expect(noisy.repairRounds).toBe(0)
  })
})

describe('hasCollision', () => {
  it('is false without a receipt — no receipt is not a clean receipt', () => {
    expect(hasCollision(null)).toBe(false)
  })

  it('flags any negative axis and accepts zero as touching, not colliding', () => {
    expect(hasCollision({ x: 0, y: 0.5, z: 2 })).toBe(false)
    expect(hasCollision({ x: 0.5, y: 0.5, z: -0.01 })).toBe(true)
  })
})

describe('engineOf', () => {
  it('accepts only the contract\'s engines', () => {
    expect(engineOf('kernel')).toBe('kernel')
    expect(engineOf('laser')).toBe(null)
    expect(engineOf(undefined)).toBe(null)
    expect(engineOf(3)).toBe(null)
  })

  it('reports a retired engine as unknown rather than naming it', () => {
    // OpenSCAD left the product path; a response still claiming it names an
    // engine this client cannot vouch for, so it reads as not-reported.
    expect(engineOf('openscad')).toBe(null)
    const enclosure = readEnclosure({ enclosure: { step: STEP_TEXT, engine: 'openscad' } })
    expect(enclosure.engine).toBe(null)
    expect(ENGINES).not.toContain('openscad')
  })
})

describe('kernelOf', () => {
  it('is null for anything that is not a report — never an empty pass', () => {
    expect(kernelOf(null)).toBe(null)
    expect(kernelOf(undefined)).toBe(null)
    expect(kernelOf('passed')).toBe(null)
    expect(kernelOf([])).toBe(null)
  })

  it('reads a clause as passed only when the engine said true', () => {
    const kernel = kernelOf({
      passed: true,
      clauses: [
        { name: 'min_wall', passed: 'true', margin_mm: '0.3', detail: 7 },
        { name: 'headroom', passed: true, margin_mm: 1.4 },
        { name: '', passed: true, margin_mm: 1 },
        'bogus',
        null,
      ],
      warnings: ['real', '', 42],
    })
    expect(kernel).toEqual({
      passed: true,
      clauses: [
        { name: 'min_wall', passed: false, marginMm: null, detail: '' },
        { name: 'headroom', passed: true, marginMm: 1.4, detail: '' },
      ],
      warnings: ['real'],
    })
  })

  it('defaults a missing verdict and clause list to failed and empty', () => {
    expect(kernelOf({})).toEqual({ passed: false, clauses: [], warnings: [] })
  })
})

describe('hasKernelFailure', () => {
  it('is false without a report — no check is not a passed check, and not a failed one', () => {
    expect(hasKernelFailure(null)).toBe(false)
    expect(hasKernelFailure(undefined)).toBe(false)
  })

  it('flags a failed verdict and any failed clause, independently', () => {
    const pass = { name: 'bbox', passed: true, marginMm: 0.5, detail: '' }
    const fail = { name: 'lid_mates', passed: false, marginMm: -0.2, detail: '' }
    expect(hasKernelFailure({ passed: true, clauses: [pass], warnings: [] })).toBe(false)
    expect(hasKernelFailure({ passed: true, clauses: [pass, fail], warnings: [] })).toBe(true)
    expect(hasKernelFailure({ passed: false, clauses: [pass], warnings: [] })).toBe(true)
    expect(failedClauses({ passed: false, clauses: [pass, fail], warnings: [] })).toEqual([fail])
    expect(failedClauses(null)).toEqual([])
  })
})

describe('filesOf', () => {
  it('always answers an object with every slot, nulls for the unwritten', () => {
    expect(filesOf(undefined)).toEqual({ step: null, base_stl: null, lid_stl: null, snapshots: [] })
    expect(filesOf({ step: '/o/e.step', snapshots: ['/o/a.png', '', 3], lid_stl: '' })).toEqual({
      step: '/o/e.step',
      base_stl: null,
      lid_stl: null,
      snapshots: ['/o/a.png'],
    })
  })
})

describe('formatting', () => {
  it('formats millimetres with a decimal point always present', () => {
    expect(formatMm(2)).toBe('2.0')
    expect(formatMm(48.2)).toBe('48.2')
    expect(formatMm(0.55)).toBe('0.55')
    expect(formatMm(1.234)).toBe('1.234')
    expect(formatMm(-0.4)).toBe('-0.4')
  })

  it('signs margins explicitly, both ways', () => {
    expect(formatMargin(1)).toBe('+1.0')
    expect(formatMargin(0)).toBe('+0.0')
    expect(formatMargin(-0.4)).toBe('-0.4')
  })
})

describe('constants', () => {
  it('names the artifact the plan freezes', () => {
    expect(STEP_FILENAME).toBe('enclosure.step')
    expect(STEP_MIME).toBe('application/octet-stream')
    expect(MARGIN_AXES).toEqual(['x', 'y', 'z'])
    expect(ENGINES).toEqual(['kernel'])
    expect(FILE_SLOTS).toEqual(['step', 'base_stl', 'lid_stl'])
    expect(KERNEL_CLAUSES).toEqual([
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
    ])
  })
})

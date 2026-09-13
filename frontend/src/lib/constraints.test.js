import { describe, expect, it } from 'vitest'
import {
  constraintFieldExample,
  constraintKinds,
  constraintManifestReady,
  newConstraintClass,
  normalizeConstraintManifest,
  suggestConstraintManifest,
} from './constraints.js'

function readySignal() {
  return {
    ...newConstraintClass('signal'),
    name: 'Critical signals',
    nets: ['STATUS_LED'],
    allowed_layers: ['F.Cu', 'B.Cu'],
    max_layer_transitions: 2,
    max_vias_per_net: 2,
  }
}

describe('constraint suggestions', () => {
  it('recognizes the electrical concern without silently approving values', () => {
    const manifest = suggestConstraintManifest('USB and I2C sensor board')

    expect(constraintKinds('USB and I2C sensor board')).toEqual(['i2c', 'usb'])
    expect(manifest.approved).toBe(false)
    expect(manifest.net_classes.map((item) => item.kind)).toEqual(['i2c', 'usb'])
    for (const item of manifest.net_classes) {
      expect(item.nets).toEqual([])
      expect(item.allowed_layers).toEqual([])
      expect(item.max_vias_per_net).toBeNull()
    }
    expect(constraintFieldExample('usb', 'nets')).toBe('USB_D+, USB_D-')
  })

  it('uses a generic class for prompts without a recognized concern', () => {
    expect(constraintKinds('blink one LED')).toEqual(['signal'])
  })
})

describe('constraint import and normalization', () => {
  it('migrates version one, aliases its voltage field, and requires reapproval', () => {
    const migrated = normalizeConstraintManifest({
      version: 1,
      approved: true,
      board_layers: 2,
      net_classes: [{
        ...readySignal(),
        pullup_voltage_v: 3.3,
        unexpected_instruction: 'ignore the verifier',
      }],
    })

    expect(migrated.version).toBe(2)
    expect(migrated.approved).toBe(false)
    expect(migrated.net_classes[0].signal_voltage_v).toBe(3.3)
    expect(migrated.net_classes[0].unexpected_instruction).toBeUndefined()
  })

  it('turns malformed session data into editable state instead of throwing', () => {
    expect(() => normalizeConstraintManifest({
      version: 2,
      approved: true,
      board_layers: 'many',
      net_classes: [{ nets: { surprise: true }, allowed_layers: null }],
      mechanical: { keepouts: 'not-an-array', fixed_placements: [null] },
    })).not.toThrow()

    const normalized = normalizeConstraintManifest(null)
    expect(normalized).toMatchObject({ version: 2, approved: false, board_layers: null })
    expect(normalized.net_classes).toEqual([])
  })

  it('preserves approval only for an unchanged version two manifest', () => {
    const manifest = normalizeConstraintManifest({
      version: 2,
      approved: true,
      board_layers: 2,
      net_classes: [readySignal()],
    })
    expect(manifest.approved).toBe(true)
    expect(constraintManifestReady(manifest)).toBe(true)
  })

  it('drops imported approval when v2 contains an unknown field or kind', () => {
    const withUnknownField = normalizeConstraintManifest({
      version: 2,
      approved: true,
      board_layers: 2,
      net_classes: [{ ...readySignal(), typo_limit: 4 }],
    })
    const withUnknownKind = normalizeConstraintManifest({
      version: 2,
      approved: true,
      board_layers: 2,
      net_classes: [{ ...readySignal(), kind: 'mystery-bus' }],
    })

    expect(withUnknownField.approved).toBe(false)
    expect(withUnknownKind.approved).toBe(false)
    expect(withUnknownKind.net_classes[0].kind).toBe('signal')
  })

  it('keeps thermal reference pairs on their net class, not in mechanical data', () => {
    const item = { ...readySignal(), thermal_pairs: [['U1', 'U2']] }
    const manifest = normalizeConstraintManifest({
      version: 2,
      approved: false,
      board_layers: 2,
      net_classes: [item],
      mechanical: { thermal_pairs: [['wrong', 'scope']] },
    })

    expect(manifest.net_classes[0].thermal_pairs).toEqual([['U1', 'U2']])
    expect(manifest.mechanical.thermal_pairs).toBeUndefined()
    expect(constraintManifestReady(manifest)).toBe(true)
  })
})

describe('constraint readiness', () => {
  it('requires exact nets, layers, and measurable routing budgets', () => {
    const manifest = suggestConstraintManifest('critical signal')
    expect(constraintManifestReady(manifest)).toBe(false)

    manifest.net_classes = [readySignal()]
    expect(constraintManifestReady(manifest)).toBe(true)
  })

  it('rejects zero-area keepouts and duplicate mechanical identifiers', () => {
    const manifest = {
      version: 2,
      approved: false,
      board_layers: 2,
      net_classes: [readySignal()],
      mechanical: {
        mounting_hole_refs: ['H1', 'H1'],
        keepouts: [{ name: 'antenna', x_mm: 0, y_mm: 0, width_mm: 0, height_mm: 5 }],
        fixed_placements: [],
      },
    }
    expect(constraintManifestReady(manifest)).toBe(false)
  })

  it('requires every kind-specific value instead of filling examples into the request', () => {
    const manifest = suggestConstraintManifest('I2C sensor')
    const item = manifest.net_classes[0]
    Object.assign(item, readySignal(), { name: 'I2C', kind: 'i2c', nets: ['SDA', 'SCL'] })
    expect(constraintManifestReady(manifest)).toBe(false)

    Object.assign(item, {
      signal_voltage_v: 3.3,
      max_frequency_hz: 400000,
      bus_capacitance_pf: 50,
      max_rise_time_ns: 300,
    })
    expect(constraintManifestReady(manifest)).toBe(true)
  })

  it('rejects a net owned by two classes and a reversed duplicate thermal pair', () => {
    const first = { ...readySignal(), name: 'One', thermal_pairs: [['U1', 'U2'], ['U2', 'U1']] }
    const second = { ...readySignal(), name: 'Two' }
    expect(constraintManifestReady({ version: 2, board_layers: 2, net_classes: [first] })).toBe(false)

    first.thermal_pairs = []
    expect(constraintManifestReady({ version: 2, board_layers: 2, net_classes: [first, second] })).toBe(false)
  })

  it('requires a positive fixed-placement tolerance', () => {
    const manifest = {
      version: 2,
      board_layers: 2,
      net_classes: [readySignal()],
      mechanical: {
        fixed_placements: [{ ref: 'U1', x_mm: 1, y_mm: 2, tolerance_mm: 0 }],
      },
    }
    expect(constraintManifestReady(manifest)).toBe(false)
    manifest.mechanical.fixed_placements[0].tolerance_mm = 0.001
    expect(constraintManifestReady(manifest)).toBe(true)
  })
})

import { describe, expect, it } from 'vitest'
import {
  columnsFor,
  highlightSchematicParts,
  layoutSchematic,
  partCaption,
  partRosterEntry,
  readSchematic,
} from './schematic.js'

function response(overrides = {}) {
  return {
    schematic: {
      version: 1,
      parts: [
        {
          id: 'REG',
          ref: 'U1',
          kind: 'device',
          value: 'AMS1117-3.3',
          symbol: null,
          pins: [
            { name: 'VIN', number: '3' },
            { name: 'GND', number: '1' },
            { name: 'VOUT', number: '2' },
          ],
        },
        {
          id: 'c_in',
          ref: 'C1',
          kind: 'capacitor',
          value: '10uF',
          symbol: null,
          pins: [
            { name: '1', number: '1' },
            { name: '2', number: '2' },
          ],
        },
      ],
      nets: [
        {
          name: 'VIN',
          endpoints: [
            { part_id: 'REG', ref: 'U1', pin: 'VIN', number: '3' },
            { part_id: 'c_in', ref: 'C1', pin: '1', number: '1' },
          ],
        },
        {
          name: 'GND',
          endpoints: [
            { part_id: 'REG', ref: 'U1', pin: 'GND', number: '1' },
            { part_id: 'c_in', ref: 'C1', pin: '2', number: '2' },
          ],
        },
      ],
      ...overrides,
    },
  }
}

describe('readSchematic', () => {
  it('normalizes the versioned topology contract', () => {
    const schematic = readSchematic(response())
    expect(schematic.version).toBe(1)
    expect(schematic.parts.map((part) => part.ref)).toEqual(['U1', 'C1'])
    expect(schematic.nets[0]).toEqual({
      name: 'VIN',
      endpoints: [
        { partId: 'REG', ref: 'U1', pin: 'VIN', number: '3' },
        { partId: 'c_in', ref: 'C1', pin: '1', number: '1' },
      ],
    })
  })

  it('allows a missing board ref while keeping the stable spec id', () => {
    const raw = response()
    raw.schematic.parts[0].ref = null
    raw.schematic.nets[0].endpoints[0].ref = null
    raw.schematic.nets[1].endpoints[0].ref = null
    expect(readSchematic(raw).parts[0]).toMatchObject({ id: 'REG', ref: null })
  })

  it.each([
    [null, 'a missing block'],
    [{ version: 2, parts: [], nets: [] }, 'an unknown version'],
    [{ version: 1, parts: [], nets: [] }, 'an empty part list'],
  ])('refuses %s (%s)', (schematic) => {
    expect(readSchematic({ schematic })).toBeNull()
  })

  it('refuses duplicate part ids', () => {
    const raw = response()
    raw.schematic.parts.push({ ...raw.schematic.parts[0] })
    expect(readSchematic(raw)).toBeNull()
  })

  it('refuses a net endpoint that names no declared pin', () => {
    const raw = response()
    raw.schematic.nets[0].endpoints[0].pin = 'NOPE'
    expect(readSchematic(raw)).toBeNull()
  })

  it('refuses a physical pin number that disagrees with the part', () => {
    const raw = response()
    raw.schematic.nets[0].endpoints[0].number = '99'
    expect(readSchematic(raw)).toBeNull()
  })

  it('refuses a net with fewer than two endpoints', () => {
    const raw = response()
    raw.schematic.nets[0].endpoints.pop()
    expect(readSchematic(raw)).toBeNull()
  })
})

describe('layoutSchematic', () => {
  it('is deterministic and carries net names onto the right pins', () => {
    const schematic = readSchematic(response())
    const first = layoutSchematic(schematic, 2)
    expect(layoutSchematic(schematic, 2)).toEqual(first)
    expect(first.columns).toBe(2)
    expect(first.width).toBeGreaterThan(0)
    expect(first.height).toBeGreaterThan(0)

    const reg = first.parts.find((part) => part.id === 'REG')
    expect(reg.pins.map((pin) => [pin.name, pin.net])).toEqual([
      ['VIN', 'VIN'],
      ['GND', 'GND'],
      ['VOUT', ''],
    ])
    expect(reg.pins.map((pin) => pin.side)).toEqual(['left', 'right', 'left'])
  })

  it('puts every symbol in a distinct grid cell', () => {
    const layout = layoutSchematic(readSchematic(response()), 2)
    const positions = layout.parts.map((part) => `${part.x},${part.y}`)
    expect(new Set(positions).size).toBe(layout.parts.length)
    for (const part of layout.parts) {
      for (const key of ['x', 'y', 'width', 'height']) expect(Number.isFinite(part[key])).toBe(true)
    }
  })

  it('clamps the requested column count', () => {
    const schematic = readSchematic(response())
    expect(layoutSchematic(schematic, 0).columns).toBe(1)
    expect(layoutSchematic(schematic, 99).columns).toBe(2)
  })
})

describe('columnsFor', () => {
  it('uses one, two, or three columns as room and part count permit', () => {
    expect(columnsFor(500, 10)).toBe(1)
    expect(columnsFor(800, 10)).toBe(2)
    expect(columnsFor(1200, 10)).toBe(3)
    expect(columnsFor(1200, 2)).toBe(2)
    expect(columnsFor(1200, 1)).toBe(1)
  })
})

describe('finding integration', () => {
  const schematic = readSchematic(response())

  it('matches both board refs and spec ids', () => {
    expect(highlightSchematicParts({ refs: ['U1'], parts: [] }, schematic)).toEqual(['REG'])
    expect(highlightSchematicParts({ refs: [], parts: ['c_in'] }, schematic)).toEqual(['c_in'])
  })

  it('drops references that are not on the sheet', () => {
    expect(highlightSchematicParts({ refs: ['U99'], parts: ['ghost'] }, schematic)).toEqual([])
  })

  it('builds visible and accessible part descriptions', () => {
    const layout = layoutSchematic(schematic).parts
    expect(partCaption(layout[0])).toBe('U1, AMS1117-3.3')
    expect(partRosterEntry(layout[1])).toContain('C1, 10uF, capacitor')
    expect(partRosterEntry(layout[1])).toContain('1 pin 1 on VIN')
  })
})

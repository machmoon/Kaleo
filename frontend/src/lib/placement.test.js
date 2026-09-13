import { describe, expect, it } from 'vitest'
import { readPlacementRepair } from './placement.js'

function board() {
  return {
    width: 20,
    height: 12,
    components: [
      { ref: 'U1', x: 2, y: 3, width: 4, height: 5, angle: 0 },
    ],
    keepouts: [],
  }
}

function result(overrides = {}) {
  return {
    placement_repair: {
      start: board(),
      board: board(),
      score: {
        before: { hard: 1, soft: 2 },
        after: { hard: 0, soft: 1 },
      },
      steps: [],
      ...overrides,
    },
  }
}

describe('readPlacementRepair', () => {
  it('accepts a renderable verifier artifact', () => {
    const placement = result().placement_repair
    expect(readPlacementRepair({ placement_repair: placement })).toBe(placement)
  })

  it.each([null, undefined, [], 'placement'])('rejects a non-object artifact: %j', (value) => {
    expect(readPlacementRepair({ placement_repair: value })).toBeNull()
  })

  it('rejects malformed boards from imported session JSON', () => {
    expect(readPlacementRepair(result({ board: { width: 20, height: 12 } }))).toBeNull()
    expect(readPlacementRepair(result({ start: { ...board(), width: Number.NaN } }))).toBeNull()
    expect(
      readPlacementRepair(
        result({ board: { ...board(), components: [{ ref: 'U1', x: '2' }] } }),
      ),
    ).toBeNull()
  })

  it('rejects duplicate SVG keys and malformed keepouts', () => {
    const duplicate = { ...board(), components: [...board().components, ...board().components] }
    expect(readPlacementRepair(result({ board: duplicate }))).toBeNull()
    expect(readPlacementRepair(result({ board: { ...board(), keepouts: [null] } }))).toBeNull()
  })

  it('rejects a missing or non-finite score instead of presenting it as zero', () => {
    expect(readPlacementRepair(result({ score: null }))).toBeNull()
    expect(
      readPlacementRepair(
        result({ score: { before: { hard: 1, soft: 2 }, after: { hard: Infinity, soft: 1 } } }),
      ),
    ).toBeNull()
  })

  it('rejects malformed receipt lists before the UI tries to iterate them', () => {
    expect(readPlacementRepair(result({ steps: {} }))).toBeNull()
    expect(readPlacementRepair(result({ steps: [{ receipts: {} }] }))).toBeNull()
    expect(readPlacementRepair(result({ steps: [{ receipts: [null] }] }))).toBeNull()
  })
})

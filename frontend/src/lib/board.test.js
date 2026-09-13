import { describe, expect, it } from 'vitest'
import {
  MAX_PX_PER_MM,
  MIN_PX_PER_MM,
  REF_MAX_MM,
  REF_MIN_MM,
  anchorPx,
  centerOf,
  fitScale,
  flipTransform,
  flipY,
  highlightRefs,
  isRect,
  labelTransform,
  layerCaption,
  layerColor,
  layerInfo,
  padRects,
  partDetails,
  partLabel,
  readPlacements,
  rectAttrs,
  refFontMm,
  stagePx,
  tipPlacement,
  viewBoxOf,
  viewBoxString,
} from './board.js'

/** One part, shaped exactly as the placements contract describes it. */
function part(overrides = {}) {
  return {
    ref: 'U1',
    footprint: 'Package_QFP:LQFP-48_7x7mm_P0.5mm',
    value: 'STM32F030C8',
    layer: 'top',
    rotated: false,
    x_mm: 8,
    y_mm: 6,
    courtyard_mm: [4, 2, 8, 8],
    pads: [{ number: '1', net: 'VDD', rect_mm: [4.5, 2.5, 1, 0.4] }],
    ...overrides,
  }
}

function response(overrides = {}) {
  const body = {
    placements: {
      board_mm: [20, 15],
      frame: 'solver-y-up',
      parts: [part()],
      ...(overrides.placements || {}),
    },
  }
  if ('wirelength_mm' in overrides) body.wirelength_mm = overrides.wirelength_mm
  return body
}

describe('readPlacements', () => {
  it('reads the board size, the parts, and the sibling wirelength', () => {
    const view = readPlacements(response({ wirelength_mm: 52.4 }))

    expect(view.widthMm).toBe(20)
    expect(view.heightMm).toBe(15)
    expect(view.parts).toHaveLength(1)
    expect(view.wirelengthMm).toBe(52.4)
  })

  it('reports a missing wirelength as null rather than zero, which would be a claim', () => {
    expect(readPlacements(response()).wirelengthMm).toBeNull()
    expect(readPlacements(response({ wirelength_mm: null })).wirelengthMm).toBeNull()
  })

  it('returns null when the response carries no placements at all', () => {
    expect(readPlacements({})).toBeNull()
    expect(readPlacements(null)).toBeNull()
    expect(readPlacements({ placements: null })).toBeNull()
    expect(readPlacements({ placements: 'soon' })).toBeNull()
  })

  it('refuses a frame it does not know instead of drawing the board mirrored', () => {
    expect(readPlacements(response({ placements: { frame: 'kicad-y-down' } }))).toBeNull()
  })

  it('takes an absent frame as the contract default', () => {
    const body = response()
    delete body.placements.frame

    expect(readPlacements(body)).not.toBeNull()
  })

  it('returns null when the board size is missing or not a pair of numbers', () => {
    expect(readPlacements(response({ placements: { board_mm: [] } }))).toBeNull()
    expect(readPlacements(response({ placements: { board_mm: [20] } }))).toBeNull()
    expect(readPlacements(response({ placements: { board_mm: ['20', '15'] } }))).toBeNull()
    expect(readPlacements(response({ placements: { board_mm: null } }))).toBeNull()
  })

  it('returns null for a board with no area, which has nothing to draw', () => {
    expect(readPlacements(response({ placements: { board_mm: [0, 15] } }))).toBeNull()
    expect(readPlacements(response({ placements: { board_mm: [20, -1] } }))).toBeNull()
  })

  it('returns null when no part survives, rather than an empty well', () => {
    expect(readPlacements(response({ placements: { parts: [] } }))).toBeNull()
    expect(readPlacements(response({ placements: { parts: 'eleven' } }))).toBeNull()
  })

  it('drops a part with no courtyard rect and keeps the rest', () => {
    const view = readPlacements(
      response({
        placements: {
          parts: [part(), part({ ref: 'C1', courtyard_mm: null }), part({ ref: 'C2' })],
        },
      }),
    )

    expect(view.parts.map((p) => p.ref)).toEqual(['U1', 'C2'])
  })

  it('drops a part whose courtyard is not four finite numbers', () => {
    expect(
      readPlacements(response({ placements: { parts: [part({ courtyard_mm: [4, 2, 8, NaN] })] } })),
    ).toBeNull()
  })
})

describe('isRect', () => {
  it('accepts four finite numbers with a non-negative size', () => {
    expect(isRect([0, 0, 1, 1])).toBe(true)
    expect(isRect([-3.5, -2, 0, 0])).toBe(true)
  })

  it('rejects a rect with a negative width or height', () => {
    expect(isRect([0, 0, -1, 1])).toBe(false)
    expect(isRect([0, 0, 1, -1])).toBe(false)
  })

  it('rejects the wrong length, non-numbers, and non-finite numbers', () => {
    expect(isRect([0, 0, 1])).toBe(false)
    expect(isRect([0, 0, 1, 1, 1])).toBe(false)
    expect(isRect(['0', 0, 1, 1])).toBe(false)
    expect(isRect([0, 0, 1, Infinity])).toBe(false)
    expect(isRect(null)).toBe(false)
  })
})

describe('rectAttrs', () => {
  it('renames a contract rect to the SVG attributes, unchanged', () => {
    expect(rectAttrs([1.5, 2.5, 3, 4])).toEqual({ x: 1.5, y: 2.5, width: 3, height: 4 })
  })

  it('returns null for a malformed rect rather than emitting NaN attributes', () => {
    expect(rectAttrs([1, 2, 3])).toBeNull()
    expect(rectAttrs(undefined)).toBeNull()
  })
})

describe('centerOf', () => {
  it('takes the middle of the rect', () => {
    expect(centerOf([4, 2, 8, 6])).toEqual([8, 5])
  })
})

describe('the Y flip', () => {
  it('maps the bottom edge of the solver frame to the top of the SVG frame', () => {
    expect(flipY(0, 15)).toBe(15)
  })

  it('maps the top edge of the solver frame to the SVG origin', () => {
    expect(flipY(15, 15)).toBe(0)
  })

  it('leaves the midline where it is', () => {
    expect(flipY(7.5, 15)).toBe(7.5)
  })

  it('is its own inverse', () => {
    expect(flipY(flipY(3.25, 15), 15)).toBe(3.25)
  })

  it('carries a point above the board through, rather than clamping it', () => {
    expect(flipY(16, 15)).toBe(-1)
  })

  it('builds the one group transform that performs it', () => {
    expect(flipTransform(15)).toBe('translate(0 15) scale(1 -1)')
  })
})

describe('labelTransform', () => {
  it('centres on the courtyard and undoes the flip, so the text is not mirrored', () => {
    expect(labelTransform([4, 2, 8, 6])).toBe('translate(8 5) scale(1 -1)')
  })

  it('rounds the centre it computes to three places, as the contract does', () => {
    expect(labelTransform([0, 0, 0.333, 0.333])).toBe('translate(0.167 0.167) scale(1 -1)')
  })
})

describe('viewBoxOf', () => {
  it('surrounds the board with the margin on all four sides', () => {
    expect(viewBoxOf({ widthMm: 20, heightMm: 15 })).toEqual({
      minX: -1.5,
      minY: -1.5,
      width: 23,
      height: 18,
    })
  })

  it('takes an explicit margin', () => {
    const box = viewBoxOf({ widthMm: 20, heightMm: 15 }, 0)

    expect(box.width).toBe(20)
    expect(box.height).toBe(15)
  })

  it('renders as the four numbers an SVG viewBox attribute wants', () => {
    expect(viewBoxString(viewBoxOf({ widthMm: 20, heightMm: 15 }))).toBe('-1.5 -1.5 23 18')
  })
})

describe('layers', () => {
  it('gives front copper the F.Cu colour', () => {
    expect(layerColor('top')).toBe('var(--board-copper-top)')
    expect(layerInfo('top').kicad).toBe('F.Cu')
  })

  it('gives back copper the B.Cu colour', () => {
    expect(layerColor('bottom')).toBe('var(--board-copper-bottom)')
    expect(layerInfo('bottom').kicad).toBe('B.Cu')
  })

  it('accepts the layer name in any case', () => {
    expect(layerColor('TOP')).toBe('var(--board-copper-top)')
    expect(layerColor('Bottom')).toBe('var(--board-copper-bottom)')
  })

  it('draws an unrecognised layer as front copper rather than dropping the part', () => {
    expect(layerColor('inner1')).toBe('var(--board-copper-top)')
    expect(layerColor(undefined)).toBe('var(--board-copper-top)')
    expect(layerInfo(null).key).toBe('top')
  })

  it('names only the layers the board actually uses', () => {
    expect(layerCaption([part()])).toBe('Board · F.Cu')
    expect(layerCaption([part({ layer: 'bottom' })])).toBe('Board · B.Cu')
    expect(layerCaption([part(), part({ layer: 'bottom' })])).toBe('Board · F.Cu + B.Cu')
  })

  it('names F.Cu before B.Cu whatever order the parts arrive in', () => {
    expect(layerCaption([part({ layer: 'bottom' }), part()])).toBe('Board · F.Cu + B.Cu')
  })

  it('names no layer at all when there are no parts', () => {
    expect(layerCaption([])).toBe('Board')
    expect(layerCaption(null)).toBe('Board')
  })
})

describe('padRects', () => {
  it('passes the absolute pad rect through and colours it by the part layer', () => {
    expect(padRects(part())).toEqual([
      { number: '1', net: 'VDD', color: 'var(--board-copper-top)', x: 4.5, y: 2.5, width: 1, height: 0.4 },
    ])
  })

  it('colours a bottom-layer part in B.Cu', () => {
    expect(padRects(part({ layer: 'bottom' }))[0].color).toBe('var(--board-copper-bottom)')
  })

  it('keeps an unconnected pad as null rather than inventing a net name', () => {
    const pads = padRects(part({ pads: [{ number: '2', net: null, rect_mm: [0, 0, 1, 1] }] }))

    expect(pads[0].net).toBeNull()
  })

  it('drops a pad with no usable rect instead of drawing it at the origin', () => {
    const pads = padRects(
      part({
        pads: [
          { number: '1', rect_mm: [0, 0, 1, 1] },
          { number: '2', rect_mm: null },
          { number: '3' },
        ],
      }),
    )

    expect(pads.map((p) => p.number)).toEqual(['1'])
  })

  it('returns an empty list when the part carries no pads', () => {
    expect(padRects(part({ pads: undefined }))).toEqual([])
    expect(padRects(part({ pads: 'two' }))).toEqual([])
  })
})

describe('refFontMm', () => {
  it('caps the size on a courtyard with room to spare', () => {
    expect(refFontMm('U1', [0, 0, 5, 5])).toBe(REF_MAX_MM)
  })

  it('floors the size on a courtyard too small for legible text', () => {
    expect(refFontMm('U1', [0, 0, 0.5, 0.5])).toBe(REF_MIN_MM)
  })

  it('is bounded by the courtyard height on a wide, flat part', () => {
    expect(refFontMm('U1', [0, 0, 4, 1])).toBe(0.62)
  })

  it('shrinks as the reference designator gets longer', () => {
    expect(refFontMm('C100', [0, 0, 1, 2])).toBeLessThan(refFontMm('C1', [0, 0, 1, 2]))
  })

  it('treats a missing reference as one character rather than dividing by zero', () => {
    expect(refFontMm('', [0, 0, 1, 2])).toBeGreaterThan(0)
    expect(Number.isFinite(refFontMm(undefined, [0, 0, 1, 2]))).toBe(true)
  })
})

describe('fitScale', () => {
  it('fills the well when the board fits inside it', () => {
    expect(fitScale({ width: 23, height: 18 }, 460, 360)).toBe(20)
  })

  it('is limited by whichever dimension runs out first', () => {
    expect(fitScale({ width: 23, height: 18 }, 460, 180)).toBe(10)
    expect(fitScale({ width: 23, height: 18 }, 230, 360)).toBe(10)
  })

  it('stops zooming in past the ceiling on a tiny board', () => {
    expect(fitScale({ width: 23, height: 18 }, 4600, 3600)).toBe(MAX_PX_PER_MM)
  })

  it('floors the scale on a board too wide to fit, so the well scrolls', () => {
    expect(fitScale({ width: 230, height: 18 }, 460, 360)).toBe(MIN_PX_PER_MM)
  })

  it('falls back to the floor before the well has been measured', () => {
    expect(fitScale({ width: 23, height: 18 }, 0, 0)).toBe(MIN_PX_PER_MM)
  })
})

describe('stagePx', () => {
  it('sizes the drawing in whole pixels', () => {
    expect(stagePx({ width: 23, height: 18 }, 20)).toEqual({ width: 460, height: 360 })
    expect(stagePx({ width: 23, height: 18 }, 7)).toEqual({ width: 161, height: 126 })
  })
})

describe('anchorPx', () => {
  it('anchors a tooltip at the top centre of the courtyard, in stage pixels', () => {
    const box = viewBoxOf({ widthMm: 20, heightMm: 15 })

    expect(anchorPx([2, 3, 4, 2], box, 10, 15)).toEqual({ left: 55, top: 115 })
  })

  it('puts a part at the board origin at the bottom left of the drawing', () => {
    const box = viewBoxOf({ widthMm: 20, heightMm: 15 }, 0)

    expect(anchorPx([0, 0, 0, 0], box, 10, 15)).toEqual({ left: 0, top: 150 })
  })
})

describe('tipPlacement', () => {
  const box = viewBoxOf({ widthMm: 20, heightMm: 15 })

  it('hangs the tooltip over a part with room above it', () => {
    // The courtyard top lands 135 px down, clear of the headroom the tip needs.
    expect(tipPlacement([10, 1, 4, 2], box, 10, 15, 400)).toEqual({
      left: 135,
      top: 135,
      below: false,
    })
  })

  it('hangs it under a part too near the top of the well to fit above', () => {
    const placed = tipPlacement([10, 13, 4, 1], box, 10, 15, 400)

    expect(placed.below).toBe(true)
    // The bottom edge of the courtyard, not the top it would have hung from.
    expect(placed.top).toBe(35)
  })

  it('keeps the anchor off the left wall, so the tooltip is not clipped', () => {
    expect(tipPlacement([0, 3, 0, 2], box, 10, 15, 400).left).toBe(80)
  })

  it('keeps the anchor off the right wall', () => {
    expect(tipPlacement([20, 3, 0, 2], box, 10, 15, 250).left).toBe(170)
  })

  it('falls back to one margin when the stage is narrower than both of them', () => {
    expect(tipPlacement([0, 3, 0, 2], box, 10, 15, 100).left).toBe(80)
    expect(tipPlacement([20, 3, 0, 2], box, 10, 15, 100).left).toBe(80)
  })
})

describe('partDetails', () => {
  it('lists what the tooltip shows, in millimetres', () => {
    expect(partDetails(part())).toEqual([
      { label: 'footprint', text: 'Package_QFP:LQFP-48_7x7mm_P0.5mm' },
      { label: 'value', text: 'STM32F030C8' },
      { label: 'x', text: '8.00 mm' },
      { label: 'y', text: '6.00 mm' },
      { label: 'layer', text: 'F.Cu' },
      { label: 'rotated', text: 'no' },
    ])
  })

  it('drops the value row when the part has none, rather than showing it blank', () => {
    expect(partDetails(part({ value: null })).map((r) => r.label)).not.toContain('value')
    expect(partDetails(part({ value: '  ' })).map((r) => r.label)).not.toContain('value')
  })

  it('says so when the placer rotated the part', () => {
    const rows = partDetails(part({ rotated: true, layer: 'bottom' }))

    expect(rows.find((r) => r.label === 'rotated').text).toBe('yes')
    expect(rows.find((r) => r.label === 'layer').text).toBe('B.Cu')
  })
})

describe('partLabel', () => {
  it('names the part for a screen reader, which the drawing cannot', () => {
    expect(partLabel(part())).toBe('U1, F.Cu, at 8.00 mm by 6.00 mm')
  })
})

describe('highlightRefs', () => {
  const board = readPlacements(
    response({ placements: { parts: [part({ ref: 'U1' }), part({ ref: 'C1' }), part({ ref: 'C2' })] } }),
  )

  it('uses the board refs the finding carries, not the spec names it is written in', () => {
    const finding = { parts: ['AMS1117-3.3', 'c_dec_vout'], refs: ['U1', 'C2'] }
    expect(highlightRefs(finding, board)).toEqual(['U1', 'C2'])
  })

  it('highlights nothing rather than everything when a finding names no placed part', () => {
    // The well dims every part that is not highlighted, so a list that matches
    // nothing would grey out the whole board while claiming to show something.
    const finding = { parts: ['AMS1117-3.3', 'c_dec_vout'], refs: [] }
    expect(highlightRefs(finding, board)).toEqual([])
    expect(highlightRefs({ parts: ['AMS1117-3.3'] }, board)).toEqual([])
  })

  it('falls back to parts for a server that does not send refs', () => {
    expect(highlightRefs({ parts: ['C1', 'U1'] }, board)).toEqual(['C1', 'U1'])
  })

  it('drops refs the board does not have, and repeats', () => {
    const finding = { refs: ['U1', 'U1', 'R9', '', null] }
    expect(highlightRefs(finding, board)).toEqual(['U1'])
  })

  it('is empty with no finding or no board', () => {
    expect(highlightRefs(null, board)).toEqual([])
    expect(highlightRefs({ refs: ['U1'] }, null)).toEqual([])
  })
})

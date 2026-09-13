import { describe, expect, it } from 'vitest'
import {
  MAX_FRACTION,
  MIN_HEIGHT,
  STEP,
  clampHeight,
  defaultHeight,
  maxHeight,
  stepHeight,
} from './console-size.js'

describe('maxHeight', () => {
  it('is the configured share of the viewport', () => {
    expect(maxHeight(1000)).toBe(800)
    expect(maxHeight(900)).toBe(Math.round(900 * MAX_FRACTION))
  })

  it('never drops below the minimum on a tiny viewport', () => {
    expect(maxHeight(100)).toBe(MIN_HEIGHT)
    expect(maxHeight(199)).toBe(MIN_HEIGHT)
  })

  it('falls back to the minimum for a viewport that is not a usable number', () => {
    expect(maxHeight(0)).toBe(MIN_HEIGHT)
    expect(maxHeight(-500)).toBe(MIN_HEIGHT)
    expect(maxHeight(Number.NaN)).toBe(MIN_HEIGHT)
    expect(maxHeight(undefined)).toBe(MIN_HEIGHT)
  })
})

describe('defaultHeight', () => {
  it('reproduces the stylesheet clamp the drawer opened at', () => {
    expect(defaultHeight(1000)).toBe(340)
    expect(defaultHeight(800)).toBe(272)
    expect(defaultHeight(400)).toBe(MIN_HEIGHT)
  })

  it('stays inside the range on a viewport too short for it', () => {
    expect(defaultHeight(150)).toBe(MIN_HEIGHT)
  })
})

describe('clampHeight', () => {
  it('passes an in-range height through', () => {
    expect(clampHeight(250, 1000)).toBe(250)
    expect(clampHeight(MIN_HEIGHT, 1000)).toBe(MIN_HEIGHT)
    expect(clampHeight(800, 1000)).toBe(800)
  })

  it('clamps a height below the minimum', () => {
    expect(clampHeight(40, 1000)).toBe(MIN_HEIGHT)
    expect(clampHeight(-2000, 1000)).toBe(MIN_HEIGHT)
  })

  it('clamps a height above the share of the viewport', () => {
    expect(clampHeight(5000, 1000)).toBe(800)
    expect(clampHeight(801, 1000)).toBe(800)
  })

  it('rounds to whole pixels', () => {
    expect(clampHeight(250.4, 1000)).toBe(250)
    expect(clampHeight(250.6, 1000)).toBe(251)
  })

  it('falls back to the default when the height is not a number', () => {
    expect(clampHeight(Number.NaN, 1000)).toBe(defaultHeight(1000))
    expect(clampHeight(Number.POSITIVE_INFINITY, 1000)).toBe(defaultHeight(1000))
    expect(clampHeight(undefined, 1000)).toBe(defaultHeight(1000))
    expect(clampHeight('tall', 1000)).toBe(defaultHeight(1000))
  })

  it('holds the minimum on a viewport whose max would invert the range', () => {
    expect(clampHeight(500, 100)).toBe(MIN_HEIGHT)
    expect(clampHeight(10, 100)).toBe(MIN_HEIGHT)
  })
})

describe('stepHeight', () => {
  it('grows and shrinks by one step', () => {
    expect(stepHeight(250, 1, 1000)).toBe(250 + STEP)
    expect(stepHeight(250, -1, 1000)).toBe(250 - STEP)
  })

  it('stops at the bounds rather than stepping past them', () => {
    expect(stepHeight(MIN_HEIGHT + 5, -1, 1000)).toBe(MIN_HEIGHT)
    expect(stepHeight(795, 1, 1000)).toBe(800)
    expect(stepHeight(MIN_HEIGHT, -1, 1000)).toBe(MIN_HEIGHT)
    expect(stepHeight(800, 1, 1000)).toBe(800)
  })

  it('starts from a clamped height, so an out-of-range one is corrected first', () => {
    expect(stepHeight(5000, -1, 1000)).toBe(800 - STEP)
    expect(stepHeight(Number.NaN, 1, 1000)).toBe(defaultHeight(1000) + STEP)
  })

  it('does not move for a direction of zero or a non-number', () => {
    expect(stepHeight(250, 0, 1000)).toBe(250)
    expect(stepHeight(250, undefined, 1000)).toBe(250)
  })
})

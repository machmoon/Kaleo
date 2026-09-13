import { describe, expect, it } from 'vitest'

import { MAX_TIME_LIMIT_S, MIN_TIME_LIMIT_S, normalizeRequest } from './api.js'
import { DEFAULT_EFFORT_INDEX, EFFORT_LEVELS, indexForSeconds, levelAt } from './effort.js'

describe('effort levels', () => {
  it('spans exactly the budget the service accepts', () => {
    expect(EFFORT_LEVELS[0].seconds).toBe(MIN_TIME_LIMIT_S)
    expect(EFFORT_LEVELS[EFFORT_LEVELS.length - 1].seconds).toBe(MAX_TIME_LIMIT_S)
  })

  it('increases monotonically, so a rightward drag is never less effort', () => {
    const seconds = EFFORT_LEVELS.map((l) => l.seconds)
    expect(seconds).toStrictEqual([...seconds].sort((a, b) => a - b))
    expect(new Set(seconds).size).toBe(seconds.length)
  })

  it('sends every level unchanged -- no position is clamped on the way out', () => {
    for (const level of EFFORT_LEVELS) {
      const sent = normalizeRequest({ intent: 'x', time_limit_s: level.seconds })
      expect(sent.time_limit_s).toBe(level.seconds)
    }
  })

  it('defaults to medium', () => {
    expect(levelAt(DEFAULT_EFFORT_INDEX).name).toBe('medium')
  })
})

describe('levelAt', () => {
  it('returns the level at a position', () => {
    expect(levelAt(0).name).toBe('low')
    expect(levelAt(EFFORT_LEVELS.length - 1).name).toBe('high')
  })

  it('clamps out-of-range positions instead of returning undefined', () => {
    expect(levelAt(-3).name).toBe('low')
    expect(levelAt(99).name).toBe('high')
  })

  it('falls back to the default for a non-number', () => {
    expect(levelAt(undefined).name).toBe('medium')
    expect(levelAt('nonsense').name).toBe('medium')
  })

  it('reads a numeric string, since range inputs bind as strings', () => {
    expect(levelAt('0').name).toBe('low')
    expect(levelAt('2').name).toBe('high')
  })
})

describe('indexForSeconds', () => {
  it('round-trips every level', () => {
    EFFORT_LEVELS.forEach((level, i) => {
      expect(indexForSeconds(level.seconds)).toBe(i)
    })
  })

  it('snaps a free-typed budget to the nearest level', () => {
    expect(levelAt(indexForSeconds(22)).name).toBe('medium')
    expect(levelAt(indexForSeconds(90)).name).toBe('high')
    expect(levelAt(indexForSeconds(1)).name).toBe('low')
    expect(levelAt(indexForSeconds(600)).name).toBe('high')
  })

  it('breaks a tie downward rather than spending more than last time', () => {
    expect(levelAt(indexForSeconds(40)).name).toBe('medium')
  })

  it('falls back to the default for a missing budget', () => {
    expect(indexForSeconds(null)).toBe(DEFAULT_EFFORT_INDEX)
    expect(indexForSeconds(undefined)).toBe(DEFAULT_EFFORT_INDEX)
  })
})

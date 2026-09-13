import { describe, expect, it } from 'vitest'
import {
  countOf,
  formatBoard,
  formatClock,
  formatCount,
  formatDuration,
  formatParts,
  formatWirelength,
  joinDot,
} from './format.js'

// The exact separators the UI renders, spelled as codepoints so a silent
// substitution (ASCII dot, letter x) fails here instead of on screen.
const DOT = '·'
const TIMES = '×'

describe('formatDuration', () => {
  it('renders zero seconds rather than an empty string', () => {
    expect(formatDuration(0)).toBe('0 s')
  })

  it('renders a bare second count below one minute', () => {
    expect(formatDuration(1)).toBe('1 s')
    expect(formatDuration(42)).toBe('42 s')
    expect(formatDuration(59)).toBe('59 s')
  })

  it('switches to minutes at exactly sixty seconds', () => {
    expect(formatDuration(60)).toBe('1 m 00 s')
  })

  it('zero-pads the seconds part so the readout does not jitter in width', () => {
    expect(formatDuration(61)).toBe('1 m 01 s')
    expect(formatDuration(69)).toBe('1 m 09 s')
    expect(formatDuration(125)).toBe('2 m 05 s')
  })

  it('keeps counting in minutes past an hour rather than adding an hours field', () => {
    expect(formatDuration(3600)).toBe('60 m 00 s')
  })

  it('rounds to the nearest second', () => {
    expect(formatDuration(1.4)).toBe('1 s')
    expect(formatDuration(1.6)).toBe('2 s')
  })

  it('rounds up across the minute boundary', () => {
    expect(formatDuration(59.6)).toBe('1 m 00 s')
  })

  it('clamps a negative duration to zero', () => {
    expect(formatDuration(-5)).toBe('0 s')
    expect(formatDuration(-0.4)).toBe('0 s')
  })

  it('treats a missing or unparseable duration as zero', () => {
    expect(formatDuration(undefined)).toBe('0 s')
    expect(formatDuration(null)).toBe('0 s')
    expect(formatDuration(NaN)).toBe('0 s')
    expect(formatDuration('not a number')).toBe('0 s')
  })

  it('accepts a numeric string, since JSON numbers arrive loosely typed', () => {
    expect(formatDuration('12')).toBe('12 s')
  })
})

describe('formatClock', () => {
  it('renders a bare second count below one minute', () => {
    expect(formatClock(0)).toBe('0 s')
    expect(formatClock(3.4)).toBe('3 s')
    expect(formatClock(59)).toBe('59 s')
  })

  it('switches to m:ss at exactly sixty seconds and zero-pads the seconds', () => {
    expect(formatClock(60)).toBe('1:00')
    expect(formatClock(65)).toBe('1:05')
    expect(formatClock(125)).toBe('2:05')
  })

  it('renders nothing for a time the server never sent', () => {
    expect(formatClock(null)).toBe('')
    expect(formatClock(undefined)).toBe('')
    expect(formatClock('')).toBe('')
    expect(formatClock('soon')).toBe('')
  })

  it('clamps a negative time to zero rather than rendering a minus sign', () => {
    expect(formatClock(-2)).toBe('0 s')
  })
})

describe('formatCount', () => {
  it('uses the singular for exactly one', () => {
    expect(formatCount(1, 'part')).toBe('1 part')
  })

  it('uses the plural for zero', () => {
    expect(formatCount(0, 'part')).toBe('0 parts')
  })

  it('uses the plural for many', () => {
    expect(formatCount(2, 'part')).toBe('2 parts')
    expect(formatCount(11, 'finding')).toBe('11 findings')
  })

  it('pluralises a multi-word noun by appending to the whole phrase', () => {
    expect(formatCount(1, 'repair round')).toBe('1 repair round')
    expect(formatCount(3, 'repair round')).toBe('3 repair rounds')
  })

  it('takes an explicit plural for nouns that do not take -s', () => {
    expect(formatCount(2, 'entry', 'entries')).toBe('2 entries')
    expect(formatCount(1, 'entry', 'entries')).toBe('1 entry')
  })

  it('compares the count strictly, so callers must pass a number', () => {
    expect(formatCount('1', 'part')).toBe('1 parts')
  })
})

describe('formatParts', () => {
  it('joins several refs with the separator dot', () => {
    expect(formatParts(['U1', 'C3'])).toBe(`U1 ${DOT} C3`)
  })

  it('returns a lone ref unadorned', () => {
    expect(formatParts(['U1'])).toBe('U1')
  })

  it('returns an empty string for an empty part list', () => {
    expect(formatParts([])).toBe('')
  })

  it('returns an empty string when the part list is missing', () => {
    expect(formatParts(undefined)).toBe('')
    expect(formatParts(null)).toBe('')
  })

  it('drops empty and null entries instead of rendering stray separators', () => {
    expect(formatParts(['U1', '', null, undefined, 'C3'])).toBe(`U1 ${DOT} C3`)
  })

  it('returns an empty string when every entry is empty', () => {
    expect(formatParts(['', null])).toBe('')
  })
})

describe('formatBoard', () => {
  it('renders width by height to one decimal place with a unit', () => {
    expect(formatBoard([20, 30])).toBe(`20.0 ${TIMES} 30.0 mm`)
  })

  it('rounds each dimension to one decimal place', () => {
    expect(formatBoard([12.34, 56.78])).toBe(`12.3 ${TIMES} 56.8 mm`)
  })

  it('renders a zero-sized board rather than treating it as missing', () => {
    expect(formatBoard([0, 0])).toBe(`0.0 ${TIMES} 0.0 mm`)
  })

  it('returns an empty string when the board size is missing', () => {
    expect(formatBoard(undefined)).toBe('')
    expect(formatBoard(null)).toBe('')
  })

  it('returns an empty string when a dimension is absent', () => {
    expect(formatBoard([])).toBe('')
    expect(formatBoard([20])).toBe('')
  })

  it('returns an empty string when the dimensions are not numbers', () => {
    expect(formatBoard(['20', '30'])).toBe('')
    expect(formatBoard([20, null])).toBe('')
  })

  it('ignores anything past the first two dimensions', () => {
    expect(formatBoard([20, 30, 1.6])).toBe(`20.0 ${TIMES} 30.0 mm`)
  })
})

describe('formatWirelength', () => {
  it('names the measure and rounds it to one decimal place', () => {
    expect(formatWirelength(52.44)).toBe('wirelength 52.4 mm')
    expect(formatWirelength(52)).toBe('wirelength 52.0 mm')
  })

  it('renders a zero-length result rather than treating it as missing', () => {
    expect(formatWirelength(0)).toBe('wirelength 0.0 mm')
  })

  it('returns an empty string when the response reported none', () => {
    expect(formatWirelength(null)).toBe('')
    expect(formatWirelength(undefined)).toBe('')
  })

  it('returns an empty string rather than printing NaN', () => {
    expect(formatWirelength('long')).toBe('')
    expect(formatWirelength(Infinity)).toBe('')
  })
})

describe('countOf', () => {
  it('counts the entries when given the list itself', () => {
    expect(countOf(['1', '2', '3'])).toBe(3)
  })

  it('counts an empty list as zero', () => {
    expect(countOf([])).toBe(0)
  })

  it('passes a number straight through', () => {
    expect(countOf(48)).toBe(48)
    expect(countOf(0)).toBe(0)
  })

  it('parses a numeric string', () => {
    expect(countOf('7')).toBe(7)
  })

  it('counts anything unparseable as zero', () => {
    expect(countOf(undefined)).toBe(0)
    expect(countOf('lots')).toBe(0)
    expect(countOf({})).toBe(0)
    expect(countOf(NaN)).toBe(0)
  })

  it('counts a non-finite number as zero', () => {
    expect(countOf(Infinity)).toBe(0)
  })

  it('counts null as zero', () => {
    expect(countOf(null)).toBe(0)
  })
})

describe('joinDot', () => {
  it('joins the pieces with the separator dot', () => {
    expect(joinDot(['20.0 mm', '11 parts'])).toBe(`20.0 mm ${DOT} 11 parts`)
  })

  it('drops the empty pieces callers pass for absent fields', () => {
    expect(joinDot(['11 parts', '', 'feasible', ''])).toBe(`11 parts ${DOT} feasible`)
  })

  it('returns an empty string when every piece is empty', () => {
    expect(joinDot(['', '', ''])).toBe('')
    expect(joinDot([])).toBe('')
  })

  it('requires an array, unlike formatParts, which tolerates a missing list', () => {
    expect(() => joinDot(null)).toThrow(TypeError)
    expect(formatParts(null)).toBe('')
  })
})

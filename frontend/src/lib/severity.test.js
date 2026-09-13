import { describe, expect, it } from 'vitest'
import { SEVERITY_ORDER, countBySeverity, severityInfo } from './severity.js'

describe('severityInfo', () => {
  it('maps blocker to the plain-language label the UI shows', () => {
    expect(severityInfo('blocker')).toEqual({ key: 'blocker', label: 'will not work', rank: 0 })
  })

  it('maps marginal to its own label', () => {
    expect(severityInfo('marginal')).toEqual({ key: 'marginal', label: 'marginal', rank: 1 })
  })

  it('maps note to its own label', () => {
    expect(severityInfo('note')).toEqual({ key: 'note', label: 'note', rank: 2 })
  })

  it('lowercases the incoming severity before matching a tier', () => {
    expect(severityInfo('BLOCKER')).toEqual({ key: 'blocker', label: 'will not work', rank: 0 })
    expect(severityInfo('Marginal').key).toBe('marginal')
  })

  it('keeps an unknown severity as its own label but styles it as a note', () => {
    const info = severityInfo('catastrophic')

    expect(info.label).toBe('catastrophic')
    expect(info.key).toBe('note')
    expect(info.rank).toBe(2)
  })

  it('never escalates an unknown severity to blocker styling', () => {
    for (const raw of ['critical', 'fatal', 'error', 'severe']) {
      expect(severityInfo(raw).key).not.toBe('blocker')
    }
  })

  it('lowercases an unknown severity too, so the chip never shouts', () => {
    expect(severityInfo('CATASTROPHIC').label).toBe('catastrophic')
  })

  it('falls back to the literal label "note" when the severity is missing', () => {
    for (const raw of [undefined, null, '']) {
      expect(severityInfo(raw)).toEqual({ key: 'note', label: 'note', rank: 2 })
    }
  })

  it('returns the shared tier object for a known severity, so ranks stay identical', () => {
    expect(severityInfo('blocker')).toBe(severityInfo('BLOCKER'))
  })
})

describe('SEVERITY_ORDER', () => {
  it('lists every tier worst-first', () => {
    expect(SEVERITY_ORDER).toEqual(['blocker', 'marginal', 'note'])
  })

  it('agrees with the rank each tier carries', () => {
    const ranks = SEVERITY_ORDER.map((key) => severityInfo(key).rank)

    expect(ranks).toEqual([...ranks].sort((a, b) => a - b))
    expect(ranks).toEqual([0, 1, 2])
  })

  it('sorts a mixed finding list blockers-first when used as the sort key', () => {
    const findings = [
      { severity: 'note' },
      { severity: 'blocker' },
      { severity: 'sideways' },
      { severity: 'marginal' },
    ]

    const sorted = [...findings].sort((a, b) => severityInfo(a.severity).rank - severityInfo(b.severity).rank)

    expect(sorted.map((f) => f.severity)).toEqual(['blocker', 'marginal', 'note', 'sideways'])
  })
})

describe('countBySeverity', () => {
  it('counts each tier', () => {
    const findings = [
      { severity: 'blocker' },
      { severity: 'blocker' },
      { severity: 'marginal' },
      { severity: 'note' },
    ]

    expect(countBySeverity(findings)).toEqual({ blocker: 2, marginal: 1, note: 1 })
  })

  it('returns a zeroed tally for an empty list', () => {
    expect(countBySeverity([])).toEqual({ blocker: 0, marginal: 0, note: 0 })
  })

  it('returns a zeroed tally when the findings list is missing', () => {
    expect(countBySeverity(undefined)).toEqual({ blocker: 0, marginal: 0, note: 0 })
    expect(countBySeverity(null)).toEqual({ blocker: 0, marginal: 0, note: 0 })
  })

  it('buckets unknown and missing severities into note', () => {
    const findings = [{ severity: 'catastrophic' }, { severity: '' }, {}]

    expect(countBySeverity(findings)).toEqual({ blocker: 0, marginal: 0, note: 3 })
  })

  it('is case-insensitive about the tier it counts', () => {
    expect(countBySeverity([{ severity: 'BLOCKER' }])).toEqual({
      blocker: 1,
      marginal: 0,
      note: 0,
    })
  })
})

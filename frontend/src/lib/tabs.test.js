import { describe, expect, it } from 'vitest'
import { TABS, parseTab, resolveTab } from './tabs.js'

describe('parseTab', () => {
  it('reads a tab name off the hash, with or without the leading hash', () => {
    expect(parseTab('#board')).toBe('board')
    expect(parseTab('review')).toBe('review')
    expect(parseTab('#chat')).toBe('chat')
  })

  it('accepts any case and surrounding whitespace', () => {
    expect(parseTab('#Board')).toBe('board')
    expect(parseTab('#  review  ')).toBe('review')
  })

  it('returns nothing for a hash that names no tab', () => {
    expect(parseTab('')).toBe('')
    expect(parseTab('#')).toBe('')
    expect(parseTab('#somewhere-else')).toBe('')
    expect(parseTab(undefined)).toBe('')
    expect(parseTab(null)).toBe('')
  })

  it('knows the seven tabs the status bar shows', () => {
    expect(TABS).toEqual(['chat', 'schematic', 'placement', 'board', 'case', 'sourcing', 'review'])
  })

  it('reads the sourcing tab off the hash', () => {
    expect(parseTab('#sourcing')).toBe('sourcing')
  })
})

describe('resolveTab', () => {
  it('shows the board once a run carries placements', () => {
    expect(resolveTab('#board', { board: true })).toBe('board')
  })

  it('falls back to the chat when there is no board to show', () => {
    expect(resolveTab('#board', { board: false })).toBe('chat')
    expect(resolveTab('#board')).toBe('chat')
  })

  it('shows the schematic once a run carries validated topology', () => {
    expect(resolveTab('#schematic', { schematic: true })).toBe('schematic')
  })

  it('shows verifier receipts only when a placement run exists', () => {
    expect(resolveTab('#placement', { placement: true })).toBe('placement')
    expect(resolveTab('#placement', { placement: false, board: true })).toBe('chat')
  })

  it('falls back to chat when there is no schematic to show', () => {
    expect(resolveTab('#schematic', { schematic: false, board: true })).toBe('chat')
    expect(resolveTab('#schematic')).toBe('chat')
  })

  it('shows the case tab only after a run finished', () => {
    expect(resolveTab('#case', { case: true })).toBe('case')
    expect(resolveTab('#case', { case: false, board: true })).toBe('chat')
    expect(resolveTab('#case')).toBe('chat')
  })

  it('shows the sourcing tab only after a run finished', () => {
    expect(resolveTab('#sourcing', { sourcing: true })).toBe('sourcing')
    expect(resolveTab('#sourcing', { sourcing: false, board: true, case: true })).toBe('chat')
    expect(resolveTab('#sourcing')).toBe('chat')
  })

  it('shows review only after a result exists', () => {
    expect(resolveTab('#review', { review: true })).toBe('review')
    expect(resolveTab('#review', { review: false })).toBe('chat')
  })

  it('defaults to the chat for an empty or unknown hash', () => {
    expect(resolveTab('', { schematic: true, board: true, review: true })).toBe('chat')
    expect(resolveTab('#nowhere', { schematic: true, board: true, review: true })).toBe('chat')
  })
})

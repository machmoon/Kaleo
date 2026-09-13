import { describe, expect, it } from 'vitest'
import {
  readRouting,
  routingPercent,
  routingStatus,
  routingSummary,
  runOutcomeText,
} from './routing.js'

// The wire shape, as service/app.py builds it from RouteResult.as_dict().
const PARTIAL = {
  routing: {
    tracks: 7,
    vias: 2,
    routed: ['GND', 'VIN', 'VOUT'],
    unrouted: {
      SW: 'no path within the 150000 expansion budget',
      FB: 'pad FB.1 has no free grid node',
    },
    warnings: ['single-layer board'],
    completion: 0.6,
  },
}

describe('readRouting', () => {
  it('reads the block verbatim, every unrouted net with its reason, sorted by name', () => {
    const routing = readRouting(PARTIAL)

    expect(routing).toEqual({
      tracks: 7,
      vias: 2,
      routed: ['GND', 'VIN', 'VOUT'],
      unrouted: [
        { net: 'FB', reason: 'pad FB.1 has no free grid node' },
        { net: 'SW', reason: 'no path within the 150000 expansion budget' },
      ],
      warnings: ['single-layer board'],
      total: 5,
      completion: 0.6,
    })
  })

  it('answers null for a response with no routing block, which is a run that was not routed', () => {
    expect(readRouting({ status: 'feasible' })).toBeNull()
    expect(readRouting(null)).toBeNull()
    expect(readRouting({ routing: null })).toBeNull()
    expect(readRouting({ routing: 'routed' })).toBeNull()
    expect(readRouting({ routing: [] })).toBeNull()
  })

  it('never turns the engine\'s 100% on an empty result into a percentage', () => {
    // routing.py's completion reads 0/0 as 1.0 -- the shape of a refusal that
    // named nothing. The client shows no fraction at all for that case.
    const routing = readRouting({ routing: { routed: [], unrouted: {}, completion: 1.0 } })

    expect(routing.total).toBe(0)
    expect(routing.completion).toBeNull()
    expect(routingPercent(routing)).toBe('')
    expect(routingStatus(routing)).toBe('empty')
    expect(routingSummary(routing)).toBe('no routable nets')
  })

  it('computes completion from the named nets rather than trusting the wire number', () => {
    const routing = readRouting({
      routing: { routed: ['A'], unrouted: { B: 'blocked' }, completion: 1.0 },
    })

    expect(routing.completion).toBe(0.5)
    expect(routingPercent(routing)).toBe('50%')
  })

  it('keeps an unrouted net whose reason is malformed, with an empty reason', () => {
    const routing = readRouting({ routing: { routed: [], unrouted: { NET: 7 } } })

    expect(routing.unrouted).toEqual([{ net: 'NET', reason: '' }])
    expect(routingStatus(routing)).toBe('partial')
  })

  it('tolerates a block with only some fields', () => {
    expect(readRouting({ routing: {} })).toEqual({
      tracks: 0,
      vias: 0,
      routed: [],
      unrouted: [],
      warnings: [],
      total: 0,
      completion: null,
    })
    expect(readRouting({ routing: { routed: 'GND', unrouted: 'SW', tracks: -3 } })).toMatchObject({
      routed: [],
      unrouted: [],
      tracks: 0,
    })
  })
})

describe('routingStatus and routingSummary', () => {
  it('says "not routed" for an absent block, never nothing', () => {
    expect(routingStatus(null)).toBe('unrouted')
    expect(routingSummary(null)).toBe('not routed')
    expect(routingPercent(null)).toBe('')
  })

  it('counts N of M and says how many were left', () => {
    const routing = readRouting(PARTIAL)

    expect(routingStatus(routing)).toBe('partial')
    expect(routingSummary(routing)).toBe('3 of 5 nets routed, 2 unrouted')
    expect(routingPercent(routing)).toBe('60%')
  })

  it('reports a fully routed board as N of N with no caveat', () => {
    const routing = readRouting({ routing: { routed: ['A', 'B'], unrouted: {} } })

    expect(routingStatus(routing)).toBe('complete')
    expect(routingSummary(routing)).toBe('2 of 2 nets routed')
    expect(routingPercent(routing)).toBe('100%')
  })
})

describe('runOutcomeText', () => {
  it('names every unrouted net in the client-side completion sentence', () => {
    expect(runOutcomeText(PARTIAL)).toBe(
      'The board run completed with 2 of 5 nets left unrouted: FB, SW.',
    )
  })

  it('says the board was not routed when the block is absent', () => {
    expect(runOutcomeText({ status: 'feasible' })).toBe(
      'The board run completed; the board was not routed.',
    )
  })

  it('does not read an empty result as a routed board', () => {
    expect(runOutcomeText({ routing: { routed: [], unrouted: {}, completion: 1.0 } })).toBe(
      'The board run completed; no net was routable.',
    )
  })

  it('states the count on a fully routed board', () => {
    expect(runOutcomeText({ routing: { routed: ['A', 'B'], unrouted: {} } })).toBe(
      'The board run completed; 2 of 2 nets routed.',
    )
  })
})

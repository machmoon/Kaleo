import { describe, expect, it } from 'vitest'
import {
  MAX_SESSION_BYTES,
  parseSession,
  serializeSession,
  sessionDocument,
  sessionFilename,
} from './session.js'

const STATE = {
  phase: 'done',
  request: { intent: 'a regulator' },
  result: { status: 'FEASIBLE', kicad_pcb: '(kicad_pcb)' },
  error: null,
  id: 'r1',
  sessionId: 'session-1',
  stages: { propose: { state: 'done' } },
  feed: [{ event: 'model.request', detail: '{"prompt":"raw"}' }],
  entries: [{ type: 'message', role: 'user', text: 'a regulator' }],
  needsClarification: false,
  actualModel: 'gemini-test',
  orchestratorModel: 'gemini-3.1-pro-preview',
  thinkingLevel: 'high',
  actualThinkingLevel: 'high',
  quotaRpm: '6',
}

describe('session JSON', () => {
  it('keeps the transcript, raw trace and board artifact', () => {
    const text = serializeSession(STATE, new Date('2026-08-30T12:00:00Z'))
    const restored = parseSession(text)

    expect(restored).toMatchObject(STATE)
    expect(text).toContain('(kicad_pcb)')
    expect(text).toContain('model.request')
  })

  it('round-trips the routing block, every unrouted net and reason intact', () => {
    // The result is saved whole, so no schema change: a session opened later
    // must still name the nets the router left as ratsnest.
    const routing = {
      tracks: 3,
      vias: 1,
      routed: ['GND'],
      unrouted: { SW: 'no path within budget' },
      warnings: [],
      completion: 0.5,
    }
    const state = { ...STATE, result: { ...STATE.result, routing } }

    const restored = parseSession(serializeSession(state))

    expect(restored.result.routing).toEqual(routing)
    expect(JSON.parse(serializeSession(state)).version).toBe(1)
  })

  it('keeps a result with no routing block as one with no routing block', () => {
    const restored = parseSession(serializeSession(STATE))

    expect('routing' in restored.result).toBe(false)
  })

  it('uses a versioned, inspectable document envelope', () => {
    expect(sessionDocument(STATE, new Date('2026-08-30T12:00:00Z'))).toMatchObject({
      schema: 'silkscreen.session',
      version: 1,
      saved_at: '2026-08-30T12:00:00.000Z',
    })
  })

  it('serializes Error fields that JSON would otherwise drop', () => {
    const error = new Error('provider down')
    error.kind = 'upstream'
    error.status = 502

    expect(JSON.parse(serializeSession({ ...STATE, error })).session.error).toEqual({
      kind: 'upstream',
      message: 'provider down',
      status: 502,
      errorId: '',
    })
  })

  it('rejects malformed, foreign, incomplete and oversized files', () => {
    expect(() => parseSession('{')).toThrow('not valid JSON')
    expect(() => parseSession('{"schema":"someone.else","version":1}')).toThrow(
      'not a supported',
    )
    expect(() =>
      parseSession('{"schema":"silkscreen.session","version":1,"session":{}}'),
    ).toThrow('incomplete')
    expect(() => parseSession('x'.repeat(MAX_SESSION_BYTES + 1))).toThrow('exceeds')
  })

  it('sanitizes the downloaded filename', () => {
    expect(sessionFilename({ sessionId: '../session:one' })).toBe('sessionone-session.json')
    expect(sessionFilename({})).toBe('silkscreen-session.json')
  })
})

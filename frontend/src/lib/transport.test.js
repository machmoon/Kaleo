import { describe, expect, it, vi } from 'vitest'

import { createTransport, resolveApiUrl } from './transport.js'

describe('desktop transport boundary', () => {
  it('keeps relative API paths unchanged in the browser', () => {
    expect(resolveApiUrl('/chat/stream', '')).toBe('/chat/stream')
  })

  it('targets the loopback sidecar in the desktop runtime', () => {
    expect(resolveApiUrl('/healthz', 'http://127.0.0.1:43123')).toBe(
      'http://127.0.0.1:43123/healthz',
    )
  })

  it.each([
    'https://example.com',
    'http://127.0.0.1.evil.test:8081',
    'http://0.0.0.0:8081',
    'http://[::1]:8081',
  ])('rejects a non-canonical sidecar origin: %s', (baseUrl) => {
    expect(() => resolveApiUrl('/healthz', baseUrl)).toThrow(/loopback/i)
  })

  it('uses the native fetch implementation only for a desktop base URL', async () => {
    const webFetch = vi.fn()
    const nativeFetch = vi.fn().mockResolvedValue({ ok: true })
    const request = createTransport({
      baseUrl: 'http://127.0.0.1:43123',
      webFetch,
      nativeFetch,
    })

    await request('/models', { headers: { accept: 'application/json' } })

    expect(nativeFetch).toHaveBeenCalledWith(
      'http://127.0.0.1:43123/models',
      { headers: { accept: 'application/json' } },
    )
    expect(webFetch).not.toHaveBeenCalled()
  })
})

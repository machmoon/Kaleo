import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import {
  LEVELS,
  LOG_CAPACITY,
  LOG_NDJSON_MIME,
  LOG_TEXT_MIME,
  MAX_ARG_BYTES,
  MAX_ENTRY_BYTES,
  MAX_TOTAL_BYTES,
  SOURCES,
  clearLog,
  isExtensionNoise,
  log,
  logError,
  logEvent,
  logFilename,
  logMeta,
  logWarn,
  nextRunId,
  record,
  redact,
  runId,
  safeArg,
  scrubText,
  suspectEngineBug,
  toNdjson,
  toText,
} from './log.js'

// The buffer is a module singleton and appends are batched, so every store read
// waits one microtask; the module's own flush was queued before this one.
async function settled() {
  await Promise.resolve()
  return get(log)
}

// `seq` is monotonic for the life of the module, which is exactly what most of
// these tests assert -- so the few that name an absolute seq get a new module.
async function freshLog() {
  vi.resetModules()
  return import('./log.js')
}

/** An entry as it reaches an exporter, with a fixed clock. */
function entryAt(fields = {}) {
  return {
    seq: 1,
    ts: Date.UTC(2026, 7, 29, 18, 15, 0, 250),
    level: 'info',
    src: 'app',
    event: 'run.start',
    msg: 'started',
    run: 'r1',
    data: null,
    bytes: 120,
    ...fields,
  }
}

/** Runs `body` with a window whose location is `href`, and takes it away again. */
function withHref(href, body) {
  globalThis.window = { location: { href }, navigator: { userAgent: 'Mozilla/5.0 (test)' } }
  try {
    body()
  } finally {
    delete globalThis.window
  }
}

const NOW = Date.UTC(2026, 7, 29, 18, 15, 4, 0)

beforeEach(() => {
  clearLog()
})

describe('the constants other modules build against', () => {
  it('caps the buffer at a thousand entries', () => {
    expect(LOG_CAPACITY).toBe(1000)
  })

  it('caps one argument at 2 KB and one entry at 8 KB', () => {
    expect(MAX_ARG_BYTES).toBe(2048)
    expect(MAX_ENTRY_BYTES).toBe(8192)
  })

  it('caps the whole buffer at 4 MiB', () => {
    expect(MAX_TOTAL_BYTES).toBe(4 * 1024 * 1024)
  })

  it('names the two export MIME types', () => {
    expect(LOG_TEXT_MIME).toBe('text/plain;charset=utf-8')
    expect(LOG_NDJSON_MIME).toBe('application/x-ndjson')
  })

  it('lists the four levels and the four sources', () => {
    expect(LEVELS).toEqual(['error', 'warn', 'info', 'debug'])
    // 'server' is the pipeline stream's relayed events; it joined the list when
    // the run store started forwarding them.
    expect(SOURCES).toEqual(['app', 'server', 'console', 'window'])
  })
})

describe('record', () => {
  it('appends an entry carrying every field of the schema', async () => {
    record({ level: 'warn', src: 'window', event: 'window.error', msg: 'boom', data: { a: 1 } })

    const [entry] = (await settled()).entries

    expect(entry).toMatchObject({
      level: 'warn',
      src: 'window',
      event: 'window.error',
      msg: 'boom',
      data: { a: 1 },
    })
    expect(typeof entry.seq).toBe('number')
    expect(typeof entry.ts).toBe('number')
    expect(typeof entry.bytes).toBe('number')
  })

  it('defaults an unknown level and source to the app info line', async () => {
    record({ level: 'fatal', src: 'martian', msg: 'x' })

    const [entry] = (await settled()).entries

    expect(entry.level).toBe('info')
    expect(entry.src).toBe('app')
  })

  it('records no data as null rather than as an empty object', async () => {
    record({ event: 'run.reset', msg: 'reset' })

    expect((await settled()).entries[0].data).toBeNull()
  })

  it('stamps the entry with the current time when the caller gives none', async () => {
    const before = Date.now()

    record({ msg: 'x' })

    const { ts } = (await settled()).entries[0]
    expect(ts).toBeGreaterThanOrEqual(before)
  })

  it('never throws, whatever it is handed', () => {
    const exploding = {
      get level() {
        throw new Error('nope')
      },
    }
    const circular = {}
    circular.self = circular

    expect(() => record()).not.toThrow()
    expect(() => record(null)).not.toThrow()
    expect(() => record(exploding)).not.toThrow()
    expect(() => record({ msg: 'x', data: circular })).not.toThrow()
  })
})

describe('extension noise', () => {
  const SCHEMES = ['chrome-extension://', 'moz-extension://', 'safari-web-extension://']

  it('flags a capture whose message names any of the three schemes', async () => {
    for (const scheme of SCHEMES) {
      clearLog()
      record({ src: 'console', level: 'warn', msg: `injected at ${scheme}abcdef/inpage.js:1:2` })

      const [entry] = (await settled()).entries
      expect(entry.ext).toBe(true)
    }
  })

  it('flags a capture whose scheme is buried in the serialized data', async () => {
    for (const scheme of SCHEMES) {
      clearLog()
      record({
        src: 'window',
        level: 'error',
        event: 'window.unhandledrejection',
        msg: 'Could not establish connection',
        data: { reason: { stack: `at f (${scheme}nkbihfbeog/inpage.js:1:1)` } },
      })

      const [entry] = (await settled()).entries
      expect(entry.ext).toBe(true)
    }
  })

  it('never flags an app entry, whatever string it carries', async () => {
    record({ src: 'app', event: 'ui.note', msg: 'blocked chrome-extension:// requests' })
    record({ src: 'app', event: 'ui.note', msg: 'x', data: { url: 'moz-extension://a/b.js' } })

    const { entries } = await settled()
    expect(entries).toHaveLength(2)
    for (const entry of entries) expect(entry.ext).toBe(false)
  })

  it('never flags a relayed pipeline event, which the page did not write either', async () => {
    record({
      src: 'server',
      level: 'info',
      event: 'pipeline.run.error',
      msg: 'run failed (502): fetching chrome-extension://a/b.js is not a datasheet url',
      data: { event: 'run.error', error: 'moz-extension://a/b.js' },
    })

    const [entry] = (await settled()).entries
    expect(entry.src).toBe('server')
    expect(entry.ext).toBe(false)
  })

  it('leaves an ordinary capture unflagged', async () => {
    record({ src: 'console', level: 'info', msg: 'hello', data: { method: 'log', args: ['hello'] } })
    record({ src: 'window', level: 'error', msg: 'TypeError at http://127.0.0.1:5173/src/App.js' })

    const { entries } = await settled()
    expect(entries).toHaveLength(2)
    for (const entry of entries) expect(entry.ext).toBe(false)
  })

  it('decides once, at capture, and stores a plain boolean', async () => {
    record({ src: 'console', level: 'warn', msg: 'chrome-extension://a/b.js said something' })

    expect(typeof (await settled()).entries[0].ext).toBe('boolean')
  })

  it('reads a scheme anywhere in the string, not only at the start', () => {
    expect(isExtensionNoise('console', 'at f (chrome-extension://a/b.js:1:1)')).toBe(true)
    expect(isExtensionNoise('window', '', '{"u":"safari-web-extension://a"}')).toBe(true)
  })

  it('flags nothing from a source the page did not write', () => {
    expect(isExtensionNoise('app', 'chrome-extension://a/b.js')).toBe(false)
    expect(isExtensionNoise('', 'chrome-extension://a/b.js')).toBe(false)
  })

  it('survives a missing message or a missing serialization', () => {
    expect(isExtensionNoise('console')).toBe(false)
    expect(isExtensionNoise('console', null, undefined)).toBe(false)
  })
})

describe('the level helpers', () => {
  it('logEvent writes an app info line', async () => {
    logEvent('run.start', 'run r1 started', { id: 'r1' })

    const [entry] = (await settled()).entries
    expect(entry).toMatchObject({ level: 'info', src: 'app', event: 'run.start', data: { id: 'r1' } })
  })

  it('logWarn writes an app warn line', async () => {
    logWarn('run.done', 'fell back', { status: 'fallback' })

    expect((await settled()).entries[0].level).toBe('warn')
  })

  it('logError writes an app error line', async () => {
    logError('run.error', 'the service is down', { status: 0 })

    expect((await settled()).entries[0].level).toBe('error')
  })
})

describe('the sequence number', () => {
  it('starts at one in a fresh module', async () => {
    const fresh = await freshLog()

    fresh.record({ msg: 'first' })
    await Promise.resolve()

    expect(get(fresh.log).entries[0].seq).toBe(1)
  })

  it('increases by one per entry', async () => {
    record({ msg: 'a' })
    record({ msg: 'b' })
    record({ msg: 'c' })

    const seqs = (await settled()).entries.map((e) => e.seq)

    expect(seqs[1]).toBe(seqs[0] + 1)
    expect(seqs[2]).toBe(seqs[1] + 1)
  })

  it('is never reset by a clear, so two exports never reuse a number', async () => {
    record({ msg: 'a' })
    const first = (await settled()).entries[0].seq

    clearLog()
    record({ msg: 'b' })

    expect((await settled()).entries[0].seq).toBe(first + 1)
  })
})

describe('clearLog', () => {
  it('empties the buffer without waiting for a flush', async () => {
    record({ msg: 'a' })
    await settled()

    clearLog()

    expect(get(log).entries).toEqual([])
  })

  it('resets the dropped count, which described a buffer that no longer exists', async () => {
    for (let i = 0; i < LOG_CAPACITY + 5; i += 1) record({ msg: `#${i}` })
    expect((await settled()).dropped).toBe(5)

    clearLog()

    expect(get(log).dropped).toBe(0)
  })
})

describe('head eviction', () => {
  it('keeps the last thousand of a thousand and one, and counts the one', async () => {
    const fresh = await freshLog()

    for (let i = 0; i < 1001; i += 1) fresh.record({ msg: `#${i}` })
    await Promise.resolve()
    const state = get(fresh.log)

    expect(state.entries).toHaveLength(1000)
    expect(state.dropped).toBe(1)
    expect(state.entries[0].seq).toBe(2)
    expect(state.entries[999].seq).toBe(1001)
  })

  it('evicts on the byte budget before the count cap is reached', async () => {
    // Four capped arguments per entry is roughly 8 KB, so the 4 MiB budget bites
    // at about 517 entries -- well short of the thousand-entry cap.
    const chunk = 'x'.repeat(2000)
    for (let i = 0; i < 600; i += 1) {
      record({ event: 'big', msg: 'big', data: { a: chunk, b: chunk, c: chunk, d: chunk } })
    }

    const state = await settled()
    const held = state.entries.reduce((total, entry) => total + entry.bytes, 0)

    expect(state.entries.length).toBeLessThan(600)
    expect(state.entries.length).toBeLessThan(LOG_CAPACITY)
    expect(state.dropped).toBeGreaterThan(0)
    expect(held).toBeLessThanOrEqual(MAX_TOTAL_BYTES)
  })

  it('drops from the head, so the newest line is always kept', async () => {
    for (let i = 0; i < LOG_CAPACITY + 3; i += 1) record({ msg: `#${i}` })

    const { entries } = await settled()

    expect(entries[0].msg).toBe('#3')
    expect(entries[entries.length - 1].msg).toBe(`#${LOG_CAPACITY + 2}`)
  })
})

describe('truncation at capture time', () => {
  it('cuts one argument to 2 KB and says how much it lost', async () => {
    record({ msg: 'big', data: { note: 'y'.repeat(5000) } })

    const { note } = (await settled()).entries[0].data

    expect(note.startsWith('y'.repeat(2048))).toBe(true)
    expect(note).toMatch(/\[truncated 2952 chars\]$/)
  })

  it('leaves an argument that fits exactly as it was', async () => {
    record({ msg: 'fits', data: { note: 'y'.repeat(MAX_ARG_BYTES) } })

    expect((await settled()).entries[0].data.note).toBe('y'.repeat(MAX_ARG_BYTES))
  })

  it('cuts the message itself at the same limit', async () => {
    record({ msg: 'z'.repeat(5000) })

    expect((await settled()).entries[0].msg).toMatch(/\[truncated 2952 chars\]$/)
  })

  it('replaces a wide entry with a preview once it passes 8 KB', async () => {
    const chunk = 'x'.repeat(2000)
    record({ msg: 'wide', data: { a: chunk, b: chunk, c: chunk, d: chunk, e: chunk } })

    const { data } = (await settled()).entries[0]

    expect(data.truncated).toBe(true)
    expect(data.original_bytes).toBeGreaterThan(MAX_ENTRY_BYTES)
    expect(typeof data.preview).toBe('string')
    expect(data.preview).toMatch(/\[truncated \d+ chars\]$/)
  })

  it('keeps a truncated entry under the entry cap', async () => {
    const chunk = 'x'.repeat(2000)
    record({ msg: 'wide', data: { a: chunk, b: chunk, c: chunk, d: chunk, e: chunk } })

    const [entry] = (await settled()).entries

    expect(JSON.stringify(entry.data).length).toBeLessThan(MAX_ENTRY_BYTES)
  })
})

describe('redact', () => {
  it('replaces a board file with its size, which is the only useful part', () => {
    expect(redact('kicad_pcb', 'x'.repeat(200000))).toBe('[kicad_pcb: 200000 chars]')
  })

  it('reports a missing board file as zero rather than throwing', () => {
    expect(redact('kicad_pcb', null)).toBe('[kicad_pcb: 0 chars]')
  })

  it('hides anything whose key reads like a credential', () => {
    expect(redact('apiKey', 'sk-live-1')).toBe('[redacted]')
    expect(redact('GOOGLE_API_KEY', 'sk-live-1')).toBe('[redacted]')
    expect(redact('token', 'abc')).toBe('[redacted]')
    expect(redact('Secret', 'abc')).toBe('[redacted]')
    expect(redact('password', 'abc')).toBe('[redacted]')
  })

  it('leaves every other key alone', () => {
    expect(redact('intent', 'a 3v3 regulator')).toBe('a 3v3 regulator')
    expect(redact('parts', 3)).toBe(3)
  })

  it('runs before truncation, so the board file never reaches the buffer', async () => {
    record({ event: 'run.done', msg: 'done', data: { kicad_pcb: 'x'.repeat(200000), apiKey: 'sk' } })

    const [entry] = (await settled()).entries

    expect(entry.data.kicad_pcb).toBe('[kicad_pcb: 200000 chars]')
    expect(entry.data.apiKey).toBe('[redacted]')
    expect(entry.bytes).toBeLessThan(1024)
  })

  it('reaches a nested credential too', async () => {
    record({ msg: 'x', data: { request: { headers: { authToken: 'abc' } } } })

    expect((await settled()).entries[0].data.request.headers.authToken).toBe('[redacted]')
  })
})

describe('scrubText', () => {
  it('hides the signature on a signed datasheet URL, keeping the URL readable', () => {
    const url =
      'https://bucket.s3.amazonaws.com/ds/lm317.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc123def&X-Amz-Expires=900'

    const out = scrubText(url)

    expect(out).not.toContain('abc123def')
    expect(out).toBe(
      'https://bucket.s3.amazonaws.com/ds/lm317.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=[redacted]&X-Amz-Expires=900',
    )
  })

  it('leaves the harmless parameter beside a redacted one', () => {
    expect(scrubText('/api/parts?token=abc&page=2')).toBe('/api/parts?token=[redacted]&page=2')
  })

  it('hides an access key, a secret, a password and a credential by name', () => {
    expect(scrubText('?api-key=k1&client_secret=s1&password=p1&X-Amz-Credential=c1')).toBe(
      '?api-key=[redacted]&client_secret=[redacted]&password=[redacted]&X-Amz-Credential=[redacted]',
    )
  })

  it('hides a bearer token wherever it was stringified from', () => {
    expect(scrubText('authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig==')).toBe(
      'authorization: Bearer [redacted]',
    )
  })

  it('hides a credential that sits in the fragment rather than the query', () => {
    expect(scrubText('https://silkscreen.app/#access_token=xyz789&state=abc')).toBe(
      'https://silkscreen.app/#access_token=[redacted]&state=abc',
    )
  })

  it('leaves an ordinary fragment alone', () => {
    expect(scrubText('http://127.0.0.1:5173/#board')).toBe('http://127.0.0.1:5173/#board')
  })

  it('leaves a string carrying no credential exactly as it is', () => {
    expect(scrubText('placed 11 parts, 214.5 mm of wire')).toBe('placed 11 parts, 214.5 mm of wire')
    expect(scrubText('POST /generate?intent=a+3v3+regulator&time_limit_s=10')).toBe(
      'POST /generate?intent=a+3v3+regulator&time_limit_s=10',
    )
  })

  it('changes nothing on a second pass, so a doubly-scrubbed line is unharmed', () => {
    const once = scrubText('/ds?token=abc')
    expect(scrubText(once)).toBe(once)
  })

  it('answers a value that is not a string at all', () => {
    expect(scrubText(null)).toBe('')
    expect(scrubText(undefined)).toBe('')
    expect(scrubText(42)).toBe('42')
  })
})

describe('scrubbing on the way into the buffer', () => {
  it('scrubs a message no key-driven redaction could reach', async () => {
    record({ event: 'window.resource-error', msg: 'failed to load <img> /ds.png?sig=abc123' })

    const [entry] = (await settled()).entries

    expect(entry.msg).toBe('failed to load <img> /ds.png?sig=[redacted]')
  })

  it('scrubs every string it serializes, however deep', async () => {
    record({
      msg: 'x',
      data: { req: { url: '/v1/ds?X-Amz-Signature=abc123' }, tries: ['?token=t1'] },
    })

    const { data } = (await settled()).entries[0]

    expect(data.req.url).toBe('/v1/ds?X-Amz-Signature=[redacted]')
    expect(data.tries).toEqual(['?token=[redacted]'])
  })

  it('scrubs an error message and its stack', async () => {
    record({ msg: 'x', data: new Error('GET /ds?token=abc123 failed') })

    const { data } = (await settled()).entries[0]

    expect(data.message).toBe('GET /ds?token=[redacted] failed')
    expect(data.stack).not.toContain('abc123')
  })

  it('scrubs before truncating, so no credential survives behind the cut', async () => {
    const long = `?token=${'a'.repeat(MAX_ARG_BYTES * 2)}`
    record({ msg: 'x', data: { long } })

    const { data } = (await settled()).entries[0]

    expect(data.long).not.toContain('aaaa')
    expect(data.long).toBe('?token=[redacted]')
  })
})

describe('safeArg', () => {
  it('passes primitives through', () => {
    expect(safeArg(null)).toBeNull()
    expect(safeArg(true)).toBe(true)
    expect(safeArg(42)).toBe(42)
    expect(safeArg('hi')).toBe('hi')
  })

  it('marks undefined, which JSON has no way to hold', () => {
    expect(safeArg(undefined)).toBe('[undefined]')
  })

  it('spells out a number JSON would flatten to null', () => {
    expect(safeArg(Infinity)).toBe('Infinity')
    expect(safeArg(NaN)).toBe('NaN')
  })

  it('reduces an Error to its name, message and stack', () => {
    const out = safeArg(new TypeError('bad shape'))

    expect(out.name).toBe('TypeError')
    expect(out.message).toBe('bad shape')
    expect(out.stack).toContain('TypeError')
  })

  it('names a DOM node by tag and id', () => {
    expect(safeArg({ nodeType: 1, tagName: 'DIV', id: 'board' })).toBe('<div#board>')
    expect(safeArg({ nodeType: 1, tagName: 'BUTTON', id: '' })).toBe('<button>')
  })

  it('names a function', () => {
    function widget() {}
    expect(safeArg(widget)).toBe('[Function: widget]')
    expect(safeArg(() => {})).toBe('[Function: anonymous]')
  })

  it('stringifies a symbol and a bigint', () => {
    expect(safeArg(Symbol('tick'))).toBe('Symbol(tick)')
    expect(safeArg(10n)).toBe('10')
  })

  it('turns a Map into pairs and a Set into a list', () => {
    expect(safeArg(new Map([['a', 1], ['b', 2]]))).toEqual([['a', 1], ['b', 2]])
    expect(safeArg(new Set([1, 2, 3]))).toEqual([1, 2, 3])
  })

  it('caps a long list and says how much it left out', () => {
    const out = safeArg(Array.from({ length: 60 }, (_, i) => i))

    expect(out).toHaveLength(51)
    expect(out[50]).toBe('…[+10 more]')
  })

  it('caps a wide object the same way', () => {
    const wide = {}
    for (let i = 0; i < 60; i += 1) wide[`k${i}`] = i

    const out = safeArg(wide)

    expect(Object.keys(out)).toHaveLength(51)
    expect(out['…']).toBe('[+10 more]')
  })

  it('walks a shared reference twice, and marks only a value holding itself', () => {
    const shared = { ref: 'U1' }

    // Two properties pointing at one object is not a cycle, and reporting it as
    // one would hide half of every response that reuses a part record.
    expect(safeArg({ a: shared, b: shared })).toEqual({ a: { ref: 'U1' }, b: { ref: 'U1' } })

    const cyclic = { name: 'root' }
    cyclic.self = cyclic
    expect(safeArg(cyclic)).toEqual({ name: 'root', self: '[Circular]' })
  })

  it('marks a cycle instead of following it', () => {
    const node = { name: 'root' }
    node.self = node

    expect(safeArg(node)).toEqual({ name: 'root', self: '[Circular]' })
  })

  it('stops walking at three levels of nesting', () => {
    expect(safeArg({ a: { b: { c: { d: 1 } } } })).toEqual({ a: { b: { c: '[max depth]' } } })
  })

  it('marks a property whose getter throws, and keeps the rest', () => {
    const value = { ok: 1 }
    Object.defineProperty(value, 'boom', {
      get() {
        throw new Error('nope')
      },
      enumerable: true,
    })

    expect(safeArg(value)).toEqual({ ok: 1, boom: '[unserializable]' })
  })

  it('marks a value it cannot inspect at all', () => {
    const hostile = new Proxy(
      {},
      {
        ownKeys() {
          throw new Error('nope')
        },
      },
    )

    expect(safeArg(hostile)).toBe('[unserializable]')
  })

  it('cuts a long string wherever it finds one', () => {
    expect(safeArg({ a: { b: 'y'.repeat(5000) } }).a.b).toMatch(/\[truncated 2952 chars\]$/)
  })

  it('renders a Date as its ISO stamp', () => {
    expect(safeArg(new Date(Date.UTC(2026, 7, 29)))).toBe('2026-08-29T00:00:00.000Z')
  })
})

describe('batching', () => {
  it('notifies once for a burst of fifty appends', async () => {
    const seen = []
    const unsubscribe = log.subscribe((state) => seen.push(state.entries.length))

    for (let i = 0; i < 50; i += 1) record({ msg: `#${i}` })

    expect(seen).toHaveLength(1)
    await Promise.resolve()
    expect(seen).toHaveLength(2)
    expect(seen[1]).toBe(50)

    unsubscribe()
  })

  it('notifies again for the next burst', async () => {
    const seen = []
    const unsubscribe = log.subscribe((state) => seen.push(state.entries.length))

    record({ msg: 'a' })
    await Promise.resolve()
    record({ msg: 'b' })
    await Promise.resolve()

    expect(seen).toEqual([0, 1, 2])

    unsubscribe()
  })

  it('publishes what a subscriber logged on the next append, never in a loop', async () => {
    const fresh = await freshLog()
    const unsubscribe = fresh.log.subscribe(() => fresh.logEvent('subscriber', 'logged on notify'))

    fresh.record({ msg: 'first' })
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    // Reaching this line is half the assertion: a flush scheduled from inside a
    // notify would keep the microtask queue busy for as long as the test waits.
    expect(get(fresh.log).entries.map((e) => e.msg)).toEqual(['logged on notify', 'first'])

    // The line the subscriber wrote during that notify was buffered, not
    // dropped, and goes out with the next append from outside.
    fresh.record({ msg: 'second' })
    await Promise.resolve()

    expect(get(fresh.log).entries.map((e) => e.msg)).toEqual([
      'logged on notify',
      'first',
      'logged on notify',
      'second',
    ])

    unsubscribe()
  })

  it('hands out a fresh array, so no subscriber holds the live buffer', async () => {
    record({ msg: 'a' })
    const first = (await settled()).entries

    record({ msg: 'b' })
    await Promise.resolve()

    expect(first).toHaveLength(1)
  })
})

describe('run ids', () => {
  it('starts at r1 in a fresh module and counts up', async () => {
    const fresh = await freshLog()

    expect(fresh.runId()).toBe('')
    expect(fresh.nextRunId()).toBe('r1')
    expect(fresh.nextRunId()).toBe('r2')
    expect(fresh.runId()).toBe('r2')
  })

  it('stamps every entry with the current run', async () => {
    const id = nextRunId()

    record({ msg: 'during the run' })

    expect((await settled()).entries[0].run).toBe(id)
    expect(id).toMatch(/^r\d+$/)
  })

  it('lets a caller name the run itself, for a line that outlives it', async () => {
    nextRunId()

    record({ msg: 'late', run: 'r0' })

    expect((await settled()).entries[0].run).toBe('r0')
  })
})

describe('logMeta', () => {
  it('falls back to empty strings where there is no window', () => {
    expect(logMeta(NOW)).toEqual({
      app: 'silkscreen',
      href: '',
      ua: '',
      capacity: LOG_CAPACITY,
      dropped: 0,
      exported: '2026-08-29T18:15:04.000Z',
    })
  })

  it('reads the location and user agent at export time, not at import time', () => {
    globalThis.window = {
      location: { href: 'http://127.0.0.1:5173/#review' },
      navigator: { userAgent: 'Mozilla/5.0 (test)' },
    }
    try {
      const meta = logMeta(NOW)
      expect(meta.href).toBe('http://127.0.0.1:5173/#review')
      expect(meta.ua).toBe('Mozilla/5.0 (test)')
    } finally {
      delete globalThis.window
    }
  })

  it('scrubs a credential the page URL was opened with', () => {
    withHref('http://127.0.0.1:5173/?api_key=abc&intent=regulator', () => {
      const meta = logMeta(NOW)
      expect(meta.href).toBe('http://127.0.0.1:5173/?api_key=[redacted]&intent=regulator')
      expect(meta.href).not.toContain('abc')
    })
  })

  it('scrubs a credential the redirect left in the fragment', () => {
    withHref('http://127.0.0.1:5173/#access_token=xyz', () => {
      expect(logMeta(NOW).href).toBe('http://127.0.0.1:5173/#access_token=[redacted]')
    })
  })

  it('carries the scrubbed URL into both exports, not the raw one', () => {
    withHref('http://127.0.0.1:5173/?api_key=abc', () => {
      const header = toText([entryAt()], NOW).split('\n')[0]
      const first = JSON.parse(toNdjson([entryAt()], NOW).split('\n')[0])

      expect(header).toContain('http://127.0.0.1:5173/?api_key=[redacted]')
      expect(header).not.toContain('abc')
      expect(first.href).toBe('http://127.0.0.1:5173/?api_key=[redacted]')
      expect(first.href).not.toContain('abc')
    })
  })

  it('carries the dropped count the buffer is holding', async () => {
    for (let i = 0; i < LOG_CAPACITY + 2; i += 1) record({ msg: `#${i}` })
    await settled()

    expect(logMeta(NOW).dropped).toBe(2)
  })
})

describe('toText', () => {
  it('opens with two header lines naming the stamp, the counts and the capacity', () => {
    const lines = toText(
      [entryAt(), entryAt({ level: 'error' }), entryAt({ level: 'warn' })],
      NOW,
    ).split('\n')

    expect(lines[0]).toBe('# silkscreen log  exported 2026-08-29T18:15:04.000Z')
    expect(lines[1]).toBe('#  3 entries  1 error  1 warning  0 dropped  capacity 1000  times UTC')
  })

  it('writes one line per entry, in columns', () => {
    const line = toText([entryAt({ msg: 'started', data: { id: 'r1' } })], NOW).split('\n')[2]

    expect(line).toBe('18:15:00.250  INFO   app      r1    started  {"id":"r1"}')
  })

  it('leaves no trailing braces on an entry that carries no data', () => {
    const line = toText([entryAt({ msg: 'reset', data: null })], NOW).split('\n')[2]

    expect(line).toBe('18:15:00.250  INFO   app      r1    reset')
    expect(line.endsWith('reset')).toBe(true)
  })

  it('treats an empty data object as no data at all', () => {
    const line = toText([entryAt({ msg: 'reset', data: {} })], NOW).split('\n')[2]

    expect(line.endsWith('reset')).toBe(true)
  })

  it('strips %c directives and the style arguments answering them', () => {
    const entry = entryAt({
      src: 'console',
      event: '',
      msg: '%cstyled%c text',
      data: { method: 'log', args: ['%cstyled%c text', 'color:red', 'font-weight:bold', 'x'] },
    })

    const line = toText([entry], NOW).split('\n')[2]

    expect(line).toContain('styled text')
    expect(line).not.toContain('%c')
    expect(line).toContain('"args":["styled text","x"]')
  })

  it('leaves an app message containing %c alone', () => {
    const line = toText([entryAt({ msg: 'literal %c stays' })], NOW).split('\n')[2]

    expect(line).toContain('literal %c stays')
  })

  it("drops Svelte's doubled snapshot lines, and the header counts what is left", () => {
    const text = toText(
      [entryAt({ msg: 'kept' }), entryAt({ src: 'console', msg: '%c[snapshot]' })],
      NOW,
    )

    expect(text).not.toContain('snapshot')
    expect(text).toContain('1 entry')
    expect(text.split('\n').filter(Boolean)).toHaveLength(3)
  })

  it('flattens a newline in the message, which would split one entry over two lines', () => {
    const text = toText([entryAt({ msg: 'console_log_state\nSee https://svelte.dev/e/x' })], NOW)

    expect(text.split('\n').filter(Boolean)).toHaveLength(3)
    expect(text).toContain('console_log_state ¶ See https://svelte.dev/e/x')
  })

  it('drops a console snapshot line written without the style directive', () => {
    const text = toText([entryAt({ src: 'console', msg: '[snapshot] { ref: U1 }' })], NOW)

    expect(text).not.toContain('snapshot')
    expect(text).toContain('0 entries')
  })

  it('keeps an app line that merely mentions a snapshot part-way through', () => {
    const text = toText([entryAt({ msg: 'placements match the [snapshot] fixture' })], NOW)

    expect(text).toContain('the [snapshot] fixture')
    expect(text).toContain('1 entry')
  })

  it('names the location in the first header line when there is one', () => {
    globalThis.window = { location: { href: 'http://x/' }, navigator: { userAgent: 'UA' } }
    try {
      expect(toText([], NOW).split('\n')[0]).toBe(
        '# silkscreen log  exported 2026-08-29T18:15:04.000Z  http://x/',
      )
    } finally {
      delete globalThis.window
    }
  })

  it('exports the live buffer when handed nothing', async () => {
    logEvent('run.start', 'started a run')
    await settled()

    expect(toText(undefined, NOW)).toContain('started a run')
  })

  it('ends with a newline', () => {
    expect(toText([entryAt()], NOW).endsWith('\n')).toBe(true)
  })

  // The drawer hides extension noise; the file someone pastes into an issue
  // does not, and does not mention the flag either -- the text format is a
  // fixed set of columns and `ext` is not one of them.
  it('writes a flagged entry exactly as it writes any other', () => {
    const entry = entryAt({ src: 'console', event: '', msg: 'noise' })

    expect(toText([{ ...entry, ext: true }], NOW)).toBe(toText([entry], NOW))
    expect(toText([{ ...entry, ext: true }], NOW)).toContain('noise')
  })
})

describe('toNdjson', () => {
  function parsedLines(text) {
    return text
      .split('\n')
      .filter(Boolean)
      .map((line) => JSON.parse(line))
  }

  it('opens with a meta record', () => {
    const [meta] = parsedLines(toNdjson([entryAt()], NOW))

    expect(meta).toEqual({
      app: 'silkscreen',
      href: '',
      ua: '',
      capacity: 1000,
      dropped: 0,
      exported: '2026-08-29T18:15:04.000Z',
    })
  })

  it('writes one parseable object per entry', () => {
    const lines = parsedLines(toNdjson([entryAt(), entryAt({ seq: 2, level: 'error' })], NOW))

    expect(lines).toHaveLength(3)
    expect(lines[1].seq).toBe(1)
    expect(lines[2].level).toBe('error')
  })

  it('writes the timestamp as an ISO stamp with milliseconds', () => {
    const [, entry] = parsedLines(toNdjson([entryAt()], NOW))

    expect(entry.ts).toBe('2026-08-29T18:15:00.250Z')
    expect(entry.ts).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/)
  })

  it('never leaks the internal byte count', () => {
    const [, entry] = parsedLines(toNdjson([entryAt()], NOW))

    expect('bytes' in entry).toBe(false)
    expect(Object.keys(entry)).toEqual([
      'ts',
      'level',
      'src',
      'event',
      'msg',
      'run',
      'seq',
      'ext',
      'data',
    ])
  })

  it('carries a console line at info with the method that wrote it', () => {
    record({ src: 'console', level: 'info', msg: 'hello 1', data: { method: 'log', args: ['hello', 1] } })

    const [, entry] = parsedLines(toNdjson(undefined, NOW))

    expect(entry.level).toBe('info')
    expect(entry.src).toBe('console')
    expect(entry.data.method).toBe('log')
    expect(entry.data.args).toEqual(['hello', 1])
  })

  it('keeps a snapshot line the text export drops, being the lossless format', () => {
    const lines = parsedLines(toNdjson([entryAt({ msg: '%c[snapshot]' })], NOW))

    expect(lines[1].msg).toBe('%c[snapshot]')
  })

  it('writes absent data as null, so every line has the same shape', () => {
    const [, entry] = parsedLines(toNdjson([entryAt({ data: null })], NOW))

    expect(entry.data).toBeNull()
  })

  it('writes just the meta record for an empty buffer', () => {
    expect(parsedLines(toNdjson([], NOW))).toHaveLength(1)
  })

  it('carries the extension flag, which the text format has no column for', () => {
    const [, flagged] = parsedLines(toNdjson([entryAt({ ext: true })], NOW))
    const [, plain] = parsedLines(toNdjson([entryAt()], NOW))

    expect(flagged.ext).toBe(true)
    expect(plain.ext).toBe(false)
  })

  it('ends with a newline, so the file appends cleanly', () => {
    expect(toNdjson([entryAt()], NOW).endsWith('\n')).toBe(true)
  })
})

describe('logFilename', () => {
  it('stamps the name with the UTC date and time', () => {
    expect(logFilename('txt', Date.UTC(2026, 7, 29, 18, 15, 3))).toBe(
      'silkscreen-log-20260829-181503.txt',
    )
  })

  it('zero-pads every field', () => {
    expect(logFilename('ndjson', Date.UTC(2026, 0, 2, 3, 4, 5))).toBe(
      'silkscreen-log-20260102-030405.ndjson',
    )
  })

  it('falls back to .txt for anything that is not the NDJSON export', () => {
    expect(logFilename('csv', Date.UTC(2026, 7, 29, 18, 15, 3))).toBe(
      'silkscreen-log-20260829-181503.txt',
    )
  })
})

describe('suspectEngineBug', () => {
  // Every 400 the service can answer with, verbatim from service/app.py.
  const VALIDATION_400 = [
    "'intent' is required",
    "'datasheets' must be an object of {part: url}",
    'each datasheet value must be a non-empty URL string',
    "'ground' requires 'datasheets'",
    "'ground' supports at most 8 datasheets per request",
    'datasheet URL is not an http(s) URL',
    'datasheet URL is not allowed',
    "'time_limit_s' must be a number",
    'invalid Content-Length',
    'invalid JSON: Expecting value: line 1 column 1 (char 0)',
    'body must be a JSON object',
  ]

  it.each(VALIDATION_400)('clears a 400 that names a field: %s', (message) => {
    expect(suspectEngineBug(400, message)).toBe(false)
  })

  it('flags a 400 that names none of them', () => {
    expect(suspectEngineBug(400, "unsupported operand type(s) for +: 'int' and 'str'")).toBe(true)
    expect(suspectEngineBug(400, 'not enough values to unpack (expected 2, got 1)')).toBe(true)
    expect(suspectEngineBug(400, '')).toBe(true)
  })

  it("clears api.js's synthesized message, which names no field by construction", () => {
    expect(suspectEngineBug(400, 'request failed with status 400')).toBe(false)
  })

  it('still flags an engine message that reached the client as a 400', () => {
    expect(suspectEngineBug(400, 'cannot pack part U1')).toBe(true)
  })

  it('never flags anything that is not a 400', () => {
    expect(suspectEngineBug(500, 'internal error')).toBe(false)
    expect(suspectEngineBug(500, '')).toBe(false)
    expect(suspectEngineBug(502, 'GOOGLE_API_KEY is not set')).toBe(false)
    expect(suspectEngineBug(413, 'request body too large')).toBe(false)
    expect(suspectEngineBug(0, '')).toBe(false)
  })

  it('reads a status the transport handed over as a string', () => {
    expect(suspectEngineBug('400', 'boom')).toBe(true)
    expect(suspectEngineBug('500', 'boom')).toBe(false)
  })

  it('tolerates a missing message', () => {
    expect(suspectEngineBug(400, undefined)).toBe(true)
    expect(suspectEngineBug(400, null)).toBe(true)
  })
})

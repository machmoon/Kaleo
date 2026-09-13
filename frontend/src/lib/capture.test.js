import { afterEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'

const METHODS = ['log', 'warn', 'error', 'info', 'debug']

// capture.js holds its wrappers, its unhooks and its reentrancy flag at module
// scope, so every test takes a fresh module graph -- and with it a fresh log
// buffer, which is the same singleton one layer down.
async function fresh() {
  vi.resetModules()
  const logModule = await import('./log.js')
  const capture = await import('./capture.js')
  return { capture, logModule }
}

/** A capture module whose record() is `impl`. Most of these tests are about
    what capture.js hands over, not about what the buffer does with it -- and a
    record() that throws, or that logs, is only reachable from here. */
async function freshWith(impl) {
  vi.resetModules()
  vi.doMock('./log.js', async () => {
    const actual = await vi.importActual('./log.js')
    return { ...actual, record: (entry) => impl(entry) }
  })
  return import('./capture.js')
}

/** The common case: every entry capture.js records, in order. */
async function freshCollecting() {
  const entries = []
  const capture = await freshWith((entry) => {
    entries.push(entry)
  })
  return { capture, entries }
}

/** A console-shaped object whose methods record the call and never print. */
function fakeConsole(onCall = () => {}) {
  const calls = []
  const target = {}
  for (const method of METHODS) {
    target[method] = (...args) => {
      calls.push([method, ...args])
      onCall(method, args)
    }
  }
  return { target, calls, originals: { ...target } }
}

function isCapture(options) {
  if (options === true) return true
  return typeof options === 'object' && options !== null && options.capture === true
}

/** An EventTarget-shaped object that remembers every listener pair, so both the
    registration and the removal can be asserted without a DOM. */
function fakeEventTarget() {
  const added = []
  const removed = []
  return {
    added,
    removed,
    addEventListener(type, handler, options) {
      added.push({ type, handler, options })
    },
    removeEventListener(type, handler, options) {
      removed.push({ type, handler, options })
    },
    /** Runs the listeners registered for `type` in one phase. */
    dispatch(type, event, phase = 'bubble') {
      for (const listener of added) {
        if (listener.type === type && isCapture(listener.options) === (phase === 'capture')) {
          listener.handler(event)
        }
      }
    },
  }
}

function errorEvent(fields = {}) {
  return { message: 'boom', filename: 'http://localhost/app.js', lineno: 12, colno: 4, ...fields }
}

// The buffer batches appends onto a microtask, so every store read waits one.
async function settled(logModule) {
  await Promise.resolve()
  return get(logModule.log).entries
}

afterEach(() => {
  vi.doUnmock('./log.js')
  vi.resetModules()
  vi.restoreAllMocks()
})

describe('hookConsole', () => {
  it('names the five methods it hooks', async () => {
    const { capture } = await freshCollecting()
    expect(capture.CONSOLE_METHODS).toEqual(METHODS)
    expect(capture.SENTINEL).toBe('__silkscreen')
  })

  it('replaces every method with a stamped wrapper carrying the original', async () => {
    const { capture } = await freshCollecting()
    const { target, originals } = fakeConsole()

    capture.hookConsole(target)

    for (const method of METHODS) {
      expect(target[method]).not.toBe(originals[method])
      expect(target[method][capture.SENTINEL]).toBe(originals[method])
    }
  })

  it('records one console entry with the raw arguments kept in data', async () => {
    const { capture, entries } = await freshCollecting()
    const { target } = fakeConsole()
    capture.hookConsole(target)

    target.log('a', 1)

    // No message: the preview belongs to record(), which builds it from the
    // serialized arguments rather than from these.
    expect(entries).toEqual([
      {
        level: 'info',
        src: 'console',
        event: '',
        msg: '',
        data: { method: 'log', args: ['a', 1] },
      },
    ])
  })

  it('runs the original last, after the entry is recorded', async () => {
    const order = []
    const capture = await freshWith(() => order.push('record'))
    const { target } = fakeConsole(() => order.push('original'))
    capture.hookConsole(target)

    target.error('down')

    expect(order).toEqual(['record', 'original'])
  })

  it('maps console.log onto info and lets every other method name its level', async () => {
    const { capture, entries } = await freshCollecting()
    const { target } = fakeConsole()
    capture.hookConsole(target)

    for (const method of METHODS) target[method]('x')

    expect(entries.map((entry) => [entry.data.method, entry.level])).toEqual([
      ['log', 'info'],
      ['warn', 'warn'],
      ['error', 'error'],
      ['info', 'info'],
      ['debug', 'debug'],
    ])
  })

  it('skips a method the target does not have rather than inventing one', async () => {
    const { capture } = await freshCollecting()
    const target = { log: () => {} }

    expect(() => capture.hookConsole(target)).not.toThrow()
    expect(Object.keys(target)).toEqual(['log'])
    expect(target.log[capture.SENTINEL]).toBeTypeOf('function')
  })

  it('leaves its own wrappers alone on a second hook, and restores by identity', async () => {
    const { capture } = await freshCollecting()
    const { target, originals } = fakeConsole()

    const unhook = capture.hookConsole(target)
    const wrappers = { ...target }
    capture.hookConsole(target)
    expect(target.log).toBe(wrappers.log)

    unhook()
    for (const method of METHODS) expect(target[method]).toBe(originals[method])
  })

  it('records nothing for a console call made from inside record()', async () => {
    const entries = []
    let hooked = null
    const capture = await freshWith((entry) => {
      entries.push(entry)
      hooked.warn('from record')
    })
    const { target, calls } = fakeConsole()
    hooked = target
    capture.hookConsole(target)

    target.log('outer')

    expect(entries).toHaveLength(1)
    expect(entries[0].data.args).toEqual(['outer'])
    // The reentrant call is not recorded, but it still reaches the console.
    expect(calls).toEqual([
      ['warn', 'from record'],
      ['log', 'outer'],
    ])
  })

  it('swallows a record() that throws and still runs the original', async () => {
    const capture = await freshWith(() => {
      throw new Error('buffer is on fire')
    })
    const { target, calls } = fakeConsole()
    capture.hookConsole(target)

    expect(() => target.log('still printed')).not.toThrow()
    expect(calls).toEqual([['log', 'still printed']])
  })
})

describe('hookErrors', () => {
  it('registers the bubbling error, capture-phase error and rejection listeners', async () => {
    const { capture } = await freshCollecting()
    const target = fakeEventTarget()

    const unhook = capture.hookErrors(target)

    expect(target.added.map((l) => [l.type, isCapture(l.options)])).toEqual([
      ['error', false],
      ['error', true],
      ['unhandledrejection', false],
    ])

    unhook()
    expect(target.removed.map((l) => [l.type, isCapture(l.options)])).toEqual(
      target.added.map((l) => [l.type, isCapture(l.options)]),
    )
    expect(target.removed.map((l) => l.handler)).toEqual(target.added.map((l) => l.handler))
  })

  it('records an uncaught script error with its location and stack', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)
    const error = new Error('boom')
    error.stack = 'Error: boom\n    at app.js:12:4'

    target.dispatch('error', errorEvent({ error }))

    expect(entries).toEqual([
      {
        level: 'error',
        src: 'window',
        event: 'window.error',
        msg: 'boom',
        data: {
          message: 'boom',
          filename: 'http://localhost/app.js',
          lineno: 12,
          colno: 4,
          stack: 'Error: boom\n    at app.js:12:4',
        },
      },
    ])
  })

  it('records a resource failure from the capture phase, its only listener', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    target.dispatch(
      'error',
      { target: { tagName: 'IMG', src: 'http://localhost/missing.png' } },
      'capture',
    )

    expect(entries).toHaveLength(1)
    expect(entries[0].event).toBe('window.resource-error')
    expect(entries[0].data).toEqual({ tag: 'img', url: 'http://localhost/missing.png' })
    expect(entries[0].msg).toBe('failed to load <img> http://localhost/missing.png')
  })

  it('reads a stylesheet failure from href, the attribute a link element uses', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    const link = { tagName: 'LINK', href: 'http://localhost/app.css' }
    target.dispatch('error', { target: link }, 'capture')

    expect(entries[0].data).toEqual({ tag: 'link', url: 'http://localhost/app.css' })
  })

  it('records one entry for the event both listeners see', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)
    const event = errorEvent()

    target.dispatch('error', event, 'capture')
    target.dispatch('error', event)

    expect(entries).toHaveLength(1)
  })

  it('labels the cross-origin error the browser redacted', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    target.dispatch('error', { message: 'Script error.', filename: '', lineno: 0, colno: 0 })

    expect(entries[0].data.crossOrigin).toBe(true)
    expect(entries[0].msg).toContain('redacted')
  })

  it('leaves crossOrigin off an error the browser did report', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    target.dispatch('error', errorEvent())

    expect(entries[0].data.crossOrigin).toBeUndefined()
  })

  it('records a rejected Error, handing the reason to the serializer intact', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)
    const reason = new TypeError('fetch failed')

    target.dispatch('unhandledrejection', { reason })

    expect(entries[0].event).toBe('window.unhandledrejection')
    expect(entries[0].level).toBe('error')
    expect(entries[0].msg).toBe('unhandled rejection: TypeError: fetch failed')
    expect(entries[0].data.reason).toBe(reason)
  })

  it('records a rejection whose reason is not an Error at all', async () => {
    const { capture, entries } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    target.dispatch('unhandledrejection', { reason: 'nope' })

    expect(entries[0].msg).toBe('unhandled rejection: nope')
    expect(entries[0].data).toEqual({ reason: 'nope' })
  })

  it('never marks a rejection handled', async () => {
    const { capture } = await freshCollecting()
    const target = fakeEventTarget()
    capture.hookErrors(target)
    const preventDefault = vi.fn()

    target.dispatch('unhandledrejection', { reason: new Error('boom'), preventDefault })

    expect(preventDefault).not.toHaveBeenCalled()
  })

  it('returns a callable unhook for a target that takes no listeners', async () => {
    const { capture } = await freshCollecting()

    expect(() => capture.hookErrors({})()).not.toThrow()
    expect(() => capture.hookErrors(null)()).not.toThrow()
  })
})

describe('install', () => {
  it('hooks both targets once and puts both back', async () => {
    const { capture } = await freshCollecting()
    const { target: consoleTarget, originals } = fakeConsole()
    const errorTarget = fakeEventTarget()

    capture.install({ console: consoleTarget, target: errorTarget })
    const wrappers = { ...consoleTarget }
    capture.install({ console: consoleTarget, target: errorTarget })

    expect(consoleTarget.log).toBe(wrappers.log)
    expect(errorTarget.added).toHaveLength(3)

    capture.uninstall()

    for (const method of METHODS) expect(consoleTarget[method]).toBe(originals[method])
    expect(errorTarget.removed).toHaveLength(3)
  })

  it('does nothing when uninstall runs with nothing installed', async () => {
    const { capture } = await freshCollecting()

    expect(() => capture.uninstall()).not.toThrow()
  })

  it('hooks the console only when install() runs with no DOM around it', async () => {
    const { capture } = await fresh()
    const before = {}
    for (const method of METHODS) before[method] = console[method]
    // globalThis has no addEventListener in node; standing one up proves the
    // window hooks are skipped by the DOM guard, not by a missing API.
    const listeners = []
    const hadListener = 'addEventListener' in globalThis
    globalThis.addEventListener = (...args) => listeners.push(args)

    try {
      capture.install()
      for (const method of METHODS) expect(console[method][capture.SENTINEL]).toBe(before[method])
      expect(listeners).toEqual([])

      capture.uninstall()
      for (const method of METHODS) expect(console[method]).toBe(before[method])
    } finally {
      // Restored here as well as by uninstall: a failed expectation above must
      // not leave the runner's own console wrapped.
      for (const method of METHODS) console[method] = before[method]
      if (!hadListener) delete globalThis.addEventListener
    }
  })
})

describe('through the real buffer', () => {
  it('lands a console call in the log store, serialized', async () => {
    const { capture, logModule } = await fresh()
    const { target } = fakeConsole()
    capture.hookConsole(target)

    target.warn('board', { ref: 'U1', apiKey: 'secret-value' })

    const entries = await settled(logModule)
    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({ level: 'warn', src: 'console', event: '' })
    expect(entries[0].data).toEqual({
      method: 'warn',
      args: ['board', { ref: 'U1', apiKey: '[redacted]' }],
    })
  })

  it('previews the serialized arguments and cuts the line, keeping them whole', async () => {
    const { capture, logModule } = await fresh()
    const { target } = fakeConsole()
    capture.hookConsole(target)

    target.log({ ref: 'U1' })
    target.log('x'.repeat(500))

    const entries = await settled(logModule)
    expect(entries[0].msg).toBe('{"ref":"U1"}')
    expect(entries[1].msg).toBe(`${'x'.repeat(200)}…`)
    expect(entries[1].data.args[0]).toHaveLength(500)
  })

  it('redacts the preview line, which both exports write verbatim', async () => {
    const { capture, logModule } = await fresh()
    const { target } = fakeConsole()
    capture.hookConsole(target)

    target.log('config', { apiKey: 'sk-123' })

    const [entry] = await settled(logModule)
    expect(entry.msg).toBe('config {"apiKey":"[redacted]"}')

    const text = logModule.toText()
    const ndjson = logModule.toNdjson()
    for (const written of [entry.msg, text, ndjson]) {
      expect(written).toContain('[redacted]')
      expect(written).not.toContain('sk-123')
    }
  })

  it('scrubs the signature out of a failed resource URL, everywhere it is written', async () => {
    const { capture, logModule } = await fresh()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    const node = { tagName: 'IMG', src: 'https://cdn.example.com/ds.png?X-Amz-Signature=abc123' }
    target.dispatch('error', { target: node }, 'capture')

    const [entry] = await settled(logModule)
    expect(entry.msg).toBe(
      'failed to load <img> https://cdn.example.com/ds.png?X-Amz-Signature=[redacted]',
    )
    expect(entry.data.url).toBe('https://cdn.example.com/ds.png?X-Amz-Signature=[redacted]')
    for (const written of [entry.msg, entry.data.url, logModule.toText(), logModule.toNdjson()]) {
      expect(written).toContain('[redacted]')
      expect(written).not.toContain('abc123')
    }
  })

  it('scrubs a token out of a rejection reason, in the message and in the data', async () => {
    const { capture, logModule } = await fresh()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    const reason = new Error('GET https://api.example.com/v1/ds?token=xyz789 failed')
    target.dispatch('unhandledrejection', { reason })

    const [entry] = await settled(logModule)
    expect(entry.msg).toBe(
      'unhandled rejection: Error: GET https://api.example.com/v1/ds?token=[redacted] failed',
    )
    expect(entry.data.reason.message).toContain('token=[redacted]')
    for (const written of [entry.msg, logModule.toText(), logModule.toNdjson()]) {
      expect(written).toContain('[redacted]')
      expect(written).not.toContain('xyz789')
    }
  })

  it('lands a rejected Error in the store as name, message and stack', async () => {
    const { capture, logModule } = await fresh()
    const target = fakeEventTarget()
    capture.hookErrors(target)

    target.dispatch('unhandledrejection', { reason: new RangeError('too many parts') })

    const entries = await settled(logModule)
    expect(entries).toHaveLength(1)
    expect(entries[0].data.reason).toMatchObject({ name: 'RangeError', message: 'too many parts' })
    expect(entries[0].data.reason.stack).toContain('RangeError')
  })
})

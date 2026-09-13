// The hooks that feed the buffer in log.js: the five console methods, plus the
// two window failure channels the app has no other way to see. Everything here
// wraps something the page already owns, so every install is reversible and the
// original always runs last -- a debug console that swallows a console.error is
// worse than no debug console at all. Every target is injectable, which is what
// makes the whole file testable in node with no DOM anywhere.

import { record } from './log.js'

/** Stamped on every wrapper we install, carrying the function it replaced: it
    makes a second install a no-op and lets uninstall prove it is putting back a
    real original rather than someone else's wrapper. */
export const SENTINEL = '__silkscreen'

export const CONSOLE_METHODS = ['log', 'warn', 'error', 'info', 'debug']

/** console.log is the one method that names no level; the rest name their own. */
const LEVEL_BY_METHOD = {
  log: 'info',
  warn: 'warn',
  error: 'error',
  info: 'info',
  debug: 'debug',
}

// Module scope on purpose: the guard has to hold across every hooked target at
// once, and the unhooks have to outlive the call that installed them.
let capturing = false
let consoleUnhook = null
let errorsUnhook = null

function noop() {}

function isBrowser() {
  return typeof window !== 'undefined' && typeof document !== 'undefined'
}

// ---------------------------------------------------------------- preview

/** A rejection reason as a short label. Deliberately not JSON: record() scrubs
    a credential out of free text in `msg`, but only its key-driven redact()
    reaches a secret named by its key, and that runs over `data` alone -- so a
    raw value must never be stringified into a message. The reason itself
    reaches the buffer through `data`, where the serializer redacts it. */
function describeReason(value) {
  if (typeof value === 'string') return value
  if (value === null) return 'null'
  if (value === undefined) return 'undefined'
  if (value instanceof Error) return `${value.name}: ${value.message}`
  if (typeof value === 'object') return Object.prototype.toString.call(value)
  return String(value)
}

/** Runs `fn` with the reentrancy flag raised, so a console call made from
    inside record() (or from a store subscriber it wakes) records nothing, and
    swallows anything it throws -- the caller's own line still has to go out. */
function guarded(fn) {
  if (capturing) return
  capturing = true
  try {
    fn()
  } catch {
    // Deliberate: a logger that throws would take the line it was logging with it.
  } finally {
    capturing = false
  }
}

// ---------------------------------------------------------------- console

function wrapMethod(method, callOriginal) {
  const level = LEVEL_BY_METHOD[method] || 'info'
  return function silkscreenConsole(...args) {
    guarded(() => {
      // Raw arguments, and no message: record() runs safeArg over `data`
      // synchronously, so the serializer -- with its redaction, truncation and
      // depth cap -- stays in exactly one place and no live proxy ever reaches
      // the buffer. The preview line is built there too, from the serialized
      // copy, because one taken from these arguments would never be redacted.
      record({
        level,
        src: 'console',
        event: '',
        msg: '',
        data: { method, args },
      })
    })
    return callOriginal(...args)
  }
}

/** Replaces the console methods on `target` with recording wrappers. Returns
    the function that puts the originals back; installing twice is harmless. */
export function hookConsole(target = typeof console !== 'undefined' ? console : null) {
  if (!target) return noop
  const hooked = []
  for (const method of CONSOLE_METHODS) {
    const original = target[method]
    // A method the target does not have is skipped rather than invented, and
    // one of ours is left alone -- that is what makes a second install a no-op.
    if (typeof original !== 'function' || original[SENTINEL]) continue
    const wrapper = wrapMethod(method, original.bind(target))
    wrapper[SENTINEL] = original
    target[method] = wrapper
    hooked.push({ method, original, wrapper })
  }
  if (!hooked.length) return noop
  return function unhookConsole() {
    for (const { method, original, wrapper } of hooked) {
      // Only our own wrapper is replaced: if something else wrapped us in turn,
      // restoring would silently delete their hook.
      if (target[method] === wrapper) target[method] = original
    }
  }
}

// ---------------------------------------------------------------- window

function isErrorEvent(event) {
  if (typeof ErrorEvent === 'function' && event instanceof ErrorEvent) return true
  // Duck-typed fallback: node has no ErrorEvent constructor to compare against,
  // and a resource failure is a plain Event, which carries no `message`.
  return typeof event === 'object' && event !== null && 'message' in event
}

function finiteOr(value, fallback = 0) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function recordScriptError(event) {
  const message = String(event.message ?? '')
  const filename = String(event.filename ?? '')
  const stack = event.error && event.error.stack ? String(event.error.stack) : ''
  // The browser reports a cross-origin script's failure as a bare
  // "Script error." with no file, line, or error object. Saying so is the only
  // useful thing left to record, and stops the empty entry reading as our bug.
  const crossOrigin = message.startsWith('Script error') && !filename
  record({
    level: 'error',
    src: 'window',
    event: 'window.error',
    msg: crossOrigin
      ? 'Script error. -- the browser redacted a cross-origin error (no message, file, or line)'
      : message || 'uncaught error',
    data: {
      message,
      filename,
      lineno: finiteOr(event.lineno),
      colno: finiteOr(event.colno),
      stack,
      ...(crossOrigin ? { crossOrigin: true } : {}),
    },
  })
}

function recordResourceError(event) {
  const node = event.target || event.srcElement || null
  const tag = node && typeof node.tagName === 'string' ? node.tagName.toLowerCase() : ''
  const url = String((node && (node.src || node.href)) || '')
  record({
    level: 'error',
    src: 'window',
    event: 'window.resource-error',
    msg: `failed to load ${tag ? `<${tag}>` : 'resource'}${url ? ` ${url}` : ''}`,
    data: { tag, url },
  })
}

/** Listens for the two window failure channels on `target`. Returns the
    function that removes every listener it added. */
export function hookErrors(target = globalThis) {
  if (!target || typeof target.addEventListener !== 'function') return noop

  // A script error is dispatched at the window itself, so both the bubbling and
  // the capture-phase listener see the same event object; this records it once.
  const seen = new WeakSet()

  function handleError(event) {
    if (!event || typeof event !== 'object' || seen.has(event)) return
    seen.add(event)
    guarded(() => {
      if (isErrorEvent(event)) recordScriptError(event)
      else recordResourceError(event)
    })
  }

  function handleRejection(event) {
    guarded(() => {
      const reason = event ? event.reason : undefined
      record({
        level: 'error',
        src: 'window',
        event: 'window.unhandledrejection',
        msg: `unhandled rejection: ${describeReason(reason)}`,
        // The raw reason: record()'s serializer is Error-aware and keeps the
        // name, message and stack.
        data: { reason },
      })
    })
    // Deliberately no preventDefault(): the browser's own unhandled-rejection
    // report carries the real stack, and marking the event handled would hide
    // the failure this hook exists to surface.
  }

  const onError = (event) => handleError(event)
  // Resource failures (an <img> or <script> that 404s) fire on the element and
  // do not bubble, so only a capture-phase listener on window ever sees them.
  const onErrorCapture = (event) => handleError(event)
  const onRejection = (event) => handleRejection(event)

  target.addEventListener('error', onError)
  target.addEventListener('error', onErrorCapture, true)
  target.addEventListener('unhandledrejection', onRejection)

  return function unhookErrors() {
    target.removeEventListener('error', onError)
    target.removeEventListener('error', onErrorCapture, true)
    target.removeEventListener('unhandledrejection', onRejection)
  }
}

// ---------------------------------------------------------------- lifecycle

/** Installs both hooks once. `console` and `target` are injectable; the window
    hooks are skipped when no target is given and there is no DOM, which is what
    keeps the node test runner (and a bare `install()` in it) console-only. */
export function install({ console: consoleTarget, target } = {}) {
  const consoleArg =
    consoleTarget === undefined ? (typeof console !== 'undefined' ? console : null) : consoleTarget
  const errorArg = target === undefined ? (isBrowser() ? globalThis : null) : target
  if (!consoleUnhook && consoleArg) consoleUnhook = hookConsole(consoleArg)
  if (!errorsUnhook && errorArg) errorsUnhook = hookErrors(errorArg)
  return uninstall
}

/** Puts everything back. Safe to call when nothing is installed. */
export function uninstall() {
  const unhooks = [consoleUnhook, errorsUnhook]
  consoleUnhook = null
  errorsUnhook = null
  for (const unhook of unhooks) {
    try {
      if (unhook) unhook()
    } catch {
      // One unhook that throws must not strand the other.
    }
  }
}

// Installed here, at module evaluation, rather than called from main.js: import
// hoisting would run an explicit install() only after every other module (App
// included) had already evaluated, and the lines worth having are the early
// ones. The DOM guard keeps the hook out of the node test runner, and the
// sentinel keeps a re-evaluated module (HMR) from wrapping its own wrappers.
if (isBrowser() && !(typeof console !== 'undefined' && console.log && console.log[SENTINEL])) {
  install()
}

import.meta.hot?.dispose(uninstall)

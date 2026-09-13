// The buffer behind the debug console, and the two formats it exports. Pure and
// node-testable: the reactive holder is a svelte/store writable (a `.svelte.js`
// rune module cannot be imported by vitest, which runs without the Svelte
// plugin by design), and nothing here reads window or document at module scope.
// capture.js owns the console and window hooks; this file owns the buffer.

import { untrack } from 'svelte'
import { writable } from 'svelte/store'
import { formatCount } from './format.js'

export const LOG_CAPACITY = 1000
export const MAX_ARG_BYTES = 2048
export const MAX_ENTRY_BYTES = 8192
export const MAX_TOTAL_BYTES = 4 * 1024 * 1024

export const LOG_TEXT_MIME = 'text/plain;charset=utf-8'
export const LOG_NDJSON_MIME = 'application/x-ndjson'

/** console.log lands on info; there is no separate trace level. */
export const LEVELS = ['error', 'warn', 'info', 'debug']
/** app and server are lines we wrote — the client's own and the pipeline's,
    relayed from the stream; console and window are captures from the page. */
export const SOURCES = ['app', 'server', 'console', 'window']

/** Longest list safeArg keeps; whatever is past it becomes one marker element. */
const MAX_ITEMS = 50
/** Containers nested deeper than this are marked rather than walked. */
const MAX_DEPTH = 3
const SECRET_KEY = /key|token|secret|password/i
/** A query parameter whose value is a credential, matched wherever it sits in a
    string: a signed datasheet URL arrives as free text (a resource-error url, an
    ApiError message, a rejection reason), where the key-driven redact() above
    cannot reach it. The name only has to contain one of the words, because
    `X-Amz-Signature`, `access_token` and `api-key` are all the same secret. The
    value runs to the next separator, so the path and the harmless parameters
    around it stay readable -- a scrubbed line is still worth reading. `#` opens
    a parameter as well as `?` and `&`, because an OAuth implicit-grant redirect
    puts the token in the fragment (`#access_token=...`) and never in the query. */
const SECRET_PARAM =
  /([?&#][^?&=\s]*(?:key|token|secret|password|signature|sig|auth|credential)[^?&=\s]*=)[^&\s#"'<>]*/gi
/** An `Authorization: Bearer …` value, wherever it was stringified from. */
const BEARER = /\bBearer\s+[A-Za-z0-9._~+\/-]+=*/g
/** The three schemes a browser extension's own code is served from. An injected
    script (MetaMask's inpage.js is the one everyone meets) warns and rejects
    from inside the page, so its output lands in this buffer looking like ours
    -- but its stack, its resource urls and its error messages all name one of
    these. No `g` flag: a shared regex carrying lastIndex between entries would
    start missing every other match. */
const EXTENSION_URL = /(?:chrome|moz|safari-web)-extension:\/\//
const UNSERIALIZABLE = '[unserializable]'
/** Svelte's dev build logs proxies as a doubled `%c[snapshot]` pair, and it
    always leads the line. Anchored on purpose: an app message that merely
    mentions the word is a line someone wrote, and it belongs in the export. */
const SNAPSHOT = /^(?:%c)?\[snapshot\]/

/** The drawer shows one line per entry, so a console entry's `msg` is a
    preview; the arguments themselves are kept whole in `data.args`. */
const MAX_MSG_CHARS = 200

const ENCODER = typeof TextEncoder === 'function' ? new TextEncoder() : null

// Captured at module evaluation, so a test's fake timers cannot defeat batching
// and a caller cannot be surprised by a flush that never arrives.
const schedule =
  typeof queueMicrotask === 'function'
    ? queueMicrotask.bind(globalThis)
    : (fn) => {
        Promise.resolve().then(fn)
      }

let entries = []
let dropped = 0
let totalBytes = 0
let seq = 0
let pending = false
let notifying = false

let runCounter = 0
let currentRun = ''

export const log = writable({ entries: [], dropped: 0 })

// ---------------------------------------------------------------- run ids

/** Mints the next client-side run id. The service returns none, and a run has
    to be nameable before its first log line, so the client owns the counter. */
export function nextRunId() {
  runCounter += 1
  currentRun = `r${runCounter}`
  return currentRun
}

/** The id record() stamps from here on; '' before the first run. */
export function runId() {
  return currentRun
}

// ---------------------------------------------------------------- capture

/** Key-driven substitution, applied before a value is walked or truncated, so
    the reported size is the real one and no secret is ever serialized. */
export function redact(key, value) {
  const name = String(key)
  if (name === 'kicad_pcb') return `[kicad_pcb: ${String(value ?? '').length} chars]`
  if (SECRET_KEY.test(name)) return '[redacted]'
  return value
}

/** Value-driven substitution over free text, the half redact() cannot do: a
    credential that arrives inside a string rather than under a name of its own.
    Pure, idempotent, and applied to every string an entry carries -- `msg` and
    every serialized value alike -- so no caller has to remember it. */
export function scrubText(text) {
  const value = String(text ?? '')
  if (!value) return value
  return value.replace(SECRET_PARAM, '$1[redacted]').replace(BEARER, 'Bearer [redacted]')
}

/** Whether an entry is a browser extension talking to itself through our page.
    Pure, and deliberately narrow: only a capture from the page can be flagged,
    because an app line is ours by definition and one that merely quotes an
    extension url -- a triage note, this file's own tests -- is still ours. */
export function isExtensionNoise(src, msg, json) {
  if (src !== 'console' && src !== 'window') return false
  return EXTENSION_URL.test(String(msg ?? '')) || EXTENSION_URL.test(String(json ?? ''))
}

/** One value, reduced to something JSON can hold and a human can read. Never
    throws: an argument that resists serialization is worth less than the line. */
export function safeArg(value, depth = 0, seen = new WeakSet()) {
  try {
    return walk(value, depth, seen)
  } catch {
    return UNSERIALIZABLE
  }
}

function walk(value, depth, seen) {
  if (value === null) return null
  if (value === undefined) return '[undefined]'

  const type = typeof value
  if (type === 'boolean') return value
  if (type === 'number') return Number.isFinite(value) ? value : String(value)
  if (type === 'string') return scrubClip(value, MAX_ARG_BYTES)
  if (type === 'bigint' || type === 'symbol') return String(value)
  if (type === 'function') return `[Function: ${value.name || 'anonymous'}]`

  const tag = Object.prototype.toString.call(value)
  if (tag === '[object Error]' || value instanceof Error) {
    return {
      name: String(value.name),
      message: scrubClip(String(value.message), MAX_ARG_BYTES),
      stack: scrubClip(String(value.stack ?? ''), MAX_ARG_BYTES),
    }
  }
  if (tag === '[object Date]') {
    return Number.isFinite(value.getTime()) ? value.toISOString() : 'Invalid Date'
  }
  // Duck-typed rather than `instanceof Element`: this module is imported by the
  // node test run, where no DOM constructor exists to compare against.
  if (typeof value.nodeType === 'number' && typeof value.tagName === 'string') {
    const id = String(value.id || '')
    return `<${value.tagName.toLowerCase()}${id ? `#${id}` : ''}>`
  }

  if (depth >= MAX_DEPTH) return '[max depth]'
  if (seen.has(value)) return '[Circular]'
  // `seen` holds the branch being walked, not every value ever walked: the mark
  // comes off on the way out, so two properties pointing at the same object are
  // both serialized and only a value that contains itself reads as circular.
  seen.add(value)
  try {
    if (Array.isArray(value)) return capped(value.slice(0, MAX_ITEMS), value.length, depth, seen)
    if (value instanceof Map) return capped(take(value, MAX_ITEMS), value.size, depth, seen)
    if (value instanceof Set) return capped(take(value, MAX_ITEMS), value.size, depth, seen)

    const out = {}
    const keys = Object.keys(value)
    for (const key of keys.slice(0, MAX_ITEMS)) {
      let raw
      try {
        raw = value[key]
      } catch {
        // A getter that throws is that property's problem, not the entry's.
        out[key] = UNSERIALIZABLE
        continue
      }
      const replaced = redact(key, raw)
      out[key] = replaced === raw ? walk(raw, depth + 1, seen) : replaced
    }
    if (keys.length > MAX_ITEMS) out['…'] = `[+${keys.length - MAX_ITEMS} more]`
    return out
  } finally {
    seen.delete(value)
  }
}

function take(iterable, limit) {
  const out = []
  for (const item of iterable) {
    if (out.length >= limit) break
    out.push(item)
  }
  return out
}

function capped(items, total, depth, seen) {
  const out = items.map((item) => walk(item, depth + 1, seen))
  if (total > items.length) out.push(`…[+${total - items.length} more]`)
  return out
}

function byteLength(text) {
  return ENCODER ? ENCODER.encode(text).length : text.length
}

/** Exact, and cheap on huge strings: a character is never fewer than one UTF-8
    byte, so more characters than the limit is already over it, unmeasured. */
function overBytes(text, limit) {
  return text.length > limit || byteLength(text) > limit
}

/** Cuts to the byte limit and says so. The marker sits outside the budget on
    purpose, so the count it reports is the exact number of characters lost. */
function clip(text, limit) {
  if (!overBytes(text, limit)) return text
  let end = Math.min(text.length, limit)
  while (end > 0 && byteLength(text.slice(0, end)) > limit) end = Math.floor(end * 0.9)
  return `${text.slice(0, end)}…[truncated ${text.length - end} chars]`
}

/** The one path every stored string takes: scrubbed first, then cut. In that
    order on purpose -- clipping first could leave half a credential behind the
    truncation marker, which is still half a credential. */
function scrubClip(text, limit) {
  return clip(scrubText(text), limit)
}

function safeStringify(value) {
  try {
    return JSON.stringify(value) ?? 'null'
  } catch {
    return `"${UNSERIALIZABLE}"`
  }
}

// ---------------------------------------------------------------- preview

/** One already-serialized argument as a line of text. It reads the redacted,
    truncated copy rather than the caller's value: append() scrubs `msg` for
    credentials in free text, but only redact() can drop a secret named by its
    key, and by here that has already happened. */
function describeSerialized(value) {
  if (typeof value === 'string') return value
  if (value === null) return 'null'
  if (typeof value === 'object') {
    // A serialized Error, whose message is the whole point of the line.
    if (typeof value.name === 'string' && typeof value.message === 'string') {
      return `${value.name}: ${value.message}`
    }
    return safeStringify(value)
  }
  return String(value)
}

/** The arguments joined the way the console would show them, cut to one line.
    Arguments past the cut are never described, so a huge tail costs nothing. */
function previewLine(args) {
  let out = ''
  for (const arg of args) {
    if (out.length >= MAX_MSG_CHARS) break
    out += out ? ` ${describeSerialized(arg)}` : describeSerialized(arg)
  }
  return out.length > MAX_MSG_CHARS ? `${out.slice(0, MAX_MSG_CHARS)}…` : out
}

// ---------------------------------------------------------------- buffer

/** Appends one entry. Never throws and never reaches for a browser global: it
    runs inside a hooked console, where an exception would take the caller down
    with it, and a lost line is strictly better than a broken run. */
export function record(entry) {
  try {
    // untracked: a console.log or a logEvent written inside an $effect would
    // otherwise subscribe that effect to every $state property the serializer
    // reads on its way through the argument.
    untrack(() => append(entry))
  } catch {
    // Deliberately swallowed: see the note above.
  }
}

function append(entry) {
  const level = LEVELS.includes(entry?.level) ? entry.level : 'info'
  const src = SOURCES.includes(entry?.src) ? entry.src : 'app'
  const event = typeof entry?.event === 'string' ? entry.event : ''
  const run = typeof entry?.run === 'string' ? entry.run : currentRun
  const ts = Number.isFinite(entry?.ts) ? entry.ts : Date.now()

  let data = entry?.data === undefined ? null : safeArg(entry.data)

  // A console capture hands over no message: its preview is built here, from
  // the serialized arguments, because `msg` is exported as it stands and a
  // preview taken from the raw ones would carry a secret straight past redact().
  const given = String(entry?.msg ?? '')
  const preview =
    !given && src === 'console' && Array.isArray(data?.args) ? previewLine(data.args) : given
  // Scrubbed here, not at the call sites: a caller-written `msg` (a window error
  // message, a failed resource's URL, a rejection reason) is free text nothing
  // else redacts, and this is the single point every one of them passes through.
  const msg = scrubClip(preview, MAX_ARG_BYTES)

  let json = safeStringify(data)
  // Read before the truncation below, so an extension url deep in a long stack
  // is still seen: what the backstop keeps is only the head of this string.
  const ext = isExtensionNoise(src, msg, json)
  if (overBytes(json, MAX_ENTRY_BYTES)) {
    // Per-argument truncation caps one value, not how many there are; this is
    // the backstop for an object that is merely wide.
    data = {
      truncated: true,
      original_bytes: byteLength(json),
      preview: clip(json, MAX_ARG_BYTES),
    }
    json = safeStringify(data)
  }

  seq += 1
  const bytes = byteLength(msg) + byteLength(event) + byteLength(json) + 64
  entries.push({ seq, ts, level, src, event, msg, run, data, ext, bytes })
  totalBytes += bytes

  while (entries.length && (entries.length > LOG_CAPACITY || totalBytes > MAX_TOTAL_BYTES)) {
    totalBytes -= entries.shift().bytes
    dropped += 1
  }

  // Not scheduled while a flush is in progress: a subscriber that logs would
  // queue the next flush from inside this one, forever. What it wrote is in the
  // buffer and goes out with the next append from outside.
  if (!pending && !notifying) {
    pending = true
    schedule(flush)
  }
}

function flush() {
  pending = false
  notifying = true
  try {
    log.set({ entries: entries.slice(), dropped })
  } catch {
    // A subscriber that throws must not stop the next flush from being scheduled.
  } finally {
    notifying = false
  }
}

export function logEvent(event, msg, data) {
  record({ level: 'info', src: 'app', event, msg, data })
}

export function logWarn(event, msg, data) {
  record({ level: 'warn', src: 'app', event, msg, data })
}

export function logError(event, msg, data) {
  record({ level: 'error', src: 'app', event, msg, data })
}

/** Empties the buffer. `seq` survives, so an exported line always names the same
    entry across a clear; `dropped` does not, because it counted what the buffer
    lost against the reader's will, and the reader just emptied it on purpose. */
export function clearLog() {
  entries = []
  totalBytes = 0
  dropped = 0
  flush()
}

function liveEntries() {
  return entries.slice()
}

function asList(value) {
  return Array.isArray(value) ? value : []
}

// ---------------------------------------------------------------- export

function isoStamp(ts) {
  const n = Number(ts)
  return new Date(Number.isFinite(n) ? n : 0).toISOString()
}

/** The NDJSON header line, and the counts the drawer shows. href and ua are read
    here rather than at module scope: in the node test run there is no window.
    The href is scrubbed like any other captured string -- whoever opened the app
    from a link carrying `?api_key=` or `#access_token=` should not have it copied
    into a support log. `ua` carries no credential and is exported as it stands. */
export function logMeta(now = Date.now()) {
  const win = typeof window !== 'undefined' ? window : null
  return {
    app: 'silkscreen',
    href: scrubText(win?.location?.href ?? ''),
    ua: win?.navigator?.userAgent ?? '',
    capacity: LOG_CAPACITY,
    dropped,
    exported: isoStamp(now),
  }
}

/** `silkscreen-log-20260829-181503.txt`. UTC, like every other stamp we export,
    so two people comparing files are not comparing time zones. */
export function logFilename(kind, now = Date.now()) {
  const ext = kind === 'ndjson' ? 'ndjson' : 'txt'
  const at = new Date(Number(now) || 0)
  const pad = (n) => String(n).padStart(2, '0')
  const date = `${at.getUTCFullYear()}${pad(at.getUTCMonth() + 1)}${pad(at.getUTCDate())}`
  const time = `${pad(at.getUTCHours())}${pad(at.getUTCMinutes())}${pad(at.getUTCSeconds())}`
  return `silkscreen-log-${date}-${time}.${ext}`
}

function hasData(data) {
  if (data === null || data === undefined) return false
  if (Array.isArray(data)) return data.length > 0
  if (typeof data === 'object') return Object.keys(data).length > 0
  return true
}

/** Drops the `%c` directives and the style strings that answer them. No format
    reimplementation: the count of directives is the count of arguments eaten. */
function stripStyleArgs(args) {
  const first = args[0]
  if (typeof first !== 'string' || !first.includes('%c')) return args
  const styles = first.split('%c').length - 1
  return [first.replaceAll('%c', ''), ...args.slice(1 + styles)]
}

function textData(entry) {
  const data = entry.data
  if (!hasData(data)) return ''
  if (entry.src === 'console' && Array.isArray(data.args)) {
    return safeStringify({ ...data, args: stripStyleArgs(data.args) })
  }
  return safeStringify(data)
}

/** The text export is one line per entry, so a newline inside a message would
    split one entry across two rows -- Svelte's `console_log_state` warning
    carries several. Flattened here, at render time: the buffer and the NDJSON
    export keep the message exactly as it arrived. */
function flattenMsg(text) {
  return String(text ?? '').replace(/[\r\n]+/g, ' ¶ ')
}

function textLine(entry) {
  const clock = isoStamp(entry.ts).slice(11, 23)
  const level = String(entry.level || '')
    .toUpperCase()
    .padEnd(5)
  const src = String(entry.src || '').padEnd(7)
  const run = String(entry.run || '').padEnd(4)
  const raw = entry.src === 'console' ? String(entry.msg ?? '').replaceAll('%c', '') : entry.msg
  return [clock, level, src, run, flattenMsg(raw), textData(entry)].join('  ').trimEnd()
}

function isSnapshotLine(entry) {
  return entry?.src === 'console' && SNAPSHOT.test(String(entry?.msg ?? ''))
}

/** The paste-into-an-issue format: two `#` header lines, then one line an eye
    can scan. The counts describe the lines actually written, not the buffer. */
export function toText(entries = liveEntries(), now = Date.now()) {
  const rows = asList(entries).filter((e) => !isSnapshotLine(e))
  const meta = logMeta(now)
  const errors = rows.filter((e) => e.level === 'error').length
  const warnings = rows.filter((e) => e.level === 'warn').length
  const head = [
    ['# silkscreen log', `exported ${meta.exported}`, meta.href].filter(Boolean).join('  '),
    [
      '#',
      formatCount(rows.length, 'entry', 'entries'),
      formatCount(errors, 'error'),
      formatCount(warnings, 'warning'),
      `${meta.dropped} dropped`,
      `capacity ${meta.capacity}`,
      'times UTC',
    ].join('  '),
  ]
  return `${[...head, ...rows.map(textLine)].join('\n')}\n`
}

/** The machine format, lossless over what the buffer holds. Fields are picked
    one by one rather than spread, so the internal byte count cannot leak. */
export function toNdjson(entries = liveEntries(), now = Date.now()) {
  const lines = [safeStringify(logMeta(now))]
  for (const entry of asList(entries)) {
    lines.push(
      safeStringify({
        ts: isoStamp(entry.ts),
        level: entry.level,
        src: entry.src,
        event: entry.event,
        msg: entry.msg,
        run: entry.run,
        seq: entry.seq,
        ext: Boolean(entry.ext),
        data: entry.data ?? null,
      }),
    )
  }
  return `${lines.join('\n')}\n`
}

// ---------------------------------------------------------------- triage

// Every 400 the service can answer with names one of these: the field-level
// raises in service/app.py:311-358 and the inline 400s at :602-616. A 400 that
// names none of them did not come from field validation, which points at known
// issue 10 -- an internal ValueError answered as a 400 carrying its message.
const SERVICE_VOCABULARY = [
  'intent',
  'datasheet',
  'time_limit_s',
  'ground',
  'invalid JSON',
  'Content-Length',
  'JSON object',
  // Not one of the service's: api.js synthesizes this when the body carried no
  // `error` field at all. It names no field because there was nothing to name,
  // which says nothing about where the 400 came from.
  'request failed',
]

export function suspectEngineBug(status, message) {
  if (Number(status) !== 400) return false
  const text = String(message ?? '')
  return !SERVICE_VOCABULARY.some((word) => text.includes(word))
}

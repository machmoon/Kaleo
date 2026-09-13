// The bounded buffer behind the debug console, and the two formats it exports.
// One entry schema for everything — the app's own events, stream frames relayed
// from the engine, and captured errors — so the export reads as one timeline.
//
// Redaction is layered and load-bearing (both layers came out of PR #7 review
// on the web UI this ports):
//   1. Key-driven: redact() drops `kicad_pcb` (a whole board file in a log line
//      is bloat, not signal) and any secret-named key before the value is ever
//      serialized.
//   2. Value-driven: scrubText() removes credential-bearing query/fragment
//      parameters and bearer tokens from EVERY string an entry carries.
// record() is the only appending export, and it applies both — a caller cannot
// write to an entry's msg, data, or an export without passing through them.
//
// Pure and node-testable: nothing here reads window or document at module
// scope, and the only reactive surface is subscribe()/getSnapshot(), shaped
// for React's useSyncExternalStore.

export const LOG_CAPACITY = 1000;
/** Per-value budget for one serialized string inside `data`. */
export const MAX_ARG_BYTES = 2048;
/** Backstop for one entry's whole serialized `data`. */
export const MAX_ENTRY_BYTES = 8192;
/** Ceiling on a whole export; older lines are omitted and the file says so. */
export const MAX_EXPORT_BYTES = 2 * 1024 * 1024;

export const LOG_TEXT_MIME = "text/plain;charset=utf-8";
export const LOG_NDJSON_MIME = "application/x-ndjson";

/** console.log-style chatter lands on info; there is no separate trace level. */
export const LEVELS = ["error", "warn", "info", "debug"] as const;
/**
 * `app` is a line this client wrote about itself; `server` is a pipeline event
 * relayed off the stream; `window` is a capture from the page (an uncaught
 * error, a rejection) should a hook ever feed one in.
 */
export const SOURCES = ["app", "server", "window"] as const;

export type LogLevel = (typeof LEVELS)[number];
export type LogSrc = (typeof SOURCES)[number];

export interface LogEntry {
  /** Monotonic across clears, so an exported line always names one entry. */
  seq: number;
  /** Epoch milliseconds. */
  t: number;
  level: LogLevel;
  src: LogSrc;
  /** Machine name (`run.done`, `ui.export`); '' when the line has none. */
  event: string;
  msg: string;
  /** Already sanitized: redacted by key, scrubbed by value, size-capped. */
  data: unknown;
}

export interface LogInput {
  level?: LogLevel;
  src?: LogSrc;
  event?: string;
  msg?: string;
  data?: unknown;
  t?: number;
}

export interface LogSnapshot {
  entries: readonly LogEntry[];
  /** Entries the ring evicted against the reader's will. */
  dropped: number;
}

/** Longest list the serializer keeps; the rest becomes one marker element. */
const MAX_ITEMS = 50;
/** Containers nested deeper than this are marked rather than walked. */
const MAX_DEPTH = 3;
const MAX_MSG_BYTES = MAX_ARG_BYTES;

const SECRET_KEY = /key|token|secret|password|authorization|credential/i;
/**
 * A query or fragment parameter whose value is a credential, matched wherever
 * it sits in a string — a signed datasheet URL arrives as free text (an error
 * message, a rejection reason) where key-driven redact() cannot reach it. The
 * name only has to contain one of the words: `X-Amz-Signature`, `access_token`
 * and `api-key` are all the same secret. The value runs to the next separator
 * so the path and the harmless parameters around it stay readable. `#` opens a
 * parameter as well as `?` and `&` because an OAuth implicit-grant redirect
 * puts the token in the fragment, never the query.
 */
const SECRET_PARAM =
  /([?&#][^?&=\s]*(?:key|token|secret|password|signature|sig|auth|credential)[^?&=\s]*=)[^&\s#"'<>]*/gi;
/** An `Authorization: Bearer …` value, wherever it was stringified from. */
const BEARER = /\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi;

const UNSERIALIZABLE = "[unserializable]";
const ENCODER = new TextEncoder();

interface StoredEntry extends LogEntry {
  /** Internal accounting for eviction; never exported. */
  bytes: number;
}

let entries: StoredEntry[] = [];
let totalBytes = 0;
let dropped = 0;
let seq = 0;

// ---------------------------------------------------------------- scrubbing

/**
 * Value-driven substitution over free text, the half key-driven redaction
 * cannot do: a credential inside a string rather than under a name of its own.
 * Pure and idempotent. record() applies it to every string an entry carries —
 * msg and every serialized value — and the exporters apply it to the metadata
 * URL, so no caller has to remember it. New log paths must append through
 * record() (or the helpers over it) rather than building lines by hand.
 */
export function scrubText(text: unknown): string {
  const value = String(text ?? "");
  if (!value) return value;
  return value
    .replace(SECRET_PARAM, "$1[redacted]")
    .replace(BEARER, "Bearer [redacted]");
}

/**
 * Key-driven substitution, applied before a value is walked or truncated, so
 * no secret is ever serialized. `kicad_pcb` is dropped for bulk, not secrecy:
 * its length is the useful fact.
 */
function redact(key: string, value: unknown): unknown {
  if (key === "kicad_pcb")
    return `[kicad_pcb: ${String(value ?? "").length} chars]`;
  if (SECRET_KEY.test(key)) return "[redacted]";
  return value;
}

function byteLength(text: string): number {
  return ENCODER.encode(text).length;
}

/** Cheap on huge strings: more characters than the limit is already over it. */
function overBytes(text: string, limit: number): boolean {
  return text.length > limit || byteLength(text) > limit;
}

/** Cuts to the byte limit and says so; the marker sits outside the budget. */
function clip(text: string, limit: number): string {
  if (!overBytes(text, limit)) return text;
  let end = Math.min(text.length, limit);
  while (end > 0 && byteLength(text.slice(0, end)) > limit)
    end = Math.floor(end * 0.9);
  return `${text.slice(0, end)}…[truncated ${text.length - end} chars]`;
}

/**
 * Scrubbed first, then cut — in that order on purpose: clipping first could
 * leave half a credential behind the truncation marker, which is still half a
 * credential.
 */
function scrubClip(text: string, limit: number): string {
  return clip(scrubText(text), limit);
}

// ---------------------------------------------------------------- serializer

/** One value, reduced to something JSON can hold. Never throws. */
function safeData(value: unknown): unknown {
  try {
    return walk(value, 0, new WeakSet());
  } catch {
    return UNSERIALIZABLE;
  }
}

function walk(value: unknown, depth: number, seen: WeakSet<object>): unknown {
  if (value === null) return null;
  if (value === undefined) return "[undefined]";

  const type = typeof value;
  if (type === "boolean") return value;
  if (type === "number")
    return Number.isFinite(value as number) ? value : String(value);
  if (type === "string") return scrubClip(value as string, MAX_ARG_BYTES);
  if (type === "bigint" || type === "symbol") return String(value);
  if (type === "function")
    return `[Function: ${(value as { name?: string }).name || "anonymous"}]`;

  if (value instanceof Error) {
    return {
      name: String(value.name),
      message: scrubClip(String(value.message), MAX_ARG_BYTES),
      stack: scrubClip(String(value.stack ?? ""), MAX_ARG_BYTES),
    };
  }
  if (value instanceof Date) {
    return Number.isFinite(value.getTime())
      ? value.toISOString()
      : "Invalid Date";
  }

  if (depth >= MAX_DEPTH) return "[max depth]";
  const obj = value as object;
  if (seen.has(obj)) return "[Circular]";
  // `seen` holds the branch being walked, not every value ever walked: the
  // mark comes off on the way out, so two properties pointing at one object
  // both serialize and only a value containing itself reads as circular.
  seen.add(obj);
  try {
    if (Array.isArray(value)) {
      const out: unknown[] = value
        .slice(0, MAX_ITEMS)
        .map((item) => walk(item, depth + 1, seen));
      if (value.length > MAX_ITEMS)
        out.push(`…[+${value.length - MAX_ITEMS} more]`);
      return out;
    }
    const out: Record<string, unknown> = {};
    const keys = Object.keys(obj);
    for (const key of keys.slice(0, MAX_ITEMS)) {
      let raw: unknown;
      try {
        raw = (obj as Record<string, unknown>)[key];
      } catch {
        // A getter that throws is that property's problem, not the entry's.
        out[key] = UNSERIALIZABLE;
        continue;
      }
      const replaced = redact(key, raw);
      out[key] = replaced === raw ? walk(raw, depth + 1, seen) : replaced;
    }
    if (keys.length > MAX_ITEMS) out["…"] = `[+${keys.length - MAX_ITEMS} more]`;
    return out;
  } finally {
    seen.delete(obj);
  }
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value) ?? "null";
  } catch {
    return `"${UNSERIALIZABLE}"`;
  }
}

// ---------------------------------------------------------------- buffer

type Listener = () => void;
const listeners = new Set<Listener>();
let dirty = true;
let snapshot: LogSnapshot = { entries: [], dropped: 0 };
let pending = false;
let notifying = false;

// Captured at module evaluation so fake timers cannot defeat the batching.
const schedule: (fn: () => void) => void =
  typeof queueMicrotask === "function"
    ? queueMicrotask.bind(globalThis)
    : (fn) => void Promise.resolve().then(fn);

function notify(): void {
  pending = false;
  notifying = true;
  try {
    for (const fn of Array.from(listeners)) {
      try {
        fn();
      } catch {
        // A subscriber that throws must not strand the others.
      }
    }
  } finally {
    notifying = false;
  }
}

/**
 * The ONE appending path, and the only exported way in. Sanitizes everything
 * it stores: `msg` is scrubbed for in-string credentials, `data` goes through
 * the redacting serializer, and an entry that is merely wide is truncated with
 * its original size recorded. Never throws — a lost line is strictly better
 * than taking the caller down.
 */
export function record(input: LogInput): void {
  try {
    append(input);
  } catch {
    // Deliberately swallowed: see above.
  }
}

function append(input: LogInput): void {
  const level = LEVELS.includes(input?.level as LogLevel)
    ? (input.level as LogLevel)
    : "info";
  const src = SOURCES.includes(input?.src as LogSrc)
    ? (input.src as LogSrc)
    : "app";
  // Event names are engine-controlled strings; they take the same scrub and
  // a short cap so the "one appending path sanitizes everything" claim holds
  // for every field, not every field but one.
  const event =
    typeof input?.event === "string" ? scrubClip(input.event, 256) : "";
  const t = Number.isFinite(input?.t) ? (input.t as number) : Date.now();

  // Scrubbed here, not at call sites: a caller-written msg (an error message,
  // a failed URL, a rejection reason) is free text nothing else redacts, and
  // this is the single point every one of them passes through.
  const msg = scrubClip(String(input?.msg ?? ""), MAX_MSG_BYTES);

  let data = input?.data === undefined ? null : safeData(input.data);
  let json = safeStringify(data);
  if (overBytes(json, MAX_ENTRY_BYTES)) {
    // Per-value truncation caps one string, not how many there are; this is
    // the backstop for an object that is merely wide.
    data = {
      truncated: true,
      original_bytes: byteLength(json),
      preview: clip(json, MAX_ARG_BYTES),
    };
    json = safeStringify(data);
  }

  seq += 1;
  const bytes = byteLength(msg) + byteLength(event) + byteLength(json) + 64;
  entries.push({ seq, t, level, src, event, msg, data, bytes });
  totalBytes += bytes;

  while (entries.length > LOG_CAPACITY) {
    const evicted = entries.shift();
    if (evicted) totalBytes -= evicted.bytes;
    dropped += 1;
  }

  dirty = true;
  // Not scheduled while a notify is in progress: a subscriber that logs would
  // otherwise queue the next flush from inside this one, forever.
  if (!pending && !notifying) {
    pending = true;
    schedule(notify);
  }
}

export function logEvent(event: string, msg: string, data?: unknown): void {
  record({ level: "info", src: "app", event, msg, data });
}

export function logWarn(event: string, msg: string, data?: unknown): void {
  record({ level: "warn", src: "app", event, msg, data });
}

export function logError(event: string, msg: string, data?: unknown): void {
  record({ level: "error", src: "app", event, msg, data });
}

/**
 * A pipeline event relayed off `/generate/stream`, as `src: 'server'`. The run
 * layer mirrors every frame through here — the frame object goes in as `data`
 * so the redacting serializer sees it (a `run.done` frame carries the whole
 * result, `kicad_pcb` included, which redact() reduces to its length).
 */
export function logServer(event: string, msg: string, data?: unknown): void {
  record({ level: "info", src: "server", event, msg, data });
}

/**
 * Empties the buffer. `seq` survives so an exported line always names the same
 * entry across a clear; `dropped` resets because it counted what the ring lost
 * against the reader's will, and the reader just emptied it on purpose.
 */
export function clearLog(): void {
  entries = [];
  totalBytes = 0;
  dropped = 0;
  dirty = true;
  if (!pending && !notifying) {
    pending = true;
    schedule(notify);
  }
}

// ---------------------------------------------------------------- reactivity

/** Listener runs (batched, on a microtask) after the buffer changes. */
export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

/**
 * Stable between changes, rebuilt lazily on read — the shape
 * `useSyncExternalStore` wants, and synchronous for tests.
 */
export function getSnapshot(): LogSnapshot {
  if (dirty) {
    snapshot = { entries: entries.map(({ bytes: _bytes, ...e }) => e), dropped };
    dirty = false;
  }
  return snapshot;
}

// ---------------------------------------------------------------- export

function isoStamp(t: number): string {
  const n = Number(t);
  return new Date(Number.isFinite(n) ? n : 0).toISOString();
}

export interface LogMeta {
  app: string;
  href: string;
  ua: string;
  capacity: number;
  dropped: number;
  exported: string;
}

/**
 * The export header. `href` and `ua` are read here rather than at module scope
 * (the node test run has no window), and the href is scrubbed like any other
 * string — whoever opened the app from a link carrying `?api_key=` should not
 * have it copied into a bug report.
 */
export function logMeta(now = Date.now()): LogMeta {
  const win =
    typeof window !== "undefined" ? (window as Window & typeof globalThis) : null;
  return {
    app: "kaleo",
    href: scrubText(win?.location?.href ?? ""),
    ua: win?.navigator?.userAgent ?? "",
    capacity: LOG_CAPACITY,
    dropped,
    exported: isoStamp(now),
  };
}

/** `kaleo-log-20260831-181503.txt` — UTC, like every stamp we export. */
export function logFilename(kind: "txt" | "ndjson", now = Date.now()): string {
  const at = new Date(Number(now) || 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${at.getUTCFullYear()}${pad(at.getUTCMonth() + 1)}${pad(at.getUTCDate())}`;
  const time = `${pad(at.getUTCHours())}${pad(at.getUTCMinutes())}${pad(at.getUTCSeconds())}`;
  return `kaleo-log-${date}-${time}.${kind === "ndjson" ? "ndjson" : "txt"}`;
}

function currentEntries(): readonly LogEntry[] {
  return getSnapshot().entries;
}

/**
 * Keeps the newest lines that fit the export budget. Returns the kept lines
 * and how many were omitted from the head — the exporters must SAY when the
 * file is not the whole buffer, or a short file reads as a short run.
 */
function fitLines(lines: string[]): { kept: string[]; omitted: number } {
  let total = 0;
  let start = lines.length;
  while (start > 0 && total + byteLength(lines[start - 1]) + 1 <= MAX_EXPORT_BYTES) {
    total += byteLength(lines[start - 1]) + 1;
    start -= 1;
  }
  return { kept: lines.slice(start), omitted: start };
}

function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function textLine(entry: LogEntry): string {
  const clock = isoStamp(entry.t).slice(11, 23);
  const level = entry.level.toUpperCase().padEnd(5);
  const src = entry.src.padEnd(6);
  // One line per entry: a newline inside a message would split it across rows.
  const msg = entry.msg.replace(/[\r\n]+/g, " ¶ ");
  const json = safeStringify(entry.data);
  const data = json === "null" || json === "{}" || json === "[]" ? "" : json;
  return [clock, level, src, entry.event, msg, data].join("  ").trimEnd();
}

/** The paste-into-an-issue format: `#` header lines, then one line per entry. */
export function toText(
  rows: readonly LogEntry[] = currentEntries(),
  now = Date.now(),
): string {
  const meta = logMeta(now);
  const { kept, omitted } = fitLines(rows.map(textLine));
  const shown = rows.slice(rows.length - kept.length);
  const errors = shown.filter((e) => e.level === "error").length;
  const warnings = shown.filter((e) => e.level === "warn").length;
  const head = [
    ["# kaleo log", `exported ${meta.exported}`, meta.href]
      .filter(Boolean)
      .join("  "),
    [
      "#",
      count(shown.length, "entry", "entries"),
      count(errors, "error"),
      count(warnings, "warning"),
      `${meta.dropped} dropped`,
      `capacity ${meta.capacity}`,
      "times UTC",
    ].join("  "),
    ...(omitted
      ? [`# truncated: first ${count(omitted, "entry", "entries")} omitted to fit ${MAX_EXPORT_BYTES} bytes`]
      : []),
  ];
  return `${[...head, ...kept].join("\n")}\n`;
}

/**
 * The machine format: one metadata line, then one JSON object per entry.
 * Fields are picked one by one rather than spread, so internal accounting can
 * never leak into a file.
 */
export function toNdjson(
  rows: readonly LogEntry[] = currentEntries(),
  now = Date.now(),
): string {
  const lines = rows.map((entry) =>
    safeStringify({
      t: isoStamp(entry.t),
      level: entry.level,
      src: entry.src,
      event: entry.event,
      msg: entry.msg,
      seq: entry.seq,
      data: entry.data ?? null,
    }),
  );
  const { kept, omitted } = fitLines(lines);
  const meta = {
    ...logMeta(now),
    ...(omitted
      ? { truncated: true, omitted_entries: omitted, limit_bytes: MAX_EXPORT_BYTES }
      : {}),
  };
  return `${[safeStringify(meta), ...kept].join("\n")}\n`;
}

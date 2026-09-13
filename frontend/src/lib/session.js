const VERSION = 1
export const SESSION_MIME = 'application/json;charset=utf-8'
export const MAX_SESSION_BYTES = 32 * 1024 * 1024

const PHASES = ['idle', 'running', 'clarification', 'done', 'error']

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

function errorOf(error) {
  if (!error) return null
  return {
    kind: String(error.kind ?? 'internal'),
    message: String(error.message ?? error),
    status: Number(error.status ?? 0),
    errorId: String(error.errorId ?? ''),
  }
}

/** A portable, inspectable demo record. API keys never enter the run store, so
    the document contains prompts and artifacts without carrying credentials. */
export function sessionDocument(state, now = new Date()) {
  const source = objectOf(state)
  return {
    schema: 'silkscreen.session',
    version: VERSION,
    saved_at: now.toISOString(),
    session: {
      phase: PHASES.includes(source.phase) ? source.phase : 'idle',
      request: source.request ?? null,
      result: source.result ?? null,
      error: errorOf(source.error),
      id: String(source.id ?? ''),
      sessionId: String(source.sessionId ?? ''),
      stages: objectOf(source.stages),
      feed: Array.isArray(source.feed) ? source.feed : [],
      entries: Array.isArray(source.entries) ? source.entries : [],
      needsClarification: source.needsClarification === true,
      actualModel: String(source.actualModel ?? ''),
      orchestratorModel: String(source.orchestratorModel ?? 'gemini-3.7-flash'),
      thinkingLevel: String(source.thinkingLevel ?? 'auto'),
      actualThinkingLevel: String(source.actualThinkingLevel ?? ''),
      quotaRpm: String(source.quotaRpm ?? 'auto'),
      startedAt: 0,
    },
  }
}

export function serializeSession(state, now = new Date()) {
  return `${JSON.stringify(sessionDocument(state, now), null, 2)}\n`
}

export function parseSession(text) {
  const source = String(text ?? '')
  if (new TextEncoder().encode(source).length > MAX_SESSION_BYTES) {
    throw new Error(`Session file exceeds ${MAX_SESSION_BYTES.toLocaleString()} bytes.`)
  }
  let document
  try {
    document = JSON.parse(source)
  } catch {
    throw new Error('Session file is not valid JSON.')
  }
  if (!document || document.schema !== 'silkscreen.session' || document.version !== VERSION) {
    throw new Error('This is not a supported Hardy session file.')
  }
  const session = objectOf(document.session)
  if (!PHASES.includes(session.phase) || !Array.isArray(session.entries)) {
    throw new Error('The Hardy session is incomplete.')
  }
  return {
    ...session,
    stages: objectOf(session.stages),
    feed: Array.isArray(session.feed) ? session.feed : [],
    entries: session.entries,
    startedAt: 0,
  }
}

export function sessionFilename(state) {
  const id = String(state?.sessionId ?? '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 48)
  return `${id || 'silkscreen'}-session.json`
}

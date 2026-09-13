// Voice, both directions: dictation into the intent fields via the browser's
// SpeechRecognition, and findings read back out via speechSynthesis.
//
// Shaped like theme.js/skin.js and api.js's fetch seam: every browser speech
// object arrives as an injected parameter (a recognition constructor, the
// synthesis object, the utterance constructor), so vitest exercises all of it
// in node with fakes and no component ever touches the Web Speech API
// directly. This module does no network of its own — Chrome's recognition
// service talks to Google from inside the browser API, which is exactly why
// the whole feature is opt-in per click and never auto-starts.
//
// Two rules are load-bearing and tested rather than assumed:
//   - dictation APPENDS to what the user typed, it never replaces it;
//   - nothing listens and nothing speaks without an explicit call — there is
//     no auto-start path in this file for a component to trip.

import { formatCount } from './format.js'
import { SEVERITY_ORDER, countBySeverity, severityInfo } from './severity.js'

// --- support detection ------------------------------------------------------

/** The recognition constructor, or null where the browser has none (Firefox).
    Chrome and Edge still ship it under the webkit prefix. Null is an answer
    the components render as an honest notice, never as a dead button. */
export function recognitionCtorOf(globalObj) {
  if (!globalObj) return null
  return globalObj.SpeechRecognition || globalObj.webkitSpeechRecognition || null
}

/** The synthesis half, split in two because the API is: a queue object that
    speaks, and a constructor for the utterances handed to it. */
export function synthesisOf(globalObj) {
  return (globalObj && globalObj.speechSynthesis) || null
}

export function utteranceCtorOf(globalObj) {
  return (globalObj && globalObj.SpeechSynthesisUtterance) || null
}

// --- transcript append ------------------------------------------------------

/** Dictation lands after whatever is already in the field, separated by one
    space. Typed text is never replaced or reflowed — losing a user's prose to
    a microphone button would be the voice equivalent of the stacked-parts bug
    class. A whitespace-only field counts as empty rather than being preserved
    as invisible padding. */
export function appendTranscript(existing, transcript) {
  const base = String(existing ?? '')
  const addition = String(transcript ?? '').trim()
  if (!addition) return base
  if (!base.trim()) return addition
  return /\s$/.test(base) ? `${base}${addition}` : `${base} ${addition}`
}

// --- dictation --------------------------------------------------------------

/** The user-facing message for a recognition error code, or null for the
    codes that just mean the session ended with nothing to say ('no-speech',
    'aborted'). Permission refusals get a message that says what to do, not a
    raw error name. */
export function dictationErrorMessage(code) {
  const key = String(code ?? '')
  if (key === 'not-allowed' || key === 'service-not-allowed') {
    return 'Microphone access was denied. Allow the microphone for this site, then press the button again.'
  }
  if (key === 'audio-capture') return 'No microphone could be captured on this device.'
  if (key === 'no-speech' || key === 'aborted') return null
  if (key === 'network') return 'Speech recognition could not reach its service over the network.'
  return `Speech recognition failed (${key || 'unknown error'}).`
}

/** A dictation session driver around an injected SpeechRecognition
    constructor. Nothing happens at construction: `start()` is the one way a
    microphone session begins, and each session is a fresh recognition
    instance so a stale one can never deliver into a new session.

    continuous stays false on purpose — the engine then ends itself after a
    silence, which is the auto-stop the control promises — and interim results
    stream through `onInterim` for display only; text reaches the field solely
    through `onFinal`, once per final result. `stop()` asks the engine to
    finish gracefully, so trailing finals still arrive before `onState(false)`. */
export function createDictation(Ctor, { lang = '', onState, onInterim, onFinal, onError } = {}) {
  const supported = typeof Ctor === 'function'
  let active = null

  function start() {
    if (!supported || active) return false
    const recognition = new Ctor()
    recognition.continuous = false
    recognition.interimResults = true
    if (lang) recognition.lang = lang

    // Some engines re-deliver earlier final results in later events; count
    // what was already handed to onFinal so nothing is appended twice.
    let delivered = 0

    recognition.onresult = (event) => {
      if (recognition !== active) return
      const results = (event && event.results) || []
      let interim = ''
      for (let i = 0; i < results.length; i += 1) {
        const result = results[i]
        const transcript = String((result && result[0] && result[0].transcript) ?? '')
        if (result && result.isFinal) {
          if (i >= delivered) {
            delivered = i + 1
            if (transcript.trim() && onFinal) onFinal(transcript)
          }
        } else {
          interim += transcript
        }
      }
      if (onInterim) onInterim(interim)
    }

    recognition.onerror = (event) => {
      if (recognition !== active) return
      const message = dictationErrorMessage(event && event.error)
      if (message && onError) onError(message, String((event && event.error) ?? ''))
    }

    recognition.onend = () => {
      if (recognition !== active) return
      active = null
      if (onInterim) onInterim('')
      if (onState) onState(false)
    }

    active = recognition
    try {
      recognition.start()
    } catch {
      active = null
      return false
    }
    if (onState) onState(true)
    return true
  }

  function stop() {
    if (!active) return false
    try {
      active.stop()
    } catch {
      // Already stopping; onend still fires and clears the session.
    }
    return true
  }

  return {
    supported,
    listening: () => active !== null,
    start,
    stop,
  }
}

// --- speaking ---------------------------------------------------------------

/** A speech queue around an injected speechSynthesis object and utterance
    constructor. Nothing is spoken at construction — `speak`/`speakAll` are
    the only entrances, each replaces whatever was playing, and `stop()`
    silences the queue immediately. A generation counter keeps the callbacks
    of cancelled utterances from miscounting a newer queue: browsers fire
    end/error on cancel, and some fire both. */
export function createSpeaker(synth, Utterance, { onChange } = {}) {
  const supported = Boolean(synth && typeof Utterance === 'function')
  let pending = 0
  let generation = 0

  function speaking() {
    return pending > 0
  }

  function notify() {
    if (onChange) onChange(speaking())
  }

  function enqueue(text) {
    const line = String(text ?? '').trim()
    if (!line) return false
    const mine = generation
    let settled = false
    const settle = () => {
      if (settled || mine !== generation) return
      settled = true
      pending -= 1
      notify()
    }
    const utterance = new Utterance(line)
    utterance.onend = settle
    utterance.onerror = settle
    pending += 1
    synth.speak(utterance)
    return true
  }

  function stop() {
    if (!supported) return false
    generation += 1
    pending = 0
    synth.cancel()
    notify()
    return true
  }

  function speak(text) {
    if (!supported) return false
    stop()
    const queued = enqueue(text)
    notify()
    return queued
  }

  function speakAll(texts) {
    if (!supported) return 0
    stop()
    let queued = 0
    for (const text of texts || []) {
      if (enqueue(text)) queued += 1
    }
    notify()
    return queued
  }

  return { supported, speaking, speak, speakAll, stop }
}

// --- what gets said ---------------------------------------------------------

function sentence(text) {
  const trimmed = String(text ?? '').trim()
  if (!trimmed) return ''
  return /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`
}

/** Blocker's on-screen label is "will not work"; spoken, it keeps both the
    tier and the plain reading. Unrecognised severities keep their own words,
    the severityInfo rule. */
function spokenSeverity(info) {
  return info.key === 'blocker' ? 'Blocker: will not work' : info.label
}

/** One finding as speech: severity, the parts it names, title, detail, the
    measured evidence when the finding carries one, the suggested fix (with
    the same "nothing is applied" honesty as the inert button), and the
    citation. Only what the finding actually says — absent fields are skipped,
    never padded. */
export function findingSpeech(finding) {
  const info = severityInfo(finding && finding.severity)
  const parts = ((finding && finding.parts) || []).filter(Boolean)
  const evidence = typeof finding?.evidence === 'string' ? finding.evidence.trim() : ''
  return [
    sentence(spokenSeverity(info)),
    parts.length ? sentence(`Parts ${parts.join(', ')}`) : '',
    sentence(finding && finding.title),
    sentence(finding && finding.detail),
    evidence ? sentence(`Measured: ${evidence}`) : '',
    finding?.suggested_fix
      ? sentence(`Suggested fix: ${String(finding.suggested_fix).trim()} — nothing is applied automatically`)
      : '',
    finding?.citation ? sentence(`Citation: ${String(finding.citation).trim()}`) : '',
  ]
    .filter(Boolean)
    .join(' ')
}

// The same two claims ReviewResults draws on screen, kept distinct out loud:
// a review that never ran is not a clean board, and a clean review is scoped
// to what the reviewer actually read.
const SKIPPED_SPEECH =
  'The design review was skipped: this run was submitted with the review turned off, '
  + 'so the board was placed but nothing checked it against the datasheets. '
  + 'There are no findings to read because nothing was checked, not because the board is clean.'

const CLEAN_SPEECH =
  'Nothing to flag: the reviewer found no blockers, marginal choices, or notes against '
  + 'the datasheets it read. That covers pin function and passive values in context, '
  + 'and it is not a substitute for a human sign-off before you order boards.'

/** A review the critic was asked for and could not answer (review.js's
    `failed`): the reason is the engine's own sentence, read verbatim. */
function failedSpeech(reason) {
  const why = String(reason ?? '').trim() || 'the critic answered nothing readable'
  return (
    `The design review failed: the critic was asked and its answer could not be read (${why}), `
    + 'so nothing is known about this board. '
    + 'There are no findings to read because the check did not complete, not because the board is clean.'
  )
}

/** The read-all script, as one utterance per list entry so `stop()` lands
    between findings. A skipped or failed review is NEVER read as zero
    findings — an absent check, a broken one and a clean one are three
    different claims, the ArtifactCards rule. */
export function reviewSpeech(result, { skipped = false, failed = false, failedReason = '' } = {}) {
  if (skipped) return [SKIPPED_SPEECH]
  if (failed) return [failedSpeech(failedReason)]
  const findings = Array.isArray(result && result.findings) ? result.findings : []
  if (!findings.length) return [CLEAN_SPEECH]

  const counts = countBySeverity(findings)
  const spokenTier = {
    blocker: (n) => `${n} will not work`,
    marginal: (n) => `${n} marginal`,
    note: (n) => formatCount(n, 'note'),
  }
  const breakdown = SEVERITY_ORDER.filter((key) => counts[key] > 0)
    .map((key) => spokenTier[key](counts[key]))
    .join(', ')
  const intro = `${formatCount(findings.length, 'finding')} from the design review: ${breakdown}.`

  return [
    intro,
    ...findings.map(
      (finding, index) => `Finding ${index + 1} of ${findings.length}. ${findingSpeech(finding)}`,
    ),
  ]
}

// --- the persisted preference ----------------------------------------------
// Deliberately shaped like skin.js: decisions about a value, never a write to
// the document, and the one localStorage key stays out of API requests and
// saved session JSON. 'on' is the default — the controls exist — and 'off'
// hides the read-aloud controls for whoever never wants a talking board.

export const VOICE_STORAGE_KEY = 'silkscreen-voice'
export const DEFAULT_VOICE = 'on'

/** A voice preference, or null for anything else localStorage hands back. */
export function normalizeVoice(value) {
  return value === 'on' || value === 'off' ? value : null
}

export function readStoredVoice(storage) {
  try {
    return normalizeVoice(storage.getItem(VOICE_STORAGE_KEY))
  } catch {
    return null
  }
}

export function writeStoredVoice(storage, value) {
  const choice = normalizeVoice(value)
  if (!choice) return false
  try {
    storage.setItem(VOICE_STORAGE_KEY, choice)
    return true
  } catch {
    return false
  }
}

export function resolveVoice(stored) {
  return normalizeVoice(stored) || DEFAULT_VOICE
}

export function toggleVoice(current) {
  return current === 'on' ? 'off' : 'on'
}

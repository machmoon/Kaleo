import { describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_VOICE,
  VOICE_STORAGE_KEY,
  appendTranscript,
  createDictation,
  createSpeaker,
  dictationErrorMessage,
  findingSpeech,
  normalizeVoice,
  readStoredVoice,
  recognitionCtorOf,
  resolveVoice,
  reviewSpeech,
  synthesisOf,
  toggleVoice,
  utteranceCtorOf,
  writeStoredVoice,
} from './voice.js'

// --- fakes ------------------------------------------------------------------

/** A fake SpeechRecognition constructor that records its instances so a test
    can drive onresult/onerror/onend by hand, the way the browser would. */
function fakeRecognitionCtor() {
  const instances = []
  const Ctor = class {
    constructor() {
      this.started = 0
      this.stopped = 0
      instances.push(this)
    }

    start() {
      this.started += 1
    }

    stop() {
      this.stopped += 1
    }
  }
  return { Ctor, instances }
}

function interimResult(transcript) {
  return { isFinal: false, 0: { transcript } }
}

function finalResult(transcript) {
  return { isFinal: true, 0: { transcript } }
}

/** A fake speechSynthesis: records spoken utterances and cancel calls. The
    matching fake utterance constructor just keeps the text and the handler
    slots the speaker assigns. */
function fakeSynth() {
  const spoken = []
  return {
    spoken,
    cancels: 0,
    speak(utterance) {
      spoken.push(utterance)
    },
    cancel() {
      this.cancels += 1
    },
  }
}

class FakeUtterance {
  constructor(text) {
    this.text = text
    this.onend = null
    this.onerror = null
  }
}

const blockedStorage = {
  getItem() {
    throw new Error('blocked')
  },
  setItem() {
    throw new Error('blocked')
  },
}

// --- support detection ------------------------------------------------------

describe('recognitionCtorOf', () => {
  it('finds the unprefixed constructor', () => {
    const Ctor = class {}
    expect(recognitionCtorOf({ SpeechRecognition: Ctor })).toBe(Ctor)
  })

  it('falls back to the webkit prefix Chrome and Edge ship', () => {
    const Ctor = class {}
    expect(recognitionCtorOf({ webkitSpeechRecognition: Ctor })).toBe(Ctor)
  })

  it('answers null in a browser without the API, and for no global at all', () => {
    expect(recognitionCtorOf({})).toBe(null)
    expect(recognitionCtorOf(null)).toBe(null)
  })
})

describe('synthesisOf / utteranceCtorOf', () => {
  it('hands back the synthesis pieces when present', () => {
    const synth = { speak() {}, cancel() {} }
    expect(synthesisOf({ speechSynthesis: synth })).toBe(synth)
    expect(utteranceCtorOf({ SpeechSynthesisUtterance: FakeUtterance })).toBe(FakeUtterance)
  })

  it('answers null when absent', () => {
    expect(synthesisOf({})).toBe(null)
    expect(utteranceCtorOf({})).toBe(null)
    expect(synthesisOf(null)).toBe(null)
  })
})

// --- transcript append ------------------------------------------------------

describe('appendTranscript', () => {
  it('preserves what the user typed and appends after one space', () => {
    expect(appendTranscript('an LDO board', 'with a motor driver')).toBe(
      'an LDO board with a motor driver',
    )
  })

  it('does not double the space when the field already ends with one', () => {
    expect(appendTranscript('an LDO board ', 'again')).toBe('an LDO board again')
  })

  it('fills an empty or whitespace-only field with just the transcript', () => {
    expect(appendTranscript('', 'a 3.3 volt regulator')).toBe('a 3.3 volt regulator')
    expect(appendTranscript('   ', 'a 3.3 volt regulator')).toBe('a 3.3 volt regulator')
  })

  it('leaves the field alone for an empty transcript', () => {
    expect(appendTranscript('typed text', '')).toBe('typed text')
    expect(appendTranscript('typed text', '   ')).toBe('typed text')
    expect(appendTranscript('typed text', null)).toBe('typed text')
  })

  it('trims the transcript, never the typed text', () => {
    expect(appendTranscript('typed', '  spoken  ')).toBe('typed spoken')
  })
})

// --- dictation --------------------------------------------------------------

describe('createDictation', () => {
  it('reports unsupported for a missing constructor and refuses to start', () => {
    const onState = vi.fn()
    const dictation = createDictation(null, { onState })
    expect(dictation.supported).toBe(false)
    expect(dictation.start()).toBe(false)
    expect(dictation.listening()).toBe(false)
    expect(onState).not.toHaveBeenCalled()
  })

  it('never constructs or starts a recognition session on its own', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const dictation = createDictation(Ctor, {})
    expect(dictation.supported).toBe(true)
    expect(instances).toHaveLength(0)
    expect(dictation.listening()).toBe(false)
  })

  it('starts exactly one session per explicit start()', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const onState = vi.fn()
    const dictation = createDictation(Ctor, { onState })
    expect(dictation.start()).toBe(true)
    expect(instances).toHaveLength(1)
    expect(instances[0].started).toBe(1)
    expect(onState).toHaveBeenCalledWith(true)
    // A second start while listening is refused rather than stacking sessions.
    expect(dictation.start()).toBe(false)
    expect(instances).toHaveLength(1)
  })

  it('streams interim text through onInterim and none of it through onFinal', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const onInterim = vi.fn()
    const onFinal = vi.fn()
    createDictation(Ctor, { onInterim, onFinal }).start()
    const recognition = instances[0]

    recognition.onresult({ resultIndex: 0, results: [interimResult('an LDO')] })
    recognition.onresult({ resultIndex: 0, results: [interimResult('an LDO board')] })

    expect(onInterim).toHaveBeenNthCalledWith(1, 'an LDO')
    expect(onInterim).toHaveBeenNthCalledWith(2, 'an LDO board')
    expect(onFinal).not.toHaveBeenCalled()
  })

  it('delivers a final result once, even when the engine re-sends it', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const onFinal = vi.fn()
    createDictation(Ctor, { onFinal }).start()
    const recognition = instances[0]

    recognition.onresult({ resultIndex: 0, results: [finalResult('an LDO board')] })
    recognition.onresult({
      resultIndex: 0,
      results: [finalResult('an LDO board'), interimResult('with')],
    })

    expect(onFinal).toHaveBeenCalledTimes(1)
    expect(onFinal).toHaveBeenCalledWith('an LDO board')
  })

  it('ends on silence: the engine onend clears listening and the interim line', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const onState = vi.fn()
    const onInterim = vi.fn()
    const dictation = createDictation(Ctor, { onState, onInterim })
    dictation.start()
    instances[0].onresult({ resultIndex: 0, results: [interimResult('half a tho')] })

    instances[0].onend()

    expect(dictation.listening()).toBe(false)
    expect(onInterim).toHaveBeenLastCalledWith('')
    expect(onState).toHaveBeenLastCalledWith(false)
  })

  it('surfaces a permission refusal as a clear message, not a code', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const onError = vi.fn()
    createDictation(Ctor, { onError }).start()

    instances[0].onerror({ error: 'not-allowed' })

    expect(onError).toHaveBeenCalledTimes(1)
    const [message, code] = onError.mock.calls[0]
    expect(message).toMatch(/microphone access was denied/i)
    expect(code).toBe('not-allowed')
  })

  it('treats hearing nothing as an ordinary end, not an error', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const onError = vi.fn()
    createDictation(Ctor, { onError }).start()

    instances[0].onerror({ error: 'no-speech' })
    instances[0].onerror({ error: 'aborted' })

    expect(onError).not.toHaveBeenCalled()
  })

  it('stop() asks the engine to finish; listening clears on its onend', () => {
    const { Ctor, instances } = fakeRecognitionCtor()
    const dictation = createDictation(Ctor, {})
    dictation.start()

    expect(dictation.stop()).toBe(true)
    expect(instances[0].stopped).toBe(1)
    // Graceful: trailing finals may still arrive before onend fires.
    expect(dictation.listening()).toBe(true)
    instances[0].onend()
    expect(dictation.listening()).toBe(false)
    // Nothing to stop once ended.
    expect(dictation.stop()).toBe(false)
  })
})

describe('dictationErrorMessage', () => {
  it('maps the denial codes to the microphone message', () => {
    expect(dictationErrorMessage('not-allowed')).toMatch(/denied/i)
    expect(dictationErrorMessage('service-not-allowed')).toMatch(/denied/i)
  })

  it('answers null for the benign session-over codes', () => {
    expect(dictationErrorMessage('no-speech')).toBe(null)
    expect(dictationErrorMessage('aborted')).toBe(null)
  })

  it('names the code for anything unrecognised', () => {
    expect(dictationErrorMessage('bad-grammar')).toContain('bad-grammar')
  })
})

// --- speaking ---------------------------------------------------------------

describe('createSpeaker', () => {
  it('reports unsupported without both synthesis pieces, and stays silent', () => {
    const synth = fakeSynth()
    expect(createSpeaker(null, FakeUtterance).supported).toBe(false)
    expect(createSpeaker(synth, null).supported).toBe(false)
    const speaker = createSpeaker(synth, null)
    expect(speaker.speak('hello')).toBe(false)
    expect(speaker.speakAll(['hello'])).toBe(0)
    expect(synth.spoken).toHaveLength(0)
  })

  it('speaks nothing without an explicit call', () => {
    const synth = fakeSynth()
    createSpeaker(synth, FakeUtterance)
    expect(synth.spoken).toHaveLength(0)
    expect(synth.cancels).toBe(0)
  })

  it('speak() queues one utterance with the text', () => {
    const synth = fakeSynth()
    const speaker = createSpeaker(synth, FakeUtterance)
    expect(speaker.speak('one finding')).toBe(true)
    expect(synth.spoken).toHaveLength(1)
    expect(synth.spoken[0].text).toBe('one finding')
    expect(speaker.speaking()).toBe(true)
  })

  it('speakAll() queues every line and stays speaking until the last ends', () => {
    const synth = fakeSynth()
    const changes = []
    const speaker = createSpeaker(synth, FakeUtterance, { onChange: (v) => changes.push(v) })

    expect(speaker.speakAll(['intro', 'finding one', 'finding two'])).toBe(3)
    expect(synth.spoken.map((u) => u.text)).toEqual(['intro', 'finding one', 'finding two'])
    expect(speaker.speaking()).toBe(true)

    synth.spoken[0].onend()
    synth.spoken[1].onend()
    expect(speaker.speaking()).toBe(true)
    synth.spoken[2].onend()
    expect(speaker.speaking()).toBe(false)
    expect(changes.at(-1)).toBe(false)
  })

  it('skips blank lines rather than queueing empty utterances', () => {
    const synth = fakeSynth()
    const speaker = createSpeaker(synth, FakeUtterance)
    expect(speaker.speakAll(['real', '', '   ', null])).toBe(1)
    expect(synth.spoken).toHaveLength(1)
  })

  it('stop() cancels the queue and reports not speaking immediately', () => {
    const synth = fakeSynth()
    const speaker = createSpeaker(synth, FakeUtterance)
    speaker.speakAll(['one', 'two'])

    expect(speaker.stop()).toBe(true)
    expect(synth.cancels).toBeGreaterThan(0)
    expect(speaker.speaking()).toBe(false)
  })

  it('ignores the end callbacks of a cancelled queue: no miscount of the next one', () => {
    const synth = fakeSynth()
    const speaker = createSpeaker(synth, FakeUtterance)
    speaker.speakAll(['stale one', 'stale two'])
    const stale = [...synth.spoken]
    speaker.stop()

    speaker.speak('fresh')
    // The browser fires end (and sometimes error too) on cancelled utterances.
    stale[0].onend()
    stale[0].onerror()
    stale[1].onend()

    expect(speaker.speaking()).toBe(true)
    synth.spoken.at(-1).onend()
    expect(speaker.speaking()).toBe(false)
  })

  it('a new speak() replaces what was playing rather than piling on', () => {
    const synth = fakeSynth()
    const speaker = createSpeaker(synth, FakeUtterance)
    speaker.speak('first')
    speaker.speak('second')
    expect(synth.cancels).toBeGreaterThan(0)
    expect(synth.spoken.at(-1).text).toBe('second')
    synth.spoken.at(-1).onend()
    expect(speaker.speaking()).toBe(false)
  })
})

// --- what gets said ---------------------------------------------------------

describe('findingSpeech', () => {
  it('says severity, parts, title, detail, fix, and citation in order', () => {
    const speech = findingSpeech({
      severity: 'blocker',
      parts: ['C3', 'U1'],
      title: 'Output cap on the wrong side of the regulator',
      detail: 'C3 is connected upstream of U1.',
      suggested_fix: 'Move C3 to VOUT',
      citation: 'p. 12',
    })
    expect(speech).toContain('Blocker: will not work')
    expect(speech).toContain('Parts C3, U1')
    expect(speech).toContain('Output cap on the wrong side of the regulator')
    expect(speech).toContain('C3 is connected upstream of U1.')
    expect(speech).toContain('Suggested fix: Move C3 to VOUT')
    expect(speech).toContain('nothing is applied automatically')
    expect(speech).toContain('Citation: p. 12')
  })

  it('speaks only what the finding carries — absent fields are not padded', () => {
    const speech = findingSpeech({ severity: 'note', title: 'Just a note' })
    expect(speech).toBe('note. Just a note.')
  })

  it('includes a measured evidence string when present', () => {
    const speech = findingSpeech({ severity: 'marginal', title: 'Gap', evidence: '0.1 mm' })
    expect(speech).toContain('Measured: 0.1 mm')
  })

  it('keeps an unrecognised severity in its own words, never upgraded', () => {
    expect(findingSpeech({ severity: 'sourcing', title: 'T' })).toContain('sourcing')
  })
})

describe('reviewSpeech', () => {
  it('never reads a skipped review as zero findings', () => {
    const lines = reviewSpeech({ findings: [] }, { skipped: true })
    expect(lines).toHaveLength(1)
    expect(lines[0]).toMatch(/skipped/i)
    expect(lines[0]).toMatch(/nothing was checked/i)
    expect(lines[0]).not.toMatch(/0 findings/)
  })

  it('never reads a failed review as a clean one, and speaks the engine reason', () => {
    const lines = reviewSpeech(
      { findings: [], review: { status: 'failed', detail: 'the critic timed out' } },
      { failed: true, failedReason: 'the critic timed out' },
    )
    expect(lines).toHaveLength(1)
    expect(lines[0]).toMatch(/failed/i)
    expect(lines[0]).toContain('the critic timed out')
    expect(lines[0]).toMatch(/not because the board is clean/i)
    expect(lines[0]).not.toMatch(/nothing to flag/i)
    // No reason given: the fixed sentence review.js falls back to, never a blank.
    expect(reviewSpeech({ findings: [] }, { failed: true })[0]).toContain('nothing readable')
  })

  it('reads a skipped review as skipped even when it is also marked failed', () => {
    const lines = reviewSpeech({ findings: [] }, { skipped: true, failed: true })
    expect(lines[0]).toMatch(/skipped/i)
  })

  it('reads a clean review as scoped, not as a blessing', () => {
    const lines = reviewSpeech({ findings: [] }, { skipped: false })
    expect(lines).toHaveLength(1)
    expect(lines[0]).toMatch(/nothing to flag/i)
    expect(lines[0]).toMatch(/human sign-off/i)
  })

  it('reads an intro with honest counts, then each finding, numbered', () => {
    const lines = reviewSpeech({
      findings: [
        { severity: 'blocker', title: 'A' },
        { severity: 'note', title: 'B' },
        { severity: 'note', title: 'C' },
      ],
    })
    expect(lines).toHaveLength(4)
    expect(lines[0]).toContain('3 findings')
    expect(lines[0]).toContain('1 will not work')
    expect(lines[0]).toContain('2 notes')
    expect(lines[1]).toContain('Finding 1 of 3')
    expect(lines[3]).toContain('Finding 3 of 3')
  })

  it('handles a malformed result as no findings, not a crash', () => {
    expect(reviewSpeech(null)).toHaveLength(1)
    expect(reviewSpeech({ findings: 'nope' })).toHaveLength(1)
  })
})

// --- the persisted preference ----------------------------------------------

function fakeStorage(initial = {}) {
  const map = { ...initial }
  return {
    map,
    getItem: (key) => (key in map ? map[key] : null),
    setItem: (key, value) => {
      map[key] = String(value)
    },
  }
}

describe('voice preference', () => {
  it('normalizes to the two values or null, like skin', () => {
    expect(normalizeVoice('on')).toBe('on')
    expect(normalizeVoice('off')).toBe('off')
    for (const junk of [null, undefined, '', 'On', 'muted', 1]) {
      expect(normalizeVoice(junk)).toBe(null)
    }
  })

  it('reads a stored choice, and junk or a blocked storage as no choice', () => {
    expect(readStoredVoice(fakeStorage({ [VOICE_STORAGE_KEY]: 'off' }))).toBe('off')
    expect(readStoredVoice(fakeStorage({ [VOICE_STORAGE_KEY]: 'loud' }))).toBe(null)
    expect(readStoredVoice(blockedStorage)).toBe(null)
  })

  it('writes only real choices and reports whether the write stuck', () => {
    const storage = fakeStorage()
    expect(writeStoredVoice(storage, 'off')).toBe(true)
    expect(storage.map[VOICE_STORAGE_KEY]).toBe('off')
    expect(writeStoredVoice(storage, 'loud')).toBe(false)
    expect(writeStoredVoice(blockedStorage, 'on')).toBe(false)
  })

  it('resolves the default when nothing is stored, and toggles both ways', () => {
    expect(resolveVoice(null)).toBe(DEFAULT_VOICE)
    expect(resolveVoice('off')).toBe('off')
    expect(toggleVoice('on')).toBe('off')
    expect(toggleVoice('off')).toBe('on')
  })
})

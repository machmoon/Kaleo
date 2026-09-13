// An on-device pre-filter for "Ada": does this burst of sound even have the
// *shape* of the wake word?
//
// Why this exists. The recorded-window ear costs one Gemini call per clip,
// and the transcribe prompt names "Ada" three times to earn its recall — so
// over silence the primed name is the likeliest completion and the model
// answers "Ada" to an empty room. A loudness gate stops silence, but it does
// not stop "the LDO regulator needs a bigger capacitor" from costing a call.
// That is why the ear was capped at one listen: without a second filter,
// always-on meant always-paying.
//
// So this module is deliberately NOT a recognizer. It is a cheap acoustic
// classifier that answers one question — "could that have been a two- or
// three-syllable sonorant word?" — and it answers it wrong in both directions
// sometimes. It is only ever a *gate in front of* the paid transcript, never
// a detection on its own: a hit opens the existing confirmation window, and
// the model still has the last word on whether "Ada" was said. Composed that
// way, a false accept costs one call and a false reject costs one missed
// wake; neither can start a board run by itself.
//
// NOTHING IMPORTS THIS YET, AND THAT IS THE CORRECT STATE. Measured on the
// corpus below it accepts 8 of 9 clips containing the wake word — an 11% miss
// on the one thing that must not miss, and a missed wake reads to the engineer
// as "it does not work at all", which is worse than the extra call it saves.
// Those numbers also come only from macOS `say` voices, which bound the
// classifier's behaviour without predicting a human at a desk. So the bar for
// wiring it is recall measured through a real microphone, not more synthesised
// clips (team decision 2026-09-06, with the paid-window lane). Until then it
// must not be wired, and — for the same reason — its `reason` strings must not
// be shown to the engineer either: the product speaks in the first person, and
// saying "I heard two syllables and a hard consonant" on the strength of an
// unvalidated classifier states something it has not earned. The ear's
// existing "(N.N s of noise, not speech — nothing sent)" is measured rather
// than inferred, which is why it is the honest thing to say instead.
//
// Everything here is pure and frame-based, so it is measurable against real
// recordings without a microphone (see `wake-spotter.test.ts`, whose fixture
// is features extracted from `say`-generated speech).
//
// Features, per ~20 ms frame, both computed from the byte time-domain data an
// `AnalyserNode` already gives the ear:
//   rms — loudness on the same 0..128 deviation scale as `peakDeviation`.
//   zcr — zero crossings per sample. Fricatives (/s/ /sh/ /f/ /th/, the /k/
//         burst in "okay") are noise-like and cross zero constantly; the
//         vowels and the /d/ in "Ada" do not. Measured on the corpus, this is
//         the single most useful feature, because "Ada" is unusual in having
//         no obstruent at all.

/** One analyser frame, reduced to the two numbers the classifier uses. */
export interface SpotterFrame {
  /** Peak-normalised RMS deviation from silence, 0..128 (AnalyserNode scale). */
  rms: number;
  /** Zero crossings per sample, 0..1. */
  zcr: number;
}

/** Frame length the thresholds below were measured at. */
export const SPOTTER_FRAME_MS = 20;

/**
 * Loudness a frame needs to count as speech rather than room, 0..128.
 *
 * It shares the number 8 with `LOUDNESS_THRESHOLD` in wake-word.ts but NOT
 * the statistic, and an earlier version of this comment claimed it did. That
 * gate is a per-frame *peak* deviation; this is an *RMS*, which for speech
 * runs several times lower — so 8 here is a materially stricter gate than 8
 * there, and the two numbers must be calibrated separately rather than kept
 * in step. This one was set from the corpus below, whose clips are `say`
 * output and therefore louder than a person at a desk.
 *
 * Open calibration question, stated as a bracket because that is all anyone
 * has measured. A native capture of speech in this room gave `max_volume`
 * -7.1 dB (~56/128) — a single global peak at the loudest instant, not a
 * level a per-frame gate should expect to see sustained — and `mean_volume`
 * -23.3 dB (~9/128), which is an RMS over the *whole* 6 s file with the
 * silence between utterances included, and so is dragged below the true
 * speech level. **The speech-only RMS lies somewhere between 9 and 56 and has
 * never been measured. Neither number licenses a threshold, and in particular
 * this one must not be lowered on the strength of the 9.** Both figures also
 * come from synthesised speech played from laptop speakers into the laptop
 * microphone at a distance of about zero: a best case, not a person across a
 * desk.
 *
 * What 8 means on the data it was actually tuned on: across the corpus below,
 * frames at or above it have p10 11.6, median 24.6, p90 29.8 — so the gate
 * sits well under the bulk of the distribution there, with 59% of all frames
 * (mostly the padded room tone) falling below it. That is reassuring about
 * `say` output and says nothing about a microphone.
 *
 * The measurement that would settle this is a per-frame RMS histogram over
 * speech regions only, from a real-microphone recording of a human voice. It
 * needs no speaker and no paid call. Whoever next has such a recording should
 * produce it rather than deriving anything further from the two numbers above.
 * Byte time-domain data also quantises in steps of 1/128, so quiet speech
 * collapses toward the floor; consider `getFloatTimeDomainData`, which removes
 * that floor but changes the scale and so means regenerating the corpus.
 */
export const SPOTTER_SPEECH_RMS = 8;

/** Frames of quiet that end an utterance (~300 ms). */
export const SPOTTER_TRAIL_FRAMES = 15;

/** Frames of speech that start one (~40 ms). Short: "Ada" is short. */
export const SPOTTER_ONSET_FRAMES = 2;

/** Never hold a segment open longer than this, however long someone talks. */
export const SPOTTER_MAX_SEGMENT_MS = 6_000;

/**
 * How much of an utterance the shape test looks at. The wake word comes
 * first — "Ada, make me a 3.3 V LDO" is one long breath whose first ~0.7 s is
 * the only part that can carry the name — so the classifier reads a leading
 * window and ignores the command behind it.
 */
export const SPOTTER_LEAD_MS = 620;

/**
 * Zero-crossing rate above which a loud frame is called a fricative.
 * 0.15 crossings/sample ≈ 2.4 kHz at 16 kHz, comfortably above voiced speech.
 */
export const SPOTTER_FRICATIVE_ZCR = 0.15;

/** Envelope height, relative to the loudest frame, that a syllable nucleus needs. */
export const SPOTTER_PEAK_RATIO = 0.3;

/** How far the envelope must fall between two nuclei for them to count as two. */
export const SPOTTER_VALLEY_RATIO = 0.85;

/** Loudness, relative to the utterance peak, above which a frame is phonation. */
export const SPOTTER_VOICED_RATIO = 0.45;

/** Voiced-frame zero-crossing rate above which an obstruent was clearly spoken. */
export const SPOTTER_MAX_VOICED_ZCR = 0.2;

/** ------------------------------------------------------------ features */

/**
 * One frame's features from byte time-domain data (`getByteTimeDomainData`),
 * which is centred on 128. Pure, so the thresholds are testable with arrays.
 */
export function frameFeatures(samples: ArrayLike<number>): SpotterFrame {
  const n = samples.length;
  if (n === 0) return { rms: 0, zcr: 0 };
  let sumSquares = 0;
  let crossings = 0;
  let prev = samples[0] - 128;
  for (let i = 0; i < n; i += 1) {
    const v = samples[i] - 128;
    sumSquares += v * v;
    if (i > 0 && (v >= 0) !== (prev >= 0)) crossings += 1;
    prev = v;
  }
  return { rms: Math.sqrt(sumSquares / n), zcr: crossings / n };
}

/** ---------------------------------------------------------- segmenting */

export interface SpotterSegment {
  /** Index of the first speech frame, inclusive. */
  start: number;
  /** Index one past the last speech frame. */
  end: number;
  /** Whether the segment ended on trailing silence rather than the length cap. */
  complete: boolean;
}

export interface SegmentOptions {
  frameMs?: number;
  speechRms?: number;
  onsetFrames?: number;
  trailFrames?: number;
  maxSegmentMs?: number;
}

/**
 * Split a frame stream into utterances: speech onset to trailing silence.
 *
 * This is the part that replaces a blind fixed window. A 4 s clip of a 0.3 s
 * word is 3.7 s of room tone for the model to hallucinate over; a clip that
 * ends when the speaker stops is both cheaper and less suggestible.
 */
export function segmentSpeech(
  frames: readonly SpotterFrame[],
  options: SegmentOptions = {}
): SpotterSegment[] {
  const speechRms = options.speechRms ?? SPOTTER_SPEECH_RMS;
  const onsetFrames = options.onsetFrames ?? SPOTTER_ONSET_FRAMES;
  const trailFrames = options.trailFrames ?? SPOTTER_TRAIL_FRAMES;
  const frameMs = options.frameMs ?? SPOTTER_FRAME_MS;
  const maxFrames = Math.max(1, Math.round((options.maxSegmentMs ?? SPOTTER_MAX_SEGMENT_MS) / frameMs));

  const segments: SpotterSegment[] = [];
  let loudStreak = 0;
  let quietStreak = 0;
  let start = -1;
  let lastLoud = -1;

  const close = (complete: boolean) => {
    if (start < 0) return;
    segments.push({ start, end: lastLoud + 1, complete });
    start = -1;
    lastLoud = -1;
    loudStreak = 0;
    quietStreak = 0;
  };

  for (let i = 0; i < frames.length; i += 1) {
    const loud = frames[i].rms >= speechRms;
    if (start < 0) {
      if (loud) {
        loudStreak += 1;
        if (loudStreak >= onsetFrames) {
          start = i - (onsetFrames - 1);
          lastLoud = i;
          quietStreak = 0;
        }
      } else {
        loudStreak = 0;
      }
      continue;
    }
    if (loud) {
      lastLoud = i;
      quietStreak = 0;
    } else {
      quietStreak += 1;
      if (quietStreak >= trailFrames) {
        close(true);
        continue;
      }
    }
    if (i - start + 1 >= maxFrames) close(false);
  }
  close(false);
  return segments;
}

/** ---------------------------------------------------------- classifier */

export interface WakeShape {
  /** Syllable-like energy peaks in the leading window. */
  syllables: number;
  /** Length of the leading speech run, ms. */
  durationMs: number;
  /** Loudest frame in the window, 0..128. */
  peak: number;
  /** Highest zero-crossing rate among frames loud enough to be phonated. */
  maxVoicedZcr: number;
  /** Share of loud frames that look like a fricative. */
  fricativeFraction: number;
  /** True when the window could plausibly be "Ada" or "hey Ada". */
  wakeShaped: boolean;
  /** Why not, in one clause; empty when `wakeShaped`. */
  reason: string;
}

export interface ShapeOptions {
  frameMs?: number;
  speechRms?: number;
  leadMs?: number;
  fricativeZcr?: number;
  /** Inclusive syllable bounds: "Ada" is 2, "hey Ada" is 3. */
  minSyllables?: number;
  maxSyllables?: number;
  minDurationMs?: number;
  /** Fraction of loud frames allowed to be fricative before rejecting. */
  maxFricativeFraction?: number;
  /** Highest voiced-frame ZCR tolerated; above it, an obstruent was spoken. */
  maxVoicedZcr?: number;
  /** Frames at least this loud, relative to the utterance peak, are "voiced". */
  voicedRatio?: number;
  /** See `countSyllables`. */
  peakRatio?: number;
  valleyRatio?: number;
}

/** 3-frame moving average, so one glottal pulse is not a syllable. */
function smooth(values: readonly number[]): number[] {
  const out = new Array<number>(values.length);
  for (let i = 0; i < values.length; i += 1) {
    let sum = 0;
    let n = 0;
    for (let j = Math.max(0, i - 1); j <= Math.min(values.length - 1, i + 1); j += 1) {
      sum += values[j];
      n += 1;
    }
    out[i] = sum / n;
  }
  return out;
}

/**
 * Count energy peaks separated by a real valley.
 *
 * A syllable nucleus is a loud vowel; the consonant between two of them dips.
 * "Ada" is /eɪ.də/ — two humps with a dip at the /d/ — and that dip, not the
 * spectrum, is what this counts. `valleyRatio` is how far the envelope has to
 * fall between two peaks before they count as two rather than one wobble.
 */
export function countSyllables(
  rms: readonly number[],
  { peakRatio, valleyRatio }: { peakRatio?: number; valleyRatio?: number } = {}
): number {
  if (rms.length === 0) return 0;
  const env = smooth(rms);
  const max = Math.max(...env);
  if (max <= 0) return 0;
  const floor = max * (peakRatio ?? SPOTTER_PEAK_RATIO);
  const valley = valleyRatio ?? SPOTTER_VALLEY_RATIO;
  // Every local maximum above the floor, including plateau shoulders.
  const candidates: number[] = [];
  for (let i = 0; i < env.length; i += 1) {
    if (env[i] < floor) continue;
    const left = i > 0 ? env[i - 1] : -Infinity;
    const right = i < env.length - 1 ? env[i + 1] : -Infinity;
    if (env[i] >= left && env[i] >= right) candidates.push(i);
  }
  // Two maxima are two syllables only if the envelope really falls between
  // them; otherwise they are one nucleus with a wobble in it.
  const kept: number[] = [];
  for (const i of candidates) {
    const prev = kept[kept.length - 1];
    if (prev === undefined) {
      kept.push(i);
      continue;
    }
    let dip = Infinity;
    for (let j = prev; j <= i; j += 1) dip = Math.min(dip, env[j]);
    if (dip <= valley * Math.min(env[prev], env[i])) kept.push(i);
    else if (env[i] > env[prev]) kept[kept.length - 1] = i;
  }
  return kept.length;
}

/**
 * Score the leading window of an utterance against the shape of "Ada".
 *
 * Honest about what it is: three coarse acoustic tests, tuned on synthesised
 * speech, that a determined sentence can pass by accident. It exists to make
 * always-on listening affordable, not to decide anything on its own.
 */
export function wakeShape(
  frames: readonly SpotterFrame[],
  options: ShapeOptions = {}
): WakeShape {
  const frameMs = options.frameMs ?? SPOTTER_FRAME_MS;
  const speechRms = options.speechRms ?? SPOTTER_SPEECH_RMS;
  const leadFrames = Math.max(1, Math.round((options.leadMs ?? SPOTTER_LEAD_MS) / frameMs));
  const fricativeZcr = options.fricativeZcr ?? SPOTTER_FRICATIVE_ZCR;
  const minSyllables = options.minSyllables ?? 2;
  const maxSyllables = options.maxSyllables ?? 3;
  const minDurationMs = options.minDurationMs ?? 140;
  const maxFricativeFraction = options.maxFricativeFraction ?? 0.14;
  const maxVoicedZcr = options.maxVoicedZcr ?? SPOTTER_MAX_VOICED_ZCR;

  const window = frames.slice(0, leadFrames);
  const rms = window.map((f) => f.rms);
  const peak = rms.length ? Math.max(...rms) : 0;
  const loud = window.filter((f) => f.rms >= speechRms);
  // "Voiced" here means loud relative to this utterance, not loud in the room:
  // the onset frame is half room tone and its zero crossings are the room's,
  // which is why a bare threshold on every loud frame rejects real "Ada"s.
  const voiced = window.filter(
    (f) => f.rms >= Math.max(speechRms, peak * (options.voicedRatio ?? SPOTTER_VOICED_RATIO))
  );
  const durationMs = loud.length * frameMs;
  const maxVoiced = voiced.length ? Math.max(...voiced.map((f) => f.zcr)) : 0;
  const fricativeFraction = loud.length
    ? loud.filter((f) => f.zcr >= fricativeZcr).length / loud.length
    : 0;
  const syllables = countSyllables(rms, {
    peakRatio: options.peakRatio,
    valleyRatio: options.valleyRatio,
  });

  let reason = "";
  if (peak < speechRms) reason = "no speech in the window";
  else if (durationMs < minDurationMs) reason = "too short to be a word";
  else if (syllables < minSyllables) reason = "one syllable, Ada has two";
  else if (syllables > maxSyllables) reason = "too many syllables to start with Ada";
  else if (maxVoiced > maxVoicedZcr) reason = "a hard consonant Ada does not have";
  else if (fricativeFraction > maxFricativeFraction) reason = "too much hiss for Ada";

  return {
    syllables,
    durationMs,
    peak: Math.round(peak * 10) / 10,
    maxVoicedZcr: Math.round(maxVoiced * 1000) / 1000,
    fricativeFraction: Math.round(fricativeFraction * 100) / 100,
    wakeShaped: reason === "",
    reason,
  };
}

/** ------------------------------------------------------------- runtime */

export interface SpotterEvents {
  /** An utterance ended. `shape` says whether it is worth a paid transcript. */
  onUtterance: (segment: { durationMs: number; shape: WakeShape }) => void;
  /** Speech started — the caller can begin recording before we know the shape. */
  onSpeechStart?: () => void;
}

export interface Spotter {
  /** Feed one analyser frame. */
  push(frame: SpotterFrame): void;
  /** Forget any open utterance without emitting it. */
  reset(): void;
  /** True while an utterance is open (the caller should be recording). */
  readonly speaking: boolean;
}

/**
 * The streaming form of the two functions above: one frame in at a time,
 * one `onUtterance` out per burst of speech.
 *
 * Deliberately separate from the recorder in wake-word.ts. Recording is free
 * and local; the caller starts it at `onSpeechStart` so the beginning of the
 * word is not lost, and only *sends* it when `onUtterance` reports a
 * wake-shaped window. The classifier never has to predict the future.
 */
export function createWakeSpotter(
  events: SpotterEvents,
  options: SegmentOptions & ShapeOptions = {}
): Spotter {
  const frameMs = options.frameMs ?? SPOTTER_FRAME_MS;
  const speechRms = options.speechRms ?? SPOTTER_SPEECH_RMS;
  const onsetFrames = options.onsetFrames ?? SPOTTER_ONSET_FRAMES;
  const trailFrames = options.trailFrames ?? SPOTTER_TRAIL_FRAMES;
  const maxFrames = Math.max(
    1,
    Math.round((options.maxSegmentMs ?? SPOTTER_MAX_SEGMENT_MS) / frameMs)
  );

  let open: SpotterFrame[] | null = null;
  let recent: SpotterFrame[] = [];
  let loudStreak = 0;
  let quietStreak = 0;

  const emit = () => {
    const frames = open;
    open = null;
    loudStreak = 0;
    quietStreak = 0;
    if (!frames) return;
    // Trim the trailing silence back off before classifying and reporting.
    let end = frames.length;
    while (end > 0 && frames[end - 1].rms < speechRms) end -= 1;
    const spoken = frames.slice(0, end);
    events.onUtterance({
      durationMs: spoken.length * frameMs,
      shape: wakeShape(spoken, options),
    });
  };

  return {
    get speaking() {
      return open !== null;
    },
    reset() {
      open = null;
      recent = [];
      loudStreak = 0;
      quietStreak = 0;
    },
    push(frame: SpotterFrame) {
      const loud = frame.rms >= speechRms;
      if (open === null) {
        // Keep the onset frames so the word does not start mid-vowel.
        recent.push(frame);
        if (recent.length > onsetFrames) recent.shift();
        if (!loud) {
          loudStreak = 0;
          return;
        }
        loudStreak += 1;
        if (loudStreak < onsetFrames) return;
        open = [...recent];
        recent = [];
        quietStreak = 0;
        events.onSpeechStart?.();
        return;
      }
      open.push(frame);
      if (loud) {
        quietStreak = 0;
      } else {
        quietStreak += 1;
        if (quietStreak >= trailFrames) {
          emit();
          return;
        }
      }
      if (open.length >= maxFrames) emit();
    },
  };
}

/** ---------------------------------------------------- analyser adapter */

export interface AnalyserFeedDeps {
  /** Live microphone stream; the feed never opens one itself. */
  stream: MediaStream;
  AudioContext: typeof AudioContext;
  setInterval?: typeof globalThis.setInterval;
  clearInterval?: typeof globalThis.clearInterval;
  frameMs?: number;
  /**
   * 1024 samples is ~21 ms at 48 kHz, and the default `frameMs` is 20 — so
   * consecutive reads very nearly tile the audio instead of sampling it.
   * That is deliberate. The ear's own meter polls a 1024-sample analyser
   * every 100 ms, which observes 21 ms in every 100 and discards four fifths
   * of the timeline; measured in the real WKWebView, 15 s of continuous
   * speech gave *that* poll a longest run of two frames over threshold where
   * its gate wanted three. Those run-length figures describe the 100 ms gate
   * and are not a bound on this feed — reading at the refresh rate should see
   * materially more frames over any threshold, precisely because it is not
   * throwing the timeline away. A spotter that samples the room cannot
   * segment an utterance, which is why this reads at the rate the analyser
   * actually refreshes.
   */
  fftSize?: number;
}

export interface AnalyserFeed {
  /** Release the audio graph. Safe to call twice; never touches the stream. */
  close(): void;
}

/**
 * Drive a spotter from a live microphone, one `AnalyserNode` frame at a time.
 *
 * Split out from the ear on purpose. The ear owns the microphone, the
 * recorder, and the budget; this owns only the arithmetic between a stream
 * and a `Spotter`, so both can be tested without the other. It throws rather
 * than degrading if the audio graph will not build — a spotter that silently
 * stops feeding is an ear that claims to listen and does not.
 */
export function createAnalyserFeed(deps: AnalyserFeedDeps, spotter: Spotter): AnalyserFeed {
  const frameMs = deps.frameMs ?? SPOTTER_FRAME_MS;
  const schedule = deps.setInterval ?? globalThis.setInterval.bind(globalThis);
  const unschedule = deps.clearInterval ?? globalThis.clearInterval.bind(globalThis);
  const context = new deps.AudioContext();
  let analyser: AnalyserNode | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;
  try {
    analyser = context.createAnalyser();
    analyser.fftSize = deps.fftSize ?? 1024;
    context.createMediaStreamSource(deps.stream).connect(analyser);
  } catch (error) {
    void context.close?.();
    throw new Error(
      `could not measure the room level: ${(error as Error)?.message || "unknown"}`
    );
  }
  const buffer = new Uint8Array(analyser.fftSize);
  const node = analyser;
  timer = schedule(() => {
    node.getByteTimeDomainData(buffer);
    spotter.push(frameFeatures(buffer));
  }, frameMs);
  return {
    close() {
      if (timer !== null) {
        unschedule(timer);
        timer = null;
      }
      analyser = null;
      spotter.reset();
      void context.close?.();
    },
  };
}

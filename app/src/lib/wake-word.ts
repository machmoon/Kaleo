// "Ada": the wake word, and the two ways the overlay can hear it.
//
// Listening for a word is a hot microphone, and a hot microphone is the one
// thing this app must never hide. So everything here is built so the page
// can show the truth at all times: which backend is listening, and — for the
// paid one — how many model calls it has made and how many it may still make.
//
// Two backends, chosen at runtime, never assumed from documentation:
//
// `speech` — the webview's own `SpeechRecognition` (`webkitSpeechRecognition`
//   in WebKit and Chromium). No request leaves this app for it; the OS
//   recognizer does the work. WKWebView does not currently expose it, so on
//   macOS this branch is usually skipped — the check is real, not decorative.
//
// `windows` — local speech detect (VAD), then one short clip POSTed to
//   `/transcribe` per utterance, tagged `purpose: "wake"` so it cannot
//   burn the board-run pacer. Silence costs nothing, so the ear can stay
//   armed across wakes and quiet spells: what bounds it is a visible
//   budget of model calls per unmute, not a cap of one listen. Rolling
//   fixed windows were retired twice over — first because always-on
//   four-second slices burned quota on room tone, then because a window
//   that starts on a loudness trigger and runs a fixed four seconds cuts
//   "Hey Ada, make me a…" in half. One window is now one utterance,
//   opened on the first loud frame and closed by trailing silence. The
//   mic button (PTT) remains the path that always works.
//
// The pure parts — matching the word, choosing a backend, the loudness gate —
// are exported on their own so they are testable without a microphone.

import { pickRecordingMime, transcribe as transcribeAudio } from "@/lib/silkscreen/voice";
import type { LocalWakeHit } from "@/lib/local-wake";

/** What the engineer says to get Ada's attention. */
export const WAKE_WORD = "Ada";

/**
 * Spellings a recognizer produces for the spoken word. "Ada" is short and
 * unusual, and both recognizers reach for a likelier word: the first name
 * "Aida", the initialism "ADA", or a phonetic guess. Matching a handful of
 * them is the difference between a wake word that works and one that
 * works in the demo video only. Whole words only — "adapter" is not "Ada".
 * Glued forms ("heyada") show up when the model drops the space after "hey".
 */
export const WAKE_ALIASES: readonly string[] = Object.freeze([
  "ada",
  "aida",
  "ayda",
  "adah",
  "adda",
  "eda",
  "ida",
  "oda",
  "odda",
  "otto", // Gemini often hears "hey Otto"
  "auto",
  "aider",
  "heyada",
  "heyaida",
  "heyadda",
  "heyotto",
  "heyauto",
]);

/** Prefaces the model often sticks before the name. */
const WAKE_PREFIXES: readonly string[] = Object.freeze([
  "hey",
  "hi",
  "ok",
  "okay",
  "yo",
]);

/**
 * How long one speech-triggered clip lasts once you start talking.
 * Not a rolling timer — idle silence costs zero engine calls.
 */
export const WINDOW_MS = 4_000;

/**
 * Legacy rolling hop (tests / no AudioContext). Production uses VAD:
 * record only after speech is heard, so this is unused on the ear.
 */
export const WINDOW_HOP_MS = 4_000;

/**
 * Loud analyser frames (~100 ms each) inside a window before that window
 * counts as speech. Filters clicks, coughs and chair squeaks.
 *
 * Recording starts on the *first* loud frame, not the third: waiting three
 * frames throws away the "H" of "Hey Ada", and the head of an utterance is
 * exactly where the wake word lives. The extra frames are still required —
 * but as a condition for *sending* the clip, not for opening the recorder.
 * Recording is free; the model call is not, so the gate belongs in front of
 * the send, which is where the measured silence-hallucinates-"Ada" failure
 * would otherwise get in.
 */
export const SPEECH_ON_FRAMES = 3;

/**
 * Consecutive quiet frames (~100 ms each) that end an utterance.
 *
 * 700 ms: long enough to survive the pause between "Hey Ada" and the thing
 * being asked for, short enough that the clip does not trail off into room
 * tone the model will happily turn into words.
 */
export const SILENCE_OFF_FRAMES = 7;

/**
 * A window shorter than this is a door, a cough or a chair, and is thrown
 * away locally rather than sent. Nothing that short carries a wake word.
 */
export const MIN_UTTERANCE_MS = 800;

/**
 * The hard ceiling on one window. Someone reading a part number out loud
 * needs more than four seconds; nobody needs more than this, and an open
 * recorder that never closes is a stuck microphone.
 */
export const MAX_UTTERANCE_MS = 9_000;

/**
 * The listening budget: model calls one unmute may spend before the ear
 * stops and says so. Not a per-arming cap — the ear keeps listening across
 * wakes and quiet spells until this is spent or you mute it, which is the
 * only shape that behaves like a wake word. Silence spends nothing.
 */
export const DEFAULT_WINDOW_CAP = 10;

/** How much one "keep listening" adds to the budget. */
export const WAKE_BUDGET_STEP = 10;

/** The most a budget may be raised to in one session. */
export const MAX_WAKE_BUDGET = 60;

/** Clamp a requested budget to something the ear will honour. */
export function clampWakeBudget(budget: number): number {
  if (!Number.isFinite(budget)) return DEFAULT_WINDOW_CAP;
  return Math.max(1, Math.min(MAX_WAKE_BUDGET, Math.round(budget)));
}

/**
 * After a bare “Ada” (no command yet), keep accepting the next utterance
 * without requiring the name again — “hey Ada” … “make me an LDO”.
 */
export const CONTINUATION_MS = 12_000;

/**
 * Peak sample deviation (0–128) that counts as speech for VAD.
 */
export const LOUDNESS_THRESHOLD = 8;

export type WakeBackendName = "speech" | "windows" | "local";

export type WakeListenerState =
  | "listening"
  | "stopped"
  /** The `windows` backend spent its cap and stopped itself. */
  | "capped"
  | "error";

export interface WakeDetection {
  /** What was said after the wake word, if anything; empty means just "Ada". */
  utterance: string;
  backend: WakeBackendName;
  /**
   * `windows` backend: did the clip that produced this detection pass the
   * local loudness gate? `false` means it did not, and a bare-name wake from
   * an ungated clip is indistinguishable from the model hallucinating the
   * primed name over silence — so the page must not spend a continuation
   * window on it. Absent where no clip was recorded (`speech`, `local`).
   */
  gated?: boolean;
}

export interface WakeListenerEvents {
  onWake: (detection: WakeDetection) => void;
  onState: (state: WakeListenerState, detail: string) => void;
  /** `windows` backend only: called after every window sent to the engine. */
  onWindow?: (sent: number, cap: number) => void;
  /**
   * `windows` backend: every transcript, matched or not — so the ear can
   * show that the mic is alive when the wake word was not in the clip.
   */
  onGlimpse?: (text: string) => void;
  /**
   * `windows` backend: the peak deviation (0-128) of each ~100 ms analyser
   * frame, as `peakDeviation` computes it, and `null` the moment the
   * microphone is released.
   *
   * `null` rather than `0`, because those are different facts: `0` is a
   * silent room with the microphone open, and `null` is no microphone. An
   * indicator that cannot tell them apart draws "listening, quietly" at a
   * closed mic, which is the lie.
   *
   * This exists so the strip can draw a level meter *from the tap that is
   * already open*. Opening a second `AudioContext` beside this one to animate
   * an indicator would mean two taps on one input device — a second
   * permission surface and a second stream to leak — and, worse, an indicator
   * with a life of its own that can keep waving after this listener has let
   * the microphone go. The trailing `0` is what makes "the mic just closed"
   * arrive as a fact rather than as a timeout.
   */
  onLevel?: (peak: number | null) => void;
}

export interface WakeWordListener {
  readonly backend: WakeBackendName;
  /** Begin listening. Resolves once the microphone is open (or rejects). */
  start(): Promise<void>;
  /** Release the microphone now. Safe to call twice. */
  stop(): void;
}

// ------------------------------------------------------------- matching

function normalise(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s']/gu, " ")
    .split(/\s+/)
    .filter(Boolean)
    .map((w) => w.replace(/^'+|'+$/g, ""));
}

/** Levenshtein distance; used for short phonetic near-misses of "ada". */
export function editDistance(a: string, b: string): number {
  if (a === b) return 0;
  if (!a.length) return b.length;
  if (!b.length) return a.length;
  const prev = new Array<number>(b.length + 1);
  const cur = new Array<number>(b.length + 1);
  for (let j = 0; j <= b.length; j += 1) prev[j] = j;
  for (let i = 1; i <= a.length; i += 1) {
    cur[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
    }
    for (let j = 0; j <= b.length; j += 1) prev[j] = cur[j];
  }
  return prev[b.length];
}

function tokenIsWake(token: string): boolean {
  if (!token) return false;
  if (WAKE_ALIASES.includes(token)) return true;
  // One edit from "ada", length-bounded so "adam"/"adapter" stay out.
  if (token.length >= 3 && token.length <= 4 && editDistance(token, "ada") <= 1) {
    return true;
  }
  return false;
}

/**
 * Does `text` contain the wake word, and what follows it?
 *
 * Returns `null` when it does not. The utterance is everything after the
 * first match — "hey Ada, I need a 3.3 volt regulator" gives "I need a 3.3
 * volt regulator" — so a single breath can carry both the word and the need.
 * Matching is by whole token against `WAKE_ALIASES` (plus short edit-distance
 * near-misses and “hey &lt;near-ada&gt;”); punctuation and case are ignored,
 * and "adapter" or "Canada" never match.
 */
export function matchWakeWord(text: string): { utterance: string } | null {
  const trimmed = text.trim();
  if (!trimmed || /^\(inaudible\)$/i.test(trimmed)) return null;
  // Walk the original words so the tail keeps its casing (part numbers,
  // "USB"); each word is normalised on its own for the comparison.
  const words = text.split(/\s+/).filter(Boolean);
  for (let i = 0; i < words.length; i += 1) {
    const tokens = normalise(words[i]);
    let hit = tokens.some((token) => tokenIsWake(token));
    // "hey Otto" / "hi Ada" — preface then a soft name on the next word.
    if (
      !hit &&
      tokens.some((t) => WAKE_PREFIXES.includes(t)) &&
      i + 1 < words.length &&
      normalise(words[i + 1]).some((token) => tokenIsWake(token))
    ) {
      hit = true;
      i += 1; // utterance starts after the name, not after "hey"
    }
    if (!hit) continue;
    const tail = words
      .slice(i + 1)
      .join(" ")
      .replace(/^[\s,.;:!?-]+/, "")
      .trim();
    return { utterance: tail };
  }
  return null;
}

/** True when a continuation window should treat this as the engineer speaking. */
export function isContinuationSpeech(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed || /^\(inaudible\)$/i.test(trimmed)) return false;
  // Drop pure filler the model invents for silence.
  if (/^(um+|uh+|hmm+|ah+)$/i.test(trimmed)) return false;
  return trimmed.length >= 2;
}

// ------------------------------------------------------------- selection

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

/** The subset of the Web Speech API's `SpeechRecognition` this file uses. */
export interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechResultEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error?: string; message?: string }) => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

export interface SpeechResultEventLike {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
}

export interface WakeGlobals {
  SpeechRecognition?: SpeechRecognitionCtor;
  webkitSpeechRecognition?: SpeechRecognitionCtor;
  MediaRecorder?: typeof MediaRecorder;
  AudioContext?: typeof AudioContext;
  webkitAudioContext?: typeof AudioContext;
  navigator?: {
    platform?: string;
    userAgent?: string;
    mediaDevices?: { getUserMedia?: MediaDevices["getUserMedia"] };
  };
}

export function speechRecognitionCtor(
  globals: WakeGlobals = globalThis as WakeGlobals
): SpeechRecognitionCtor | null {
  const ctor = globals.SpeechRecognition ?? globals.webkitSpeechRecognition;
  return typeof ctor === "function" ? ctor : null;
}

/** True when recorded windows are the reliable path (Tauri WKWebView on macOS). */
export function prefersRecordedWake(
  globals: WakeGlobals = globalThis as WakeGlobals
): boolean {
  const platform = (globals.navigator?.platform ?? "").toLowerCase();
  const ua = (globals.navigator?.userAgent ?? "").toLowerCase();
  // macOS WKWebView exposes webkitSpeechRecognition but it fails with
  // not-allowed for non-Safari hosts — the same "microphone permission was
  // refused" the ear was showing. Prefer the mic + engine path there.
  return platform.includes("mac") || ua.includes("mac os");
}

/**
 * Which backend this webview can run, or `null` when it can run neither.
 *
 * Checked against the real globals every time, because the answer differs
 * between Tauri's webviews and across OS versions, and a wake word that
 * claims to listen while its API is missing is worse than none.
 */
export function chooseWakeBackend(
  globals: WakeGlobals = globalThis as WakeGlobals
): WakeBackendName | null {
  const canRecord =
    typeof globals.MediaRecorder === "function" &&
    typeof globals.navigator?.mediaDevices?.getUserMedia === "function";
  if (canRecord && prefersRecordedWake(globals)) return "windows";
  if (speechRecognitionCtor(globals)) return "speech";
  return canRecord ? "windows" : null;
}

/** The one sentence the bar shows for a backend, cost included. */
export function describeWakeBackend(backend: WakeBackendName | null): string {
  switch (backend) {
    case "speech":
      return "the OS speech recognizer, no engine calls";
    case "local":
      return "on-device wake word, then one engine transcript for the command";
    case "windows":
      return `local speech detect, then one Gemini transcript per utterance — silence costs nothing, and the listening budget is ${DEFAULT_WINDOW_CAP} calls (not always-on; I stop and say so when it is spent). The mic button is the reliable path`;
    default:
      return "no speech recognition or recorder in this webview";
  }
}

// ------------------------------------------------------------- loudness

/**
 * Peak deviation from silence in one analyser frame (byte time-domain data
 * is centred on 128). Pure, so the gate's threshold is testable with arrays.
 */
export function peakDeviation(samples: ArrayLike<number>): number {
  let peak = 0;
  for (let i = 0; i < samples.length; i += 1) {
    const deviation = Math.abs(samples[i] - 128);
    if (deviation > peak) peak = deviation;
  }
  return peak;
}

export function loudEnough(peak: number, threshold = LOUDNESS_THRESHOLD): boolean {
  return peak >= threshold;
}

// ------------------------------------------------------------- listeners

export interface SpeechListenerDeps {
  recognition: SpeechRecognitionCtor;
  lang?: string;
}

/**
 * The free backend: continuous recognition, final results only.
 *
 * Final results only, because an interim "ada" with nothing after it would
 * fire before the engineer finished the sentence that carries the need. The
 * recognizer ends itself after silence, so `onend` restarts it for as long
 * as the listener is armed — and `stop()` flips the flag first, so a restart
 * can never race a stop.
 */
export function createSpeechListener(
  deps: SpeechListenerDeps,
  events: WakeListenerEvents
): WakeWordListener {
  let recognition: SpeechRecognitionLike | null = null;
  let armed = false;

  const stop = () => {
    armed = false;
    const current = recognition;
    recognition = null;
    if (current) {
      current.onresult = null;
      current.onend = null;
      current.onerror = null;
      try {
        current.abort();
      } catch {
        // Already ended; nothing to release.
      }
    }
  };

  const spin = () => {
    if (!armed) return;
    const instance = new deps.recognition();
    recognition = instance;
    instance.continuous = true;
    instance.interimResults = false;
    instance.lang = deps.lang ?? "en-US";
    instance.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (!result.isFinal) continue;
        const match = matchWakeWord(result[0].transcript);
        if (!match) continue;
        stop();
        events.onState("stopped", "heard the wake word");
        events.onWake({ utterance: match.utterance, backend: "speech" });
        return;
      }
    };
    instance.onerror = (event) => {
      const code = event.error ?? "";
      // Silence and the recognizer's own housekeeping are not errors; the
      // restart in onend covers them. Permission and network are.
      if (code === "no-speech" || code === "aborted" || code === "audio-capture") return;
      stop();
      events.onState(
        "error",
        code === "not-allowed" || code === "service-not-allowed"
          ? "speech recognition was refused — enable Microphone (and Speech Recognition) for Hardy in System Settings, or use the ear after a restart"
          : `speech recognition failed: ${code || event.message || "unknown"}`
      );
    };
    instance.onend = () => {
      if (armed && recognition === instance) {
        recognition = null;
        spin();
      }
    };
    instance.start();
  };

  return {
    backend: "speech",
    async start() {
      if (armed) return;
      armed = true;
      try {
        spin();
      } catch (error) {
        armed = false;
        recognition = null;
        throw new Error(
          `could not start speech recognition: ${(error as Error)?.message || "unknown"}`
        );
      }
      events.onState("listening", "listening for the wake word with the OS speech recognizer");
    },
    stop() {
      if (!armed && !recognition) return;
      stop();
      events.onState("stopped", "stopped listening");
    },
  };
}

export interface WindowListenerDeps {
  baseUrl: string;
  token?: string;
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  MediaRecorder: typeof MediaRecorder;
  /** Absent refuses to arm unless `vad: false` opts into ungated windows. */
  AudioContext?: typeof AudioContext;
  transcribe?: typeof transcribeAudio;
  /** Prefer a Gemini-friendly container; defaults to `pickRecordingMime`. */
  pickMime?: () => string;
  windowMs?: number;
  /**
   * Start the next window this many ms after the current one. Defaults to
   * `WINDOW_HOP_MS`. Set equal to `windowMs` for non-overlapping windows.
   */
  hopMs?: number;
  /** The session budget in model calls (see DEFAULT_WINDOW_CAP). */
  cap?: number;
  /**
   * Calls already spent this session, so a re-armed listener continues one
   * budget rather than starting a fresh one. `onWindow` reports the running
   * total against `cap`, which is what the control puts on its face.
   */
  spent?: number;
  /** Shortest window worth sending; below this it is a cough. */
  minUtteranceMs?: number;
  /** Longest window; the recorder is closed here whatever the room is doing. */
  maxUtteranceMs?: number;
  /** Consecutive quiet analyser frames that end an utterance. */
  silenceFrames?: number;
  setTimeout?: typeof globalThis.setTimeout;
  clearTimeout?: typeof globalThis.clearTimeout;
  /**
   * After a bare “Ada”, true while the page still wants the next utterance
   * without the wake word again.
   */
  continuing?: () => boolean;
  /**
   * When left unset, the loudness gate is required: only record after local
   * speech is detected, and refuse to arm at all when no analyser can be
   * built. `false` is the explicit ungated opt-in (rolling windows, every
   * clip sent blind) and nothing in production passes it.
   */
  vad?: boolean;
}

/**
 * The paid backend: mic stays open, engine only runs when you speak (VAD).
 *
 * One window is one **utterance**, not one slice of the clock. The analyser
 * opens the recorder on the first loud frame — so the clip starts before
 * "Hey" is finished — and closes it after `silenceFrames` quiet frames, with
 * `minUtteranceMs` as a floor and `maxUtteranceMs` as a ceiling. A window
 * that never accumulated `SPEECH_ON_FRAMES` loud frames, or that ended
 * before the floor, is discarded locally and costs nothing: that is the
 * cough. Everything else is POSTed to `/transcribe` as `purpose: "wake"`.
 *
 * Idle silence sends nothing at all, which is what makes an always-listening
 * ear affordable; `cap` is the session budget in model calls and `spent`
 * carries it across re-armings, so the number on the control counts one
 * unmute rather than resetting behind the user's back. Without AudioContext
 * (tests) it falls back to fixed rolling windows so the suite stays
 * deterministic — and in production that path refuses to arm instead,
 * because a window nothing measured is a paid call whose likeliest answer
 * over silence is the wake name the transcribe prompt primes.
 */
export function createWindowListener(
  deps: WindowListenerDeps,
  events: WakeListenerEvents
): WakeWordListener {
  const windowMs = deps.windowMs ?? WINDOW_MS;
  const hopMs = deps.hopMs ?? WINDOW_HOP_MS;
  const overlap = hopMs < windowMs;
  const cap = deps.cap ?? DEFAULT_WINDOW_CAP;
  const minUtteranceMs = deps.minUtteranceMs ?? MIN_UTTERANCE_MS;
  const maxUtteranceMs = deps.maxUtteranceMs ?? MAX_UTTERANCE_MS;
  const silenceFrames = deps.silenceFrames ?? SILENCE_OFF_FRAMES;
  const transcribe = deps.transcribe ?? transcribeAudio;
  const pickMime = deps.pickMime ?? pickRecordingMime;
  const schedule = deps.setTimeout ?? globalThis.setTimeout.bind(globalThis);
  const unschedule = deps.clearTimeout ?? globalThis.clearTimeout.bind(globalThis);
  // `vad: false` is the only way to arm without a loudness gate, and nothing
  // in production passes it: an unmeasured window is a paid call whose
  // likeliest answer over silence is the wake name the prompt primes.
  const gateRequired = deps.vad !== false;
  const vadMode = gateRequired && typeof deps.AudioContext === "function";

  let armed = false;
  let stream: MediaStream | null = null;
  const recorders = new Set<MediaRecorder>();
  const stopTimers = new Set<ReturnType<typeof setTimeout>>();
  let hopTimer: ReturnType<typeof setTimeout> | null = null;
  let sent = deps.spent ?? 0;
  let generation = 0;
  let consecutiveFails = 0;
  const inflight = new Set<AbortController>();
  let context: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  /** Per-window peaks so overlapping recorders do not share one counter. */
  const activePeaks = new Set<{ value: number }>();
  let meter: ReturnType<typeof setInterval> | null = null;
  /**
   * The utterance being recorded right now, counted in 100 ms analyser
   * frames rather than wall-clock so the endpointing is deterministic under
   * a test's fake clock and cannot drift with `Date.now`.
   */
  let utterance: {
    instance: MediaRecorder;
    frames: number;
    loudFrames: number;
    quietFrames: number;
  } | null = null;

  const clearTimers = () => {
    for (const id of stopTimers) unschedule(id);
    stopTimers.clear();
    if (hopTimer) {
      unschedule(hopTimer);
      hopTimer = null;
    }
  };

  const releaseMedia = () => {
    clearTimers();
    if (meter) {
      clearInterval(meter);
      meter = null;
    }
    const current = stream;
    stream = null;
    if (current) {
      for (const track of current.getTracks()) {
        track.stop();
        track.enabled = false;
      }
    }
    if (context) {
      void context.close().catch(() => undefined);
      context = null;
      analyser = null;
    }
    activePeaks.clear();
    utterance = null;
    // The microphone is gone; say so now rather than letting a meter decide
    // from a timeout. Anything drawing this level must stop drawing it.
    events.onLevel?.(null);
  };

  /** Microphone off and recorders gone; windows already sent keep settling. */
  const park = () => {
    armed = false;
    clearTimers();
    for (const current of [...recorders]) {
      recorders.delete(current);
      current.ondataavailable = null;
      current.onstop = null;
      if (current.state === "recording") {
        try {
          current.stop();
        } catch {
          // Already inactive.
        }
      }
    }
    releaseMedia();
  };

  /** Everything off, including every window still in flight. */
  const stop = () => {
    park();
    generation += 1;
    for (const controller of inflight) controller.abort();
    inflight.clear();
  };

  const finish = (state: WakeListenerState, detail: string) => {
    stop();
    events.onState(state, detail);
  };

  const send = (blob: Blob, mimeType: string, peak: number | null, gated: boolean) => {
    sent += 1;
    events.onWindow?.(sent, cap);
    const mine = new AbortController();
    const myGeneration = generation;
    inflight.add(mine);
    const live = () => myGeneration === generation && !mine.signal.aborted;
    transcribe(
      deps.baseUrl,
      {
        blob,
        mimeType,
        token: deps.token,
        language: "en-US",
        purpose: "wake",
        // Only a measured peak travels: an ungated window has no number, and
        // sending 0 would claim a measurement that was never taken.
        ...(peak === null ? {} : { peak }),
      },
      mine.signal
    )
      .then((result) => {
        if (!live()) return;
        consecutiveFails = 0;
        const text = result.text.trim();
        if (text) events.onGlimpse?.(text);
        // This listener no longer spots the wake word. Deciding whether a
        // name was spoken by transcribing room tone and string-matching the
        // result is what the on-device spotter replaced, and it cost a model
        // call per window to do it. What is left is the honest job: record
        // one utterance the engineer has already asked for — inside a
        // continuation window, so only after a wake or push-to-talk — and
        // transcribe that.
        if (deps.continuing?.() && isContinuationSpeech(result.text)) {
          // The clip can still catch the name, because the spotter fires on
          // "Hey Ada" and the engineer keeps talking: "Hey Ada, make me an
          // LDO" arrives whole. Hand on the request, not the greeting.
          const named = matchWakeWord(result.text);
          finish("stopped", "heard the rest of what you said");
          events.onWake({
            utterance: named ? named.utterance : result.text.trim(),
            backend: "windows",
            gated,
          });
        }
      })
      .catch((error) => {
        if (!live()) return;
        const kind = (error as { kind?: string })?.kind;
        if (kind === "auth" || kind === "setup" || kind === "offline") {
          finish("error", `the engine refused the audio: ${(error as Error).message}`);
          return;
        }
        if (kind === "timeout" || kind === "upstream" || kind === "server") {
          consecutiveFails += 1;
          events.onGlimpse?.(
            `engine: ${(error as Error).message || kind} (${consecutiveFails}/3)`
          );
          if (consecutiveFails >= 3) {
            finish(
              "error",
              `transcription keeps failing: ${(error as Error).message || kind}`
            );
          }
        }
      })
      .finally(() => {
        inflight.delete(mine);
      });
  };

  const record = () => {
    if (!armed || !stream) return;
    if (sent >= cap) return;
    if (recorders.size > 0) return;
    let instance: MediaRecorder;
    const preferred = pickMime();
    try {
      instance = preferred
        ? new deps.MediaRecorder(stream, {
            mimeType: preferred,
            audioBitsPerSecond: 32_000,
          })
        : new deps.MediaRecorder(stream, { audioBitsPerSecond: 32_000 });
    } catch (error) {
      finish("error", `could not start the recorder: ${(error as Error)?.message || "unknown"}`);
      return;
    }
    recorders.add(instance);
    if (vadMode) {
      // The frame that opened this window was loud; that is loud frame one.
      utterance = { instance, frames: 0, loudFrames: 1, quietFrames: 0 };
    }
    const chunks: Blob[] = [];
    const windowPeak = { value: 0 };
    activePeaks.add(windowPeak);
    instance.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };
    instance.onstop = () => {
      recorders.delete(instance);
      activePeaks.delete(windowPeak);
      const shape = utterance?.instance === instance ? utterance : null;
      if (shape) utterance = null;
      const mimeType = instance.mimeType || preferred || "audio/webm";
      const blob = new Blob(chunks, { type: mimeType });
      if (!armed) return;
      const capped = () => {
        park();
        events.onState(
          "capped",
          `that was the last of my ${cap} listening call${cap === 1 ? "" : "s"} — unmute me again for another ${cap}`
        );
      };
      if (vadMode) {
        // An utterance earns its call twice over: it must have carried
        // `SPEECH_ON_FRAMES` loud frames, and it must have lasted longer than
        // a cough. Both are measured locally and neither costs anything, so
        // the model never sees a window nobody spoke into.
        const durationMs = (shape?.frames ?? 0) * 100;
        const loudFrames = shape?.loudFrames ?? 0;
        const measured = windowPeak.value;
        const speech =
          loudFrames >= SPEECH_ON_FRAMES &&
          durationMs >= minUtteranceMs &&
          loudEnough(measured);
        if (!speech || blob.size === 0) {
          events.onGlimpse?.(
            blob.size === 0
              ? "(no audio in that window)"
              : `(${(durationMs / 1000).toFixed(1)} s of noise, not speech — nothing sent)`
          );
          return; // still armed; the meter opens the next window
        }
        send(blob, mimeType, measured, true);
        if (sent >= cap) capped();
        return;
      }
      // No VAD: the peak measured across the window is the gate — and with no
      // analyser at all there is no gate, which only `vad: false` allows.
      const measured = analyser === null ? null : windowPeak.value;
      const gated = measured !== null && loudEnough(measured);
      const heard = gated || measured === null;
      if (heard && blob.size > 0) {
        send(blob, mimeType, measured, gated);
        if (sent >= cap) {
          capped();
          return;
        }
      } else if (armed && blob.size === 0) {
        events.onGlimpse?.("(no audio in that window)");
      }
      if (!overlap && armed) record();
    };
    try {
      instance.start(1_000);
    } catch {
      instance.start();
    }
    const stopId = schedule(() => {
      stopTimers.delete(stopId);
      if (recorders.has(instance) && instance.state === "recording") {
        try {
          instance.requestData?.();
        } catch {
          // Older recorders lack requestData.
        }
        instance.stop();
      }
    }, vadMode ? maxUtteranceMs : windowMs);
    stopTimers.add(stopId);
    if (!vadMode && overlap && hopTimer === null && armed && sent < cap) {
      hopTimer = schedule(() => {
        hopTimer = null;
        if (armed && sent < cap) record();
      }, hopMs);
    }
  };

  return {
    backend: "windows",
    async start() {
      if (armed) return;
      const alreadySpent = deps.spent ?? 0;
      if (alreadySpent >= cap) {
        // Re-arming on a spent budget would open the microphone to do
        // nothing, which is the one thing this must never look like.
        events.onState(
          "capped",
          `the ${cap} listening calls for this unmute are spent — unmute me again for another ${cap}`
        );
        return;
      }
      armed = true;
      sent = alreadySpent;
      consecutiveFails = 0;
      generation += 1;
      try {
        stream = await deps.getUserMedia({ audio: true });
      } catch (error) {
        armed = false;
        throw new Error(
          `could not use the microphone: ${(error as Error)?.message || "permission denied"}`
        );
      }
      if (!armed) {
        releaseMedia();
        return;
      }
      if (vadMode && deps.AudioContext) {
        try {
          context = new deps.AudioContext();
          analyser = context.createAnalyser();
          analyser.fftSize = 1024;
          context.createMediaStreamSource(stream).connect(analyser);
          const buffer = new Uint8Array(analyser.fftSize);
          meter = setInterval(() => {
            if (!analyser || !armed) return;
            analyser.getByteTimeDomainData(buffer);
            const frame = peakDeviation(buffer);
            // Measured once, used twice: the gate below and whatever is
            // drawing the level. Never a second tap on the microphone.
            events.onLevel?.(frame);
            for (const p of activePeaks) {
              if (frame > p.value) p.value = frame;
            }
            const loud = loudEnough(frame);
            const open = utterance;
            if (open) {
              // Endpointing: the window closes when you stop talking, not
              // when a stopwatch says so, so one window is one utterance.
              open.frames += 1;
              if (loud) {
                open.loudFrames += 1;
                open.quietFrames = 0;
              } else {
                open.quietFrames += 1;
              }
              const elapsed = open.frames * 100;
              if (
                elapsed >= maxUtteranceMs ||
                (open.quietFrames >= silenceFrames && elapsed >= minUtteranceMs)
              ) {
                if (open.instance.state === "recording") {
                  try {
                    open.instance.requestData?.();
                  } catch {
                    // Older recorders lack requestData.
                  }
                  open.instance.stop();
                }
              }
              return;
            }
            if (recorders.size > 0 || sent >= cap) return;
            // One loud frame opens the recorder. Recording is free; the send
            // is what costs, and that still needs SPEECH_ON_FRAMES.
            if (loud) record();
          }, 100);
          events.onState(
            "listening",
            `listening — I send one clip per thing you say, up to ${cap} this unmute (${cap - sent} left). Silence costs nothing.`
          );
          return;
        } catch (error) {
          context = null;
          analyser = null;
          finish(
            "error",
            `I couldn’t start the loudness meter (${(error as Error)?.message || "unknown"}), so I can’t tell speech from silence — I won’t send clips I can’t gate. Use the mic button instead.`
          );
          return;
        }
      } else if (gateRequired) {
        finish(
          "error",
          "I can’t measure the room level in this webview, so I can’t tell speech from silence — I won’t send clips I can’t gate. Use the mic button instead."
        );
        return;
      } else if (deps.AudioContext) {
        try {
          context = new deps.AudioContext();
          analyser = context.createAnalyser();
          analyser.fftSize = 1024;
          context.createMediaStreamSource(stream).connect(analyser);
          const buffer = new Uint8Array(analyser.fftSize);
          meter = setInterval(() => {
            if (!analyser) return;
            analyser.getByteTimeDomainData(buffer);
            const frame = peakDeviation(buffer);
            events.onLevel?.(frame);
            for (const p of activePeaks) {
              if (frame > p.value) p.value = frame;
            }
          }, 100);
        } catch {
          context = null;
          analyser = null;
        }
      }
      events.onState(
        "listening",
        analyser === null
          ? `one arming, ungated — I can’t hear the room level, so I send every ${windowMs / 1000} s window blind (at most ${cap})`
          : overlap
            ? `one arming, not always-on — ${windowMs / 1000} s windows every ${hopMs / 1000} s (at most ${cap})`
            : `one arming, not always-on — ${windowMs / 1000} s windows (at most ${cap})`
      );
      record();
    },
    stop() {
      if (!armed && !stream && inflight.size === 0 && recorders.size === 0) return;
      finish("stopped", "stopped listening");
    },
  };
}

export interface LocalListenerDeps {
  /** Open the Rust cpal + spotter loop. */
  start: () => Promise<void>;
  /** Drop the Rust mic. Safe to call twice. */
  stop: () => Promise<void>;
  /** Subscribe to `ada-wake`. Null means Tauri events are missing. */
  subscribe: (onHit: (hit: LocalWakeHit) => void) => Promise<(() => void) | null>;
}

/**
 * On-device listener: Rust owns the always-on mic. This object only starts
 * and stops that loop and forwards `ada-wake`. No getUserMedia.
 */
export function createLocalListener(
  deps: LocalListenerDeps,
  events: WakeListenerEvents
): WakeWordListener {
  let unlisten: (() => void) | null = null;
  let armed = false;

  const release = () => {
    const stopListen = unlisten;
    unlisten = null;
    armed = false;
    if (stopListen) stopListen();
    void deps.stop();
  };

  return {
    backend: "local",
    async start() {
      if (armed) return;
      await deps.start();
      unlisten = (await deps.subscribe((hit) => {
        const utterance = (hit.utterance ?? "").trim();
        release();
        events.onState("stopped", "heard the wake word");
        events.onWake({ utterance, backend: "local" });
      })) ?? null;
      if (!unlisten) {
        await deps.stop();
        throw new Error("could not listen for ada-wake");
      }
      armed = true;
      events.onState("listening", "listening on-device for Hey Ada");
    },
    stop() {
      if (!armed && !unlisten) return;
      release();
      events.onState("stopped", "stopped listening");
    },
  };
}

export interface CreateListenerOptions {
  baseUrl: string;
  token?: string;
  globals?: WakeGlobals;
  /** The session budget in model calls. */
  cap?: number;
  /** Calls already spent this session; see WindowListenerDeps.spent. */
  spent?: number;
  /** See WindowListenerDeps.continuing. */
  continuing?: () => boolean;
}

/**
 * The listener this webview can actually run, or `null` with the reason.
 * The page calls this once per arming so a permission granted in between
 * changes the answer.
 */
export function createWakeWordListener(
  options: CreateListenerOptions,
  events: WakeListenerEvents
): WakeWordListener | null {
  const globals = options.globals ?? (globalThis as WakeGlobals);
  const backend = chooseWakeBackend(globals);
  if (backend === "speech") {
    const ctor = speechRecognitionCtor(globals);
    if (!ctor) return null;
    return createSpeechListener(
      { recognition: ctor, lang: (globalThis as { navigator?: Navigator }).navigator?.language },
      events
    );
  }
  if (backend === "windows") {
    // The paid backend is a command clip now, never an idle spotter. Asking
    // Gemini "was my name just said?" once per window is what the on-device
    // classifier replaced: it billed for every four seconds of an empty room
    // and answered by string-matching seventeen spellings of "Ada" — "otto"
    // and "auto" among them — against a transcript of the silence.
    //
    // `continuing` is the seam that already knows the difference: it is true
    // only inside the follow-up window a wake or push-to-talk opened. Outside
    // one there is nothing to record, so refuse and let the caller say so,
    // exactly as it does for a webview with no recorder.
    if (!options.continuing?.()) return null;
    const getUserMedia = globals.navigator?.mediaDevices?.getUserMedia;
    if (!getUserMedia || !globals.MediaRecorder) return null;
    return createWindowListener(
      {
        baseUrl: options.baseUrl,
        token: options.token,
        getUserMedia: (constraints) =>
          getUserMedia.call(globals.navigator!.mediaDevices, constraints),
        MediaRecorder: globals.MediaRecorder,
        AudioContext: globals.AudioContext ?? globals.webkitAudioContext,
        cap: options.cap ?? DEFAULT_WINDOW_CAP,
        spent: options.spent ?? 0,
        // Only inside a continuation window is speech without the name the
        // command; otherwise a wake word that answers to anything is not a
        // wake word, it is an open microphone.
        continuing: options.continuing ?? (() => false),
      },
      events
    );
  }
  return null;
}

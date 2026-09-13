// The two ways Kaleo can turn text into sound, behind one interface.
//
// `webspeech` is the default because it costs nothing and needs nothing: the
// webview's own `speechSynthesis`, which WKWebView (Tauri on macOS) and the
// Chromium webviews both ship. Its absence is checked at runtime and treated
// as "voice unavailable", never a crash — an embedded webview build without
// it must degrade to silence.
//
// `elevenlabs` is the paid upgrade, selected purely by the presence of an API
// key. The fetch goes through `@tauri-apps/plugin-http` exactly like the
// silkscreen client's — the app origin is `tauri://localhost`, so a webview
// fetch would be cross-origin.
//
// SCOPE (checked 2026-09-07, replacing a comment that said the opposite):
// `api.elevenlabs.io` is REACHABLE. Both `src-tauri/capabilities/default.json`
// and `cross-platform.json` grant `http:default` with `https://*`,
// `https://*/*`, `https://*:*` and `https://*:*/*` — the scope had to be that
// wide for a deployed engine behind a bearer token, and it covers this host as
// a side effect. So what actually decides whether the paid voice talks is one
// thing only: whether a key was found (`speaker.chooseBackendName`). The old
// note claimed the request was scope-blocked and that webspeech was therefore
// the only voice that ever ran; it was stale, and it read as a reason not to
// bother finishing this path.
//
// The API key travels in the `xi-api-key` header and NOWHERE else: not in a
// URL, not in a log, not in an error message, not in a thrown value. That is
// the same rule `googleapps/transport.py` and `billing/transport.py` keep, and
// it is why the error strings below carry a status code and nothing more.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";

export interface SpeechBackend {
  // "service" is `POST /speak` on the engine — Kokoro-82M locally, or
  // ElevenLabs when a key exists, decided server-side. See
  // `../voice/serviceBackend.ts`; it is the only backend whose absence is a
  // normal state rather than a failure, which is why it can hand back
  // `ServiceVoiceUnavailable` and let `speaker` fall through to webspeech.
  readonly name: "webspeech" | "elevenlabs" | "service";
  /**
   * Speak one utterance; resolves when playback ends, is stopped, or fails.
   * Rejections carry no secrets — the controller catches and logs them.
   */
  speak(text: string): Promise<void>;
  /** Cut playback now. Safe to call when nothing is playing. */
  stop(): void;
}

/** ElevenLabs' "Rachel", their long-standing default demo voice. */
export const DEFAULT_ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM";
export const ELEVENLABS_MODEL_ID = "eleven_multilingual_v2";
/** A digest is a few sentences; a minute is already generous. */
export const ELEVENLABS_TIMEOUT_MS = 30_000;

type SpeechWindow = {
  speechSynthesis?: SpeechSynthesis;
  SpeechSynthesisUtterance?: typeof SpeechSynthesisUtterance;
};

function speechGlobals(): Required<SpeechWindow> | null {
  const w = globalThis as SpeechWindow;
  // Verified defensively at runtime, not assumed from documentation: a
  // webview without the API must read as "no voice", never as a TypeError.
  if (!("speechSynthesis" in w) || !w.speechSynthesis) return null;
  if (typeof w.SpeechSynthesisUtterance !== "function") return null;
  return {
    speechSynthesis: w.speechSynthesis,
    SpeechSynthesisUtterance: w.SpeechSynthesisUtterance,
  };
}

export function webSpeechAvailable(): boolean {
  return speechGlobals() !== null;
}

/** How long to wait for a webview that loads its voice list asynchronously. */
export const VOICES_WAIT_MS = 1_500;

/**
 * The two `error` values that mean "you stopped me", not "I failed".
 *
 * `stop()` calls `cancel()`, and a cancelled utterance reports an error in
 * Chromium. Treating those as failures would put a warning in the console
 * every time the human takes the microphone, which is the fastest way to make
 * the real failures unreadable.
 */
const BENIGN_SPEECH_ERRORS = new Set(["canceled", "cancelled", "interrupted"]);

/**
 * Some webviews populate `getVoices()` asynchronously and answer with an empty
 * list until `voiceschanged` fires; speaking into that window can produce an
 * utterance nobody hears. Waiting is bounded and never fatal — if the list is
 * still empty we try anyway, because a platform with no enumerable voices can
 * still have a working default, and the `error` path below is what tells the
 * truth if it does not.
 */
function voicesReady(synth: SpeechSynthesis): Promise<void> {
  let voices: SpeechSynthesisVoice[] = [];
  try {
    voices = synth.getVoices();
  } catch {
    return Promise.resolve();
  }
  if (voices.length > 0) return Promise.resolve();
  return new Promise<void>((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      try {
        synth.removeEventListener?.("voiceschanged", finish);
      } catch {
        /* an implementation without listeners just waits out the timeout */
      }
      resolve();
    };
    try {
      synth.addEventListener?.("voiceschanged", finish);
    } catch {
      finish();
      return;
    }
    setTimeout(finish, VOICES_WAIT_MS);
  });
}

/**
 * Voice-quality tiers, as macOS spells them in `SpeechSynthesisVoice.name`.
 *
 * macOS ships three grades of every system voice and hands `getVoices()`
 * whichever ones are installed. The default install has only *Compact* — the
 * formant-synthesis voice from the 2000s, and the reason Kaleo has sounded
 * like a robot: nothing here ever asked for better, so the platform default
 * won. *Enhanced* and *Premium* are neural, sound like a person, and are a
 * free opt-in download (System Settings → Accessibility → Spoken Content →
 * System Voice → Manage Voices).
 *
 * The webview does not expose a quality field, so the tier is read off the
 * name — the suffix Apple appends, e.g. `"Ava (Premium)"`. That is a string
 * match against a vendor's UI text, so it is a PREFERENCE and never a
 * requirement: an unrecognised name scores zero and still gets picked when it
 * is the only candidate, and an empty list leaves `utterance.voice` unset so
 * the platform picks. Nothing here downloads, prompts, waits or blocks.
 */
const VOICE_TIERS: ReadonlyArray<{ pattern: RegExp; score: number }> = [
  { pattern: /\(premium\)/i, score: 40 },
  { pattern: /\(enhanced\)/i, score: 30 },
  // Windows/Chromium spellings for the same idea. Harmless where absent.
  { pattern: /\bneural\b/i, score: 30 },
  { pattern: /\bnatural\b/i, score: 20 },
  { pattern: /\(compact\)/i, score: -10 },
];

function tierScore(name: string): number {
  for (const tier of VOICE_TIERS) {
    if (tier.pattern.test(name)) return tier.score;
  }
  return 0;
}

/** The language to speak in when the platform gives us nothing better. */
export const DEFAULT_SPEECH_LANG = "en-US";

function preferredLang(): string {
  const nav = (globalThis as { navigator?: { language?: string } }).navigator;
  return (nav?.language || DEFAULT_SPEECH_LANG).trim() || DEFAULT_SPEECH_LANG;
}

/**
 * Pick the best installed voice for `lang`, or `null` to let the platform
 * decide.
 *
 * Deliberately total and deterministic: it never throws, never waits, and two
 * runs over the same list give the same answer (ties break on the platform's
 * own ordering, which is why the sort is stable by index rather than by name).
 * A language mismatch is a *penalty*, not a filter — a machine with only
 * `en-GB (Premium)` installed should still get the good voice for an `en-US`
 * request, and a machine with nothing matching should still speak.
 */
export function pickVoice(
  voices: readonly SpeechSynthesisVoice[],
  lang: string = DEFAULT_SPEECH_LANG
): SpeechSynthesisVoice | null {
  if (!voices || voices.length === 0) return null;
  const want = (lang || DEFAULT_SPEECH_LANG).toLowerCase();
  const wantPrimary = want.split("-")[0];
  let best: SpeechSynthesisVoice | null = null;
  let bestScore = -Infinity;
  voices.forEach((voice, index) => {
    const voiceLang = (voice?.lang ?? "").toLowerCase().replace("_", "-");
    let score = tierScore(voice?.name ?? "");
    if (voiceLang === want) score += 12;
    else if (voiceLang.split("-")[0] === wantPrimary) score += 8;
    // Wrong language reads the digest as gibberish, so this must outrank the
    // whole tier spread (-10 Compact .. +40 Premium = 50). It was -25 first,
    // and the test caught it: a Japanese Premium voice beat an American
    // Compact one for an en-US digest. Quality is only worth having inside
    // the right language.
    else score -= 100;
    // A local voice cannot fail on a plane or leak the digest to a server.
    if (voice?.localService) score += 3;
    if (voice?.default) score += 1;
    // Stable: earlier entries win a tie, so the platform's own preference
    // order is the tie-break rather than something we invented.
    score -= index * 1e-6;
    if (score > bestScore) {
      bestScore = score;
      best = voice;
    }
  });
  return best;
}

function applyVoice(
  utterance: SpeechSynthesisUtterance,
  synth: SpeechSynthesis
): void {
  // Every step is best-effort: a webview that throws from `getVoices()` or
  // refuses the assignment must still speak with its default.
  try {
    const lang = preferredLang();
    const chosen = pickVoice(synth.getVoices?.() ?? [], lang);
    if (chosen) {
      utterance.voice = chosen;
      // Chromium ignores `voice` when `lang` contradicts it; setting both
      // from the same object keeps them consistent.
      if (chosen.lang) utterance.lang = chosen.lang;
    } else if (!utterance.lang) {
      utterance.lang = lang;
    }
  } catch {
    /* platform default it is */
  }
}

export function createWebSpeechBackend(): SpeechBackend {
  // Which utterance is current. Bumped by `stop()`, so a stop that lands
  // *before* the sound starts still counts: `speak` waits on the voice list
  // for up to VOICES_WAIT_MS, and without this the cancel arrives at a
  // synthesiser that is not speaking yet and the utterance begins anyway —
  // the mute pressed a moment earlier silently dropped.
  let generation = 0;
  return {
    name: "webspeech",
    async speak(text: string): Promise<void> {
      const globals = speechGlobals();
      if (!globals) {
        throw new Error("speechSynthesis is not available in this webview");
      }
      const mine = ++generation;
      await voicesReady(globals.speechSynthesis);
      // Stopped while the voices loaded. Resolve rather than reject: being
      // stopped is not a failure, and the speaker treats a rejection as one.
      if (mine !== generation) return;
      return new Promise<void>((resolve, reject) => {
        const utterance = new globals.SpeechSynthesisUtterance(text);
        applyVoice(utterance, globals.speechSynthesis);
        // `end` fires on natural completion AND after cancel(). `error` fires
        // on everything else, and it is the ONLY witness that a line the app
        // thinks it said was never audible — so a real error rejects (the
        // speaker warns and, for a paid backend, retries) while a cancel
        // resolves. Either way the promise settles: a voice that leaves a
        // promise hanging leaves the speaking flag stuck on.
        utterance.onend = () => resolve();
        utterance.onerror = (event: SpeechSynthesisErrorEvent) => {
          const reason = event?.error ?? "";
          if (!reason || BENIGN_SPEECH_ERRORS.has(reason)) resolve();
          else reject(new Error(`speechSynthesis reported ${reason}`));
        };
        globals.speechSynthesis.cancel();
        globals.speechSynthesis.speak(utterance);
      });
    },
    stop(): void {
      generation += 1;
      speechGlobals()?.speechSynthesis.cancel();
    },
  };
}

/**
 * One text-to-speech call, returning the mp3 bytes.
 *
 * Split out from playback so a test can assert the request shape — URL, the
 * `xi-api-key` header, the JSON body — against a mocked fetch without ever
 * needing an Audio element or a real key.
 */
export async function fetchElevenLabsAudio(
  apiKey: string,
  text: string,
  voiceId: string = DEFAULT_ELEVENLABS_VOICE_ID
): Promise<Blob> {
  const id = voiceId.trim() || DEFAULT_ELEVENLABS_VOICE_ID;
  const response = await tauriFetch(
    `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(id)}`,
    {
      method: "POST",
      headers: {
        "xi-api-key": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text, model_id: ELEVENLABS_MODEL_ID }),
      signal: AbortSignal.timeout(ELEVENLABS_TIMEOUT_MS),
    }
  );
  if (!response.ok) {
    // Status only. The response body could echo request details and the
    // message may end up in a console line, so neither the body nor the key
    // is allowed anywhere near this string.
    throw new Error(`ElevenLabs answered ${response.status}`);
  }
  return await response.blob();
}


// ---------------------------------------------------------------------------
// Streaming playback
// ---------------------------------------------------------------------------
//
// WHY THIS IS SHAPED AS A PRODUCER PLUS A SINK.
//
// The old path was `fetch` the whole mp3, then `new Audio(blobUrl)`. So
// time-to-first-word was the time to download the LAST byte: a fifteen-second
// digest is a fifteen-second file, and Ada sat silent through all of it. The
// fix is progressive playback, and the design constraint is that Phase 1
// replaces the transport (ElevenLabs' `stream-input` WebSocket, Flash v2.5,
// multi-context, for barge-in) without replacing the player.
//
// So the two halves are split at the only boundary that survives that change:
//
//   producer — something that yields mp3 chunks. Today `streamElevenLabsAudio`
//              (HTTP POST to `/stream`, reading `response.body`). In Phase 1 a
//              WebSocket reader that base64-decodes each `audio` frame. Both
//              are "an async source of Uint8Array", and the sink cannot tell
//              them apart.
//   sink     — something that turns chunks into sound as they arrive. Reused
//              verbatim in Phase 1, including its stop semantics.
//
// WHY NOT THE OTHER TWO OPTIONS.
//
//   `audio.src = <the request URL>` is by far the simplest — the media element
//   streams for you, no MSE, no pump. It is impossible here: ElevenLabs' TTS
//   endpoint is a POST whose body carries the text, and the credential is the
//   `xi-api-key` HEADER. A media element issues a GET and sets no headers, and
//   the documented query-string alternative (`?xi_api_key=`) exists only on
//   the WebSocket endpoint. Using it would put the key in a URL, which is the
//   one thing this repo does not do with a credential. Ruled out on both
//   counts, not just the convenient one.
//
//   Going straight to the WebSocket now would skip a step, but Phase 1 wants
//   it for barge-in and multi-context, which is a conversation-state problem,
//   not a playback one. Building the sink first means Phase 1 is a producer
//   swap against a player that has already been used in anger.
//
// MSE IS PREFERRED, NOT REQUIRED. `MediaSource` with an `audio/mpeg`
// SourceBuffer gives true first-chunk playback, and Chromium webviews (Tauri
// on Windows/Linux) support it. WKWebView's MSE has historically not accepted
// raw `audio/mpeg`, so this is decided at runtime by
// `MediaSource.isTypeSupported` rather than by a platform guess, and the
// buffered sink below is the fallback — same interface, same stop semantics,
// no better than the old behaviour but no worse either. `createChunkSink` is
// the one place that chooses, so the day WKWebView answers true, or the day we
// switch to a PCM sink over Web Audio, exactly one function changes.

/** ElevenLabs' progressive endpoint. Same auth, same body, chunked reply. */
export const ELEVENLABS_STREAM_OUTPUT_FORMAT = "mp3_44100_128";
/** The MIME the SourceBuffer is opened with, matching the format above. */
export const ELEVENLABS_STREAM_MIME = "audio/mpeg";

/**
 * Somewhere for chunks to go, that makes noise.
 *
 * `push` never rejects for backpressure reasons — a sink that cannot keep up
 * buffers. `done` settles when playback finishes or fails, and `dispose` is
 * the stop path: it must silence immediately and settle `done`.
 */
export interface AudioChunkSink {
  push(chunk: Uint8Array): void;
  /** No more chunks are coming. Playback may still be running. */
  end(): void;
  /** Resolves when the audio has finished playing; rejects if it could not. */
  readonly done: Promise<void>;
  /** Stop now and release the element. Safe to call twice. */
  dispose(): void;
}

type MediaSourceCtor = {
  new (): MediaSource;
  isTypeSupported(type: string): boolean;
};

function mediaSourceCtor(): MediaSourceCtor | null {
  const ctor = (globalThis as { MediaSource?: MediaSourceCtor }).MediaSource;
  if (typeof ctor !== "function") return null;
  if (typeof ctor.isTypeSupported !== "function") return null;
  return ctor;
}

/**
 * Can this webview play mp3 progressively?
 *
 * Probed, never assumed from the platform string — the answer differs between
 * WKWebView and the Chromium webviews and has changed across OS releases, and
 * being wrong in the optimistic direction means silence rather than a robot
 * voice.
 */
export function progressivePlaybackSupported(): boolean {
  const ctor = mediaSourceCtor();
  if (!ctor) return false;
  try {
    return ctor.isTypeSupported(ELEVENLABS_STREAM_MIME) === true;
  } catch {
    return false;
  }
}

/** Shared by both sinks: the element teardown that must not throw. */
function releaseAudio(audio: HTMLAudioElement | null, url: string | null) {
  if (audio) {
    audio.onended = null;
    audio.onerror = null;
    try {
      audio.pause();
    } catch {
      /* an element mid-teardown may refuse; nothing to do about it */
    }
    // Detaching the source is what actually stops a MediaSource-backed
    // element: pause() alone leaves it holding the buffer.
    try {
      audio.removeAttribute("src");
      audio.load();
    } catch {
      /* jsdom and some webviews do not implement load() */
    }
  }
  if (url) {
    // Object URLs pin their bytes (a whole mp3, or the MediaSource) until
    // revoked; a digest per run would otherwise leak one per board.
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* already gone */
    }
  }
}

/**
 * The progressive sink: a MediaSource whose SourceBuffer is fed as bytes land.
 *
 * The append queue exists because `appendBuffer` is illegal while the buffer
 * is `updating`, and chunks arrive faster than they append. `end()` cannot
 * call `endOfStream()` directly for the same reason — it sets a flag that the
 * drain honours once the queue is empty, otherwise the stream ends while
 * bytes are still queued and the tail of the sentence is cut off.
 */
export function createMediaSourceSink(): AudioChunkSink {
  const ctor = mediaSourceCtor();
  if (!ctor) throw new Error("MediaSource is not available in this webview");

  const media = new ctor();
  const url = URL.createObjectURL(media);
  const audio = new Audio();
  audio.src = url;

  const queue: Uint8Array[] = [];
  let buffer: SourceBuffer | null = null;
  let ended = false;
  let disposed = false;
  let started = false;
  let settle: (() => void) | null = null;
  let fail: ((error: Error) => void) | null = null;

  const done = new Promise<void>((resolve, reject) => {
    settle = resolve;
    fail = reject;
  });
  const finish = () => {
    settle?.();
    settle = null;
    fail = null;
  };
  const abort = (message: string) => {
    const reject = fail;
    settle = null;
    fail = null;
    reject?.(new Error(message));
  };

  const drain = () => {
    if (disposed || !buffer) return;
    if (buffer.updating) return;
    const next = queue.shift();
    if (next) {
      try {
        // A fresh copy: `appendBuffer` is asynchronous and the reader may
        // reuse the chunk's backing memory.
        buffer.appendBuffer(next.slice().buffer as ArrayBuffer);
      } catch {
        abort("audio buffer refused a chunk");
      }
      return;
    }
    if (ended && media.readyState === "open") {
      try {
        media.endOfStream();
      } catch {
        /* already ended */
      }
    }
  };

  media.addEventListener("sourceopen", () => {
    if (disposed || buffer) return;
    try {
      buffer = media.addSourceBuffer(ELEVENLABS_STREAM_MIME);
      buffer.addEventListener("updateend", drain);
      buffer.addEventListener("error", () => abort("audio buffer failed"));
      drain();
    } catch {
      abort("this webview cannot play the streamed audio format");
    }
  });

  audio.onended = () => finish();
  audio.onerror = () => abort("audio playback failed");

  return {
    push(chunk: Uint8Array): void {
      if (disposed) return;
      queue.push(chunk);
      drain();
      if (!started) {
        started = true;
        // Playback begins on the FIRST chunk — the whole point of this file.
        audio.play().catch((error: unknown) => {
          abort((error as Error)?.message || "audio playback refused");
        });
      }
    },
    end(): void {
      ended = true;
      drain();
      // A stream that carried no audio at all must not hang `done` forever.
      if (!started) finish();
    },
    done,
    dispose(): void {
      if (disposed) return;
      disposed = true;
      queue.length = 0;
      releaseAudio(audio, url);
      // Stopped is not failed: settle rather than reject, so the speaker does
      // not warn and does not fall back to the free voice for a deliberate
      // mute. Same reasoning as BENIGN_SPEECH_ERRORS above.
      finish();
    },
  };
}

/**
 * The fallback sink: collect every chunk, then play the assembled blob.
 *
 * This is the pre-streaming behaviour, kept deliberately — a webview whose
 * MediaSource will not take `audio/mpeg` must still speak. It is not a
 * regression path so much as the floor.
 */
export function createBufferedSink(): AudioChunkSink {
  const chunks: Uint8Array[] = [];
  let audio: HTMLAudioElement | null = null;
  let url: string | null = null;
  let disposed = false;
  let settle: (() => void) | null = null;
  let fail: ((error: Error) => void) | null = null;

  const done = new Promise<void>((resolve, reject) => {
    settle = resolve;
    fail = reject;
  });
  const finish = () => {
    settle?.();
    settle = null;
    fail = null;
  };
  const abort = (message: string) => {
    const reject = fail;
    settle = null;
    fail = null;
    reject?.(new Error(message));
  };

  return {
    push(chunk: Uint8Array): void {
      if (!disposed) chunks.push(chunk);
    },
    end(): void {
      if (disposed) return;
      if (chunks.length === 0) {
        finish();
        return;
      }
      const blob = new Blob(chunks as BlobPart[], {
        type: ELEVENLABS_STREAM_MIME,
      });
      url = URL.createObjectURL(blob);
      audio = new Audio(url);
      audio.onended = () => {
        releaseAudio(audio, url);
        audio = null;
        url = null;
        finish();
      };
      audio.onerror = () => abort("audio playback failed");
      audio.play().catch((error: unknown) => {
        abort((error as Error)?.message || "audio playback refused");
      });
    },
    done,
    dispose(): void {
      if (disposed) return;
      disposed = true;
      chunks.length = 0;
      releaseAudio(audio, url);
      audio = null;
      url = null;
      finish();
    },
  };
}

/** The one place that decides which sink this machine gets. */
export function createChunkSink(): AudioChunkSink {
  if (progressivePlaybackSupported()) {
    try {
      return createMediaSourceSink();
    } catch {
      // Supported-but-unconstructible is a real state (a webview under memory
      // pressure). Falling through is better than going mute.
    }
  }
  return createBufferedSink();
}

/**
 * Open the streaming text-to-speech response and hand back its byte stream.
 *
 * Split from playback for the same reason `fetchElevenLabsAudio` was: a test
 * can assert the request shape — URL, `xi-api-key` header, JSON body, and
 * that neither the URL nor the body carries the key — against a mocked fetch,
 * with no Audio element and no real key anywhere.
 *
 * The `signal` is the caller's, not an `AbortSignal.timeout`: `stop()` has to
 * abort this request, and combining two signals needs `AbortSignal.any`,
 * which is newer than the WKWebView this has to run in. The caller arms the
 * timeout on the same controller instead.
 */
export async function streamElevenLabsAudio(
  apiKey: string,
  text: string,
  voiceId: string = DEFAULT_ELEVENLABS_VOICE_ID,
  signal?: AbortSignal
): Promise<ReadableStream<Uint8Array>> {
  const id = voiceId.trim() || DEFAULT_ELEVENLABS_VOICE_ID;
  const response = await tauriFetch(
    `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(
      id
    )}/stream?output_format=${ELEVENLABS_STREAM_OUTPUT_FORMAT}`,
    {
      method: "POST",
      headers: {
        "xi-api-key": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text, model_id: ELEVENLABS_MODEL_ID }),
      signal,
    }
  );
  if (!response.ok) {
    // Status only — see the header note. The body of a 401 quotes the request.
    throw new Error(`ElevenLabs answered ${response.status}`);
  }
  const body = response.body;
  if (!body) throw new Error("ElevenLabs answered with no audio stream");
  return body;
}

/**
 * Pump a byte stream into a sink until it runs dry.
 *
 * `shouldStop` is checked every chunk so a stop mid-stream stops *reading*,
 * not just playing: without it a cancelled utterance would go on downloading
 * its own mp3 in the background, which is exactly the bill nobody pressed for.
 */
export async function pumpStreamIntoSink(
  stream: ReadableStream<Uint8Array>,
  sink: AudioChunkSink,
  shouldStop: () => boolean = () => false
): Promise<void> {
  const reader = stream.getReader();
  try {
    for (;;) {
      if (shouldStop()) return;
      const { done, value } = await reader.read();
      if (done) break;
      if (shouldStop()) return;
      if (value && value.byteLength > 0) sink.push(value);
    }
    sink.end();
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* a cancelled reader may already be released */
    }
  }
}

export interface ElevenLabsBackendOptions {
  apiKey: string;
  voiceId?: string;
  /** Injectable for tests; production uses the runtime-chosen sink. */
  makeSink?: () => AudioChunkSink;
}

export function createElevenLabsBackend(
  options: ElevenLabsBackendOptions
): SpeechBackend {
  let sink: AudioChunkSink | null = null;
  let inflight: AbortController | null = null;
  // Which utterance is current. Bumped by `stop()`, so a stop that lands
  // before any sound exists still counts: the audio does not exist until the
  // POST replies, so a `stop()` during that round trip has nothing to pause
  // and the reply would start playing after it was silenced.
  //
  // Streaming widens that window rather than closing it — the request is now
  // open for the whole utterance — so the generation check is needed in three
  // places, not one: after the response headers, on every chunk (via
  // `shouldStop`), and around the await on `done`.
  let generation = 0;

  const teardown = () => {
    // Abort first: the sink's `dispose` settles `done`, and the pump must not
    // be left reading a stream nobody is listening to.
    if (inflight) {
      try {
        inflight.abort();
      } catch {
        /* an already-aborted controller is fine */
      }
      inflight = null;
    }
    if (sink) {
      sink.dispose();
      sink = null;
    }
  };

  return {
    name: "elevenlabs",
    async speak(text: string): Promise<void> {
      const mine = ++generation;
      const stale = () => mine !== generation;
      teardown();
      const controller = new AbortController();
      inflight = controller;
      // The old code used `AbortSignal.timeout`; the ceiling is now armed on
      // the same controller `stop()` uses, so there is one abort path.
      const timer = setTimeout(
        () => controller.abort(),
        ELEVENLABS_TIMEOUT_MS
      );
      try {
        const stream = await streamElevenLabsAudio(
          options.apiKey,
          text,
          options.voiceId,
          controller.signal
        );
        // Stopped while the headers were in flight; play nothing.
        if (stale()) return;
        const mySink = (options.makeSink ?? createChunkSink)();
        sink = mySink;
        await Promise.all([
          pumpStreamIntoSink(stream, mySink, stale),
          mySink.done,
        ]);
        if (sink === mySink) sink = null;
      } catch (error) {
        // An abort is a stop, not a failure: resolving keeps the speaker from
        // warning and from retrying a muted utterance on the free voice.
        if (stale() || controller.signal.aborted) return;
        throw error;
      } finally {
        clearTimeout(timer);
        if (inflight === controller) inflight = null;
      }
    },
    stop(): void {
      generation += 1;
      teardown();
    },
  };
}

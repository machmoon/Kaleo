// The one mouth: a controller that speaks at most one utterance at a time,
// and QUEUES the rest.
//
// Three rules shape everything here.
//
// One utterance at a time, because a voice that talks over itself reads as
// broken. Until 2026-09-07 that was implemented as "a new `speak` stops the
// old", which is a different rule wearing the same words, and it cost two
// real defects: a digest split into per-sentence utterances was audible only
// for its last sentence, and two stage moments landing close together
// truncated the first one mid-word. So a second `speak` now WAITS instead of
// interrupting. The queue is what makes sentence-at-a-time synthesis possible
// at all, which is also the cheapest half of the time-to-first-audio work —
// sentence one can be spoken while sentence two is still being generated.
//
// Stop still means stop. `stop()` clears the queue AND halts the current
// utterance; nothing pending survives it. That is the point of the generation
// counter in `backends.ts` carried up a level: barge-in beats queueing, always
// — an assistant that finishes reading a stale digest after you interrupted it
// is worse than one that drops the line. `announce.takeMicFloor` calls
// `stop()`, so the user taking the microphone empties the queue by
// construction.
//
// And `speak` never throws to the caller: voice is garnish, not the meal, so
// every failure mode — no key, no speechSynthesis, a 401, a refused
// autoplay — degrades to silence with one console warning that names the
// backend and the status, never the key.

import { safeLocalStorage } from "@/lib/storage/helper";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import {
  ServiceVoiceUnavailable,
  createServiceBackend,
} from "@/lib/voice/serviceBackend";
import {
  createElevenLabsBackend,
  createWebSpeechBackend,
  type SpeechBackend,
} from "./backends";
import { DEFAULT_BASE_URL } from "@/lib/silkscreen/client";
import {
  hydrateVoiceEnv,
  isPlatformVoiceAllowed,
  loadVoiceSettings,
  type VoiceSettings,
} from "./settings";

export type BackendName = SpeechBackend["name"];

/**
 * Key present means ElevenLabs; absent means the free built-in voice. This is
 * the whole selection policy, kept as its own function so a test can pin it.
 */
export function chooseBackendName(apiKey: string | null | undefined): BackendName {
  return (apiKey ?? "").trim() ? "elevenlabs" : "webspeech";
}

export interface SpeakerDeps {
  /** Read fresh per utterance so a settings change applies to the next digest. */
  getSettings: () => VoiceSettings;
  makeBackend: (settings: VoiceSettings) => SpeechBackend;
  /**
   * The voice a failed backend retries on, or `null` for "stay silent".
   *
   * Nullable since 2026-09-08. It used to be unconditionally
   * `createWebSpeechBackend`, which meant any transient failure of the good
   * voice was finished off by the robot one, with the reason visible only in
   * a console nobody in a release build has open.
   */
  makeFallback: () => SpeechBackend | null;
  warn: (message: string) => void;
  /**
   * Look for `ELEVENLABS_API_KEY` in `~/.kaleo/voice.env`, once.
   *
   * Started at construction and never awaited on the speaking path. The
   * alternative — awaiting it inside `speak` — would put a microtask between
   * `speak()` and the backend's own `speak()`, and this controller's whole
   * contract is that a `stop()` issued in that gap still lands. Construction
   * happens at app load and the first digest is many seconds later, so in
   * practice the key is there for the first utterance; in the worst case one
   * digest uses the free voice and every later one is upgraded, which is the
   * right way round for a failure nobody can hear.
   */
  hydrateEnv: () => Promise<unknown>;
}

/**
 * How many lines may wait their turn.
 *
 * Bounded because a burst of stage moments would otherwise leave Ada
 * monologuing through news that stopped being true a minute ago. When the cap
 * is reached the OLDEST pending line is dropped, not the newest: the newest is
 * the most recent status, and it is the one worth hearing.
 */
export const MAX_QUEUED_UTTERANCES = 3;

export interface Speaker {
  /**
   * Say `text` after anything already queued. Never rejects.
   *
   * Resolves when this line has been spoken — or when it was dropped, either
   * by `stop()` or by the queue cap. A caller cannot tell those apart on
   * purpose: "the line is no longer pending" is the only fact it needs, and a
   * rejection would make a deliberate mute look like a failure.
   */
  speak(text: string): Promise<void>;
  /** Barge-in: halt what is speaking and discard everything queued. */
  stop(): void;
  /** Is sound coming out right now? Not "is there work" — see `pending`. */
  isSpeaking(): boolean;
  /** Lines waiting their turn, excluding the one being spoken. */
  pending(): number;
}

/**
 * Set once the service has told us it has no voice, so we stop paying a
 * round trip per utterance to be told again.
 *
 * Deliberately NOT persisted: "the engine has no TTS engine configured" is a
 * fact about a process that may be restarted with weights in place a minute
 * later, and a cached "no" in localStorage would outlive the reason for it.
 * One wasted request per app run is the right price.
 */
let serviceVoiceRefused = false;

/** Reset the refusal — for tests, and for a settings change that may fix it. */
export function resetServiceVoice(): void {
  serviceVoiceRefused = false;
}

/**
 * Where the engine is, defaulting the way every other caller in this app
 * defaults.
 *
 * **This function is the bug that made the robot voice the product.** It used
 * to return the raw localStorage value, i.e. `""` on any install where nobody
 * had opened the Engine pane and typed an address by hand — which is every
 * fresh install, and was in fact this machine, verified against the app's own
 * localStorage on 2026-09-08: the keys present were `silkscreen_overlay_skin`,
 * `silkscreen_tour` and `silkscreen_last_run`, and `silkscreen_engine_base_url`
 * was simply not there. So `baseUrl` was falsy, the service backend was never
 * constructed, `POST /speak` was never called even once, and every word Ada
 * has ever said on this machine came out of `speechSynthesis`. Kokoro was
 * running, reachable and correct the whole time, and nothing ever asked it.
 *
 * `EngineConnection.loadEngineBaseUrl` had always applied `DEFAULT_BASE_URL`
 * here; `useSilkscreenRun`, `useEngineHealth` and `BillingSetup` all do too.
 * This one reader was the odd one out, so the voice was the one feature that
 * silently required manual configuration nobody knew about.
 */
function engineBaseUrl(): string {
  const stored = (
    safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ENGINE_BASE_URL) ?? ""
  ).trim();
  return stored || DEFAULT_BASE_URL;
}

/**
 * Which voice will speak the next line, and — when the answer is "none" —
 * why not, in words a person can act on.
 *
 * Exported so the strip can show it. The rule this whole module now keeps is
 * that a missing engine voice produces **silence**, and silence on its own
 * says only "broken"; this is how the UI turns it into "no voice is
 * provisioned, here is the fix". Nothing in `lib/speech` renders it, because
 * `VoiceControl.tsx` belongs to another lane tonight.
 *
 * **This is synchronous, so `"service"` is a claim about which backend will
 * be BUILT, not a promise that it will answer.** It has made no request. A
 * service that is down still reads as `"service"` here until an utterance
 * actually fails against it. For a status line that must not overstate, probe
 * first with `serviceVoiceReady(baseUrl, token)` — it is async, it really
 * calls `GET /speak`, and it returns the engine name the service itself
 * selected. Using this function for display without that probe would repeat,
 * in the UI, exactly the mistake this file just fixed in the audio path:
 * asserting a voice is there because the code path exists.
 */
export type VoiceStatus =
  | { readonly backend: "elevenlabs"; readonly reason: "" }
  | { readonly backend: "service"; readonly reason: "" }
  | { readonly backend: "webspeech"; readonly reason: "" }
  | { readonly backend: "none"; readonly reason: string };

export function voiceStatus(
  settings: VoiceSettings = loadVoiceSettings()
): VoiceStatus {
  if (chooseBackendName(settings.elevenLabsKey) === "elevenlabs") {
    return { backend: "elevenlabs", reason: "" };
  }
  if (!serviceVoiceRefused) return { backend: "service", reason: "" };
  if (settings.platformVoice) return { backend: "webspeech", reason: "" };
  return {
    backend: "none",
    reason:
      "the engine has no voice provisioned, so Hardy is staying silent rather " +
      "than falling back to the platform's robot voice — run " +
      "`scripts/install_voice.sh` to install Kokoro, or choose the platform " +
      "voice deliberately in the voice menu",
  };
}

function defaultMakeBackend(settings: VoiceSettings): SpeechBackend {
  // An ElevenLabs key set in this app is an explicit user choice and keeps
  // winning: it was configured deliberately, and silently routing around it
  // would be the same class of surprise this file's honesty rules forbid.
  if (chooseBackendName(settings.elevenLabsKey) === "elevenlabs") {
    return createElevenLabsBackend({
      apiKey: settings.elevenLabsKey,
      voiceId: settings.voiceId,
    });
  }
  // Otherwise the engine's own voice — Kokoro-82M locally, a real neural
  // voice rather than the platform's Compact one. No probe is made here on
  // purpose: `makeBackend` is synchronous, and a service with no engine
  // configured answers 503, which `utter` below turns into a stated
  // fall-through. One round trip buys correctness without an async seam.
  if (!serviceVoiceRefused) {
    return createServiceBackend({
      baseUrl: engineBaseUrl(),
      token: safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ENGINE_TOKEN),
    }) as SpeechBackend;
  }
  // The service has already told us it has no voice. The platform voice is
  // reached here ONLY because someone asked for it by name — see
  // `isPlatformVoiceAllowed`. Otherwise `makeBackend` throws, `utter` catches
  // it, and Ada is silent with a stated reason.
  if (settings.platformVoice) return createWebSpeechBackend();
  throw new Error(voiceStatus(settings).reason);
}

/**
 * The backend a failed one retries on — or `null`, meaning "do not retry,
 * stay silent".
 *
 * Null rather than a webspeech instance is the entire behavioural change of
 * this file. A paid or local backend that dies used to hand the line to
 * `speechSynthesis`, which is how a one-off 503 turned into the robot voice
 * finishing the sentence.
 */
function defaultMakeFallback(): SpeechBackend | null {
  return isPlatformVoiceAllowed() ? createWebSpeechBackend() : null;
}

/** Factory rather than a bare singleton so tests can inject fake backends. */
export function createSpeaker(deps?: Partial<SpeakerDeps>): Speaker {
  const getSettings = deps?.getSettings ?? loadVoiceSettings;
  const makeBackend = deps?.makeBackend ?? defaultMakeBackend;
  const makeFallback = deps?.makeFallback ?? defaultMakeFallback;
  const warn =
    deps?.warn ?? ((message: string) => console.warn(`[kaleo voice] ${message}`));
  // Fire and forget: `hydrateVoiceEnv` never rejects, and nothing downstream
  // may block on it. The `catch` is belt-and-braces against a future reader
  // that does reject — an unhandled rejection at app load is a bad look for a
  // feature whose entire failure mode is supposed to be silence.
  const hydrateEnv = deps?.hydrateEnv ?? hydrateVoiceEnv;
  void Promise.resolve()
    .then(() => hydrateEnv())
    .catch(() => {});

  let current: SpeechBackend | null = null;
  // Each utterance takes a ticket; only the holder may clear the flag, so a
  // slow old utterance settling late cannot mark a newer one as finished.
  // `stop()` is the only thing that bumps it, so `ticket !== mine` reads as
  // exactly one thing: "you were interrupted".
  let ticket = 0;
  let speaking = false;
  /** Lines accepted but not yet started. The one being spoken is NOT here. */
  let queue: Array<{ text: string; done: () => void }> = [];
  let draining = false;

  /** Resolve and forget everything pending. Used by `stop()` and the cap. */
  const discardPending = () => {
    const dropped = queue;
    queue = [];
    for (const item of dropped) item.done();
  };

  const stop = () => {
    ticket += 1;
    speaking = false;
    // Order matters: empty the queue BEFORE stopping the backend, so a drain
    // loop that wakes on the backend settling finds nothing left to say.
    discardPending();
    try {
      current?.stop();
    } catch {
      // A backend failing to stop must not stop the caller.
    }
    current = null;
  };

  /** One line, start to finish. Never throws; warns and degrades to silence. */
  const utter = async (trimmed: string, mine: number): Promise<void> => {
    let backend: SpeechBackend;
    try {
      backend = makeBackend(getSettings());
    } catch (error) {
      warn(`voice unavailable: ${(error as Error)?.message ?? "unknown"}`);
      return;
    }
    current = backend;
    speaking = true;
    try {
      await backend.speak(trimmed);
    } catch (error) {
      // "The engine has no voice configured" is not a failure, it is a
      // deliberate downgrade to the platform voice — but it is still SAID.
      // A silent downgrade to the robot voice is precisely the failure this
      // product's honesty rules exist to prevent: the user hears the voice
      // they complained about and nothing anywhere explains why.
      if (error instanceof ServiceVoiceUnavailable) {
        serviceVoiceRefused = true;
        // Note what this no longer says: "so Ada is using the platform voice
        // instead". She is not, unless the platform voice was asked for by
        // name. The engine having no voice now produces silence, and the
        // sentence says which one it is.
        warn(
          getSettings().platformVoice
            ? `the engine has no voice provisioned, so Hardy is using the ` +
                `platform voice you selected — ${error.reason}`
            : `the engine has no voice provisioned, so Hardy is staying ` +
                `silent — ${error.reason}`
        );
      } else {
        // The message never carries the API key: the backends are written to
        // throw status codes and API names only.
        warn(
          `${backend.name} text-to-speech failed: ${
            (error as Error)?.message ?? "unknown"
          }`
        );
      }
      // A paid voice that fails must not take the free one down with it:
      // retry the utterance once on the built-in voice — unless stop() has
      // claimed the mouth meanwhile, or the failed backend already was the
      // built-in voice (then degrade to silence).
      if (backend.name !== "webspeech" && ticket === mine) {
        try {
          const fallback = makeFallback();
          // Null means "nobody chose the platform voice, so there is no
          // second voice to try". Silence, having already been explained by
          // the warning above.
          if (!fallback) return;
          current = fallback;
          await fallback.speak(trimmed);
        } catch (fallbackError) {
          warn(
            `webspeech fallback failed: ${
              (fallbackError as Error)?.message ?? "unknown"
            }`
          );
        }
      }
    } finally {
      if (ticket === mine) {
        speaking = false;
        current = null;
      }
    }
  };

  /**
   * Drain the queue, one line at a time.
   *
   * Re-entrant by refusal rather than by locking: a `speak` during a drain
   * pushes and returns, and this loop picks the line up on its next turn.
   * The loop deliberately does NOT break when `ticket` moves — `stop()` has
   * already emptied the queue, so anything still there was enqueued *after*
   * the stop and is owed its turn. Breaking instead would strand it, since
   * the `speak` that added it saw `draining` and did not start a loop.
   */
  const drain = async (): Promise<void> => {
    if (draining) return;
    draining = true;
    try {
      for (;;) {
        const next = queue.shift();
        if (!next) return;
        const mine = ticket;
        try {
          await utter(next.text, mine);
        } finally {
          next.done();
        }
      }
    } finally {
      draining = false;
    }
  };

  return {
    speak(text: string): Promise<void> {
      const trimmed = text.trim();
      if (!trimmed) return Promise.resolve();
      // The executor runs synchronously, so the line is queued — and the
      // drain below starts speaking it — before this function returns. That
      // is what keeps `isSpeaking()` true immediately after `speak()`.
      const spoken = new Promise<void>((resolve) => {
        queue.push({ text: trimmed, done: resolve });
      });
      while (queue.length > MAX_QUEUED_UTTERANCES) {
        // Oldest out. See MAX_QUEUED_UTTERANCES: the newest line is the one
        // that still describes the run.
        queue.shift()?.done();
      }
      void drain();
      return spoken;
    },
    stop,
    isSpeaking(): boolean {
      return speaking;
    },
    pending(): number {
      return queue.length;
    },
  };
}

/**
 * The app-wide instance. One per webview is exactly right: the point is that
 * the whole window shares a single voice that never talks over itself.
 */
export const speaker: Speaker = createSpeaker();

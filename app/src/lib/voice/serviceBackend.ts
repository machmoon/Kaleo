// Ada's voice, synthesized by the engine service instead of by the webview.
//
// This is one more backend behind the existing `SpeechBackend` interface in
// `../speech/backends.ts` — deliberately a NEW file in a NEW directory rather
// than an edit there, so it can land without touching a file another change
// is already in. Wiring it up is three small edits, listed at the bottom of
// this comment.
//
// WHY IT EXISTS. Every word Ada speaks today is `speechSynthesis`, which on
// macOS resolves to a *Compact* system voice — the robot the complaint is
// about — and the webview cannot do better, because it can only speak with
// voices the OS installed. `POST /speak` can run a real neural model
// (Kokoro-82M, Apache-2.0 weights, no key, no per-word cost) or a hosted one,
// and it keeps any API key out of the renderer entirely.
//
// THE WIRE FORMAT. `format: "pcm16"` is asked for, not `wav`, and the frames
// are scheduled through Web Audio rather than handed to an `<audio>` element.
// That is the whole latency argument: an `<audio>` element cannot start until
// it has enough of a container to decode, which is exactly the whole-file
// download that makes the current ElevenLabs path feel slow. Raw PCM has no
// container, so the first 2 KB that arrive are playable. The sample rate is
// NOT assumed — it is read from the `X-Kaleo-Sample-Rate` header, because
// engines differ and resampling a 24 kHz stream as 44.1 kHz is a chipmunk.
//
// FALLBACK, LOUDLY. A 503 from `/speak` means no engine is configured, and
// its body says so in words. That is not an error to swallow: it is the
// signal to use `speechSynthesis` — i.e. today's behaviour — and to say that
// is what happened. `ServiceVoiceUnavailable` is thrown for exactly that case
// so the caller can tell it apart from a real failure, matching the honesty
// rule the speaker already keeps: a failed synthesis must never read as "the
// agent had nothing to say".
//
// TO WIRE IT UP (three edits, all in files owned elsewhere right now):
//   1. `speech/backends.ts`, the `SpeechBackend["name"]` union:
//          readonly name: "webspeech" | "elevenlabs" | "service";
//   2. `speech/speaker.ts`, `defaultMakeBackend`: prefer this backend when
//      `serviceVoiceReady(baseUrl, token)` resolved true, e.g.
//          if (settings.serviceVoice) return createServiceBackend({ baseUrl, token });
//   3. `speech/speaker.ts`, the `speak` catch: treat `ServiceVoiceUnavailable`
//      as a fall-through to `makeFallback()` **with** a `warn(...)`, never a
//      silent downgrade.
// Nothing here imports from `speech/`, so this file compiles and tests on its
// own before any of that lands.

/** How long one utterance may take end to end. A digest is a few sentences. */
export const SERVICE_SPEAK_TIMEOUT_MS = 30_000;

/** Frames are scheduled this far ahead of the clock, to absorb network jitter
 *  without adding audible delay. Below ~60 ms a slow chunk produces a gap. */
export const SCHEDULE_LEAD_S = 0.12;

/** What the service says when no engine is configured. */
export class ServiceVoiceUnavailable extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super(reason);
    this.name = "ServiceVoiceUnavailable";
    this.reason = reason;
  }
}

/** A real failure: an engine was configured, asked, and could not deliver. */
export class ServiceVoiceFailed extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ServiceVoiceFailed";
    this.status = status;
  }
}

export interface ServiceBackendOptions {
  /** Engine base URL, e.g. `http://127.0.0.1:8081`. */
  baseUrl: string;
  /** Access token, when the service is gated. */
  token?: string | null;
  /** Force one engine (`"kokoro"`, `"elevenlabs"`, `"system"`). Omit to let
   *  the service choose — which is what you want: it knows what is installed. */
  engine?: string;
  /** Voice id within the engine. Omit for the engine's default. */
  voice?: string;
  /** Injected for tests. Defaults to Tauri's HTTP fetch, because the app
   *  origin is `tauri://localhost` and a webview fetch would be cross-origin. */
  fetchImpl?: typeof fetch;
  /** Injected for tests. Defaults to the platform AudioContext. */
  audioContextFactory?: () => AudioContext;
}

type ErrorBody = { error?: string; fallback?: string };

function authHeaders(token?: string | null): Record<string, string> {
  const value = (token ?? "").trim();
  return value ? { Authorization: `Bearer ${value}` } : {};
}

async function defaultFetch(...args: Parameters<typeof fetch>) {
  // Imported lazily so this module can be unit-tested in plain Node, where
  // `@tauri-apps/plugin-http` has no host to talk to.
  const { fetch: tauriFetch } = await import("@tauri-apps/plugin-http");
  return tauriFetch(...args);
}

/**
 * Ask the service whether it has a voice, without synthesizing anything.
 *
 * `GET /speak` is read-only, always 200, and never makes a live call, so this
 * is safe to call at startup to decide which backend to build. Returns the
 * selected engine name, or null when the service cannot speak — in which case
 * the caller should stay on `speechSynthesis`.
 */
export async function serviceVoiceReady(
  baseUrl: string,
  token?: string | null,
  fetchImpl?: typeof fetch
): Promise<string | null> {
  const doFetch = fetchImpl ?? defaultFetch;
  try {
    const response = await doFetch(`${baseUrl}/speak`, {
      method: "GET",
      headers: { ...authHeaders(token) },
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) return null;
    const body = (await response.json()) as { selected?: string | null };
    return typeof body.selected === "string" && body.selected ? body.selected : null;
  } catch {
    // A service that is not running is not an error worth surfacing here —
    // the caller's fallback is the voice it already has.
    return null;
  }
}

/**
 * Convert signed 16-bit little-endian PCM to the float samples Web Audio
 * wants. Division by 32768 (not 32767) keeps the mapping symmetric, so a
 * full-scale negative sample does not clip on the way in.
 */
export function pcm16ToFloat32(bytes: Uint8Array, offset = 0): Float32Array {
  const usable = (bytes.length - offset) & ~1; // whole samples only
  const view = new DataView(bytes.buffer, bytes.byteOffset + offset, usable);
  const out = new Float32Array(usable / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }
  return out;
}

/**
 * Speak through `POST /speak`, scheduling frames as they arrive.
 *
 * Shaped to satisfy `SpeechBackend` structurally; it is not typed as one so
 * that this file has no import from `speech/`. Once the name union there
 * gains `"service"` it will be assignable with no change here.
 */
export function createServiceBackend(options: ServiceBackendOptions) {
  const {
    baseUrl,
    token,
    engine,
    voice,
    fetchImpl,
    audioContextFactory,
  } = options;
  const doFetch = fetchImpl ?? defaultFetch;
  let context: AudioContext | null = null;
  let sources: AudioBufferSourceNode[] = [];
  let controller: AbortController | null = null;
  let stopped = false;

  function teardown() {
    for (const source of sources) {
      try {
        source.stop();
      } catch {
        // Already ended. Stopping a finished source throws in some engines
        // and means nothing here.
      }
    }
    sources = [];
    if (context) {
      const dying = context;
      context = null;
      void dying.close().catch(() => {});
    }
  }

  return {
    name: "service" as const,

    stop(): void {
      stopped = true;
      controller?.abort();
      controller = null;
      teardown();
    },

    async speak(text: string): Promise<void> {
      stopped = false;
      const body: Record<string, string> = { text, format: "pcm16" };
      if (engine) body.engine = engine;
      if (voice) body.voice = voice;

      controller = new AbortController();
      const signal = AbortSignal.any([
        controller.signal,
        AbortSignal.timeout(SERVICE_SPEAK_TIMEOUT_MS),
      ]);

      let response: Response;
      try {
        response = await doFetch(`${baseUrl}/speak`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders(token) },
          body: JSON.stringify(body),
          signal,
        });
      } catch (error) {
        const name = (error as Error)?.name ?? "";
        if (name === "AbortError") return; // stop() — not a failure.
        throw new ServiceVoiceFailed(
          "Could not reach the engine to speak.",
          0
        );
      }

      if (!response.ok) {
        let parsed: ErrorBody = {};
        try {
          parsed = (await response.json()) as ErrorBody;
        } catch {
          parsed = {};
        }
        const detail = parsed.error || `The engine answered ${response.status}.`;
        // 503 is "nothing is configured", which is a fallback signal rather
        // than a failure. Anything else is a genuine problem worth reporting.
        if (response.status === 503) throw new ServiceVoiceUnavailable(detail);
        throw new ServiceVoiceFailed(detail, response.status);
      }

      // Read, never assume: engines differ, and playing 24 kHz frames at the
      // context's own rate is an audible pitch error, not a rounding one.
      const declared = Number(response.headers.get("X-Kaleo-Sample-Rate"));
      const sampleRate =
        Number.isFinite(declared) && declared > 0 ? declared : 24_000;

      context = (audioContextFactory ?? (() => new AudioContext()))();
      let cursor = context.currentTime + SCHEDULE_LEAD_S;
      let spoke = false;
      // An odd byte left over from a chunk boundary: PCM16 samples are two
      // bytes and a chunk may split one. Carried, never dropped — dropping it
      // shifts every later sample by one byte and turns speech into noise.
      let carry: Uint8Array | null = null;

      const schedule = (chunk: Uint8Array) => {
        let bytes = chunk;
        if (carry && carry.length) {
          const joined = new Uint8Array(carry.length + chunk.length);
          joined.set(carry, 0);
          joined.set(chunk, carry.length);
          bytes = joined;
        }
        const whole = bytes.length & ~1;
        carry = bytes.length > whole ? bytes.slice(whole) : null;
        if (whole === 0) return;
        const samples = pcm16ToFloat32(bytes.subarray(0, whole));
        if (!samples.length || !context) return;
        const buffer = context.createBuffer(1, samples.length, sampleRate);
        buffer.copyToChannel(samples, 0);
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.connect(context.destination);
        // Never behind the clock: a chunk that arrived late would otherwise
        // be scheduled in the past and play instantly, overlapping its
        // predecessor as a stutter.
        cursor = Math.max(cursor, context.currentTime);
        source.start(cursor);
        cursor += buffer.duration;
        sources.push(source);
        spoke = true;
      };

      const stream = response.body;
      if (stream) {
        const reader = stream.getReader();
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            if (stopped) return;
            if (value && value.length) schedule(value);
          }
        } finally {
          reader.releaseLock();
        }
      } else {
        // Some HTTP clients (including, at the time of writing, Tauri's
        // plugin-http on some platforms) hand back a fully buffered body with
        // no `.body` stream. That costs the streaming benefit and nothing
        // else, so it degrades rather than failing — but it is stated here
        // rather than hidden, because it is the difference between ~150 ms
        // and whole-utterance latency.
        const buffered = new Uint8Array(await response.arrayBuffer());
        if (stopped) return;
        schedule(buffered);
      }

      if (stopped) return;
      if (!spoke) {
        // A 200 with no frames. Silence must never be reported as speech.
        throw new ServiceVoiceFailed(
          "The engine answered with no audio.",
          response.status
        );
      }

      // Resolve when the last scheduled frame has actually played, so the
      // speaker's queue does not start the next line over this one.
      const remaining = context ? cursor - context.currentTime : 0;
      if (remaining > 0) {
        await new Promise<void>((resolve) => setTimeout(resolve, remaining * 1000));
      }
      if (!stopped) teardown();
    },
  };
}

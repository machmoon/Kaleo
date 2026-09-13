// @vitest-environment jsdom
//
// One rule, checked in both backends: **stop means make no sound**, not
// "cut sound that is already playing".
//
// Neither backend produces audio the instant `speak` is called. Web speech
// waits for the webview's voice list (VOICES_WAIT_MS), ElevenLabs waits for
// a POST to come back. A mute pressed inside that gap used to reach a
// synthesiser that was not speaking yet — `cancel()` did nothing, the fetch
// was never checked — and the utterance began afterwards, with the click
// that was supposed to prevent it already spent. That is the founder's
// "can't mute Ada": the stop signal was dropped, not ignored.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { createWebSpeechBackend } from "./backends";

type Spoken = { text: string; utterance: SpeechSynthesisUtterance };

/**
 * A `speechSynthesis` that answers with an empty voice list until told
 * otherwise — the shape that makes `speak` wait, which is the whole window
 * this test is about.
 */
function installSynth(): {
  spoken: Spoken[];
  cancels: number;
  ready: () => void;
} {
  const spoken: Spoken[] = [];
  const state = { cancels: 0 };
  let voices: unknown[] = [];
  const listeners = new Set<() => void>();
  const synth = {
    getVoices: () => voices,
    speak: (utterance: SpeechSynthesisUtterance) => {
      spoken.push({ text: utterance.text, utterance });
    },
    cancel: () => {
      state.cancels += 1;
    },
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  Object.defineProperty(globalThis, "speechSynthesis", {
    value: synth,
    configurable: true,
    writable: true,
  });
  Object.defineProperty(globalThis, "SpeechSynthesisUtterance", {
    value: class {
      text: string;
      onend: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(text: string) {
        this.text = text;
      }
    },
    configurable: true,
    writable: true,
  });
  return {
    spoken,
    get cancels() {
      return state.cancels;
    },
    ready: () => {
      voices = [{ name: "test" }];
      for (const fn of [...listeners]) fn();
    },
  };
}

afterEach(() => {
  Reflect.deleteProperty(globalThis, "speechSynthesis");
  Reflect.deleteProperty(globalThis, "SpeechSynthesisUtterance");
});

describe("web speech backend", () => {
  it("a stop that lands before the voice list does keeps me silent", async () => {
    const synth = installSynth();
    const backend = createWebSpeechBackend();
    const speaking = backend.speak("the board is placed");
    // The mute, pressed while the webview is still loading its voices.
    backend.stop();
    synth.ready();
    await speaking;
    expect(synth.spoken).toEqual([]);
  });

  it("without a stop, the same wait still ends in speech", async () => {
    const synth = installSynth();
    const backend = createWebSpeechBackend();
    const speaking = backend.speak("the board is placed");
    synth.ready();
    // One tick for the voices promise to settle before the utterance lands.
    await Promise.resolve();
    await Promise.resolve();
    expect(synth.spoken).toHaveLength(1);
    (synth.spoken[0].utterance.onend as unknown as () => void)?.();
    await speaking;
  });
});

// ---------------------------------------------------------------------------
// Voice quality (the free half)
// ---------------------------------------------------------------------------

import {
  DEFAULT_ELEVENLABS_VOICE_ID,
  ELEVENLABS_MODEL_ID,
  ELEVENLABS_STREAM_OUTPUT_FORMAT,
  createElevenLabsBackend,
  pickVoice,
  progressivePlaybackSupported,
  pumpStreamIntoSink,
  streamElevenLabsAudio,
  type AudioChunkSink,
} from "./backends";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";

const mockFetch = vi.mocked(tauriFetch);

beforeEach(() => {
  mockFetch.mockReset();
});

const voice = (
  name: string,
  lang = "en-US",
  extra: Partial<SpeechSynthesisVoice> = {}
): SpeechSynthesisVoice =>
  ({
    name,
    lang,
    localService: true,
    default: false,
    voiceURI: name,
    ...extra,
  }) as SpeechSynthesisVoice;

describe("pickVoice", () => {
  it("prefers the neural voices macOS only installs on request", () => {
    const voices = [
      voice("Albert (Compact)"),
      voice("Samantha"),
      voice("Ava (Enhanced)"),
      voice("Zoe (Premium)"),
    ];
    expect(pickVoice(voices, "en-US")?.name).toBe("Zoe (Premium)");
    // Premium not installed: Enhanced is the next best, still not Compact.
    expect(pickVoice(voices.slice(0, 3), "en-US")?.name).toBe("Ava (Enhanced)");
  });

  it("degrades to whatever is installed rather than to silence", () => {
    // The default macOS install: one robot, nothing else. It must still be
    // chosen — a preference that becomes a requirement is a mute button.
    expect(pickVoice([voice("Albert (Compact)")], "en-US")?.name).toBe(
      "Albert (Compact)"
    );
    expect(pickVoice([], "en-US")).toBeNull();
  });

  it("would rather be understood than be pretty", () => {
    // A Premium voice in the wrong language reads the digest as gibberish.
    const chosen = pickVoice(
      [voice("Kyoko (Premium)", "ja-JP"), voice("Albert (Compact)", "en-US")],
      "en-US"
    );
    expect(chosen?.name).toBe("Albert (Compact)");
  });

  it("is deterministic: the same list gives the same voice", () => {
    const voices = [voice("Ava (Enhanced)"), voice("Allison (Enhanced)")];
    const first = pickVoice(voices, "en-US");
    expect(pickVoice(voices, "en-US")).toBe(first);
    // The tie breaks on the platform's own ordering, not on ours.
    expect(first?.name).toBe("Ava (Enhanced)");
  });
});

// ---------------------------------------------------------------------------
// Streaming (the paid half)
// ---------------------------------------------------------------------------

/** A sink that records rather than plays; `done` is settled by the test. */
function fakeSink() {
  let settle!: () => void;
  const chunks: number[][] = [];
  const state = { ended: false, disposed: false };
  const sink: AudioChunkSink & typeof state & { chunks: number[][] } = {
    chunks,
    get ended() {
      return state.ended;
    },
    get disposed() {
      return state.disposed;
    },
    push: (chunk: Uint8Array) => {
      chunks.push([...chunk]);
    },
    end: () => {
      state.ended = true;
      settle();
    },
    done: new Promise<void>((resolve) => {
      settle = resolve;
    }),
    dispose: () => {
      state.disposed = true;
      settle();
    },
  };
  return sink;
}

function streamOf(...chunks: number[][]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new Uint8Array(chunk));
      controller.close();
    },
  });
}

/** Only `.ok`, `.status` and `.body` are read; a real Response is not needed. */
function streamingResponse(
  stream: ReadableStream<Uint8Array> | null,
  status = 200
) {
  return { ok: status >= 200 && status < 300, status, body: stream } as Response;
}

describe("progressivePlaybackSupported", () => {
  it("answers false rather than throwing where MediaSource is absent", () => {
    // jsdom has no MediaSource — the same shape as a webview that refuses
    // audio/mpeg, and the reason the buffered sink still exists.
    expect(progressivePlaybackSupported()).toBe(false);
  });
});

describe("streamElevenLabsAudio", () => {
  it("asks the streaming endpoint, with the key in the header and nowhere else", async () => {
    mockFetch.mockResolvedValueOnce(streamingResponse(streamOf([1, 2, 3])));
    await streamElevenLabsAudio("secret-key", "hello board", "voice-42");

    const [url, init] = mockFetch.mock.calls[0];
    expect(String(url)).toBe(
      `https://api.elevenlabs.io/v1/text-to-speech/voice-42/stream?output_format=${ELEVENLABS_STREAM_OUTPUT_FORMAT}`
    );
    const headers = init?.headers as Record<string, string>;
    expect(headers["xi-api-key"]).toBe("secret-key");
    expect(JSON.parse(String(init?.body))).toEqual({
      text: "hello board",
      model_id: ELEVENLABS_MODEL_ID,
    });
    // The rule this repo does not bend: never in a URL, never in a body.
    expect(String(url)).not.toContain("secret-key");
    expect(String(init?.body)).not.toContain("secret-key");
  });

  it("falls back to the default voice id, and reports a status only", async () => {
    mockFetch.mockResolvedValueOnce(streamingResponse(streamOf([1])));
    await streamElevenLabsAudio("k", "text");
    expect(String(mockFetch.mock.calls[0][0])).toContain(
      DEFAULT_ELEVENLABS_VOICE_ID
    );

    mockFetch.mockResolvedValueOnce(streamingResponse(null, 401));
    await expect(
      streamElevenLabsAudio("secret-key", "text")
    ).rejects.toThrow("ElevenLabs answered 401");
  });
});

describe("pumpStreamIntoSink", () => {
  it("hands every chunk over, then closes the sink", async () => {
    const sink = fakeSink();
    await pumpStreamIntoSink(streamOf([1, 2], [3]), sink);
    expect(sink.chunks).toEqual([[1, 2], [3]]);
    expect(sink.ended).toBe(true);
  });

  it("a stop mid-stream stops reading, and does not end the sink", async () => {
    const second = fakeSink();
    // "The mute was pressed" becomes true once one chunk has been handed over.
    await pumpStreamIntoSink(
      streamOf([1], [2], [3]),
      second,
      () => second.chunks.length >= 1
    );
    expect(second.chunks).toEqual([[1]]);
    // Not `end()`: the utterance was cut short, it did not finish.
    expect(second.ended).toBe(false);
  });
});

describe("elevenlabs backend", () => {
  it("a stop while the request is in flight plays nothing and aborts it", async () => {
    let seenSignal: AbortSignal | undefined;
    let release!: (r: Response) => void;
    mockFetch.mockImplementationOnce((_url, init) => {
      seenSignal = init?.signal ?? undefined;
      return new Promise<Response>((resolve) => {
        release = resolve;
      });
    });
    const makeSink = vi.fn(fakeSink);
    const backend = createElevenLabsBackend({ apiKey: "k", makeSink });

    const speaking = backend.speak("the board is placed");
    backend.stop();
    release(streamingResponse(streamOf([1, 2, 3])));
    await speaking;

    // No sink was ever built, so no sound was ever made...
    expect(makeSink).not.toHaveBeenCalled();
    // ...and the download was cancelled rather than left running.
    expect(seenSignal?.aborted).toBe(true);
  });

  it("plays a stream through to the end when nobody interrupts", async () => {
    const sink = fakeSink();
    mockFetch.mockResolvedValueOnce(streamingResponse(streamOf([9], [8])));
    const backend = createElevenLabsBackend({
      apiKey: "k",
      makeSink: () => sink,
    });
    await backend.speak("digest");
    expect(sink.chunks).toEqual([[9], [8]]);
    expect(sink.ended).toBe(true);
  });

  it("a stop mid-playback silences the sink and never reports a failure", async () => {
    const sink = fakeSink();
    let signal: AbortSignal | undefined;
    mockFetch.mockImplementationOnce((_url, init) => {
      signal = init?.signal ?? undefined;
      // One chunk, then a body that only ever ends by being aborted — which
      // is what a real fetch does: aborting the request errors the stream.
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new Uint8Array([1]));
        },
        pull() {
          return new Promise<void>((_resolve, reject) => {
            signal?.addEventListener("abort", () =>
              reject(new Error("request aborted"))
            );
          });
        },
      });
      return Promise.resolve(streamingResponse(stream));
    });

    const backend = createElevenLabsBackend({
      apiKey: "k",
      makeSink: () => sink,
    });
    const speaking = backend.speak("digest");
    await vi.waitFor(() => expect(sink.chunks).toEqual([[1]]));
    backend.stop();

    // Resolves rather than rejects: being stopped is not a failure, and the
    // speaker retries a *failed* paid backend on the free voice — which would
    // make a mute audibly speak the digest all over again.
    await expect(speaking).resolves.toBeUndefined();
    expect(sink.disposed).toBe(true);
    // Halting audio is only half of stop: the download stops too.
    expect(signal?.aborted).toBe(true);
  });
});

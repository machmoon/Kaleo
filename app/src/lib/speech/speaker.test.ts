// Tests for backend selection, the one-mouth rule, and the never-throw rule.
//
// `@tauri-apps/plugin-http` is mocked wholesale, same as the client tests:
// no test here may ever reach the real ElevenLabs API — there is no key in
// CI and a real call would spend someone's quota to run a unit test. The
// fetch mock also lets the ElevenLabs test assert the exact request shape
// (URL, `xi-api-key` header, JSON body) that the API contract demands.

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import {
  DEFAULT_ELEVENLABS_VOICE_ID,
  ELEVENLABS_MODEL_ID,
  fetchElevenLabsAudio,
  webSpeechAvailable,
  type SpeechBackend,
} from "./backends";
import {
  MAX_QUEUED_UTTERANCES,
  chooseBackendName,
  createSpeaker,
} from "./speaker";
import type { VoiceSettings } from "./settings";

const mockFetch = vi.mocked(tauriFetch);

const settings = (overrides: Partial<VoiceSettings> = {}): VoiceSettings => ({
  enabled: true,
  elevenLabsKey: "",
  voiceId: "",
  // Absence means "nobody chose the platform voice", which is the default a
  // fresh install has and therefore the one these tests should exercise.
  platformVoice: false,
  ...overrides,
});

beforeEach(() => {
  mockFetch.mockReset();
});

describe("chooseBackendName", () => {
  it("uses the free built-in voice when no key is configured", () => {
    expect(chooseBackendName("")).toBe("webspeech");
    expect(chooseBackendName("   ")).toBe("webspeech");
    expect(chooseBackendName(null)).toBe("webspeech");
    expect(chooseBackendName(undefined)).toBe("webspeech");
  });

  it("upgrades to elevenlabs on the presence of a key alone", () => {
    expect(chooseBackendName("xi-abc123")).toBe("elevenlabs");
  });
});

describe("webSpeechAvailable", () => {
  it("reports unavailable in an environment without speechSynthesis", () => {
    // Node has no speechSynthesis; the check must answer false, not throw —
    // this is the defensive runtime probe the WKWebView note demands.
    expect(webSpeechAvailable()).toBe(false);
  });
});

describe("fetchElevenLabsAudio", () => {
  it("sends the documented request shape with the key only in the header", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(new Uint8Array([1, 2, 3]), { status: 200 })
    );
    await fetchElevenLabsAudio("secret-key", "hello board", "voice-42");

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("https://api.elevenlabs.io/v1/text-to-speech/voice-42");
    const headers = init?.headers as Record<string, string>;
    expect(headers["xi-api-key"]).toBe("secret-key");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual({
      text: "hello board",
      model_id: ELEVENLABS_MODEL_ID,
    });
    // The key travels in the header and nowhere else.
    expect(url).not.toContain("secret-key");
    expect(String(init?.body)).not.toContain("secret-key");
  });

  it("falls back to the default voice id when none is set", async () => {
    mockFetch.mockResolvedValueOnce(new Response(new Uint8Array(), { status: 200 }));
    await fetchElevenLabsAudio("k", "text");
    expect(String(mockFetch.mock.calls[0][0])).toContain(
      DEFAULT_ELEVENLABS_VOICE_ID
    );
  });

  it("throws the status only — never the key, never the response body", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "bad key qqq" }), { status: 401 })
    );
    await expect(fetchElevenLabsAudio("secret-key", "text")).rejects.toThrow(
      "ElevenLabs answered 401"
    );
    try {
      mockFetch.mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "bad key qqq" }), { status: 401 })
      );
      await fetchElevenLabsAudio("secret-key", "text");
    } catch (error) {
      expect(String((error as Error).message)).not.toContain("secret-key");
      expect(String((error as Error).message)).not.toContain("qqq");
    }
  });
});

/** A controllable fake backend: resolves when the test says so. */
function fakeBackend(name: SpeechBackend["name"] = "webspeech") {
  let resolveSpeak: (() => void) | null = null;
  const backend: SpeechBackend & { spoke: string[]; stops: number } = {
    name,
    spoke: [],
    stops: 0,
    speak(text: string) {
      backend.spoke.push(text);
      return new Promise<void>((resolve) => {
        resolveSpeak = resolve;
      });
    },
    stop() {
      backend.stops += 1;
      resolveSpeak?.();
      resolveSpeak = null;
    },
  };
  return backend;
}

describe("createSpeaker", () => {
  it("queues instead of interrupting: the second line waits for the first", async () => {
    // This replaces a test that pinned the OPPOSITE rule ("a new speak stops
    // the old"). That rule is what made a sentence-chunked digest audible
    // only for its last sentence, and what truncated the first of two stage
    // moments mid-word. Cutting a line off is now something only `stop()`
    // does.
    const backend = fakeBackend();
    const warn = vi.fn();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings(),
      makeBackend: () => backend,
      warn,
    });

    const first = speakerUnderTest.speak("first digest");
    expect(speakerUnderTest.isSpeaking()).toBe(true);
    const second = speakerUnderTest.speak("second digest");
    // The second line is waiting, not speaking, and it did not cut the first
    // one off on its way in.
    expect(backend.spoke).toEqual(["first digest"]);
    expect(speakerUnderTest.pending()).toBe(1);
    expect(backend.stops).toBe(0);

    backend.stop(); // the first utterance reaches its natural end
    await first;
    await vi.waitFor(() =>
      expect(backend.spoke).toEqual(["first digest", "second digest"])
    );
    expect(speakerUnderTest.pending()).toBe(0);
    backend.stop();
    await second;

    expect(speakerUnderTest.isSpeaking()).toBe(false);
    expect(warn).not.toHaveBeenCalled();
  });

  it("stop() clears the queue: a cancelled line is never resurrected", async () => {
    // The rule that outranks queueing. `announce.takeMicFloor` calls stop()
    // when the human takes the microphone, so this is what makes barge-in
    // beat everything pending rather than merely pausing it.
    const backend = fakeBackend();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings(),
      makeBackend: () => backend,
      warn: vi.fn(),
    });

    const first = speakerUnderTest.speak("first digest");
    const queued = speakerUnderTest.speak("queued digest");
    expect(speakerUnderTest.pending()).toBe(1);

    speakerUnderTest.stop();
    // Both settle — a dropped line resolves rather than hanging its caller.
    await Promise.all([first, queued]);
    expect(speakerUnderTest.pending()).toBe(0);
    expect(speakerUnderTest.isSpeaking()).toBe(false);
    // A tick for any drain loop that might still be alive to prove it is not.
    await vi.waitFor(() => expect(backend.spoke).toEqual(["first digest"]));
    expect(backend.spoke).not.toContain("queued digest");
  });

  it("is bounded, and drops the oldest pending line rather than the newest", async () => {
    // A burst of stage moments must not leave Ada monologuing through news
    // that stopped being true. The newest line is the one still worth saying.
    const backend = fakeBackend();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings(),
      makeBackend: () => backend,
      warn: vi.fn(),
    });

    const speaking = speakerUnderTest.speak("now speaking");
    const dropped = speakerUnderTest.speak("oldest pending");
    for (let i = 1; i <= MAX_QUEUED_UTTERANCES; i += 1) {
      void speakerUnderTest.speak(`pending ${i}`);
    }
    expect(speakerUnderTest.pending()).toBe(MAX_QUEUED_UTTERANCES);
    // The dropped line settles rather than hanging whoever awaited it.
    await dropped;

    speakerUnderTest.stop();
    await speaking;
    expect(backend.spoke).toEqual(["now speaking"]);
  });

  it("retries a failed paid backend on the built-in voice, never throwing", async () => {
    const warn = vi.fn();
    const fallback = fakeBackend("webspeech");
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings({ elevenLabsKey: "secret-key" }),
      makeBackend: () => ({
        name: "elevenlabs",
        speak: () => Promise.reject(new Error("ElevenLabs answered 500")),
        stop: () => {},
      }),
      makeFallback: () => fallback,
      warn,
    });

    const spoken = speakerUnderTest.speak("digest");
    // Let the rejection propagate, then finish the fallback utterance.
    await Promise.resolve().then(() => {});
    await vi.waitFor(() => expect(fallback.spoke).toEqual(["digest"]));
    fallback.stop();
    await expect(spoken).resolves.toBeUndefined();
    expect(speakerUnderTest.isSpeaking()).toBe(false);
    // One warning for the paid backend; the fallback then speaks the digest.
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).not.toContain("secret-key");
  });

  it("degrades to silence with two warnings when the fallback also fails", async () => {
    const warn = vi.fn();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings({ elevenLabsKey: "secret-key" }),
      makeBackend: () => ({
        name: "elevenlabs",
        speak: () => Promise.reject(new Error("ElevenLabs answered 500")),
        stop: () => {},
      }),
      makeFallback: () => ({
        name: "webspeech",
        speak: () =>
          Promise.reject(
            new Error("speechSynthesis is not available in this webview")
          ),
        stop: () => {},
      }),
      warn,
    });

    await expect(speakerUnderTest.speak("digest")).resolves.toBeUndefined();
    expect(speakerUnderTest.isSpeaking()).toBe(false);
    expect(warn).toHaveBeenCalledTimes(2);
    for (const call of warn.mock.calls) {
      expect(String(call[0])).not.toContain("secret-key");
    }
  });

  it("does not retry when the built-in voice itself was the failure", async () => {
    const warn = vi.fn();
    const makeFallback = vi.fn();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings(),
      makeBackend: () => ({
        name: "webspeech",
        speak: () =>
          Promise.reject(
            new Error("speechSynthesis is not available in this webview")
          ),
        stop: () => {},
      }),
      makeFallback,
      warn,
    });

    await expect(speakerUnderTest.speak("digest")).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(1);
    expect(makeFallback).not.toHaveBeenCalled();
  });

  it("survives a backend factory that throws (no speechSynthesis at all)", async () => {
    const warn = vi.fn();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings(),
      makeBackend: () => {
        throw new Error("speechSynthesis is not available in this webview");
      },
      warn,
    });
    await expect(speakerUnderTest.speak("digest")).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("ignores empty text without touching a backend", async () => {
    const makeBackend = vi.fn();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings(),
      makeBackend,
      warn: vi.fn(),
    });
    await speakerUnderTest.speak("   ");
    expect(makeBackend).not.toHaveBeenCalled();
  });

  it("stop() silences and clears the speaking flag", async () => {
    const backend = fakeBackend();
    const speakerUnderTest = createSpeaker({
      getSettings: () => settings(),
      makeBackend: () => backend,
      warn: vi.fn(),
    });
    const speaking = speakerUnderTest.speak("digest");
    speakerUnderTest.stop();
    await speaking;
    expect(backend.stops).toBe(1);
    expect(speakerUnderTest.isSpeaking()).toBe(false);
  });
});

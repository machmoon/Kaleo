// The wake word, without a microphone.
//
// The two properties this file exists to pin: the listener never keeps the
// microphone after it should have let go (stop, wake, cap), and the paid
// backend never sends more windows than its cap — a forgotten overlay must
// not bill in the background.

import { describe, expect, it, vi } from "vitest";

import {
  DEFAULT_WINDOW_CAP,
  LOUDNESS_THRESHOLD,
  chooseWakeBackend,
  createLocalListener,
  createSpeechListener,
  createWakeWordListener,
  createWindowListener,
  describeWakeBackend,
  loudEnough,
  matchWakeWord,
  peakDeviation,
  type SpeechRecognitionLike,
  type SpeechResultEventLike,
  type WakeListenerEvents,
} from "./wake-word";

describe("matchWakeWord", () => {
  it("finds the word and hands back what followed it, casing kept", () => {
    expect(matchWakeWord("Ada, I need a 3.3V LDO for USB")).toEqual({
      utterance: "I need a 3.3V LDO for USB",
    });
    expect(matchWakeWord("hey ada")).toEqual({ utterance: "" });
    expect(matchWakeWord("ADA.")).toEqual({ utterance: "" });
  });

  it("accepts the recognizer's usual mishearings", () => {
    expect(matchWakeWord("Aida can you draft a board")?.utterance).toBe(
      "can you draft a board"
    );
    expect(matchWakeWord("eda")).not.toBeNull();
    expect(matchWakeWord("heyadda make an LDO")?.utterance).toBe("make an LDO");
    expect(matchWakeWord("hey Otto make an LDO")?.utterance).toBe("make an LDO");
    expect(matchWakeWord("(inaudible)")).toBeNull();
  });

  it("accepts what the engine actually returned for a real utterance", () => {
    // Measured 2026-09-06 against the running service (gemini-3.5-flash-lite)
    // on clips cut by the windowing in this file from `say` output. The first
    // two are what the utterance-bounded window produced; the third is what
    // the retired fixed 4 s window produced from the same recording — it
    // still matches, but it lost "Hey" off the front and "microfarad input
    // capacitor" off the end, which is the whole reason the window changed.
    expect(
      matchWakeWord("Hey Ada, make me a 3.3 volt LDO board with a 10 microfarad input capacitor.")
        ?.utterance
    ).toBe("make me a 3.3 volt LDO board with a 10 microfarad input capacitor.");
    expect(matchWakeWord("hey Ada, make me a 3.3 volt LDO board.")?.utterance).toBe(
      "make me a 3.3 volt LDO board."
    );
    expect(matchWakeWord("Ada, make me a 3.3 volt LDO board with a 10")?.utterance).toBe(
      "make me a 3.3 volt LDO board with a 10"
    );
  });

  it("matches whole words only", () => {
    expect(matchWakeWord("the USB adapter needs a regulator")).toBeNull();
    expect(matchWakeWord("shipping to Canada")).toBeNull();
    expect(matchWakeWord("")).toBeNull();
  });
});

describe("chooseWakeBackend", () => {
  const ctor = class {} as unknown as typeof MediaRecorder;
  const gum = async () => ({}) as MediaStream;

  it("prefers the OS recognizer when the webview exposes one (non-mac)", () => {
    expect(
      chooseWakeBackend({
        webkitSpeechRecognition: ctor as never,
        MediaRecorder: ctor,
        navigator: {
          platform: "Win32",
          mediaDevices: { getUserMedia: gum },
        },
      })
    ).toBe("speech");
  });

  it("on macOS prefers recorded windows even when SpeechRecognition exists", () => {
    expect(
      chooseWakeBackend({
        webkitSpeechRecognition: ctor as never,
        MediaRecorder: ctor,
        navigator: {
          platform: "MacIntel",
          userAgent: "Macintosh",
          mediaDevices: { getUserMedia: gum },
        },
      })
    ).toBe("windows");
  });

  it("falls back to recorded windows, and to nothing when it cannot record", () => {
    expect(
      chooseWakeBackend({
        MediaRecorder: ctor,
        navigator: { platform: "Linux x86_64", mediaDevices: { getUserMedia: gum } },
      })
    ).toBe("windows");
    expect(chooseWakeBackend({ MediaRecorder: ctor, navigator: {} })).toBeNull();
    expect(chooseWakeBackend({})).toBeNull();
  });

  it("describes the paid path as one listen, not always-on", () => {
    expect(describeWakeBackend("windows")).toContain("local speech detect");
    expect(describeWakeBackend("windows")).toContain(String(DEFAULT_WINDOW_CAP));
    expect(describeWakeBackend("windows")).toMatch(/not always-on/i);
    expect(describeWakeBackend("windows")).toMatch(/mic button is the reliable path/);
    expect(describeWakeBackend("speech")).toContain("no engine calls");
    expect(describeWakeBackend("local")).toContain("on-device wake word");
    expect(describeWakeBackend(null)).toContain("no speech recognition");
  });
});

describe("loudness gate", () => {
  it("measures peak deviation from the analyser's 128 centre", () => {
    expect(peakDeviation([128, 128, 128])).toBe(0);
    expect(peakDeviation([128, 140, 120])).toBe(12);
  });

  it("drops room tone and keeps speech", () => {
    expect(loudEnough(LOUDNESS_THRESHOLD - 1)).toBe(false);
    expect(loudEnough(LOUDNESS_THRESHOLD)).toBe(true);
  });
});

// --------------------------------------------------------------- speech

class FakeRecognition implements SpeechRecognitionLike {
  static instances: FakeRecognition[] = [];
  continuous = false;
  interimResults = true;
  lang = "";
  onresult: ((event: SpeechResultEventLike) => void) | null = null;
  onend: (() => void) | null = null;
  onerror: ((event: { error?: string }) => void) | null = null;
  started = 0;
  aborted = 0;
  constructor() {
    FakeRecognition.instances.push(this);
  }
  start() {
    this.started += 1;
  }
  stop() {}
  abort() {
    this.aborted += 1;
  }
  say(transcript: string, isFinal = true) {
    this.onresult?.({ resultIndex: 0, results: [{ isFinal, 0: { transcript } }] });
  }
}

function events(): WakeListenerEvents & {
  wake: ReturnType<typeof vi.fn>;
  state: ReturnType<typeof vi.fn>;
  window: ReturnType<typeof vi.fn>;
} {
  const wake = vi.fn();
  const state = vi.fn();
  const window = vi.fn();
  return { onWake: wake, onState: state, onWindow: window, wake, state, window };
}

describe("createSpeechListener", () => {
  it("listens continuously, wakes on a final result, and lets the recognizer go", async () => {
    FakeRecognition.instances = [];
    const ev = events();
    const listener = createSpeechListener({ recognition: FakeRecognition }, ev);
    await listener.start();
    const rec = FakeRecognition.instances[0];
    expect(rec.continuous).toBe(true);
    expect(rec.interimResults).toBe(false);
    expect(ev.state).toHaveBeenCalledWith("listening", expect.any(String));

    rec.say("ada draft me a board", false); // interim: not yet
    expect(ev.wake).not.toHaveBeenCalled();
    rec.say("the adapter is fine"); // no wake word
    expect(ev.wake).not.toHaveBeenCalled();
    rec.say("Ada draft me a board");
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "draft me a board",
      backend: "speech",
    });
    expect(rec.aborted).toBe(1);
    // Ended after the wake: it must not spin up again.
    rec.onend?.();
    expect(FakeRecognition.instances).toHaveLength(1);
  });

  it("restarts after the recognizer ends on silence, but not after stop()", async () => {
    FakeRecognition.instances = [];
    const ev = events();
    const listener = createSpeechListener({ recognition: FakeRecognition }, ev);
    await listener.start();
    FakeRecognition.instances[0].onend?.();
    expect(FakeRecognition.instances).toHaveLength(2);
    listener.stop();
    expect(FakeRecognition.instances[1].aborted).toBe(1);
    FakeRecognition.instances[1].onend?.();
    expect(FakeRecognition.instances).toHaveLength(2);
    expect(ev.state).toHaveBeenLastCalledWith("stopped", expect.any(String));
  });

  it("reports a refused microphone as an error and stops", async () => {
    FakeRecognition.instances = [];
    const ev = events();
    const listener = createSpeechListener({ recognition: FakeRecognition }, ev);
    await listener.start();
    FakeRecognition.instances[0].onerror?.({ error: "no-speech" });
    expect(ev.state).not.toHaveBeenCalledWith("error", expect.anything());
    FakeRecognition.instances[0].onerror?.({ error: "not-allowed" });
    expect(ev.state).toHaveBeenLastCalledWith(
      "error",
      expect.stringContaining("speech recognition was refused")
    );
  });
});

// -------------------------------------------------------------- windows

type Track = { stop: ReturnType<typeof vi.fn>; enabled: boolean };

function makeStream(): { stream: MediaStream; tracks: Track[] } {
  const tracks: Track[] = [{ stop: vi.fn(), enabled: true }];
  return { stream: { getTracks: () => tracks } as unknown as MediaStream, tracks };
}

class FakeRecorder {
  static instances: FakeRecorder[] = [];
  state: "inactive" | "recording" = "inactive";
  mimeType = "audio/mp4";
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor(_stream: MediaStream, _options?: unknown) {
    FakeRecorder.instances.push(this);
  }
  start(timeslice?: number) {
    void timeslice;
    this.state = "recording";
  }
  requestData() {
    this.ondataavailable?.({ data: new Blob(["aud"], { type: this.mimeType }) });
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["aud"], { type: this.mimeType }) });
    this.onstop?.();
  }
}

/** A hand-cranked clock so each window is closed exactly when the test says. */
function clock() {
  const timers: Array<() => void> = [];
  return {
    setTimeout: ((fn: () => void) => {
      timers.push(fn);
      return timers.length as unknown as ReturnType<typeof setTimeout>;
    }) as unknown as typeof globalThis.setTimeout,
    clearTimeout: (() => undefined) as unknown as typeof globalThis.clearTimeout,
    tick: () => {
      const fn = timers.shift();
      fn?.();
    },
  };
}

async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("createWindowListener", () => {
  it("sends each window, wakes on the transcript, and releases the mic", async () => {
    FakeRecorder.instances = [];
    const { stream, tracks } = makeStream();
    const transcribe = vi
      .fn()
      .mockResolvedValueOnce({ text: "(inaudible)", model: "m" })
      .mockResolvedValueOnce({ text: "Ada, an LDO please", model: "m" });
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        token: "tok",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        hopMs: 3_000,
        windowMs: 3_000,
        cap: 5,
        // No AudioContext: the ungated rolling mode, which is opt-in only.
        vad: false,
        // This listener is a command clip, never an idle spotter: it is only
        // ever armed after the on-device wake word or push-to-talk, which is
        // what a live continuation window means.
        continuing: () => true,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    expect(ev.state).toHaveBeenCalledWith("listening", expect.stringContaining("at most 5"));
    expect(FakeRecorder.instances).toHaveLength(1);

    c.tick();
    expect(transcribe).toHaveBeenCalledTimes(1);
    expect(transcribe.mock.calls[0][0]).toBe("http://engine");
    expect(transcribe.mock.calls[0][1].token).toBe("tok");
    expect(transcribe.mock.calls[0][1].purpose).toBe("wake");
    expect(ev.window).toHaveBeenLastCalledWith(1, 5);
    // Recording continues while the first window is in flight.
    expect(FakeRecorder.instances).toHaveLength(2);
    await flush();
    expect(ev.wake).not.toHaveBeenCalled();

    c.tick();
    await flush();
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "an LDO please",
      backend: "windows",
      // This arming opted out of the gate, and the detection says so.
      gated: false,
    });
    expect(tracks[0].stop).toHaveBeenCalled();
    expect(FakeRecorder.instances).toHaveLength(3);
    expect(FakeRecorder.instances[2].state).toBe("inactive");
  });

  it("stops itself at the cap and says so", async () => {
    FakeRecorder.instances = [];
    const { stream, tracks } = makeStream();
    const transcribe = vi.fn().mockResolvedValue({ text: "nothing", model: "m" });
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        hopMs: 3_000,
        windowMs: 3_000,
        cap: 2,
        // No AudioContext: the ungated rolling mode, which is opt-in only.
        vad: false,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    c.tick();
    c.tick();
    c.tick(); // nothing scheduled any more
    expect(transcribe).toHaveBeenCalledTimes(2);
    expect(ev.state).toHaveBeenLastCalledWith(
      "capped",
      expect.stringContaining("last of my 2 listening calls")
    );
    expect(tracks[0].stop).toHaveBeenCalled();
  });

  it("a window still in flight when the ear is switched off can never wake the page", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const settle: Array<(r: { text: string; model: string }) => void> = [];
    const transcribe = vi.fn(
      () => new Promise<{ text: string; model: string }>((resolve) => settle.push(resolve))
    );
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        hopMs: 3_000,
        windowMs: 3_000,
        cap: 5,
        // No AudioContext: the ungated rolling mode, which is opt-in only.
        vad: false,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    c.tick();
    c.tick();
    expect(transcribe).toHaveBeenCalledTimes(2);
    listener.stop();
    expect(ev.state).toHaveBeenLastCalledWith("stopped", "stopped listening");
    // Both requests were aborted, not only the most recent one.
    const signals = transcribe.mock.calls.map((call) => (call as unknown[])[2] as AbortSignal);
    expect(signals.map((s) => s.aborted)).toEqual([true, true]);

    // The older window answers late, with the wake word in it.
    settle[0]({ text: "Ada go", model: "m" });
    settle[1]({ text: "Ada go", model: "m" });
    await flush();
    expect(ev.wake).not.toHaveBeenCalled();
    expect(ev.state.mock.calls.filter(([state]) => state === "stopped")).toHaveLength(1);
  });

  it("the window that reaches the cap is still read: its wake word is not thrown away", async () => {
    FakeRecorder.instances = [];
    const { stream, tracks } = makeStream();
    let settle: (r: { text: string; model: string }) => void = () => {};
    const transcribe = vi.fn(
      () => new Promise<{ text: string; model: string }>((resolve) => (settle = resolve))
    );
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        hopMs: 3_000,
        windowMs: 3_000,
        cap: 1,
        // No AudioContext: the ungated rolling mode, which is opt-in only.
        vad: false,
        continuing: () => true,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    c.tick();
    expect(transcribe).toHaveBeenCalledTimes(1);
    expect(ev.state).toHaveBeenLastCalledWith(
      "capped",
      expect.stringContaining("last of my 1 listening call")
    );
    // The microphone is gone the moment the cap is hit…
    expect(tracks[0].stop).toHaveBeenCalled();
    const signal = (transcribe.mock.calls[0] as unknown[])[2] as AbortSignal;
    expect(signal.aborted).toBe(false);
    // …but the call that was paid for still answers.
    settle({ text: "Ada draft an LDO", model: "m" });
    await flush();
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "draft an LDO",
      backend: "windows",
      // This arming opted out of the gate, and the detection says so.
      gated: false,
    });
    expect(["capped", "stopped"]).toContain(ev.state.mock.lastCall?.[0]);
  });

  it("drops a silent window without a call when the loudness gate is on", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const transcribe = vi.fn().mockResolvedValue({ text: "", model: "m" });
    const c = clock();
    vi.useFakeTimers();
    let loud = false;
    const analyser = {
      fftSize: 0,
      getByteTimeDomainData: (buffer: Uint8Array) => {
        buffer.fill(loud ? 200 : 129);
      },
    };
    class FakeContext {
      createAnalyser() {
        return analyser;
      }
      createMediaStreamSource() {
        return { connect: () => undefined };
      }
      close() {
        return Promise.resolve();
      }
    }
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        AudioContext: FakeContext as unknown as typeof AudioContext,
        transcribe,
        hopMs: 3_000,
        windowMs: 3_000,
        cap: 5,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      events()
    );
    await listener.start();
    vi.advanceTimersByTime(300); // the meter samples room tone
    c.tick();
    expect(transcribe).not.toHaveBeenCalled();
    loud = true;
    // Long enough to clear MIN_UTTERANCE_MS: a window shorter than that is
    // a cough, and is dropped without a call.
    vi.advanceTimersByTime(1_000);
    c.tick();
    expect(transcribe).toHaveBeenCalledTimes(1);
    listener.stop();
    vi.useRealTimers();
  });

  it("stop() during the permission prompt still releases the stream", async () => {
    const { stream, tracks } = makeStream();
    let grant: (s: MediaStream) => void = () => {};
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: () => new Promise<MediaStream>((resolve) => (grant = resolve)),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe: vi.fn(),
      },
      events()
    );
    const started = listener.start();
    listener.stop();
    grant(stream);
    await started;
    expect(tracks[0].stop).toHaveBeenCalled();
  });

  it("arm-once: speech without the wake word is still the command", async () => {
    FakeRecorder.instances = [];
    const { stream, tracks } = makeStream();
    const transcribe = vi.fn().mockResolvedValue({ text: "make me an LDO", model: "m" });
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        hopMs: 3_000,
        windowMs: 3_000,
        cap: 1,
        // No AudioContext: the ungated rolling mode, which is opt-in only.
        vad: false,
        continuing: () => true,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    c.tick();
    await flush();
    expect(transcribe.mock.calls[0][1].purpose).toBe("wake");
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "make me an LDO",
      backend: "windows",
      // This arming opted out of the gate, and the detection says so.
      gated: false,
    });
    expect(tracks[0].stop).toHaveBeenCalled();
  });

  it("a refused microphone rejects start() with the reason", async () => {
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockRejectedValue(new Error("Permission denied")),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe: vi.fn(),
      },
      events()
    );
    await expect(listener.start()).rejects.toThrow(/Permission denied/);
  });

  it("refuses to arm when no loudness gate can be built, rather than sending blind", async () => {
    FakeRecorder.instances = [];
    const { stream, tracks } = makeStream();
    const transcribe = vi.fn();
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        windowMs: 3_000,
        hopMs: 3_000,
        cap: 5,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    // Nothing recorded, nothing sent, and the microphone is already gone.
    expect(FakeRecorder.instances).toHaveLength(0);
    c.tick();
    expect(transcribe).not.toHaveBeenCalled();
    expect(tracks[0].stop).toHaveBeenCalled();
    expect(ev.state).toHaveBeenLastCalledWith("error", expect.stringContaining("can’t gate"));
    expect(ev.state).toHaveBeenLastCalledWith("error", expect.stringContaining("I "));
    expect(ev.state).not.toHaveBeenCalledWith("listening", expect.anything());
  });

  it("an AudioContext that throws refuses to arm instead of falling through", async () => {
    FakeRecorder.instances = [];
    const { stream, tracks } = makeStream();
    const transcribe = vi.fn();
    const c = clock();
    const ev = events();
    class BrokenContext {
      constructor() {
        throw new Error("audio hardware busy");
      }
    }
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        AudioContext: BrokenContext as unknown as typeof AudioContext,
        transcribe,
        windowMs: 3_000,
        hopMs: 3_000,
        cap: 5,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    expect(FakeRecorder.instances).toHaveLength(0);
    c.tick();
    expect(transcribe).not.toHaveBeenCalled();
    expect(tracks[0].stop).toHaveBeenCalled();
    expect(ev.state).toHaveBeenLastCalledWith(
      "error",
      expect.stringContaining("audio hardware busy")
    );
  });

  it("sends the window's measured peak and marks the detection gated", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const transcribe = vi.fn().mockResolvedValue({ text: "Ada", model: "m" });
    const c = clock();
    vi.useFakeTimers();
    const analyser = {
      fftSize: 0,
      getByteTimeDomainData: (buffer: Uint8Array) => {
        buffer.fill(180); // deviation 52, well over LOUDNESS_THRESHOLD
      },
    };
    class FakeContext {
      createAnalyser() {
        return analyser;
      }
      createMediaStreamSource() {
        return { connect: () => undefined };
      }
      close() {
        return Promise.resolve();
      }
    }
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        AudioContext: FakeContext as unknown as typeof AudioContext,
        transcribe,
        windowMs: 3_000,
        hopMs: 3_000,
        cap: 5,
        continuing: () => true,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    vi.advanceTimersByTime(1_200);
    c.tick();
    expect(transcribe).toHaveBeenCalledTimes(1);
    expect(transcribe.mock.calls[0][1].peak).toBe(52);
    vi.useRealTimers();
    await flush();
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "",
      backend: "windows",
      gated: true,
    });
  });

  it("an ungated window's wake says so, so no continuation is opened for a bare name", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const transcribe = vi.fn().mockResolvedValue({ text: "Ada", model: "m" });
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        windowMs: 3_000,
        hopMs: 3_000,
        cap: 1,
        vad: false,
        continuing: () => true,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    expect(ev.state).toHaveBeenCalledWith("listening", expect.stringContaining("ungated"));
    c.tick();
    // No measurement was taken, so none is claimed.
    expect(transcribe.mock.calls[0][1].peak).toBeUndefined();
    await flush();
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "",
      backend: "windows",
      gated: false,
    });
  });

  it("outside a continuation window it never wakes — idle spotting by transcript is gone", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    // The clearest possible wake word, and the alias the old matcher was
    // widest on. Neither may arm anything: deciding whether a name was
    // spoken is the on-device spotter's job, and doing it by transcribing
    // the room cost a model call per window to answer "nobody spoke".
    const transcribe = vi
      .fn()
      .mockResolvedValueOnce({ text: "Hey Ada, make me an LDO", model: "m" })
      .mockResolvedValueOnce({ text: "hey Otto", model: "m" });
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        windowMs: 3_000,
        hopMs: 3_000,
        cap: 5,
        vad: false,
        // No continuation: nothing has woken, so nothing was asked for.
        continuing: () => false,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    c.tick();
    await flush();
    c.tick();
    await flush();
    expect(ev.wake).not.toHaveBeenCalled();
    listener.stop();
  });
});

describe("createWakeWordListener: the paid backend is a command clip, not an ear", () => {
  /** A webview that can record but has no OS speech recognizer — WKWebView. */
  const recorderOnly = {
    MediaRecorder: class {} as unknown as typeof MediaRecorder,
    navigator: {
      userAgent: "Mozilla/5.0 (Macintosh)",
      mediaDevices: { getUserMedia: vi.fn() as unknown as MediaDevices["getUserMedia"] },
    },
  };

  const ev = (): WakeListenerEvents => ({
    onState: vi.fn(),
    onWake: vi.fn(),
  });

  it("refuses to arm while nothing has woken, so an idle room costs nothing", () => {
    expect(chooseWakeBackend(recorderOnly)).toBe("windows");
    const listener = createWakeWordListener(
      { baseUrl: "http://engine", globals: recorderOnly, continuing: () => false },
      ev()
    );
    expect(listener).toBeNull();
  });

  it("refuses when the caller offers no continuation at all", () => {
    const listener = createWakeWordListener(
      { baseUrl: "http://engine", globals: recorderOnly },
      ev()
    );
    expect(listener).toBeNull();
  });

  it("arms inside the window a wake or push-to-talk opened", () => {
    const listener = createWakeWordListener(
      { baseUrl: "http://engine", globals: recorderOnly, continuing: () => true },
      ev()
    );
    expect(listener?.backend).toBe("windows");
  });
});

/** An analyser whose room level the test drives, frame by frame. */
function meterRoom() {
  let level = 0;
  const analyser = {
    fftSize: 0,
    getByteTimeDomainData: (buffer: Uint8Array) => buffer.fill(128 + level),
  };
  class Ctx {
    createAnalyser() {
      return analyser;
    }
    createMediaStreamSource() {
      return { connect: () => undefined };
    }
    close() {
      return Promise.resolve();
    }
  }
  return {
    AudioContext: Ctx as unknown as typeof AudioContext,
    quiet: () => {
      level = 0;
    },
    talk: () => {
      level = 60;
    },
  };
}

describe("createWindowListener: one window is one utterance", () => {
  it("keeps recording while you are still talking and closes on the silence after", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const room = meterRoom();
    const transcribe = vi
      .fn()
      .mockResolvedValue({ text: "Hey Ada, make me a 3.3 volt LDO board", model: "m" });
    const c = clock();
    const ev = events();
    vi.useFakeTimers();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        AudioContext: room.AudioContext,
        transcribe,
        cap: 5,
        // A command clip, the only thing this listener is armed as now. The
        // clip can still catch the name — "Hey Ada, make me…" is one breath —
        // and the greeting is stripped rather than handed to the page.
        continuing: () => true,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();

    room.talk();
    // Longer than the retired fixed 4 s window would have allowed the
    // sentence: nothing is cut off, because nothing is on a stopwatch.
    vi.advanceTimersByTime(5_000);
    expect(FakeRecorder.instances).toHaveLength(1);
    expect(transcribe).not.toHaveBeenCalled();

    room.quiet();
    vi.advanceTimersByTime(700);
    expect(transcribe).toHaveBeenCalledTimes(1);
    // The ceiling timer was never reached; the room closed the window.
    expect(transcribe.mock.calls[0][1].peak).toBe(60);

    vi.useRealTimers();
    await flush();
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "make me a 3.3 volt LDO board",
      backend: "windows",
      gated: true,
    });
  });

  it("throws a cough away locally: too short to be speech, so no call is spent", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const room = meterRoom();
    const transcribe = vi.fn().mockResolvedValue({ text: "nothing", model: "m" });
    const c = clock();
    const ev = events();
    vi.useFakeTimers();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        AudioContext: room.AudioContext,
        transcribe,
        cap: 5,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();

    room.talk();
    vi.advanceTimersByTime(100); // one bang, and then the room again
    room.quiet();
    vi.advanceTimersByTime(1_000);
    expect(FakeRecorder.instances).toHaveLength(1); // it did open a recorder…
    expect(transcribe).not.toHaveBeenCalled(); // …and never sent it
    expect(ev.window).not.toHaveBeenCalled();
    expect(ev.state).not.toHaveBeenCalledWith("capped", expect.anything());

    // Still armed: the next real utterance is heard.
    room.talk();
    vi.advanceTimersByTime(1_500);
    room.quiet();
    vi.advanceTimersByTime(700);
    expect(transcribe).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
    listener.stop();
  });

  it("hits the ceiling on someone who does not stop talking", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const room = meterRoom();
    const transcribe = vi.fn().mockResolvedValue({ text: "nothing here", model: "m" });
    const c = clock();
    const ev = events();
    vi.useFakeTimers();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        AudioContext: room.AudioContext,
        transcribe,
        maxUtteranceMs: 2_000,
        cap: 5,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    room.talk();
    vi.advanceTimersByTime(5_000);
    // Two whole windows closed by the ceiling and a third still open: a
    // monologue is chunked, never held in one recorder that never lets go.
    expect(transcribe).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
    listener.stop();
  });
});

describe("createWindowListener: the listening budget", () => {
  it("counts against calls already spent, so a re-arming does not hand any back", async () => {
    FakeRecorder.instances = [];
    const { stream } = makeStream();
    const transcribe = vi.fn().mockResolvedValue({ text: "nothing", model: "m" });
    const c = clock();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia: vi.fn().mockResolvedValue(stream),
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe,
        windowMs: 3_000,
        hopMs: 3_000,
        cap: 3,
        spent: 2, // two calls already made this unmute
        vad: false,
        setTimeout: c.setTimeout,
        clearTimeout: c.clearTimeout,
      },
      ev
    );
    await listener.start();
    c.tick();
    expect(transcribe).toHaveBeenCalledTimes(1);
    expect(ev.window).toHaveBeenLastCalledWith(3, 3);
    expect(ev.state).toHaveBeenLastCalledWith(
      "capped",
      expect.stringContaining("last of my 3 listening calls")
    );
  });

  it("refuses to open the microphone at all on a spent budget", async () => {
    const getUserMedia = vi.fn();
    const ev = events();
    const listener = createWindowListener(
      {
        baseUrl: "http://engine",
        getUserMedia,
        MediaRecorder: FakeRecorder as unknown as typeof MediaRecorder,
        transcribe: vi.fn(),
        cap: 3,
        spent: 3,
      },
      ev
    );
    await listener.start();
    // A green ear over a budget with nothing left is the lie this prevents.
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(ev.state).toHaveBeenCalledWith("capped", expect.stringContaining("are spent"));
    expect(ev.state).not.toHaveBeenCalledWith("listening", expect.anything());
  });
});

describe("createLocalListener", () => {
  it("starts Rust, forwards ada-wake, and never touches getUserMedia", async () => {
    const start = vi.fn(async () => {});
    const stop = vi.fn(async () => {});
    let deliver: (hit: { utterance?: string }) => void = () => {};
    const subscribe = vi.fn(async (onHit: typeof deliver) => {
      deliver = onHit;
      return vi.fn();
    });
    const ev = events();
    const listener = createLocalListener({ start, stop, subscribe }, ev);
    expect(listener.backend).toBe("local");
    await listener.start();
    expect(start).toHaveBeenCalledTimes(1);
    expect(ev.state).toHaveBeenCalledWith("listening", expect.stringMatching(/on-device/));
    deliver({ utterance: "make an LDO" });
    expect(ev.wake).toHaveBeenCalledExactlyOnceWith({
      utterance: "make an LDO",
      backend: "local",
    });
    expect(stop).toHaveBeenCalled();
  });

  it("stop releases the Rust mic", async () => {
    const start = vi.fn(async () => {});
    const stop = vi.fn(async () => {});
    const unlisten = vi.fn();
    const subscribe = vi.fn(async () => unlisten);
    const ev = events();
    const listener = createLocalListener({ start, stop, subscribe }, ev);
    await listener.start();
    listener.stop();
    expect(unlisten).toHaveBeenCalled();
    expect(stop).toHaveBeenCalled();
    expect(ev.state).toHaveBeenCalledWith("stopped", "stopped listening");
  });
});

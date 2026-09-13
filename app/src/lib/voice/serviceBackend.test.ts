// The service-voice backend, with the network and the audio device faked.
//
// Nothing here opens a socket or an AudioContext: the fetch is injected and
// the AudioContext is a recorder that captures what would have been played.
// That is what lets the scheduling be asserted rather than listened to.

import { describe, expect, it, vi } from "vitest";

import {
  ServiceVoiceFailed,
  ServiceVoiceUnavailable,
  createServiceBackend,
  pcm16ToFloat32,
  serviceVoiceReady,
} from "./serviceBackend";

/** An AudioContext that records every buffer and start time. */
function recordingContext() {
  const played: { start: number; samples: Float32Array; rate: number }[] = [];
  let now = 0;
  const context = {
    get currentTime() {
      return now;
    },
    createBuffer(_channels: number, length: number, rate: number) {
      const data = new Float32Array(length);
      return {
        duration: length / rate,
        length,
        sampleRate: rate,
        copyToChannel(source: Float32Array) {
          data.set(source);
        },
        _data: data,
      };
    },
    createBufferSource() {
      const node: Record<string, unknown> = {
        buffer: null,
        connect() {},
        start(when: number) {
          const buffer = node.buffer as { _data: Float32Array; sampleRate: number };
          played.push({ start: when, samples: buffer._data, rate: buffer.sampleRate });
        },
        stop() {},
      };
      return node;
    },
    destination: {},
    close: async () => {},
  };
  return { context: context as unknown as AudioContext, played, advance: (t: number) => (now = t) };
}

/** A fetch that streams the given chunks as a real ReadableStream. */
function streamingFetch(
  chunks: Uint8Array[],
  { status = 200, sampleRate = "24000", body = null as unknown } = {}
) {
  const calls: { url: string; init: RequestInit }[] = [];
  const impl = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (status !== 200) {
      return {
        ok: false,
        status,
        headers: new Headers(),
        json: async () => body ?? {},
      } as unknown as Response;
    }
    return {
      ok: true,
      status,
      headers: new Headers({ "X-Kaleo-Sample-Rate": sampleRate }),
      body: new ReadableStream({
        start(ctrl) {
          for (const chunk of chunks) ctrl.enqueue(chunk);
          ctrl.close();
        },
      }),
      arrayBuffer: async () => new Uint8Array().buffer,
    } as unknown as Response;
  });
  return { impl: impl as unknown as typeof fetch, calls };
}

/** N samples of PCM16 at a fixed value. */
function pcm(count: number, value = 1000): Uint8Array {
  const bytes = new Uint8Array(count * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < count; i += 1) view.setInt16(i * 2, value, true);
  return bytes;
}

describe("pcm16ToFloat32", () => {
  it("maps full scale symmetrically", () => {
    const bytes = new Uint8Array(6);
    const view = new DataView(bytes.buffer);
    view.setInt16(0, 0, true);
    view.setInt16(2, 32767, true);
    view.setInt16(4, -32768, true);
    const out = pcm16ToFloat32(bytes);
    expect(out[0]).toBe(0);
    expect(out[1]).toBeCloseTo(0.99997, 4);
    // Dividing by 32768 rather than 32767 is what keeps this exactly -1
    // instead of clipping just past it.
    expect(out[2]).toBe(-1);
  });

  it("ignores a trailing odd byte rather than reading past the buffer", () => {
    expect(pcm16ToFloat32(new Uint8Array([1, 2, 3])).length).toBe(1);
  });
});

describe("serviceVoiceReady", () => {
  it("returns the selected engine", async () => {
    const impl = vi.fn(async () => ({
      ok: true,
      json: async () => ({ selected: "kokoro" }),
    })) as unknown as typeof fetch;
    expect(await serviceVoiceReady("http://svc", null, impl)).toBe("kokoro");
  });

  it("returns null when the service has no voice", async () => {
    const impl = vi.fn(async () => ({
      ok: true,
      json: async () => ({ selected: null }),
    })) as unknown as typeof fetch;
    expect(await serviceVoiceReady("http://svc", null, impl)).toBeNull();
  });

  it("returns null rather than throwing when the service is not running", async () => {
    const impl = vi.fn(async () => {
      throw new Error("ECONNREFUSED");
    }) as unknown as typeof fetch;
    // The caller's fallback is the voice it already has; a down service is
    // not an error worth surfacing from a capability probe.
    expect(await serviceVoiceReady("http://svc", null, impl)).toBeNull();
  });
});

describe("createServiceBackend", () => {
  it("asks for raw pcm so the first chunk is playable", async () => {
    const { impl, calls } = streamingFetch([pcm(240)]);
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await backend.speak("V bus.");
    const sent = JSON.parse(calls[0].init.body as string);
    // Not "wav": a container cannot be decoded until enough of it exists,
    // which is the whole-file wait this backend exists to remove.
    expect(sent.format).toBe("pcm16");
    expect(sent.text).toBe("V bus.");
    expect(calls[0].url).toBe("http://svc/speak");
  });

  it("schedules chunks back to back without gaps or overlap", async () => {
    // Three chunks of 240 samples = 10 ms each at 24 kHz.
    const { impl } = streamingFetch([pcm(240), pcm(240), pcm(240)]);
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await backend.speak("Placement is done.");
    expect(rec.played.length).toBe(3);
    const starts = rec.played.map((p) => p.start);
    expect(starts[1] - starts[0]).toBeCloseTo(0.01, 5);
    expect(starts[2] - starts[1]).toBeCloseTo(0.01, 5);
  });

  it("honours the declared sample rate instead of assuming one", async () => {
    // Playing 24 kHz frames as 44.1 kHz is a chipmunk, not a rounding error.
    const { impl } = streamingFetch([pcm(240)], { sampleRate: "16000" });
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await backend.speak("V bus.");
    expect(rec.played[0].rate).toBe(16000);
  });

  it("carries an odd byte across a chunk boundary", async () => {
    // Dropping it would shift every later sample by one byte, which turns
    // speech into noise -- silently, and only for some chunk sizes.
    const first = new Uint8Array([0x10, 0x27, 0x10]); // 1.5 samples
    const second = new Uint8Array([0x27, 0x10, 0x27]); // completes 2, leaves 1
    const { impl } = streamingFetch([first, second]);
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await backend.speak("hi");
    const total = rec.played.reduce((n, p) => n + p.samples.length, 0);
    // 6 bytes delivered, 3 whole samples reconstructed across the boundary.
    expect(total).toBe(3);
    for (const p of rec.played) {
      for (const s of p.samples) expect(s).toBeCloseTo(10000 / 32768, 5);
    }
  });

  it("treats a 503 as a fallback signal, not a failure", async () => {
    const { impl } = streamingFetch([], {
      status: 503,
      body: { error: "no speech engine is configured", fallback: "client speechSynthesis" },
    });
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await expect(backend.speak("V bus.")).rejects.toBeInstanceOf(ServiceVoiceUnavailable);
    // And it carries the reason, so the caller can say WHY it fell back
    // rather than silently changing voice.
    await expect(backend.speak("V bus.")).rejects.toThrow(/no speech engine is configured/);
  });

  it("treats a 502 as a real failure", async () => {
    const { impl } = streamingFetch([], {
      status: 502,
      body: { error: "the speech host refused the request: HTTP 401" },
    });
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await expect(backend.speak("V bus.")).rejects.toBeInstanceOf(ServiceVoiceFailed);
  });

  it("refuses to report an empty 200 as having spoken", async () => {
    const { impl } = streamingFetch([]);
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await expect(backend.speak("V bus.")).rejects.toThrow(/no audio/);
  });

  it("falls back to a buffered body when the client cannot stream", async () => {
    // Tauri's plugin-http may hand back a fully buffered response. That costs
    // the streaming benefit and nothing else, so it must still speak.
    const impl = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ "X-Kaleo-Sample-Rate": "24000" }),
      body: null,
      arrayBuffer: async () => pcm(240).buffer,
    })) as unknown as typeof fetch;
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await backend.speak("V bus.");
    expect(rec.played.length).toBe(1);
    expect(rec.played[0].samples.length).toBe(240);
  });

  it("stop() during a stream ends quietly rather than throwing", async () => {
    const rec = recordingContext();
    let backend: ReturnType<typeof createServiceBackend>;
    const impl = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ "X-Kaleo-Sample-Rate": "24000" }),
      body: new ReadableStream({
        start(ctrl) {
          ctrl.enqueue(pcm(240));
          // The human took the microphone mid-utterance.
          backend.stop();
          ctrl.enqueue(pcm(240));
          ctrl.close();
        },
      }),
      arrayBuffer: async () => new Uint8Array().buffer,
    })) as unknown as typeof fetch;
    backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    // Barge-in is not a failure and must not surface as one.
    await expect(backend.speak("Placement is done.")).resolves.toBeUndefined();
  });

  it("names the engine and voice when told to", async () => {
    const { impl, calls } = streamingFetch([pcm(240)]);
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      token: "tok",
      engine: "kokoro",
      voice: "af_heart",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await backend.speak("V bus.");
    const sent = JSON.parse(calls[0].init.body as string);
    expect(sent.engine).toBe("kokoro");
    expect(sent.voice).toBe("af_heart");
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe(
      "Bearer tok"
    );
  });

  it("omits the Authorization header entirely when there is no token", async () => {
    const { impl, calls } = streamingFetch([pcm(240)]);
    const rec = recordingContext();
    const backend = createServiceBackend({
      baseUrl: "http://svc",
      fetchImpl: impl,
      audioContextFactory: () => rec.context,
    });
    await backend.speak("V bus.");
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});

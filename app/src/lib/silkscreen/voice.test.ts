// Tests for the transcription client.
//
// Same discipline as client.test.ts: the plugin fetch is mocked wholesale, so
// every byte of the "network" is owned here. The cap tests matter most — an
// over-limit blob must be refused before a single byte moves, and the cap
// itself must fit the service's 1 MiB body limit after base64 inflation.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError } from "./client";
import {
  MAX_AUDIO_BYTES,
  blobToBase64,
  pickRecordingMime,
  transcribe,
} from "./voice";

const mockFetch = vi.mocked(tauriFetch);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function failure(promise: Promise<unknown>): Promise<SilkscreenError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(SilkscreenError);
    return error as SilkscreenError;
  }
  throw new Error("expected the promise to reject");
}

const audioBlob = (bytes: number, type = "audio/webm") =>
  new Blob([new Uint8Array(bytes).fill(7)], { type });

beforeEach(() => {
  mockFetch.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("pickRecordingMime", () => {
  const stubRecorder = (supported: string[]) => {
    vi.stubGlobal("MediaRecorder", {
      isTypeSupported: (type: string) => supported.includes(type),
    });
  };

  it("prefers ogg/opus, which Gemini accepts natively", () => {
    stubRecorder([
      "audio/ogg;codecs=opus",
      "audio/mp4",
      "audio/webm;codecs=opus",
    ]);
    expect(pickRecordingMime()).toBe("audio/ogg;codecs=opus");
  });

  it("takes mp4/AAC when opus containers are unsupported (WKWebView)", () => {
    stubRecorder(["audio/mp4"]);
    expect(pickRecordingMime()).toBe("audio/mp4");
  });

  it("falls back to webm/opus, Chromium's native format", () => {
    stubRecorder(["audio/webm;codecs=opus", "audio/webm"]);
    expect(pickRecordingMime()).toBe("audio/webm;codecs=opus");
  });

  it("returns empty (recorder's choice) when nothing matches", () => {
    stubRecorder([]);
    expect(pickRecordingMime()).toBe("");
  });

  it("returns empty when MediaRecorder does not exist at all", () => {
    vi.stubGlobal("MediaRecorder", undefined);
    expect(pickRecordingMime()).toBe("");
  });
});

describe("size cap", () => {
  it("fits the service's 1 MiB body limit after base64 inflation", () => {
    const encoded = Math.ceil(MAX_AUDIO_BYTES / 3) * 4;
    const envelope = 1024; // {audio_b64, mime_type, language} plus JSON syntax
    expect(encoded + envelope).toBeLessThan(1 << 20);
  });

  it("refuses an over-cap blob locally, before any network call", async () => {
    const error = await failure(
      transcribe("http://x", {
        blob: audioBlob(MAX_AUDIO_BYTES + 1),
        mimeType: "audio/webm",
      })
    );
    expect(error.kind).toBe("request");
    expect(error.message).toContain(String(MAX_AUDIO_BYTES));
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("refuses an empty blob", async () => {
    const error = await failure(
      transcribe("http://x", { blob: audioBlob(0), mimeType: "audio/webm" })
    );
    expect(error.kind).toBe("request");
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe("transcribe", () => {
  it("POSTs base64 audio to /transcribe and returns the transcript", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(200, { text: "a 3.3V LDO board", model: "gemini-test" })
    );
    const blob = audioBlob(9, "audio/ogg;codecs=opus");
    const result = await transcribe("http://127.0.0.1:8081", {
      blob,
      mimeType: "audio/ogg;codecs=opus",
      language: "en",
      token: "sekrit",
    });

    expect(result).toEqual({ text: "a 3.3V LDO board", model: "gemini-test" });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8081/transcribe");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer sekrit",
    });
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({
      audio_b64: await blobToBase64(blob),
      mime_type: "audio/ogg;codecs=opus",
      language: "en",
    });
    expect(body.purpose).toBeUndefined();
    // The payload decodes back to the exact recorded bytes.
    expect(atob(body.audio_b64)).toBe("\x07".repeat(9));
  });

  it("forwards purpose wake so the engine can spare the board-run pacer", async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, { text: "hi", model: "m" }));
    await transcribe("http://x", {
      blob: audioBlob(3),
      mimeType: "audio/webm",
      purpose: "wake",
    });
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body)).purpose).toBe("wake");
  });

  it("omits language and Authorization when not provided", async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, { text: "hi", model: "m" }));
    await transcribe("http://x", { blob: audioBlob(3), mimeType: "audio/mp4" });
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body));
    expect(body.language).toBeUndefined();
    expect(
      (init.headers as Record<string, string>).Authorization
    ).toBeUndefined();
  });

  it.each([
    [400, "request"],
    [413, "request"],
    [401, "auth"],
    [502, "upstream"],
    [503, "upstream"],
    [500, "server"],
  ])("maps a %s to kind %s", async (status, kind) => {
    mockFetch.mockResolvedValue(
      jsonResponse(status, { error: "nope", detail: "why", error_id: "e-1" })
    );
    const error = await failure(
      transcribe("http://x", { blob: audioBlob(3), mimeType: "audio/webm" })
    );
    expect(error.kind).toBe(kind);
    expect(error.status).toBe(status);
    expect(error.message).toBe("nope");
    expect(error.detail).toBe("why");
    expect(error.errorId).toBe("e-1");
  });

  it("reads a missing-key 502 as setup, not an outage", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(502, { error: "GOOGLE_API_KEY is not set" })
    );
    const error = await failure(
      transcribe("http://x", { blob: audioBlob(3), mimeType: "audio/webm" })
    );
    expect(error.kind).toBe("setup");
  });

  it("maps an unreachable engine to offline", async () => {
    mockFetch.mockRejectedValue(new TypeError("Failed to fetch"));
    const error = await failure(
      transcribe("http://x", { blob: audioBlob(3), mimeType: "audio/webm" })
    );
    expect(error.kind).toBe("offline");
    expect(error.detail).toBe("Failed to fetch");
  });

  it("maps an abort to cancelled", async () => {
    const controller = new AbortController();
    mockFetch.mockImplementation((_url, init) => {
      const signal = (init as RequestInit).signal as AbortSignal;
      return new Promise((_resolve, reject) => {
        const abort = () => reject(new DOMException("Aborted", "AbortError"));
        if (signal.aborted) abort();
        else signal.addEventListener("abort", abort);
      });
    });
    const promise = failure(
      transcribe(
        "http://x",
        { blob: audioBlob(3), mimeType: "audio/webm" },
        controller.signal
      )
    );
    controller.abort();
    expect((await promise).kind).toBe("cancelled");
  });

  it("refuses a 200 with an empty transcript rather than handing back nothing", async () => {
    mockFetch.mockResolvedValue(jsonResponse(200, { text: "  ", model: "m" }));
    const error = await failure(
      transcribe("http://x", { blob: audioBlob(3), mimeType: "audio/webm" })
    );
    expect(error.kind).toBe("server");
  });
});

describe("blobToBase64", () => {
  it("round-trips bytes exactly, without a data-URL prefix", async () => {
    const bytes = new Uint8Array([0, 1, 2, 250, 255]);
    const encoded = await blobToBase64(new Blob([bytes]));
    expect(encoded).not.toContain(",");
    const decoded = atob(encoded);
    expect([...decoded].map((c) => c.charCodeAt(0))).toEqual([0, 1, 2, 250, 255]);
  });
});

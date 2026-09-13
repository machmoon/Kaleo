// @vitest-environment jsdom
//
// The MediaRecorder lifecycle, driven with a scripted recorder and a mocked
// transcription call. The assertion this file exists for is track release:
// every path out of "recording" — stop, cancel, unmount — must stop every
// track, because a mic light left on is the one bug this feature cannot have.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn(async () => undefined) }));

vi.mock("@/lib/capture-permission", () => ({
  ensureCaptureAccess: vi.fn(async () => "granted" as const),
  ensureMicrophoneAccess: vi.fn(async () => "granted" as const),
}));

vi.mock("@/lib/silkscreen/voice", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/silkscreen/voice")>();
  return { ...original, transcribe: vi.fn() };
});

import { SilkscreenError } from "@/lib/silkscreen/client";
import { ensureCaptureAccess } from "@/lib/capture-permission";
import { transcribe } from "@/lib/silkscreen/voice";
import { useVoiceInput } from "./useVoiceInput";

const mockTranscribe = vi.mocked(transcribe);
const mockEnsureMic = vi.mocked(ensureCaptureAccess);

type Track = { stop: ReturnType<typeof vi.fn>; enabled: boolean };

function makeStream(): { stream: MediaStream; tracks: Track[] } {
  const tracks: Track[] = [{ stop: vi.fn(), enabled: true }];
  const stream = { getTracks: () => tracks } as unknown as MediaStream;
  return { stream, tracks };
}

/** A scripted stand-in: `stop()` emits one chunk then fires onstop, like the real one. */
class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  static isTypeSupported = () => true;

  state: "inactive" | "recording" = "inactive";
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? "audio/webm";
    FakeMediaRecorder.instances.push(this);
  }

  start() {
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["aud"], { type: this.mimeType }) });
    this.onstop?.();
  }
}

let getUserMedia: ReturnType<typeof vi.fn>;

beforeEach(() => {
  FakeMediaRecorder.instances = [];
  mockTranscribe.mockReset();
  mockEnsureMic.mockReset();
  mockEnsureMic.mockResolvedValue("granted");
  getUserMedia = vi.fn();
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  Object.defineProperty(navigator, "mediaDevices", {
    value: { getUserMedia },
    configurable: true,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const options = { baseUrl: "http://engine", token: "tok" };

describe("useVoiceInput", () => {
  it("records, transcribes, delivers the text, and releases the mic", async () => {
    const { stream, tracks } = makeStream();
    getUserMedia.mockResolvedValue(stream);
    mockTranscribe.mockResolvedValue({ text: "an LDO board", model: "m" });
    const onTranscript = vi.fn();

    const { result } = renderHook(() =>
      useVoiceInput({ ...options, onTranscript })
    );
    expect(result.current.status).toBe("idle");

    await act(() => result.current.start());
    expect(result.current.status).toBe("recording");
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });

    act(() => result.current.stop());
    // The mic is released before the network round trip, not after.
    expect(tracks[0].stop).toHaveBeenCalled();

    await waitFor(() => expect(result.current.status).toBe("idle"));
    expect(result.current.transcript).toBe("an LDO board");
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith("an LDO board");

    const [url, sent] = mockTranscribe.mock.calls[0];
    expect(url).toBe("http://engine");
    expect(sent.token).toBe("tok");
    expect(sent.blob.size).toBeGreaterThan(0);
    // Named, not omitted: the service routes on `purpose`, and a clip that
    // arrives without one used to be paced as if it were a board run.
    expect(sent.purpose).toBe("dictate");
  });

  it("ticks elapsed seconds from a real clock", async () => {
    vi.useFakeTimers();
    const { stream } = makeStream();
    getUserMedia.mockResolvedValue(stream);

    const { result } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());
    expect(result.current.elapsedS).toBe(0);

    act(() => vi.advanceTimersByTime(2100));
    expect(result.current.elapsedS).toBe(2);
  });

  it("stops itself at the recording cap", async () => {
    vi.useFakeTimers();
    const { stream, tracks } = makeStream();
    getUserMedia.mockResolvedValue(stream);
    mockTranscribe.mockResolvedValue({ text: "t", model: "m" });

    const { result } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());

    act(() => vi.advanceTimersByTime(60_000));
    expect(FakeMediaRecorder.instances[0].state).toBe("inactive");
    expect(tracks[0].stop).toHaveBeenCalled();
  });

  it("treats a denied permission as an ordinary error state", async () => {
    mockEnsureMic.mockRejectedValue(
      new Error(
        "Microphone permission was refused. Enable Hardy in System Settings → Privacy & Security → Microphone, then click the ear again."
      )
    );

    const { result } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());

    expect(result.current.status).toBe("error");
    expect(result.current.error).toContain("Microphone permission was refused");
    expect(mockTranscribe).not.toHaveBeenCalled();
  });

  it("surfaces a transcription failure with its real message", async () => {
    const { stream } = makeStream();
    getUserMedia.mockResolvedValue(stream);
    mockTranscribe.mockRejectedValue(
      new SilkscreenError("upstream", "Gemini answered 502.")
    );

    const { result } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());
    act(() => result.current.stop());

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).toBe("Gemini answered 502.");
    expect(result.current.transcript).toBe("");
  });

  it("cancel mid-recording releases the tracks and never transcribes", async () => {
    const { stream, tracks } = makeStream();
    getUserMedia.mockResolvedValue(stream);

    const { result } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());
    act(() => result.current.cancel());

    expect(result.current.status).toBe("idle");
    expect(tracks[0].stop).toHaveBeenCalled();
    // Flush any stray microtasks, then confirm nothing was sent.
    await act(async () => {});
    expect(mockTranscribe).not.toHaveBeenCalled();
  });

  it("cancel mid-transcription aborts and settles back to idle", async () => {
    const { stream } = makeStream();
    getUserMedia.mockResolvedValue(stream);
    let rejectCall: (error: unknown) => void = () => {};
    mockTranscribe.mockImplementation(
      (_base, _opts, signal) =>
        new Promise((_resolve, reject) => {
          rejectCall = reject;
          signal?.addEventListener("abort", () =>
            reject(new SilkscreenError("cancelled", "Transcription was cancelled."))
          );
        })
    );

    const { result } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());
    act(() => result.current.stop());
    expect(result.current.status).toBe("transcribing");

    act(() => result.current.cancel());
    await act(async () => {});
    expect(result.current.status).toBe("idle");
    expect(result.current.error).toBe("");
    void rejectCall;
  });

  it("unmount during recording releases the tracks", async () => {
    const { stream, tracks } = makeStream();
    getUserMedia.mockResolvedValue(stream);

    const { result, unmount } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());
    expect(result.current.status).toBe("recording");

    unmount();
    expect(tracks[0].stop).toHaveBeenCalled();
    await act(async () => {});
    expect(mockTranscribe).not.toHaveBeenCalled();
  });

  it("ignores a second start while already recording", async () => {
    const { stream } = makeStream();
    getUserMedia.mockResolvedValue(stream);

    const { result } = renderHook(() => useVoiceInput(options));
    await act(() => result.current.start());
    await act(() => result.current.start());

    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(FakeMediaRecorder.instances).toHaveLength(1);
  });
});

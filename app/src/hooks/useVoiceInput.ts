import { useCallback, useEffect, useRef, useState } from "react";
import { SilkscreenError } from "@/lib/silkscreen/client";
import {
  MAX_RECORDING_MS,
  pickRecordingMime,
  transcribe,
} from "@/lib/silkscreen/voice";
import { ensureCaptureAccess } from "@/lib/capture-permission";

export type VoiceStatus = "idle" | "recording" | "transcribing" | "error";

export interface UseVoiceInputOptions {
  baseUrl: string;
  token?: string;
  language?: string;
  /** Called once per successful transcription, with the reviewed-later text. */
  onTranscript?: (text: string) => void;
}

export interface VoiceInput {
  status: VoiceStatus;
  /** The last successful transcript. Informational; `onTranscript` is the delivery path. */
  transcript: string;
  /** Plain-language failure message; empty unless `status` is "error". */
  error: string;
  /** Whole seconds since recording started, from a real clock. */
  elapsedS: number;
  start: () => Promise<void>;
  /** Stop recording and transcribe what was captured. */
  stop: () => void;
  /** Throw the recording (or in-flight transcription) away. */
  cancel: () => void;
}

/**
 * The MediaRecorder lifecycle behind the mic button.
 *
 * The one invariant everything here bends around: the microphone track is
 * released the moment recording ends — before the network round trip, on
 * cancel, and on unmount — because an OS mic indicator that stays lit after
 * the user stopped talking reads as surveillance, not as a bug.
 */
export function useVoiceInput({
  baseUrl,
  token,
  language,
  onTranscript,
}: UseVoiceInputOptions): VoiceInput {
  const [status, setStatus] = useState<VoiceStatus>("idle");
  const [transcript, setTranscript] = useState("");
  const [error, setError] = useState("");
  const [elapsedS, setElapsedS] = useState(0);

  const statusRef = useRef<VoiceStatus>("idle");
  const mountedRef = useRef(true);
  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const discardRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const maxTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setStatusSafe = useCallback((next: VoiceStatus) => {
    statusRef.current = next;
    if (mountedRef.current) setStatus(next);
  }, []);

  /** Timers off, tracks stopped, refs cleared. Safe to call twice. */
  const releaseMedia = useCallback(() => {
    if (tickRef.current) {
      clearInterval(tickRef.current);
      tickRef.current = null;
    }
    if (maxTimerRef.current) {
      clearTimeout(maxTimerRef.current);
      maxTimerRef.current = null;
    }
    const stream = streamRef.current;
    streamRef.current = null;
    if (stream) {
      for (const track of stream.getTracks()) {
        track.stop();
        track.enabled = false;
      }
    }
  }, []);

  const finishRecording = useCallback(() => {
    const recorder = recorderRef.current;
    recorderRef.current = null;
    const mimeType = recorder?.mimeType || "audio/webm";
    const blob = new Blob(chunksRef.current, { type: mimeType });
    chunksRef.current = [];

    // Mic off before any network work — the light must not outlive the talking.
    releaseMedia();

    if (discardRef.current) {
      discardRef.current = false;
      if (statusRef.current !== "idle") setStatusSafe("idle");
      return;
    }
    if (!mountedRef.current) return;

    setStatusSafe("transcribing");
    const controller = new AbortController();
    controllerRef.current = controller;
    // `purpose` is what routes this clip to the service's voice pacer. Without
    // it the request used to land on the board-run pacer, where a dictation
    // that followed a generate waited out that run's whole RPM interval.
    transcribe(
      baseUrl,
      { blob, mimeType, language, token, purpose: "dictate" },
      controller.signal
    )
      .then((result) => {
        if (!mountedRef.current || controller.signal.aborted) return;
        setTranscript(result.text);
        setStatusSafe("idle");
        onTranscript?.(result.text);
      })
      .catch((err) => {
        if (!mountedRef.current || controller.signal.aborted) return;
        if (err instanceof SilkscreenError && err.kind === "cancelled") {
          setStatusSafe("idle");
          return;
        }
        setError((err as Error)?.message || "Transcription failed.");
        setStatusSafe("error");
      })
      .finally(() => {
        if (controllerRef.current === controller) controllerRef.current = null;
      });
  }, [baseUrl, language, token, onTranscript, releaseMedia, setStatusSafe]);

  const start = useCallback(async () => {
    if (statusRef.current === "recording" || statusRef.current === "transcribing")
      return;
    setError("");
    setStatusSafe("idle");

    let stream: MediaStream;
    try {
      await ensureCaptureAccess();
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("this environment has no audio capture");
      }
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      // No permission or no device is an ordinary state, not a crash.
      setError(
        `Could not open capture: ${(err as Error)?.message || "permission denied"}.`
      );
      setStatusSafe("error");
      return;
    }

    if (!mountedRef.current) {
      for (const track of stream.getTracks()) track.stop();
      return;
    }

    let recorder: MediaRecorder;
    try {
      const mimeType = pickRecordingMime();
      // 32 kbps keeps a full minute of opus near 240 KB, well under the
      // upload cap. A hint, not a guarantee — the size cap still backstops.
      recorder = new MediaRecorder(stream, {
        ...(mimeType ? { mimeType } : {}),
        audioBitsPerSecond: 32_000,
      });
    } catch (err) {
      for (const track of stream.getTracks()) track.stop();
      setError(
        `Could not start recording: ${(err as Error)?.message || "recorder unavailable"}.`
      );
      setStatusSafe("error");
      return;
    }

    streamRef.current = stream;
    recorderRef.current = recorder;
    chunksRef.current = [];
    discardRef.current = false;

    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onstop = finishRecording;
    recorder.start(250);

    const startedAt = Date.now();
    setElapsedS(0);
    tickRef.current = setInterval(() => {
      if (mountedRef.current) {
        setElapsedS(Math.floor((Date.now() - startedAt) / 1000));
      }
    }, 250);
    maxTimerRef.current = setTimeout(() => {
      if (recorderRef.current?.state === "recording") recorderRef.current.stop();
    }, MAX_RECORDING_MS);

    setStatusSafe("recording");
  }, [finishRecording, setStatusSafe]);

  const stop = useCallback(() => {
    if (statusRef.current !== "recording") return;
    const recorder = recorderRef.current;
    if (recorder && recorder.state === "recording") {
      recorder.stop(); // finishRecording runs via onstop
    } else {
      finishRecording();
    }
  }, [finishRecording]);

  const cancel = useCallback(() => {
    discardRef.current = true;
    const recorder = recorderRef.current;
    if (recorder && recorder.state === "recording") {
      recorder.stop(); // onstop sees the discard flag and throws the blob away
    } else {
      recorderRef.current = null;
      chunksRef.current = [];
      releaseMedia();
    }
    controllerRef.current?.abort();
    controllerRef.current = null;
    setError("");
    setStatusSafe("idle");
  }, [releaseMedia, setStatusSafe]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      discardRef.current = true;
      const recorder = recorderRef.current;
      if (recorder && recorder.state === "recording") {
        try {
          recorder.stop();
        } catch {
          // Already stopped; the release below still runs.
        }
      }
      recorderRef.current = null;
      controllerRef.current?.abort();
      controllerRef.current = null;
      releaseMedia();
    };
  }, [releaseMedia]);

  return { status, transcript, error, elapsedS, start, stop, cancel };
}

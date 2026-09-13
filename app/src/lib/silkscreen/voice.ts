// Voice transcription against the engine's `POST /transcribe`.
//
// Deliberately a sibling of `client.ts` rather than an addition to it: the
// run client is owned by another lane and transcription shares nothing with a
// run except the base URL, the bearer token, and the error taxonomy. The
// contract mirrors the run routes: JSON in, JSON out, errors in the service's
// `{error, detail?, error_id?}` shape with the same status meanings.
//
// Transcription is cheap and generation is not, so nothing in this module can
// start a run — it returns text for the user to review, and the caller puts
// that text in the intent draft, never in a request.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError, authHeaders, type ErrorKind } from "./client";

/** A minute of intent is plenty; the cap also bounds the upload size. */
export const MAX_RECORDING_MS = 60_000;

/**
 * The largest audio blob this client will send, in raw bytes.
 *
 * The service refuses request bodies over 1 MiB (1,048,576 bytes, service
 * `MAX_BODY_BYTES`), and base64 inflates by 4/3: `ceil(B/3) * 4` characters
 * for B bytes. At B = 700,000 the encoded audio is 933,336 bytes, leaving
 * ~115 KB of headroom for the JSON envelope — far more than the few dozen
 * bytes `{audio_b64, mime_type, language}` needs. A 60 s opus recording at
 * the 32 kbps we request is ~240 KB, so this trips only when a recorder
 * ignores the bitrate hint (WKWebView's AAC encoder can).
 */
export const MAX_AUDIO_BYTES = 700_000;

/** Transcription is one short model call; it does not get the run's 300 s. */
export const TRANSCRIBE_TIMEOUT_MS = 30_000;

/**
 * The recording container to ask MediaRecorder for, in preference order.
 *
 * Both of Tauri's webviews ship here — Chromium (Windows/Linux) records
 * webm/opus natively, WKWebView (macOS) records mp4/AAC — and Gemini's audio
 * understanding accepts ogg/opus and AAC directly, while webm is the format
 * it is least documented to take. So: ogg/opus first (Gemini-native, and
 * Chromium can often produce it), then mp4/AAC (Gemini-native, WKWebView's
 * only real option), then the webm/opus variants as the fallback Chromium
 * always satisfies. An empty string means "let the recorder pick".
 */
const PREFERRED_MIME_TYPES = [
  "audio/ogg;codecs=opus",
  "audio/mp4",
  "audio/webm;codecs=opus",
  "audio/webm",
];

export function pickRecordingMime(): string {
  const recorder = (globalThis as { MediaRecorder?: typeof MediaRecorder })
    .MediaRecorder;
  if (!recorder || typeof recorder.isTypeSupported !== "function") return "";
  for (const type of PREFERRED_MIME_TYPES) {
    if (recorder.isTypeSupported(type)) return type;
  }
  return "";
}

/** Base64 without the data-URL prefix, chunked so large blobs cannot blow the stack. */
export async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/** Why this clip was recorded — wake spotting must not share the board-run pacer. */
export type TranscribePurpose = "wake" | "dictate";

export interface TranscribeOptions {
  blob: Blob;
  mimeType: string;
  language?: string;
  token?: string;
  /** `wake` uses a separate server pacer so spotting cannot delay a board run. */
  purpose?: TranscribePurpose;
  /**
   * The loudest sample deviation (0–128) measured across this clip, when the
   * caller measured one. The service refuses a window whose peak never
   * crossed its speech threshold, so a silent clip costs no model call; an
   * unmeasured clip omits the field and the service behaves as before.
   */
  peak?: number;
}

export interface Transcription {
  text: string;
  model: string;
}

interface ErrorBody {
  error?: string;
  detail?: string;
  error_id?: string;
}

/** Same taxonomy as the run client; kept local because client.ts does not export its mapper. */
function kindForStatus(status: number, body: ErrorBody): ErrorKind {
  const text = `${body.error ?? ""} ${body.detail ?? ""}`;
  if (status === 502 || status === 503) {
    return /GOOGLE_API_KEY|api key/i.test(text) ? "setup" : "upstream";
  }
  if (status === 401) return "auth";
  if (status === 400 || status === 413) return "request";
  return "server";
}

function withTimeout(signal?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(TRANSCRIBE_TIMEOUT_MS);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

/**
 * Send one recording to `POST {baseUrl}/transcribe`, returning the transcript.
 *
 * Throws `SilkscreenError` for everything that goes wrong, with the same
 * `kind` values the run client uses so the UI's error language stays one
 * vocabulary. An over-cap blob is refused here, before any bytes move —
 * the service would 413 it anyway, and a local refusal names the real limit.
 */
export async function transcribe(
  baseUrl: string,
  { blob, mimeType, language, token, purpose, peak }: TranscribeOptions,
  signal?: AbortSignal
): Promise<Transcription> {
  if (blob.size === 0) {
    throw new SilkscreenError("request", "No audio was captured.");
  }
  if (blob.size > MAX_AUDIO_BYTES) {
    throw new SilkscreenError(
      "request",
      `The recording is too large to send (${blob.size} bytes; the limit is ${MAX_AUDIO_BYTES}). Try a shorter recording.`
    );
  }

  const audio_b64 = await blobToBase64(blob);
  const payload: Record<string, string | number> = { audio_b64, mime_type: mimeType };
  const trimmedLanguage = (language ?? "").trim();
  if (trimmedLanguage) payload.language = trimmedLanguage;
  if (purpose === "wake" || purpose === "dictate") payload.purpose = purpose;
  if (typeof peak === "number" && Number.isFinite(peak)) payload.peak = peak;

  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders(token) },
      body: JSON.stringify(payload),
      signal: withTimeout(signal),
    });
  } catch (error) {
    const name = (error as Error)?.name ?? "";
    if (name === "AbortError") {
      throw new SilkscreenError("cancelled", "Transcription was cancelled.");
    }
    if (name === "TimeoutError") {
      throw new SilkscreenError("timeout", "Transcription timed out.");
    }
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }

  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }

  if (!response.ok) {
    const err = body as ErrorBody;
    throw new SilkscreenError(
      kindForStatus(response.status, err),
      err.error || `The engine answered ${response.status}.`,
      {
        status: response.status,
        errorId: err.error_id ?? "",
        detail: err.detail ?? "",
      }
    );
  }

  const text = typeof body.text === "string" ? body.text : "";
  if (!text.trim()) {
    // A 200 with no words is still not a transcript the user can review.
    throw new SilkscreenError(
      "server",
      "The engine returned an empty transcript.",
      { status: response.status }
    );
  }
  return { text, model: typeof body.model === "string" ? body.model : "" };
}

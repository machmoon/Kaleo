// Desk deixis against the engine's `POST /desk/resolve`.
//
// Sibling of `voice.ts`: the run client is owned by another lane, and a
// pointing phrase must never start a board. The contract mirrors /transcribe:
// JSON in, JSON out, errors in the service's `{error, detail?, error_id?}`
// shape with the same status meanings. The PNG is the point of the call —
// throwing it away and claiming a screenshot in text is the bug this client
// exists to close.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError, authHeaders, type ErrorKind } from "./client";
import type { DeskCandidate } from "../desk-context";

/**
 * Largest PNG this client will send, in raw bytes.
 *
 * The desk route raises the shared 1 MiB body cap to 12 MiB
 * (`DESK_MAX_BODY_BYTES`). Base64 inflates by 4/3, and the JSON envelope
 * needs a few kilobytes for the utterance and candidates. 8 MiB of PNG
 * encodes to ~10.7 MiB, leaving headroom under 12 MiB.
 */
export const MAX_PNG_BYTES = 8_000_000;

/** One cheap vision call; it does not get the run's 300 s. */
export const DESK_TIMEOUT_MS = 30_000;

export interface DeskResolveOptions {
  png_base64: string;
  utterance: string;
  cursor_x: number;
  cursor_y: number;
  width?: number;
  height?: number;
  candidates?: DeskCandidate[];
  token?: string;
}

export interface DeskTarget {
  testid: string;
  attrs: Record<string, string>;
  tab?: string;
}

export interface DeskResolution {
  caption: string;
  abstain: boolean;
  target: DeskTarget | null;
  model: string;
}

interface ErrorBody {
  error?: string;
  detail?: string;
  error_id?: string;
}

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
  const timeout = AbortSignal.timeout(DESK_TIMEOUT_MS);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

function decodedPngBytes(b64: string): number {
  const trimmed = b64.trim();
  if (!trimmed) return 0;
  const padding = trimmed.endsWith("==") ? 2 : trimmed.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((trimmed.length * 3) / 4) - padding);
}

function readTarget(raw: unknown): DeskTarget | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  if (typeof obj.testid !== "string" || !obj.testid.trim()) return null;
  const attrs: Record<string, string> = {};
  if (obj.attrs && typeof obj.attrs === "object") {
    for (const [key, value] of Object.entries(obj.attrs as Record<string, unknown>)) {
      if (typeof value === "string") attrs[key] = value;
    }
  }
  const tab = typeof obj.tab === "string" && obj.tab.trim() ? obj.tab.trim() : undefined;
  return { testid: obj.testid.trim(), attrs, ...(tab ? { tab } : {}) };
}

/**
 * Send one desk snap to `POST {baseUrl}/desk/resolve`.
 *
 * Throws `SilkscreenError` with the same `kind` values the run client uses.
 * An over-cap PNG is refused here, before any bytes move.
 */
export async function resolveDesk(
  baseUrl: string,
  options: DeskResolveOptions,
  signal?: AbortSignal
): Promise<DeskResolution> {
  const png_b64 = options.png_base64.trim();
  if (!png_b64) {
    throw new SilkscreenError("request", "No screenshot was captured.");
  }
  const rawBytes = decodedPngBytes(png_b64);
  if (rawBytes > MAX_PNG_BYTES) {
    throw new SilkscreenError(
      "request",
      `The screenshot is too large to send (${rawBytes} bytes; the limit is ${MAX_PNG_BYTES}).`
    );
  }

  const payload: Record<string, unknown> = {
    png_b64,
    utterance: options.utterance,
    cursor_x: options.cursor_x,
    cursor_y: options.cursor_y,
  };
  if (options.width) payload.width = options.width;
  if (options.height) payload.height = options.height;
  if (options.candidates?.length) payload.candidates = options.candidates;

  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/desk/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders(options.token) },
      body: JSON.stringify(payload),
      signal: withTimeout(signal),
    });
  } catch (error) {
    const name = (error as Error)?.name ?? "";
    if (name === "AbortError") {
      throw new SilkscreenError("cancelled", "Desk resolve was cancelled.");
    }
    if (name === "TimeoutError") {
      throw new SilkscreenError("timeout", "Desk resolve timed out.");
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

  const caption = typeof body.caption === "string" ? body.caption : "";
  const abstain = body.abstain === true;
  return {
    caption,
    abstain,
    target: abstain ? null : readTarget(body.target),
    model: typeof body.model === "string" ? body.model : "",
  };
}

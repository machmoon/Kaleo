// The engine step's second question: the engine answered `/healthz`, but
// does it have a Gemini key? `GET /config/status` (bearer) says so in its
// `gemini` feature row. Everything else on that route is the Engine page's
// business; the wizard reads one row and takes the service's own sentence.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { authHeaders } from "@/lib/silkscreen/client";
import { SETUP_TIMEOUT_MS } from "./service";

export type KeyState = "ready" | "warning" | "error" | "restart" | "unknown";

export interface KeyStatus {
  state: KeyState;
  /** The service's summary, verbatim. */
  summary: string;
}

/** Continue is blocked only when the service said `error` or `restart`. */
export function keyAllowsContinue(status: KeyStatus | null): boolean {
  if (!status) return true;
  return status.state !== "error" && status.state !== "restart";
}

export async function fetchKeyStatus(baseUrl: string, token: string): Promise<KeyStatus> {
  const route = "/config/status";
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}${route}`, {
      method: "GET",
      headers: authHeaders(token),
      signal: AbortSignal.timeout(SETUP_TIMEOUT_MS),
    });
  } catch {
    return { state: "unknown", summary: "The engine did not answer /config/status." };
  }
  console.info("[kaleo setup]", { route, status: response.status });
  if (response.status === 401) {
    return { state: "unknown", summary: "The engine refused the token for /config/status." };
  }
  if (!response.ok) {
    return { state: "unknown", summary: `The engine answered ${response.status} for /config/status.` };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { state: "unknown", summary: "The engine's /config/status answer is not JSON." };
  }
  const features =
    typeof body === "object" && body !== null && Array.isArray((body as { features?: unknown }).features)
      ? ((body as { features: unknown[] }).features as unknown[])
      : [];
  for (const raw of features) {
    if (typeof raw !== "object" || raw === null) continue;
    const feature = raw as { id?: unknown; state?: unknown; summary?: unknown };
    if (feature.id !== "gemini") continue;
    const state = feature.state;
    const summary = typeof feature.summary === "string" ? feature.summary : "";
    if (state === "ready" || state === "warning" || state === "error" || state === "restart") {
      return { state, summary };
    }
    return { state: "unknown", summary: summary || "The engine reported an unknown key state." };
  }
  return { state: "unknown", summary: "The engine's /config/status names no Gemini row." };
}

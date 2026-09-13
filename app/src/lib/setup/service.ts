// The client half of the engine's `/setup` family (docs/setup.md, contract C2).
//
// Same shape as `silkscreen/integrations.ts`: Tauri's fetch (the service ships
// no CORS headers on purpose), the bearer on every call, a 15 s ceiling — a
// setup probe is fast or broken — and `SilkscreenError` kinds so the wizard
// can tell "the engine is not running" from "the engine refused".
//
// Two rules are load-bearing here rather than in the UI:
//   - logging names `{route, status}` only. A body from this family carries
//     an OAuth client id, a tenant id, a masked key preview — none of it is a
//     log line;
//   - `consentUrlAllowed` decides whether an `auth_url` may be opened at all.
//     The browser is handed a URL the engine chose; the only two hosts that
//     have any business receiving a sign-in are Google's own, or the engine
//     itself when it is playing Google in demo mode.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError, authHeaders } from "@/lib/silkscreen/client";

export const SETUP_TIMEOUT_MS = 15_000;

export type SetupMode = "live" | "demo";
export type SetupItemState = "ready" | "partial" | "unconfigured" | "unavailable";
export type GoogleJobState = "idle" | "waiting" | "connected" | "failed";

export interface GoogleJob {
  state: GoogleJobState;
  error: string | null;
}

export interface EngineSetup {
  state: SetupItemState;
  detail: string;
  demo: boolean;
}

export interface GoogleSetup {
  state: SetupItemState;
  detail: string;
  demo: boolean;
  oauth_client: boolean;
  signed_in: boolean;
  token: boolean;
  job: GoogleJob;
}

export interface MicrosoftField {
  key: string;
  set: boolean;
  shown: string;
}

export interface MicrosoftSetup {
  state: SetupItemState;
  detail: string;
  demo: boolean;
  fields: MicrosoftField[];
  verified_at: string | null;
  /** The honesty string, rendered verbatim under the card. */
  meaning: string;
  unverified: boolean;
}

export interface StripeStep {
  key: string;
  present: boolean;
  preview: string | null;
  warning: string | null;
}

export interface StripeSetup {
  state: SetupItemState;
  detail: string;
  demo: boolean;
  ready: boolean;
  key_mode: string;
  steps: StripeStep[];
}

export interface SetupReport {
  schema_version: number;
  mode: SetupMode;
  home: string;
  demo_home: string;
  engine: EngineSetup;
  google: GoogleSetup;
  microsoft: MicrosoftSetup;
  stripe: StripeSetup;
}

export interface GoogleConnectResponse {
  auth_url: string;
  job: GoogleJob;
  demo: boolean;
}

export interface GooglePollResponse {
  state: SetupItemState;
  job: GoogleJob;
  signed_in: boolean;
  token: boolean;
  token_path: string;
  demo: boolean;
}

export interface DisconnectResponse {
  disconnected: boolean;
  removed?: string[];
  /** Keys the running engine still carries from its environment. */
  still_configured_from_environment?: string[];
  /** The sign-out sentence, when the engine sends one. */
  note?: string;
}

export interface MicrosoftVerdict {
  ok: boolean;
  status: number;
  code?: string;
  meaning?: string;
  expires_in?: number;
}

export interface MicrosoftSaveResponse {
  saved: boolean;
  verdict: MicrosoftVerdict;
  meaning: string;
  active?: boolean;
  active_source?: "file" | "environment";
  note?: string;
  demo: boolean;
}

export interface GoogleCredentialsResponse {
  saved: boolean;
  verified: boolean;
  note: string;
  demo: boolean;
}

export type SetupProvider = "google" | "microsoft";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `{route, status}` and nothing else. Bodies here carry identifiers. */
function log(route: string, status: number): void {
  console.info("[kaleo setup]", { route, status });
}

async function call(
  baseUrl: string,
  token: string,
  route: string,
  init: { method: "GET" | "POST"; body?: unknown } = { method: "GET" },
): Promise<{ status: number; body: unknown }> {
  const timeout = AbortSignal.timeout(SETUP_TIMEOUT_MS);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}${route}`, {
      method: init.method,
      headers:
        init.method === "POST"
          ? { "Content-Type": "application/json", ...authHeaders(token) }
          : authHeaders(token),
      ...(init.method === "POST" ? { body: JSON.stringify(init.body ?? {}) } : {}),
      signal: timeout,
    });
  } catch (error) {
    if (timeout.aborted) {
      throw new SilkscreenError(
        "timeout",
        `The engine did not answer ${route} within ${Math.round(SETUP_TIMEOUT_MS / 1000)} seconds.`,
      );
    }
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }
  log(route, response.status);
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (response.status === 404) {
    throw new SilkscreenError("request", `This engine has no ${route} route; update the service.`, {
      status: 404,
    });
  }
  if (response.status === 401) {
    throw new SilkscreenError("auth", "The engine refused the token.", { status: 401 });
  }
  return { status: response.status, body };
}

/** A non-2xx with a JSON `error` becomes that sentence; otherwise a generic one. */
function refused(route: string, status: number, body: unknown): SilkscreenError {
  const message =
    isRecord(body) && typeof body.error === "string" && body.error
      ? body.error
      : `The engine answered ${status} for ${route}.`;
  return new SilkscreenError(status >= 500 ? "server" : "request", message, {
    status,
    detail: isRecord(body) && typeof body.detail === "string" ? body.detail : "",
  });
}

function expectObject(route: string, status: number, body: unknown): Record<string, unknown> {
  if (!isRecord(body)) {
    throw new SilkscreenError("server", `The engine's ${route} answer is not readable.`, {
      status,
    });
  }
  return body;
}

export async function fetchSetup(baseUrl: string, token: string): Promise<SetupReport> {
  const route = "/setup";
  const { status, body } = await call(baseUrl, token, route);
  if (status < 200 || status >= 300) throw refused(route, status, body);
  const report = expectObject(route, status, body);
  if (report.mode !== "live" && report.mode !== "demo") {
    throw new SilkscreenError("server", "The engine's /setup answer names no mode.", { status });
  }
  for (const key of ["engine", "google", "microsoft", "stripe"]) {
    if (!isRecord(report[key])) {
      throw new SilkscreenError("server", `The engine's /setup answer has no ${key} entry.`, {
        status,
      });
    }
  }
  return report as unknown as SetupReport;
}

export async function googleConnect(baseUrl: string, token: string): Promise<GoogleConnectResponse> {
  const route = "/setup/google/connect";
  const { status, body } = await call(baseUrl, token, route, { method: "POST", body: {} });
  if (status === 409) {
    throw new SilkscreenError("request", "A Google sign-in is already waiting in the browser.", {
      status,
    });
  }
  if (status < 200 || status >= 300) throw refused(route, status, body);
  const out = expectObject(route, status, body);
  if (typeof out.auth_url !== "string") {
    throw new SilkscreenError("server", "The engine started a sign-in but sent no URL.", { status });
  }
  return out as unknown as GoogleConnectResponse;
}

export async function googlePoll(baseUrl: string, token: string): Promise<GooglePollResponse> {
  const route = "/setup/google";
  const { status, body } = await call(baseUrl, token, route);
  if (status < 200 || status >= 300) throw refused(route, status, body);
  return expectObject(route, status, body) as unknown as GooglePollResponse;
}

export async function googleSaveCredentials(
  baseUrl: string,
  token: string,
  credentials: { client_id: string; client_secret: string },
): Promise<GoogleCredentialsResponse> {
  const route = "/setup/google/credentials";
  const { status, body } = await call(baseUrl, token, route, { method: "POST", body: credentials });
  if (status < 200 || status >= 300) throw refused(route, status, body);
  return expectObject(route, status, body) as unknown as GoogleCredentialsResponse;
}

export async function microsoftStatus(baseUrl: string, token: string): Promise<MicrosoftSetup> {
  const route = "/setup/microsoft";
  const { status, body } = await call(baseUrl, token, route);
  if (status < 200 || status >= 300) throw refused(route, status, body);
  return expectObject(route, status, body) as unknown as MicrosoftSetup;
}

/**
 * Save (or only verify) a Microsoft app registration.
 *
 * A 400 here is an answer, not an outage: the engine verified the ids against
 * Entra and Entra said no. It comes back as `saved: false` with the verdict
 * so the card can show the fixed-vocabulary `meaning`, rather than as a thrown
 * error the card would render as "engine refused".
 */
export async function microsoftSave(
  baseUrl: string,
  token: string,
  credentials: { app_id: string; app_secret: string; tenant_id: string; verify_only?: boolean },
): Promise<MicrosoftSaveResponse> {
  const route = "/setup/microsoft/credentials";
  const { status, body } = await call(baseUrl, token, route, { method: "POST", body: credentials });
  if (status === 400 && isRecord(body) && isRecord(body.verdict)) {
    return { saved: false, demo: body.demo === true, meaning: "", ...body } as MicrosoftSaveResponse;
  }
  if (status < 200 || status >= 300) throw refused(route, status, body);
  return expectObject(route, status, body) as unknown as MicrosoftSaveResponse;
}

export async function disconnect(
  baseUrl: string,
  token: string,
  provider: SetupProvider,
): Promise<DisconnectResponse> {
  const route = `/setup/${provider}/disconnect`;
  const { status, body } = await call(baseUrl, token, route, { method: "POST", body: {} });
  if (status < 200 || status >= 300) throw refused(route, status, body);
  return expectObject(route, status, body) as unknown as DisconnectResponse;
}

/**
 * May this consent URL be opened in the browser?
 *
 * Live: only `accounts.google.com` over https. Demo: only the engine's own
 * origin, which is where the demo consent page lives. Anything else — an
 * engine that has been tampered with, a proxy in the middle, a bug — is
 * refused with the sentence the wizard shows, because the browser tab that
 * opens next is where the user types a password.
 */
export function consentUrlAllowed(
  authUrl: string,
  engineBaseUrl: string,
  mode: SetupMode,
): { ok: true } | { ok: false; reason: string } {
  const reason = "the engine returned a consent URL on an unexpected host";
  let url: URL;
  try {
    url = new URL(authUrl);
  } catch {
    return { ok: false, reason };
  }
  if (mode === "live") {
    return url.protocol === "https:" && url.hostname === "accounts.google.com"
      ? { ok: true }
      : { ok: false, reason };
  }
  let engine: URL;
  try {
    engine = new URL(engineBaseUrl);
  } catch {
    return { ok: false, reason };
  }
  return url.origin === engine.origin ? { ok: true } : { ok: false, reason };
}

/** The sentence a card shows when the engine cannot be asked at all. */
export const ENGINE_DOWN_SENTENCE = "Engine not running, connect later in Settings.";

/** The persistent banner on every wizard step while the engine is in demo mode. */
export const DEMO_BANNER =
  "Demo mode. Nothing here reaches Google, Microsoft or Stripe; Integrations will still show them as unconfigured.";

/** The done screen, when every connection made was a demo one. */
export const ALL_DEMO_SENTENCE =
  "Nothing was actually connected. Google, Microsoft and Billing were demo sign-ins. Restart the engine without KALEO_SETUP_MODE=demo to connect for real.";

/** The sentence a save response earns when the environment still wins. */
export function environmentWinsSentence(
  response: { active?: boolean; active_source?: string } | null | undefined,
): string {
  if (!response) return "";
  if (response.active === false || response.active_source === "environment") {
    return "Saved and verified, but the running engine still uses the value from its environment; restart it or remove that variable.";
  }
  return "";
}

/** After a disconnect: which keys the engine still carries, in a sentence. */
export function stillConfiguredSentence(response: DisconnectResponse): string {
  const keys = response.still_configured_from_environment ?? [];
  if (!keys.length) return "";
  return `The running engine still has ${keys.join(", ")} from its environment; restart it or remove ${
    keys.length === 1 ? "that variable" : "those variables"
  }.`;
}

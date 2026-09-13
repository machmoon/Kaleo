// What the engine can talk to, and whether it is actually set up.
//
// `GET /integrations` is read-only, always 200, and carries no secrets — only
// whether a setting is set, plus the hints that name the fix. This module is
// the client half of that contract (`docs/integrations-plan.md`, Contract 2):
// it fetches, it *validates*, and it turns the roster into the two pure
// answers the page draws with (`groupByKind`, `badgeFor`).
//
// The validation is the load-bearing part, not the fetch. An integrations page
// that renders an empty list reads as "nothing is configured here" — a
// sentence about the user's machine. If the engine answered garbage, the true
// sentence is "the engine answered garbage", which is a sentence about the
// engine. Those must never be confused, so every malformed response is a
// `SilkscreenError` naming what was wrong and there is no path that quietly
// returns a short list. Failures are batched the way `netlist.py` batches
// them: every bad entry is named in one error, not just the first.
//
// Unknown *ids* pass through untouched. The roster grows (a new front end, a
// new fabricator) and a desktop build older than the engine must show the new
// row rather than hide it. Unknown *states* and *kinds* do not pass: they are
// the vocabulary the page switches on, and a state it cannot read is a state
// it cannot report honestly.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError, authHeaders } from "./client";

/** This client's own ceiling for the integrations calls: a read-only aggregate is fast or broken. */
export const INTEGRATIONS_TIMEOUT_MS = 15_000;

export type IntegrationState = "ready" | "partial" | "unconfigured" | "unavailable";
export type IntegrationKind = "delivery" | "meeting" | "design" | "fabrication";

export interface IntegrationSetting {
  key: string;
  required: boolean;
  set: boolean;
  shown: string;
  note: string;
}

export interface IntegrationAction {
  id: string;
  label: string;
  method: "GET" | "POST";
  path: string;
}

export interface Integration {
  id: string;
  name: string;
  kind: IntegrationKind;
  state: IntegrationState;
  summary: string;
  detail: string;
  settings: IntegrationSetting[];
  hints: string[];
  actions: IntegrationAction[];
  docs: string;
  unverified: boolean;
}

/**
 * The order kinds are shown in, and it is a claim about attention: what a
 * finished run goes out through, then what starts a run, then what shapes the
 * design, then what builds it.
 */
export const KIND_ORDER: readonly IntegrationKind[] = [
  "delivery",
  "meeting",
  "design",
  "fabrication",
];

const STATES: readonly IntegrationState[] = [
  "ready",
  "partial",
  "unconfigured",
  "unavailable",
];

const METHODS: readonly string[] = ["GET", "POST"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function quote(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  if (value === undefined) return "missing";
  if (value === null) return "null";
  if (Array.isArray(value)) return "an array";
  if (isRecord(value)) return "an object";
  return String(value);
}

/**
 * One malformed response is one error naming every problem in it.
 *
 * Reported as `server` because that is exactly what it is: the engine is
 * reachable, answered 200, and sent something this client cannot read. Telling
 * the user to check their network or their key would send them to fix the
 * wrong thing.
 */
function malformed(problems: readonly string[]): SilkscreenError {
  return new SilkscreenError(
    "server",
    `The engine's integrations list is not readable: ${problems.length} problem${
      problems.length === 1 ? "" : "s"
    }.`,
    { detail: problems.join("; ") }
  );
}

function str(
  entry: Record<string, unknown>,
  key: string,
  where: string,
  problems: string[]
): string {
  const value = entry[key];
  if (typeof value !== "string") {
    problems.push(`${where}: ${key} is not a string (${quote(value)})`);
    return "";
  }
  return value;
}

function settingsOf(
  value: unknown,
  where: string,
  problems: string[]
): IntegrationSetting[] {
  if (!Array.isArray(value)) {
    problems.push(`${where}: settings is not an array (${quote(value)})`);
    return [];
  }
  const out: IntegrationSetting[] = [];
  value.forEach((raw, index) => {
    const at = `${where}.settings[${index}]`;
    if (!isRecord(raw)) {
      problems.push(`${at}: not an object (${quote(raw)})`);
      return;
    }
    out.push({
      key: str(raw, "key", at, problems),
      required: raw.required === true,
      set: raw.set === true,
      shown: typeof raw.shown === "string" ? raw.shown : "",
      note: typeof raw.note === "string" ? raw.note : "",
    });
  });
  return out;
}

function hintsOf(value: unknown, where: string, problems: string[]): string[] {
  if (!Array.isArray(value)) {
    problems.push(`${where}: hints is not an array (${quote(value)})`);
    return [];
  }
  const out: string[] = [];
  value.forEach((raw, index) => {
    if (typeof raw !== "string") {
      problems.push(`${where}.hints[${index}]: not a string (${quote(raw)})`);
      return;
    }
    out.push(raw);
  });
  return out;
}

function actionsOf(
  value: unknown,
  where: string,
  problems: string[]
): IntegrationAction[] {
  if (!Array.isArray(value)) {
    problems.push(`${where}: actions is not an array (${quote(value)})`);
    return [];
  }
  const out: IntegrationAction[] = [];
  value.forEach((raw, index) => {
    const at = `${where}.actions[${index}]`;
    if (!isRecord(raw)) {
      problems.push(`${at}: not an object (${quote(raw)})`);
      return;
    }
    const method = raw.method;
    if (typeof method !== "string" || !METHODS.includes(method)) {
      // An action is a button that sends a request. A method this client does
      // not understand is not a button it may draw.
      problems.push(`${at}: unknown method ${quote(method)} (expected GET or POST)`);
      return;
    }
    const path = str(raw, "path", at, problems);
    out.push({
      id: str(raw, "id", at, problems),
      label: str(raw, "label", at, problems),
      method: method as "GET" | "POST",
      path,
    });
  });
  return out;
}

/**
 * Validate one entry. Unknown `id`s are fine — the roster grows — but an
 * unknown `kind` or `state` is not: those are the words the page renders and
 * groups by, and guessing at one would put a row in the wrong section or under
 * the wrong badge.
 */
function integrationOf(
  raw: unknown,
  index: number,
  problems: string[]
): Integration | null {
  const where = `integrations[${index}]`;
  if (!isRecord(raw)) {
    problems.push(`${where}: not an object (${quote(raw)})`);
    return null;
  }
  const id = typeof raw.id === "string" && raw.id ? raw.id : "";
  if (!id) problems.push(`${where}: id is missing or not a string (${quote(raw.id)})`);
  const at = id ? `${where} (${id})` : where;

  const kind = raw.kind;
  const state = raw.state;
  let ok = Boolean(id);
  if (typeof kind !== "string" || !KIND_ORDER.includes(kind as IntegrationKind)) {
    problems.push(
      `${at}: unknown kind ${quote(kind)} (expected ${KIND_ORDER.join(", ")})`
    );
    ok = false;
  }
  if (typeof state !== "string" || !STATES.includes(state as IntegrationState)) {
    problems.push(
      `${at}: unknown state ${quote(state)} (expected ${STATES.join(", ")})`
    );
    ok = false;
  }

  const entry: Integration = {
    id,
    name: typeof raw.name === "string" && raw.name ? raw.name : id,
    kind: kind as IntegrationKind,
    state: state as IntegrationState,
    summary: typeof raw.summary === "string" ? raw.summary : "",
    detail: typeof raw.detail === "string" ? raw.detail : "",
    settings: settingsOf(raw.settings, at, problems),
    hints: hintsOf(raw.hints, at, problems),
    actions: actionsOf(raw.actions, at, problems),
    docs: typeof raw.docs === "string" ? raw.docs : "",
    unverified: raw.unverified === true,
  };
  return ok ? entry : null;
}

/**
 * Parse a whole `/integrations` body.
 *
 * Exported because the validation is worth testing without a network, and
 * because a caller holding a body from somewhere else (a saved session, a
 * fixture) gets the same guarantee: either a fully-readable list, or a thrown
 * `SilkscreenError` naming everything wrong with it.
 */
export function parseIntegrations(body: unknown): Integration[] {
  if (!isRecord(body)) {
    throw malformed([`the response body is not an object (${quote(body)})`]);
  }
  const list = body.integrations;
  if (list === undefined) {
    throw malformed(["the response has no `integrations` key"]);
  }
  if (!Array.isArray(list)) {
    throw malformed([`\`integrations\` is not an array (${quote(list)})`]);
  }
  const problems: string[] = [];
  const out: Integration[] = [];
  list.forEach((raw, index) => {
    const entry = integrationOf(raw, index, problems);
    if (entry) out.push(entry);
  });
  // Any problem at all fails the whole response. A partly-parsed roster is
  // the exact failure this module exists to prevent: the missing row would
  // read as an integration that does not exist.
  if (problems.length) throw malformed(problems);
  return out;
}

/**
 * `GET /integrations`.
 *
 * Throws on a bad response and never returns a partial list. A 404 is not
 * silently an empty page either — an engine too old to have the route is a
 * fact worth stating, not an integrations screen that says nothing is
 * installed.
 */
export async function fetchIntegrations(
  baseUrl: string,
  token: string,
  signal?: AbortSignal
): Promise<Integration[]> {
  const timeout = AbortSignal.timeout(INTEGRATIONS_TIMEOUT_MS);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/integrations`, {
      method: "GET",
      headers: authHeaders(token),
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
  } catch (error) {
    if (timeout.aborted && !signal?.aborted) {
      throw new SilkscreenError(
        "timeout",
        `The engine did not answer /integrations within ${Math.round(
          INTEGRATIONS_TIMEOUT_MS / 1000
        )} seconds.`
      );
    }
    if (signal?.aborted) {
      throw new SilkscreenError("cancelled", "The integrations request was cancelled.");
    }
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }

  if (response.status === 404) {
    throw new SilkscreenError(
      "request",
      "This engine has no /integrations route; update the service.",
      { status: 404 }
    );
  }
  if (response.status === 401) {
    throw new SilkscreenError("auth", "The engine refused the token.", { status: 401 });
  }
  if (!response.ok) {
    throw new SilkscreenError(
      "server",
      `The engine answered ${response.status} for /integrations.`,
      { status: response.status }
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    throw malformed([`the response body is not JSON (${(error as Error)?.message ?? ""})`]);
  }
  return parseIntegrations(body);
}

/**
 * Run one of an integration's own actions.
 *
 * The action came from the engine's own response, so the path is the engine's
 * to choose; this only sends it. Nothing here retries — an action is a real
 * request (a sign-in, a probe) and a retry is a second one.
 */
export async function runIntegrationAction(
  baseUrl: string,
  token: string,
  action: IntegrationAction
): Promise<unknown> {
  const timeout = AbortSignal.timeout(INTEGRATIONS_TIMEOUT_MS);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}${action.path}`, {
      method: action.method,
      headers:
        action.method === "POST"
          ? { "Content-Type": "application/json", ...authHeaders(token) }
          : authHeaders(token),
      ...(action.method === "POST" ? { body: "{}" } : {}),
      signal: timeout,
    });
  } catch (error) {
    if (timeout.aborted) {
      throw new SilkscreenError(
        "timeout",
        `${action.label} did not finish within ${Math.round(
          INTEGRATIONS_TIMEOUT_MS / 1000
        )} seconds.`
      );
    }
    throw new SilkscreenError("offline", "Could not reach the silkscreen engine.", {
      detail: (error as Error)?.message ?? "",
    });
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const message =
      isRecord(body) && typeof body.error === "string" && body.error
        ? body.error
        : `${action.label} was refused (${response.status}).`;
    throw new SilkscreenError(
      response.status === 401 ? "auth" : response.status < 500 ? "request" : "server",
      message,
      {
        status: response.status,
        errorId: isRecord(body) && typeof body.error_id === "string" ? body.error_id : "",
        detail: isRecord(body) && typeof body.detail === "string" ? body.detail : "",
      }
    );
  }
  return body;
}

/**
 * Group by kind in a fixed order, keeping the engine's order within each kind.
 *
 * Both halves are deliberate. The kind order is fixed so the page does not
 * reshuffle between polls, and server order is preserved inside a kind so the
 * engine stays the one authority on which integration leads its section — a
 * client-side sort (alphabetical, or by state) would silently overrule it and
 * make two builds disagree about the same roster. A kind nothing landed in is
 * omitted rather than rendered as an empty heading.
 */
export function groupByKind(
  list: readonly Integration[]
): { kind: IntegrationKind; items: Integration[] }[] {
  const groups: { kind: IntegrationKind; items: Integration[] }[] = [];
  for (const kind of KIND_ORDER) {
    const items = list.filter((item) => item.kind === kind);
    if (items.length) groups.push({ kind, items });
  }
  return groups;
}

/**
 * The card's status word.
 *
 * `unavailable` and `unconfigured` are different sentences to a user and must
 * never collapse into one grey pill: "Not installed" is a missing package the
 * user cannot fix from the settings screen, "Not set up" is a credential they
 * can go and set right now. `partial` is the one that earns a warn tone — it
 * is the state that looks like it works until the first real call.
 */
export function badgeFor(item: Integration): {
  label: string;
  tone: "ok" | "warn" | "off";
} {
  switch (item.state) {
    case "ready":
      // `unverified` is stated, not hidden: configuration is complete, but
      // this integration has never run against a live account here.
      return item.unverified
        ? { label: "Ready, unverified", tone: "warn" }
        : { label: "Ready", tone: "ok" };
    case "partial":
      return { label: "Partly set up", tone: "warn" };
    case "unconfigured":
      return { label: "Not set up", tone: "off" };
    case "unavailable":
      return { label: "Not installed", tone: "off" };
  }
}

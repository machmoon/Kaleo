// What the strip says when it cannot do the thing it was asked to do.
//
// Two failures belong to the shell rather than to any panel, because neither
// one is about a board: the engine is not running, and the engine is running
// without an API key. Both are five-second fixes that the user can only make
// outside this app, so both banners carry the literal line to paste into a
// terminal. Neither invents a number — the address comes from the configured
// base URL, and "last seen" is measured from the last probe that actually
// answered, or omitted when there has never been one.
//
// Deliberately dependency-free: it takes the shape of a `SilkscreenError`
// structurally rather than importing the client, so it stays a pure module
// that tests without a Tauri mock.

/** The failing call, as much of it as this module needs. */
export interface FailureLike {
  /** `SilkscreenError.kind`. */
  kind: string;
  /** The engine's own sentence, when it had one. */
  message?: string;
  /** The transport's own words. */
  detail?: string;
}

export interface Notice {
  /** Which failure this is. Drives the test id's `data-kind` and nothing else. */
  kind: "offline" | "setup";
  /** First person, one sentence, no exclamation mark. */
  title: string;
  /** The measured part: an address, an interval, the transport's own words. */
  detail: string | null;
  /** The literal command that fixes it, or null when there is not one. */
  command: string | null;
  /** The label on the button that tries again, or null when retrying is wrong. */
  retryLabel: string | null;
}

/** `m:ss` for a duration in ms. Never negative, never NaN. */
export function formatAgo(ms: number): string {
  const n = Number(ms);
  const total = Number.isFinite(n) && n > 0 ? Math.floor(n / 1000) : 0;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/**
 * The port the engine was configured on, so the command names the port the
 * user actually needs rather than the one in the docs.
 */
export function enginePort(baseUrl: string): string {
  const match = /:(\d{2,5})(?:\/|$)/.exec(String(baseUrl ?? ""));
  return match ? match[1] : "8081";
}

/** The command that starts the engine on the port this app is looking at. */
export function startCommand(baseUrl: string): string {
  return `PORT=${enginePort(baseUrl)} python -m service.app`;
}

/**
 * Nothing is listening at the configured address.
 *
 * `lastOkAt` is the last probe that answered; null means this app has never
 * reached the engine, which is a different sentence and must not be dressed
 * up as an outage that just started.
 */
export function engineDownNotice(
  baseUrl: string,
  lastOkAt: number | null,
  now: number = Date.now(),
  transportDetail = ""
): Notice {
  const seen =
    lastOkAt === null
      ? "I have not reached it since this window opened."
      : `Last seen ${formatAgo(now - lastOkAt)} ago.`;
  const said = transportDetail.trim() ? ` It said: ${transportDetail.trim()}.` : "";
  return {
    kind: "offline",
    title: `I cannot reach the engine at ${baseUrl}.`,
    detail: `${seen}${said} Nothing on this strip can run until it answers.`,
    command: startCommand(baseUrl),
    retryLabel: "Retry",
  };
}

/** The engine answered, but it has no key, so it could not call the model. */
export function missingKeyNotice(baseUrl: string, said = ""): Notice {
  const quoted = said.trim() ? ` It said: ${said.trim()}.` : "";
  return {
    kind: "setup",
    title: "The engine is running, but it has no API key, so it could not call the model.",
    detail: `${quoted} Nothing was spent on this attempt.`.trim(),
    command: `export GOOGLE_API_KEY=… && PORT=${enginePort(baseUrl)} python -m service.app`,
    retryLabel: null,
  };
}

/**
 * The banner for a failed call, or null when this failure is not the shell's
 * to explain.
 *
 * Only the two setup failures are handled here. Everything else — a refused
 * prompt, a 500 with an error id, a timeout — is about the run, and the panel
 * that owns the run states it with its own evidence. A shell banner that also
 * claimed those would say the same thing twice, in fewer words.
 */
export function callFailureNotice(
  error: FailureLike | null | undefined,
  baseUrl: string,
  lastOkAt: number | null = null,
  now: number = Date.now()
): Notice | null {
  if (!error) return null;
  if (error.kind === "setup") {
    return missingKeyNotice(baseUrl, error.message ?? "");
  }
  if (error.kind === "offline") {
    return engineDownNotice(baseUrl, lastOkAt, now, error.detail ?? "");
  }
  return null;
}

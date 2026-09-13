import { useState } from "react";
import { CheckCircle2Icon, Loader2Icon, ShieldAlertIcon, XCircleIcon } from "lucide-react";
import { Button, Header, Input, Label } from "@/components";
import { safeLocalStorage } from "@/lib";
import { cn } from "@/lib/utils";
import { DEFAULT_BASE_URL, health } from "@/lib/silkscreen/client";
import { LOOPBACK_HOSTS, KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";

export interface BaseUrlCheck {
  ok: boolean;
  /** The URL to actually store, trailing slash removed. Empty when invalid. */
  url: string;
  /** Why it was refused, phrased for the user. Empty when ok. */
  reason: string;
}

/**
 * Accept only a loopback `http://` origin.
 *
 * This is a refusal, not a warning, and the reason is worth stating plainly:
 * the silkscreen service ships **no authentication and no CORS headers**, by
 * design — it expects to be reachable only from the machine it runs on. A base
 * URL pointing at anything else means Kaleo is handing an unauthenticated
 * `/generate` endpoint — one that spends the user's Gemini quota on every call
 * — to whatever else can route to that address.
 *
 * `https` is accepted for any host: a deployed engine sits behind the
 * SILKSCREEN_ACCESS_TOKEN bearer gate and TLS is what keeps that token off the
 * wire. The Tauri `http:default` capability grants the same set (loopback http
 * plus any https origin), so the validator and the scope agree.
 */
export function validateEngineBaseUrl(raw: string): BaseUrlCheck {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { ok: false, url: "", reason: "Enter the engine's address." };
  }

  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return {
      ok: false,
      url: "",
      reason: `Not a URL. It should look like ${DEFAULT_BASE_URL}.`,
    };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return {
      ok: false,
      url: "",
      reason: `Only http:// (loopback) or https:// (deployed) is supported, not ${parsed.protocol}//`,
    };
  }

  // A remote engine is allowed only over TLS: the deployed service sits behind
  // the SILKSCREEN_ACCESS_TOKEN bearer gate, and https is what keeps that
  // token from crossing the network in the clear. Plain-http remote is still
  // refused below -- an http URL that is not loopback is either a typo or a
  // token leak waiting to happen.
  if (parsed.protocol === "https:") {
    if (parsed.search || parsed.hash) {
      return {
        ok: false,
        url: "",
        reason: "Give the origin only — no query string or fragment.",
      };
    }
    const path = parsed.pathname.replace(/\/+$/, "");
    return { ok: true, url: `${parsed.origin}${path}`, reason: "" };
  }

  const host = parsed.hostname.toLowerCase();
  // A real loopback literal, not a name that merely starts with "127." —
  // `127.evil.com` and `127.0.0.1.evil.com` are DNS names an attacker
  // resolves anywhere they like. Four numeric octets, first one 127, or the
  // exact IPv6/name forms; anything else is a hostname and is refused.
  const isV4Loopback = (h: string): boolean => {
    const octets = h.split(".");
    if (octets.length !== 4) return false;
    if (!octets.every((o) => /^\d{1,3}$/.test(o) && Number(o) <= 255)) {
      return false;
    }
    return Number(octets[0]) === 127;
  };
  const isLoopback =
    (LOOPBACK_HOSTS as readonly string[]).includes(host) || isV4Loopback(host);
  if (!isLoopback) {
    return {
      ok: false,
      url: "",
      reason: `"${parsed.hostname}" is not this machine. Over plain http the bearer token and your intents cross the network unencrypted. Use 127.0.0.1 for a local engine, or https:// for a deployed one.`,
    };
  }

  if (parsed.search || parsed.hash) {
    return {
      ok: false,
      url: "",
      reason: "Give the origin only — no query string or fragment.",
    };
  }

  // The client builds `${baseUrl}/healthz`, so a trailing slash would produce
  // a double slash. Normalise here rather than at every call site.
  const path = parsed.pathname.replace(/\/+$/, "");
  return { ok: true, url: `${parsed.origin}${path}`, reason: "" };
}

/** The saved engine address, falling back to the client's default. */
export function loadEngineBaseUrl(): string {
  const stored = safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ENGINE_BASE_URL);
  if (!stored) return DEFAULT_BASE_URL;
  // Never trust storage: a key written by an older build (or by hand) must not
  // be able to smuggle a non-loopback address past the check above.
  const check = validateEngineBaseUrl(stored);
  return check.ok ? check.url : DEFAULT_BASE_URL;
}

export function saveEngineBaseUrl(url: string): void {
  safeLocalStorage.setItem(KALEO_STORAGE_KEYS.ENGINE_BASE_URL, url);
}

/**
 * The optional bearer token, for an engine deployed behind a token gate.
 * Empty string means "no token" — the client then sends no Authorization
 * header at all, which is the correct shape for a local, ungated engine.
 */
export function loadEngineToken(): string {
  return (safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ENGINE_TOKEN) ?? "").trim();
}

export function saveEngineToken(token: string): void {
  const trimmed = token.trim();
  if (trimmed) {
    safeLocalStorage.setItem(KALEO_STORAGE_KEYS.ENGINE_TOKEN, trimmed);
  } else {
    safeLocalStorage.removeItem(KALEO_STORAGE_KEYS.ENGINE_TOKEN);
  }
}

type TestResult =
  | { state: "idle" }
  | { state: "testing" }
  | { state: "ok"; url: string }
  | { state: "failed"; url: string; detail: string };

export interface EngineConnectionProps {
  /** The address currently in effect, owned by the page. */
  baseUrl: string;
  /** Called with a validated, normalised URL once the user saves. */
  onBaseUrlChange: (url: string) => void;
  /** The bearer token currently in effect; empty string when none. */
  token?: string;
  /** Called with the trimmed token (possibly empty) once the user saves. */
  onTokenChange?: (token: string) => void;
  className?: string;
}

/**
 * Where the user points Kaleo at a running engine.
 *
 * The draft is local state and only a validated value is committed, so a
 * half-typed address never becomes the one the rest of the app uses.
 */
export const EngineConnection = ({
  baseUrl,
  onBaseUrlChange,
  token = "",
  onTokenChange,
  className,
}: EngineConnectionProps) => {
  const [draft, setDraft] = useState(baseUrl);
  const [tokenDraft, setTokenDraft] = useState(token);
  const [test, setTest] = useState<TestResult>({ state: "idle" });

  const check = validateEngineBaseUrl(draft);
  const dirty = check.ok && check.url !== baseUrl;

  const commit = () => {
    if (!check.ok) return baseUrl;
    saveEngineBaseUrl(check.url);
    setDraft(check.url);
    if (check.url !== baseUrl) onBaseUrlChange(check.url);
    return check.url;
  };

  const commitToken = () => {
    const trimmed = tokenDraft.trim();
    saveEngineToken(trimmed);
    setTokenDraft(trimmed);
    if (trimmed !== token) onTokenChange?.(trimmed);
    return trimmed;
  };

  const runTest = async () => {
    if (!check.ok) return;
    // Test what the user is about to use: saving first means the result and
    // the stored address can never disagree.
    const url = commit();
    const savedToken = commitToken();
    setTest({ state: "testing" });
    const result = await health(url, savedToken);
    setTest(
      result.ok
        ? { state: "ok", url }
        : { state: "failed", url, detail: result.detail || "no reason given" }
    );
  };

  const reset = () => {
    setDraft(DEFAULT_BASE_URL);
    setTest({ state: "idle" });
  };

  return (
    <div id="engine-connection" className={cn("space-y-3", className)}>
      <Header
        title="Engine address"
        description="Hardy talks to the silkscreen Python service over HTTP on your own machine"
        isMainTitle
      />

      <div className="flex flex-col gap-2">
        <Label htmlFor="engine-base-url" className="text-sm font-medium">
          Base URL
        </Label>
        <div className="flex items-center gap-2">
          <Input
            id="engine-base-url"
            data-testid="engine-base-url"
            value={draft}
            placeholder={DEFAULT_BASE_URL}
            aria-invalid={!check.ok}
            aria-describedby="engine-base-url-help"
            onChange={(event) => {
              setDraft(event.target.value);
              // A result about the previous address would be a lie about this
              // one, so it goes the moment the field changes.
              setTest({ state: "idle" });
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") void runTest();
            }}
            className="max-w-96"
          />
          <Button
            data-testid="engine-test-connection"
            onClick={() => void runTest()}
            disabled={!check.ok || test.state === "testing"}
            size="sm"
          >
            {test.state === "testing" ? (
              <>
                <Loader2Icon className="size-3.5 animate-spin" />
                Testing…
              </>
            ) : (
              "Test connection"
            )}
          </Button>
          {draft.trim() !== DEFAULT_BASE_URL && (
            <Button variant="ghost" size="sm" onClick={reset}>
              Reset
            </Button>
          )}
        </div>

        <p id="engine-base-url-help" className="text-xs text-muted-foreground">
          Default is {DEFAULT_BASE_URL} — the port README.md uses for the
          service ({" "}
          <code className="font-mono">PORT=8081 python -m service.app</code> ).
          Loopback addresses only.
        </p>

        {dirty && check.ok && (
          <p className="text-xs text-muted-foreground">
            Not saved yet — testing the connection saves it.
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="engine-token" className="text-sm font-medium">
          Access token <span className="font-normal text-muted-foreground">(optional)</span>
        </Label>
        <Input
          id="engine-token"
          data-testid="engine-token"
          type="password"
          autoComplete="off"
          value={tokenDraft}
          placeholder="Leave empty for a local engine"
          aria-describedby="engine-token-help"
          onChange={(event) => {
            setTokenDraft(event.target.value);
            setTest({ state: "idle" });
          }}
          onBlur={commitToken}
          onKeyDown={(event) => {
            if (event.key === "Enter") void runTest();
          }}
          className="max-w-96"
        />
        <p id="engine-token-help" className="text-xs text-muted-foreground">
          Only needed when the engine was started with a token gate; requests
          then carry it as <code className="font-mono">Authorization: Bearer</code>.
          A local engine with no token configured has no gate — leave this
          empty and nothing is sent.
        </p>
      </div>

      {!check.ok && (
        <div
          data-testid="engine-base-url-error"
          className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-3"
        >
          <ShieldAlertIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
          <p className="text-xs leading-relaxed text-destructive">
            {check.reason}
          </p>
        </div>
      )}

      {test.state === "ok" && (
        <div
          data-testid="engine-test-result"
          data-result="ok"
          className="flex items-start gap-2 rounded-xl border border-input/50 p-3"
        >
          <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-chart-2" />
          <div className="text-xs leading-relaxed">
            <p className="font-medium">
              {test.url}/healthz answered{" "}
              <code className="font-mono">ok: true</code>
            </p>
            <p className="text-muted-foreground mt-1">
              That is the readiness probe only. It does not prove the service
              has a GOOGLE_API_KEY — a keyless engine passes this check and then
              fails the first run with a 502 naming that variable.
            </p>
          </div>
        </div>
      )}

      {test.state === "failed" && (
        <div
          data-testid="engine-test-result"
          data-result="failed"
          className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-3"
        >
          <XCircleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div className="text-xs leading-relaxed">
            <p className="font-medium text-destructive">
              {test.url}/healthz did not answer ok
            </p>
            {/* The service's own words, verbatim. "Connection refused" and
                "answered 404" send the user to entirely different fixes. */}
            <p className="mt-1 font-mono text-muted-foreground break-all">
              {test.detail}
            </p>
            <p className="mt-1 text-muted-foreground">
              If that reads as a refused connection, nothing is listening on
              that port — start the engine with one of the commands below.
            </p>
          </div>
        </div>
      )}
    </div>
  );
};

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: (...args: unknown[]) => fetchMock(...args) }));

import { SilkscreenError } from "@/lib/silkscreen/client";
import {
  consentUrlAllowed,
  disconnect,
  environmentWinsSentence,
  fetchSetup,
  googleConnect,
  microsoftSave,
  stillConfiguredSentence,
} from "./service";

const BASE = "http://127.0.0.1:8081";

function json(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

const REPORT = {
  schema_version: 1,
  mode: "demo",
  home: "/x",
  demo_home: "/x/demo",
  engine: { state: "ready", detail: "", demo: false },
  google: { state: "unconfigured", detail: "", demo: true, oauth_client: false, signed_in: false, token: false, job: { state: "idle", error: null } },
  microsoft: { state: "unconfigured", detail: "", demo: true, fields: [], verified_at: null, meaning: "m", unverified: true },
  stripe: { state: "unconfigured", detail: "", demo: true, ready: false, key_mode: "demo", steps: [] },
};

beforeEach(() => {
  fetchMock.mockReset();
  vi.spyOn(console, "info").mockImplementation(() => undefined);
});
afterEach(() => vi.restoreAllMocks());

describe("the setup client", () => {
  it("sends the bearer on every call and logs route and status only", async () => {
    fetchMock.mockResolvedValueOnce(json(200, REPORT));
    await fetchSetup(BASE, "secret-token");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${BASE}/setup`);
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer secret-token");
    const logged = (console.info as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    expect(logged).toHaveLength(1);
    expect(logged[0][1]).toEqual({ route: "/setup", status: 200 });
    expect(JSON.stringify(logged)).not.toContain("demo_home");
  });

  it("maps a refused connection to `offline` rather than throwing raw", async () => {
    fetchMock.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    await expect(fetchSetup(BASE, "")).rejects.toMatchObject({ kind: "offline" });
  });

  it("refuses a /setup body with no mode or a missing provider", async () => {
    fetchMock.mockResolvedValueOnce(json(200, { ...REPORT, mode: "maybe" }));
    await expect(fetchSetup(BASE, "")).rejects.toBeInstanceOf(SilkscreenError);
    fetchMock.mockResolvedValueOnce(json(200, { ...REPORT, stripe: undefined }));
    await expect(fetchSetup(BASE, "")).rejects.toThrow(/stripe/);
  });

  it("names a 401 and a 404 in the client's own vocabulary", async () => {
    fetchMock.mockResolvedValueOnce(json(401, {}));
    await expect(fetchSetup(BASE, "x")).rejects.toMatchObject({ kind: "auth", status: 401 });
    fetchMock.mockResolvedValueOnce(json(404, {}));
    await expect(fetchSetup(BASE, "x")).rejects.toMatchObject({ kind: "request", status: 404 });
  });

  it("POSTs an empty JSON object to connect, and reads a 409 as a running sign-in", async () => {
    fetchMock.mockResolvedValueOnce(json(202, { auth_url: `${BASE}/setup/demo/consent?x`, job: { state: "waiting" }, demo: true }));
    const out = await googleConnect(BASE, "t");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
    expect(out.job.state).toBe("waiting");
    fetchMock.mockResolvedValueOnce(json(409, { error: "already running" }));
    await expect(googleConnect(BASE, "t")).rejects.toThrow(/already waiting/);
  });

  it("returns an Entra refusal as a verdict, not a thrown error", async () => {
    fetchMock.mockResolvedValueOnce(
      json(400, { saved: false, verdict: { ok: false, status: 401, code: "AADSTS7000215", meaning: "the secret is wrong" }, demo: false }),
    );
    const out = await microsoftSave(BASE, "t", { app_id: "a", app_secret: "s", tenant_id: "c" });
    expect(out.saved).toBe(false);
    expect(out.verdict.meaning).toBe("the secret is wrong");
  });

  it("addresses disconnect by provider", async () => {
    fetchMock.mockResolvedValueOnce(json(200, { disconnected: true, still_configured_from_environment: ["TEAMS_APP_ID"] }));
    const out = await disconnect(BASE, "t", "microsoft");
    expect((fetchMock.mock.calls[0] as [string])[0]).toBe(`${BASE}/setup/microsoft/disconnect`);
    expect(stillConfiguredSentence(out)).toMatch(/TEAMS_APP_ID.*that variable/);
  });
});

describe("consentUrlAllowed", () => {
  it("live: only https accounts.google.com", () => {
    expect(consentUrlAllowed("https://accounts.google.com/o/oauth2/v2/auth?x=1", BASE, "live").ok).toBe(true);
    expect(consentUrlAllowed("http://accounts.google.com/o/oauth2", BASE, "live").ok).toBe(false);
    expect(consentUrlAllowed("https://accounts.google.com.evil.io/", BASE, "live").ok).toBe(false);
    expect(consentUrlAllowed(`${BASE}/setup/demo/consent`, BASE, "live").ok).toBe(false);
  });

  it("demo: only the engine's own origin, and says why otherwise", () => {
    expect(consentUrlAllowed(`${BASE}/setup/demo/consent?state=abc`, BASE, "demo").ok).toBe(true);
    const refused = consentUrlAllowed("https://accounts.google.com/x", BASE, "demo");
    expect(refused).toEqual({ ok: false, reason: "the engine returned a consent URL on an unexpected host" });
    expect(consentUrlAllowed("not a url", BASE, "demo").ok).toBe(false);
  });
});

describe("the environment-wins sentence", () => {
  it("appears only when the engine said the environment still wins", () => {
    expect(environmentWinsSentence({ active: true, active_source: "file" })).toBe("");
    expect(environmentWinsSentence({ active: false, active_source: "environment" })).toMatch(/restart it/);
    expect(environmentWinsSentence(undefined)).toBe("");
  });
});

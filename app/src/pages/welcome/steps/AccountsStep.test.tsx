// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const openUrl = vi.fn(async (_url: string) => undefined);
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: (url: string) => openUrl(url) }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })) }));

const svc = {
  googleConnect: vi.fn(),
  googlePoll: vi.fn(),
  disconnect: vi.fn(),
  microsoftSave: vi.fn(),
};
vi.mock("@/lib/setup/service", async () => {
  const actual = await vi.importActual<typeof import("@/lib/setup/service")>("@/lib/setup/service");
  return {
    ...actual,
    googleConnect: (...a: unknown[]) => svc.googleConnect(...a),
    googlePoll: (...a: unknown[]) => svc.googlePoll(...a),
    disconnect: (...a: unknown[]) => svc.disconnect(...a),
    microsoftSave: (...a: unknown[]) => svc.microsoftSave(...a),
  };
});

import type { SetupReport } from "@/lib/setup/service";
import type { SetupReportState } from "../useSetupReport";
import { AccountsStep, CONSENT_HOST_REFUSED, GOOGLE_DEMO_SENTENCE, MICROSOFT_DEMO_SENTENCE, SIGNED_OUT_SENTENCE } from "./AccountsStep";

const BASE = "http://127.0.0.1:8081";
const MEANING =
  "Entra issued an app token for this registration. That proves the ids and the secret; it does not prove Graph permissions are consented or that the Teams bot (`python -m teamsbot`) is running.";

function report(over: Partial<SetupReport> = {}, mode: "live" | "demo" = "live"): SetupReport {
  const demo = mode === "demo";
  return {
    schema_version: 1,
    mode,
    home: "~/.kaleo",
    demo_home: "~/.kaleo/demo",
    engine: { state: "ready", detail: "", demo: false },
    google: { state: "unconfigured", detail: "", demo, oauth_client: true, signed_in: false, token: false, job: { state: "idle", error: null } },
    microsoft: { state: "unconfigured", detail: "", demo, fields: [], verified_at: null, meaning: MEANING, unverified: true },
    stripe: { state: "unconfigured", detail: "", demo, ready: false, key_mode: demo ? "demo" : "none", steps: [] },
    ...over,
  };
}

function state(data: SetupReport | null, down = false): SetupReportState {
  return { report: data, down, error: "", loading: false, refresh: vi.fn(async () => data) };
}

const card = (id: string) =>
  screen.getAllByTestId("connect-card").find((el) => el.getAttribute("data-id") === id) as HTMLElement;
const within = (el: HTMLElement, testid: string) => el.querySelector(`[data-testid="${testid}"]`) as HTMLElement;

beforeEach(() => {
  openUrl.mockClear();
  for (const fn of Object.values(svc)) fn.mockReset();
  vi.spyOn(console, "info").mockImplementation(() => undefined);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const mount = (rep: SetupReportState) => {
  const setCard = vi.fn();
  render(<AccountsStep baseUrl={BASE} token="t" report={rep} setCard={setCard} />);
  return setCard;
};

describe("AccountsStep", () => {
  it("shows Google and Billing, with Microsoft under More accounts", () => {
    mount(state(report()));
    expect(card("google")).toBeTruthy();
    expect(card("stripe")).toBeTruthy();
    expect(screen.queryAllByTestId("connect-card").some((el) => el.getAttribute("data-id") === "microsoft")).toBe(false);
    fireEvent.click(screen.getByTestId("accounts-more"));
    expect(card("microsoft")).toBeTruthy();
    expect(screen.getByTestId("microsoft-meaning").textContent).toBe(MEANING);
  });

  it("with the engine down every card says so and offers nothing to press", () => {
    mount(state(null, true));
    for (const id of ["google", "stripe"]) {
      expect(card(id).getAttribute("data-state")).toBe("down");
      expect(within(card(id), "connect-detail").textContent).toBe("Engine not running, connect later in Settings.");
    }
    expect((within(card("google"), "connect-action") as HTMLButtonElement).disabled).toBe(true);
  });

  it("marks cards done from what the engine said, not from what was pressed", () => {
    const setCard = mount(state(report({ google: { ...report().google, state: "ready", signed_in: true, token: true } })));
    expect(setCard).toHaveBeenCalledWith("google", true);
    expect(setCard).toHaveBeenCalledWith("stripe", false);
    expect(setCard).toHaveBeenCalledWith("microsoft", false);
    expect(within(card("google"), "connect-ready").textContent).toContain("Signed in");
  });

  it("Connect opens the consent URL in the browser only on an allowed host, then polls", async () => {
    svc.googleConnect.mockResolvedValue({ auth_url: "https://accounts.google.com/o/oauth2/v2/auth?s=1", job: { state: "waiting", error: null }, demo: false });
    svc.googlePoll.mockResolvedValue({ state: "ready", job: { state: "connected", error: null }, signed_in: true, token: true, token_path: "", demo: false });
    const rep = state(report());
    mount(rep);
    fireEvent.click(within(card("google"), "connect-action"));
    await waitFor(() => expect(openUrl).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/v2/auth?s=1"));
    expect(card("google").getAttribute("data-state")).toBe("connecting");
    await waitFor(() => expect(svc.googlePoll).toHaveBeenCalled(), { timeout: 3000 });
    await waitFor(() => expect(rep.refresh).toHaveBeenCalled());
  });

  it("refuses a consent URL on an unexpected host and says so", async () => {
    svc.googleConnect.mockResolvedValue({ auth_url: "https://evil.example/consent", job: { state: "waiting", error: null }, demo: false });
    mount(state(report()));
    fireEvent.click(within(card("google"), "connect-action"));
    await waitFor(() => expect(screen.getByTestId("google-note").textContent).toBe(CONSENT_HOST_REFUSED));
    expect(openUrl).not.toHaveBeenCalled();
  });

  it("in demo the engine's own origin is allowed, and the card says nothing was contacted", async () => {
    svc.googleConnect.mockResolvedValue({ auth_url: `${BASE}/setup/demo/consent?state=n`, job: { state: "waiting", error: null }, demo: true });
    const base = report({}, "demo");
    const rep = state(report({ google: { ...base.google, state: "ready", signed_in: true, token: true } }, "demo"));
    mount(rep);
    expect(within(card("google"), "demo-badge")).toBeTruthy();
    expect(within(card("google"), "connect-detail").textContent).toBe(GOOGLE_DEMO_SENTENCE);
    expect(screen.queryByTestId("billing-connect")).toBeNull();
  });

  it("Disconnect says signed out here, not revoked at Google, plus what the environment still holds", async () => {
    svc.disconnect.mockResolvedValue({ disconnected: true, removed: ["token"], still_configured_from_environment: ["GOOGLEAPPS_CLIENT_ID"] });
    const base = report();
    mount(state(report({ google: { ...base.google, state: "ready", signed_in: true, token: true } })));
    fireEvent.click(within(card("google"), "connect-secondary"));
    await waitFor(() => expect(screen.getByTestId("google-note").textContent).toContain(SIGNED_OUT_SENTENCE));
    expect(screen.getByTestId("google-note").textContent).toContain("GOOGLEAPPS_CLIENT_ID");
  });

  it("Microsoft posts the three fields as password-safe inputs and shows the fixed-vocabulary refusal", async () => {
    svc.microsoftSave.mockResolvedValue({ saved: false, verdict: { ok: false, status: 401, code: "AADSTS7000215", meaning: "the secret is wrong" }, meaning: MEANING, demo: false });
    mount(state(report()));
    fireEvent.click(screen.getByTestId("accounts-more"));
    expect((screen.getByTestId("microsoft-app-secret") as HTMLInputElement).type).toBe("password");
    expect((screen.getByTestId("microsoft-app-secret") as HTMLInputElement).autocomplete).toBe("off");
    fireEvent.change(screen.getByTestId("microsoft-app-id"), { target: { value: "app" } });
    fireEvent.change(screen.getByTestId("microsoft-app-secret"), { target: { value: "sec" } });
    fireEvent.change(screen.getByTestId("microsoft-tenant-id"), { target: { value: "ten" } });
    fireEvent.click(screen.getByTestId("microsoft-save"));
    await waitFor(() => expect(screen.getByTestId("microsoft-note").textContent).toBe("the secret is wrong"));
    expect(svc.microsoftSave).toHaveBeenCalledWith(BASE, "t", { app_id: "app", app_secret: "sec", tenant_id: "ten" });
  });

  it("a ready Microsoft card says Token issued, and in demo that Entra was not contacted", () => {
    const base = report({}, "demo");
    mount(state(report({ microsoft: { ...base.microsoft, state: "ready" } }, "demo")));
    fireEvent.click(screen.getByTestId("accounts-more"));
    expect(within(card("microsoft"), "connect-ready").textContent).toContain("Token issued");
    expect(within(card("microsoft"), "connect-ready").textContent).not.toMatch(/Verified|Connected/);
    expect(within(card("microsoft"), "connect-detail").textContent).toBe(MICROSOFT_DEMO_SENTENCE);
  });

  it("Billing in demo offers the demo key and no OAuth button", async () => {
    mount(state(report({}, "demo")));
    await waitFor(() => expect(screen.getByTestId("billing-demo-key")).toBeTruthy());
    fireEvent.click(screen.getByTestId("billing-demo-key"));
    expect((screen.getByTestId("billing-input-STRIPE_API_KEY") as HTMLInputElement).value).toBe("rk_test_kaleo_demo");
    expect((screen.getByTestId("billing-input-STRIPE_API_KEY") as HTMLInputElement).type).toBe("password");
    expect(screen.queryByTestId("billing-connect-button")).toBeNull();
  });
});

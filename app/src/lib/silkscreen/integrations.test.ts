// Tests for the integrations client.
//
// `@tauri-apps/plugin-http` is mocked wholesale, the same way `client.test.ts`
// does it, so these tests own every byte the "network" answers with. The
// assertions that matter most are the negative ones: a response the engine
// mangled must reject, and it must reject naming what was wrong — a resolved
// empty array here would draw a page that says the user has nothing set up.

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError } from "./client";
import {
  badgeFor,
  fetchIntegrations,
  groupByKind,
  parseIntegrations,
  runIntegrationAction,
} from "./integrations";
import type { Integration, IntegrationKind, IntegrationState } from "./integrations";

const mockFetch = vi.mocked(tauriFetch);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A complete, well-formed entry; each test overrides only what it is about. */
function entry(over: Partial<Integration> & { id: string }): Record<string, unknown> {
  return {
    name: over.id,
    kind: "delivery",
    state: "ready",
    summary: "",
    detail: "",
    settings: [],
    hints: [],
    actions: [],
    docs: "",
    unverified: false,
    ...over,
  };
}

function item(
  id: string,
  kind: IntegrationKind,
  state: IntegrationState = "ready"
): Integration {
  return {
    id,
    name: id,
    kind,
    state,
    summary: "",
    detail: "",
    settings: [],
    hints: [],
    actions: [],
    docs: "",
    unverified: false,
  };
}

async function failure(promise: Promise<unknown>): Promise<SilkscreenError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(SilkscreenError);
    return error as SilkscreenError;
  }
  throw new Error("expected the promise to reject");
}

beforeEach(() => {
  mockFetch.mockReset();
});

/* ------------------------------------------------------ fetchIntegrations */

describe("fetchIntegrations", () => {
  it("reads a good response whole, keeping every field and the server's order", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        schema_version: 1,
        integrations: [
          entry({
            id: "slack",
            name: "Slack",
            kind: "delivery",
            state: "partial",
            summary: "Posts runs into a thread in #hardware.",
            detail: "Signing secret set; bot token missing.",
            settings: [
              {
                key: "SLACK_BOT_TOKEN",
                required: true,
                set: false,
                shown: "",
                note: "",
              },
            ],
            hints: ["Set SLACK_BOT_TOKEN in the service's environment."],
            actions: [
              {
                id: "google_sign_in",
                label: "Sign in with Google",
                method: "POST",
                path: "/deliver/auth/start",
              },
            ],
            docs: "docs/slack.md",
            unverified: true,
          } as Partial<Integration> & { id: string }),
          entry({ id: "meet", kind: "meeting", state: "unavailable" }),
        ],
      })
    );

    const list = await fetchIntegrations("http://127.0.0.1:8081", "tok");

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8081/integrations");
    expect(init?.method).toBe("GET");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer tok");

    expect(list.map((i) => i.id)).toEqual(["slack", "meet"]);
    expect(list[0]).toMatchObject({
      name: "Slack",
      kind: "delivery",
      state: "partial",
      summary: "Posts runs into a thread in #hardware.",
      docs: "docs/slack.md",
      unverified: true,
    });
    expect(list[0].settings).toEqual([
      { key: "SLACK_BOT_TOKEN", required: true, set: false, shown: "", note: "" },
    ]);
    expect(list[0].hints).toHaveLength(1);
    expect(list[0].actions[0]).toEqual({
      id: "google_sign_in",
      label: "Sign in with Google",
      method: "POST",
      path: "/deliver/auth/start",
    });
  });

  it("sends no Authorization header when the token is blank", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { integrations: [] }));
    await fetchIntegrations("http://127.0.0.1:8081", "   ");
    const headers = mockFetch.mock.calls[0][1]?.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it("passes an id it has never heard of straight through — the roster grows", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        integrations: [entry({ id: "some_future_fab", kind: "fabrication" })],
      })
    );
    const list = await fetchIntegrations("http://x", "");
    expect(list.map((i) => i.id)).toEqual(["some_future_fab"]);
  });

  it("rejects an unknown state rather than rendering a row it cannot describe", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        integrations: [
          entry({ id: "slack" }),
          entry({ id: "meet", state: "kind-of-working" as IntegrationState }),
        ],
      })
    );
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.kind).toBe("server");
    expect(error.detail).toContain("meet");
    expect(error.detail).toContain("unknown state");
    expect(error.detail).toContain('"kind-of-working"');
  });

  it("rejects an unknown kind", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        integrations: [entry({ id: "meet", kind: "chatting" as IntegrationKind })],
      })
    );
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.detail).toContain("unknown kind");
  });

  it("names every problem in one error, not just the first", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        integrations: [
          entry({ id: "slack", state: "nope" as IntegrationState }),
          entry({ id: "meet", kind: "nope" as IntegrationKind }),
          entry({ id: "cad", hints: "not a list" as unknown as string[] }),
        ],
      })
    );
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.message).toContain("3 problems");
    expect(error.detail).toContain("slack");
    expect(error.detail).toContain("meet");
    expect(error.detail).toContain("cad");
  });

  it("rejects a non-array settings / hints / actions", async () => {
    for (const key of ["settings", "hints", "actions"]) {
      mockFetch.mockResolvedValueOnce(
        jsonResponse(200, {
          integrations: [{ ...entry({ id: "slack" }), [key]: { nope: true } }],
        })
      );
      const error = await failure(fetchIntegrations("http://x", ""));
      expect(error.detail).toContain(`${key} is not an array`);
    }
  });

  it("rejects an action with a method it will not send", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        integrations: [
          {
            ...entry({ id: "slack" }),
            actions: [{ id: "wipe", label: "Wipe", method: "DELETE", path: "/x" }],
          },
        ],
      })
    );
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.detail).toContain("unknown method");
  });

  it("rejects a response with no integrations key instead of showing an empty page", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { schema_version: 1 }));
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.kind).toBe("server");
    expect(error.detail).toContain("no `integrations` key");
  });

  it("rejects an integrations value that is not an array", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { integrations: {} }));
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.detail).toContain("not an array");
  });

  it("rejects a body that is not JSON at all", async () => {
    mockFetch.mockResolvedValueOnce(new Response("<html>nope</html>", { status: 200 }));
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.kind).toBe("server");
    expect(error.detail).toContain("not JSON");
  });

  it("accepts an empty roster the engine really sent", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { integrations: [] }));
    await expect(fetchIntegrations("http://x", "")).resolves.toEqual([]);
  });

  it("reports an aborted request as cancelled, not as a broken engine", async () => {
    const controller = new AbortController();
    mockFetch.mockImplementationOnce(async (_url, init) => {
      controller.abort();
      const signal = (init as { signal?: AbortSignal } | undefined)?.signal;
      expect(signal?.aborted).toBe(true);
      throw new DOMException("aborted", "AbortError");
    });
    const error = await failure(
      fetchIntegrations("http://x", "", controller.signal)
    );
    expect(error.kind).toBe("cancelled");
  });

  it("reports an unreachable engine as offline", async () => {
    mockFetch.mockRejectedValueOnce(new Error("connection refused"));
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.kind).toBe("offline");
    expect(error.detail).toBe("connection refused");
  });

  it("says an old engine has no route rather than showing nothing configured", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(404, {}));
    const error = await failure(fetchIntegrations("http://x", ""));
    expect(error.kind).toBe("request");
    expect(error.message).toContain("/integrations");
  });

  it("maps 401 to auth and any other failure status to server", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(401, {}));
    expect((await failure(fetchIntegrations("http://x", ""))).kind).toBe("auth");
    mockFetch.mockResolvedValueOnce(jsonResponse(500, {}));
    expect((await failure(fetchIntegrations("http://x", ""))).kind).toBe("server");
  });
});

/* ------------------------------------------------------ parseIntegrations */

describe("parseIntegrations", () => {
  it("throws on a body that is not an object", () => {
    expect(() => parseIntegrations([])).toThrow(SilkscreenError);
    expect(() => parseIntegrations(null)).toThrow(SilkscreenError);
  });

  it("throws on an entry that is not an object", () => {
    expect(() => parseIntegrations({ integrations: ["slack"] })).toThrow(
      SilkscreenError
    );
  });

  it("throws when an entry has no id", () => {
    const bad = { ...entry({ id: "x" }) };
    delete bad.id;
    expect(() => parseIntegrations({ integrations: [bad] })).toThrow(SilkscreenError);
  });

  it("fills the soft fields but never invents a state", () => {
    const list = parseIntegrations({
      integrations: [
        { id: "spice", kind: "design", state: "ready", settings: [], hints: [], actions: [] },
      ],
    });
    expect(list[0]).toEqual({
      id: "spice",
      name: "spice",
      kind: "design",
      state: "ready",
      summary: "",
      detail: "",
      settings: [],
      hints: [],
      actions: [],
      docs: "",
      unverified: false,
    });
  });
});

/* -------------------------------------------------- runIntegrationAction */

describe("runIntegrationAction", () => {
  it("POSTs an action's path once with the token and an empty JSON body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    const result = await runIntegrationAction("http://x", "tok", {
      id: "google_sign_in",
      label: "Sign in with Google",
      method: "POST",
      path: "/deliver/auth/start",
    });
    expect(result).toEqual({ ok: true });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("http://x/deliver/auth/start");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe("{}");
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json"
    );
  });

  it("GETs without a body", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    await runIntegrationAction("http://x", "", {
      id: "probe",
      label: "Probe",
      method: "GET",
      path: "/deliver/config",
    });
    const init = mockFetch.mock.calls[0][1];
    expect(init?.method).toBe("GET");
    expect(init?.body).toBeUndefined();
  });

  it("surfaces the engine's own refusal text and does not retry", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(400, { error: "no OAuth client" }));
    const error = await failure(
      runIntegrationAction("http://x", "", {
        id: "google_sign_in",
        label: "Sign in with Google",
        method: "POST",
        path: "/deliver/auth/start",
      })
    );
    expect(error.kind).toBe("request");
    expect(error.message).toBe("no OAuth client");
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});

/* -------------------------------------------------------------- groupByKind */

describe("groupByKind", () => {
  it("orders kinds delivery, meeting, design, fabrication whatever order they arrived in", () => {
    const groups = groupByKind([
      item("kicad", "fabrication"),
      item("spice", "design"),
      item("slack", "delivery"),
      item("meet", "meeting"),
    ]);
    expect(groups.map((g) => g.kind)).toEqual([
      "delivery",
      "meeting",
      "design",
      "fabrication",
    ]);
  });

  it("keeps the server's order inside a kind", () => {
    const groups = groupByKind([
      item("zoom", "meeting"),
      item("google", "delivery"),
      item("meet", "meeting"),
      item("slack", "delivery"),
      item("teams", "meeting"),
    ]);
    expect(groups[0].items.map((i) => i.id)).toEqual(["google", "slack"]);
    expect(groups[1].items.map((i) => i.id)).toEqual(["zoom", "meet", "teams"]);
  });

  it("omits a kind nothing landed in, and handles an empty list", () => {
    const groups = groupByKind([item("slack", "delivery")]);
    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe("delivery");
    expect(groupByKind([])).toEqual([]);
  });

  it("does not mutate or alias the input list", () => {
    const list = [item("slack", "delivery"), item("meet", "meeting")];
    const groups = groupByKind(list);
    groups[0].items.push(item("extra", "delivery"));
    expect(list).toHaveLength(2);
  });
});

/* ----------------------------------------------------------------- badgeFor */

describe("badgeFor", () => {
  it("calls a fully configured integration ready", () => {
    expect(badgeFor(item("slack", "delivery", "ready"))).toEqual({
      label: "Ready",
      tone: "ok",
    });
  });

  it("keeps unverified visible on an otherwise ready integration", () => {
    expect(badgeFor({ ...item("meet", "meeting", "ready"), unverified: true })).toEqual({
      label: "Ready, unverified",
      tone: "warn",
    });
  });

  it("warns on partial — the state that looks like it works until the first call", () => {
    expect(badgeFor(item("google", "delivery", "partial"))).toEqual({
      label: "Partly set up",
      tone: "warn",
    });
  });

  it("distinguishes not-installed from not-set-up", () => {
    const unconfigured = badgeFor(item("slack", "delivery", "unconfigured"));
    const unavailable = badgeFor(item("cad", "design", "unavailable"));
    expect(unconfigured).toEqual({ label: "Not set up", tone: "off" });
    expect(unavailable).toEqual({ label: "Not installed", tone: "off" });
    expect(unconfigured.label).not.toBe(unavailable.label);
  });

  it("does not let unverified rewrite a state that is not ready", () => {
    expect(
      badgeFor({ ...item("zoom", "meeting", "unavailable"), unverified: true }).label
    ).toBe("Not installed");
  });
});

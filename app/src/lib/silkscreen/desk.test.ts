import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError } from "./client";
import { MAX_PNG_BYTES, resolveDesk } from "./desk";

const mockFetch = vi.mocked(tauriFetch);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
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

const PNG_B64 = "iVBORw0KGgo=";

function options(
  extra: Partial<Parameters<typeof resolveDesk>[1]> = {}
): Parameters<typeof resolveDesk>[1] {
  return {
    png_base64: PNG_B64,
    utterance: "what's this",
    cursor_x: 100,
    cursor_y: 200,
    width: 1512,
    height: 982,
    ...extra,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveDesk", () => {
  it("POSTs the snap to /desk/resolve and returns the caption", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(200, {
        caption: "That blocker is about C1.",
        abstain: false,
        target: { testid: "finding-card", attrs: { sev: "blocker" }, tab: "review" },
        model: "gemini-desk-test",
      })
    );
    const result = await resolveDesk("http://127.0.0.1:8081", {
      ...options({
        candidates: [{ testid: "finding-card", attrs: { sev: "blocker" } }],
        token: "sekrit",
      }),
    });
    expect(result.caption).toBe("That blocker is about C1.");
    expect(result.abstain).toBe(false);
    expect(result.target).toEqual({
      testid: "finding-card",
      attrs: { sev: "blocker" },
      tab: "review",
    });
    expect(result.model).toBe("gemini-desk-test");

    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8081/desk/resolve");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer sekrit",
    });
    expect(JSON.parse(String(init.body))).toEqual({
      png_b64: PNG_B64,
      utterance: "what's this",
      cursor_x: 100,
      cursor_y: 200,
      width: 1512,
      height: 982,
      candidates: [{ testid: "finding-card", attrs: { sev: "blocker" } }],
    });
  });

  it("drops a target when the engine abstains", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse(200, {
        caption: "I cannot tell.",
        abstain: true,
        target: { testid: "finding-card" },
        model: "m",
      })
    );
    const result = await resolveDesk("http://x", options());
    expect(result.abstain).toBe(true);
    expect(result.target).toBeNull();
  });

  it("refuses an empty PNG before any bytes move", async () => {
    const error = await failure(resolveDesk("http://x", options({ png_base64: "  " })));
    expect(error.kind).toBe("request");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("refuses an over-cap PNG before any bytes move", async () => {
    const huge = "A".repeat(Math.ceil((MAX_PNG_BYTES + 100) * 4 / 3));
    const error = await failure(
      resolveDesk("http://x", options({ png_base64: huge }))
    );
    expect(error.kind).toBe("request");
    expect(error.message).toMatch(/too large/);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it.each([
    [400, "request"],
    [413, "request"],
    [401, "auth"],
    [502, "upstream"],
    [503, "upstream"],
    [500, "server"],
  ])("maps a %s to kind %s", async (status, kind) => {
    mockFetch.mockResolvedValue(
      jsonResponse(status, { error: "nope", detail: "why", error_id: "e-1" })
    );
    const error = await failure(resolveDesk("http://x", options()));
    expect(error.kind).toBe(kind);
    expect(error.status).toBe(status);
    expect(error.message).toBe("nope");
  });

  it("maps an unreachable engine to offline", async () => {
    mockFetch.mockRejectedValue(new TypeError("Failed to fetch"));
    const error = await failure(resolveDesk("http://x", options()));
    expect(error.kind).toBe("offline");
  });
});

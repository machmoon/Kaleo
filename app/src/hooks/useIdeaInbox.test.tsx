// @vitest-environment jsdom
//
// The Slack inbox poll, against a mocked Tauri fetch standing in for
// service/inbox.py. The money rules are asserted directly: nothing is accepted
// while Ada is busy, a lost accept (409) never starts a run, one accepted idea
// starts exactly one run, and the session reported is the new run's, never
// the one that was already on screen.

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { useIdeaInbox, type IdeaInboxOptions } from "./useIdeaInbox";

const mockFetch = vi.mocked(tauriFetch);
const IDEA = { id: "idea_1", text: "a 3.3V LDO board", source: "slack", user: "U1" };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

function calls(): Array<{ url: string; method: string; body: Record<string, unknown> }> {
  return mockFetch.mock.calls.map(([url, init]) => ({
    url: String(url),
    method: String(init?.method),
    body: init?.body ? JSON.parse(String(init.body)) : {},
  }));
}

function engine({ ideas = [IDEA], accept = 200 }: { ideas?: unknown[]; accept?: number } = {}) {
  mockFetch.mockImplementation(async (url) => {
    const path = String(url).replace("http://127.0.0.1:8081", "");
    if (path === "/inbox") return json(200, { ideas });
    if (path.endsWith("/accept")) return json(accept, accept === 200 ? IDEA : { error: "taken" });
    return json(200, {});
  });
}

function options(partial: Partial<IdeaInboxOptions> = {}): IdeaInboxOptions {
  return {
    baseUrl: "http://127.0.0.1:8081",
    busy: false,
    session: null,
    status: "idle",
    onIdea: vi.fn(),
    intervalMs: 60_000,
    ...partial,
  };
}

describe("useIdeaInbox", () => {
  beforeEach(() => mockFetch.mockReset());
  afterEach(() => vi.clearAllMocks());

  it("accepts the oldest idea and starts it once", async () => {
    engine();
    const opts = options();
    renderHook(() => useIdeaInbox(opts));
    await waitFor(() => expect(opts.onIdea).toHaveBeenCalledTimes(1));
    expect(opts.onIdea).toHaveBeenCalledWith(IDEA);
    const accept = calls().find((c) => c.url.endsWith("/inbox/idea_1/accept"));
    expect(accept?.method).toBe("POST");
    expect(String(accept?.body.claimant)).toMatch(/^ada-desktop-/);
  });

  it("does not even look while Ada is busy", async () => {
    engine();
    const opts = options({ busy: true });
    renderHook(() => useIdeaInbox(opts));
    await new Promise((r) => setTimeout(r, 20));
    expect(mockFetch).not.toHaveBeenCalled();
    expect(opts.onIdea).not.toHaveBeenCalled();
  });

  it("never starts an idea another desktop accepted first", async () => {
    engine({ accept: 409 });
    const opts = options();
    renderHook(() => useIdeaInbox(opts));
    await waitFor(() => expect(calls().some((c) => c.url.endsWith("/accept"))).toBe(true));
    await new Promise((r) => setTimeout(r, 20));
    expect(opts.onIdea).not.toHaveBeenCalled();
  });

  it("reports the new run's session, not the one already on screen", async () => {
    engine();
    const onIdea = vi.fn();
    const { rerender } = renderHook((props: IdeaInboxOptions) => useIdeaInbox(props), {
      initialProps: options({ session: "old", status: "done", onIdea }),
    });
    await waitFor(() => expect(onIdea).toHaveBeenCalledTimes(1));
    rerender(options({ session: null, status: "running", onIdea, busy: true }));
    rerender(options({ session: "new", status: "waiting", onIdea }));
    await waitFor(() =>
      expect(calls().find((c) => c.url.endsWith("/inbox/idea_1/start"))?.body).toMatchObject({
        session: "new",
      }),
    );
    expect(calls().filter((c) => c.url.endsWith("/start"))).toHaveLength(1);
  });

  it("says the start failed when the step run errors before a session exists", async () => {
    engine();
    const onIdea = vi.fn();
    const { rerender } = renderHook((props: IdeaInboxOptions) => useIdeaInbox(props), {
      initialProps: options({ onIdea }),
    });
    await waitFor(() => expect(onIdea).toHaveBeenCalledTimes(1));
    rerender(options({ status: "running", onIdea, busy: true }));
    rerender(options({ status: "error", onIdea }));
    await waitFor(() =>
      expect(calls().some((c) => c.url.endsWith("/inbox/idea_1/fail"))).toBe(true),
    );
  });

  it("treats an engine with no inbox route as ordinary", async () => {
    mockFetch.mockResolvedValue(json(404, { error: "no route /inbox" }));
    const opts = options();
    renderHook(() => useIdeaInbox(opts));
    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(1));
    expect(opts.onIdea).not.toHaveBeenCalled();
  });
});

// Tests for the one place Kaleo talks to the engine.
//
// `@tauri-apps/plugin-http` is mocked wholesale: these tests own every byte
// the "network" answers with, including how the NDJSON body is chunked, so the
// stream parser's boundary handling is exercised deliberately rather than by
// luck. The single most load-bearing assertion in this file is the mock's call
// count: a second POST that the contract does not license is a paid engine run
// the user never asked for.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import {
  MAX_TIME_LIMIT_S,
  MIN_TIME_LIMIT_S,
  SilkscreenError,
  authHeaders,
  generate,
  generateStream,
  health,
  normalizeRequest,
  newRunId,
  parseFrame,
  startSteps,
} from "./client";
import type { StreamFrame } from "./types";

const mockFetch = vi.mocked(tauriFetch);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** An NDJSON 200 whose body arrives in exactly the chunks given. */
function streamResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream, { status });
}

function line(frame: Record<string, unknown>): string {
  return `${JSON.stringify(frame)}\n`;
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

afterEach(() => {
  vi.useRealTimers();
});

/* -------------------------------------------------------- normalizeRequest */

describe("normalizeRequest", () => {
  it("trims the intent and keeps only fully-filled datasheet rows", () => {
    const out = normalizeRequest({
      intent: "  a 3.3V LDO board  ",
      datasheets: {
        AMS1117: " https://example.com/ds.pdf ",
        "": "https://example.com/orphan.pdf",
        "  ": "https://example.com/blank-part.pdf",
        NoUrl: "",
        Spacey: "   ",
      },
    });
    expect(out.intent).toBe("a 3.3V LDO board");
    expect(out.datasheets).toEqual({ AMS1117: "https://example.com/ds.pdf" });
  });

  it("clamps the time limit into the service's accepted range", () => {
    expect(normalizeRequest({ intent: "x", time_limit_s: 0 }).time_limit_s).toBe(
      MIN_TIME_LIMIT_S
    );
    expect(
      normalizeRequest({ intent: "x", time_limit_s: 9999 }).time_limit_s
    ).toBe(MAX_TIME_LIMIT_S);
    expect(
      normalizeRequest({ intent: "x", time_limit_s: 22.6 }).time_limit_s
    ).toBe(23);
    expect(
      normalizeRequest({ intent: "x", time_limit_s: -5 }).time_limit_s
    ).toBe(MIN_TIME_LIMIT_S);
  });

  it("falls back to the minimum when the limit is absent or not a number", () => {
    expect(normalizeRequest({ intent: "x" }).time_limit_s).toBe(MIN_TIME_LIMIT_S);
    expect(
      normalizeRequest({ intent: "x", time_limit_s: Number.NaN }).time_limit_s
    ).toBe(MIN_TIME_LIMIT_S);
    expect(
      normalizeRequest({ intent: "x", time_limit_s: Infinity }).time_limit_s
    ).toBe(MIN_TIME_LIMIT_S);
  });

  it("defaults review to true and preserves an explicit false", () => {
    expect(normalizeRequest({ intent: "x" }).review).toBe(true);
    expect(normalizeRequest({ intent: "x", review: false }).review).toBe(false);
    expect(normalizeRequest({ intent: "x", review: true }).review).toBe(true);
  });

  it("sends ground/debug only when explicitly true — absence is the service default", () => {
    const bare = normalizeRequest({ intent: "x" });
    expect("ground" in bare).toBe(false);
    expect("debug" in bare).toBe(false);

    const falsy = normalizeRequest({ intent: "x", ground: false, debug: false });
    expect("ground" in falsy).toBe(false);
    expect("debug" in falsy).toBe(false);

    const on = normalizeRequest({
      intent: "x",
      datasheets: { AMS1117: "https://x/ds.pdf" },
      ground: true,
      debug: true,
    });
    expect(on.ground).toBe(true);
    expect(on.debug).toBe(true);
  });

  it("drops ground when normalization leaves no datasheets to ground on", () => {
    // The service 400s a ground request with no datasheets; ground:true only
    // survives when at least one fully-filled datasheet row survives too.
    const none = normalizeRequest({ intent: "x", ground: true });
    expect("ground" in none).toBe(false);
    const halfFilled = normalizeRequest({
      intent: "x",
      datasheets: { AMS1117: "   " },
      ground: true,
    });
    expect("ground" in halfFilled).toBe(false);
  });

  it("carries the summary mode in both directions, and drops anything else", () => {
    // Additive and explicit: an engine that has never heard of `summary`
    // ignores the key, and one that has is told which mode the user picked
    // rather than being left to guess from its absence. A value that is
    // neither mode is not a request the engine can read, so it never leaves.
    expect(normalizeRequest({ intent: "x", summary: "prose" }).summary).toBe("prose");
    expect(normalizeRequest({ intent: "x", summary: "structured" }).summary).toBe(
      "structured"
    );
    expect("summary" in normalizeRequest({ intent: "x" })).toBe(false);
    expect(
      "summary" in normalizeRequest({ intent: "x", summary: "verbose" as never })
    ).toBe(false);
  });

  it("survives a missing intent and missing datasheets", () => {
    const out = normalizeRequest({} as never);
    expect(out.intent).toBe("");
    expect(out.datasheets).toEqual({});
  });
});

/* ------------------------------------------------------------ error kinds */

describe("generate error classification", () => {
  it("reads a 502 that mentions GOOGLE_API_KEY as a setup problem, not an outage", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(502, {
        error: "model call failed",
        detail: "GOOGLE_API_KEY is not set",
        status: 502,
      })
    );
    const error = await failure(generate("http://x", { intent: "board" }));
    expect(error.kind).toBe("setup");
    expect(error.status).toBe(502);
  });

  it("also matches a lowercase 'api key' mention in the top-level error", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(503, { error: "no api key configured", status: 503 })
    );
    const error = await failure(generate("http://x", { intent: "board" }));
    expect(error.kind).toBe("setup");
  });

  it("reads a plain 502 as upstream", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(502, { error: "gemini answered 500", status: 502 })
    );
    const error = await failure(generate("http://x", { intent: "board" }));
    expect(error.kind).toBe("upstream");
  });

  it("reads 400 and 413 as request errors", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(400, { error: "intent is required", status: 400 })
    );
    expect((await failure(generate("http://x", { intent: "" }))).kind).toBe(
      "request"
    );
    mockFetch.mockResolvedValueOnce(
      jsonResponse(413, { error: "body too large", status: 413 })
    );
    expect((await failure(generate("http://x", { intent: "x" }))).kind).toBe(
      "request"
    );
  });

  it("reads a 500 as a server bug and carries the error_id through", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(500, {
        error: "internal error",
        error_id: "abc123",
        detail: "traceback elided",
        status: 500,
      })
    );
    const error = await failure(generate("http://x", { intent: "board" }));
    expect(error.kind).toBe("server");
    expect(error.errorId).toBe("abc123");
    expect(error.detail).toBe("traceback elided");
  });

  it("classifies an unexpected non-5xx failure status as server rather than guessing", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(418, { error: "teapot" }));
    const error = await failure(generate("http://x", { intent: "board" }));
    expect(error.kind).toBe("server");
    expect(error.status).toBe(418);
  });

  it("still classifies when the error body is not JSON", async () => {
    mockFetch.mockResolvedValueOnce(new Response("<html>bad gateway</html>", { status: 502 }));
    const error = await failure(generate("http://x", { intent: "board" }));
    expect(error.kind).toBe("upstream");
    expect(error.message).toBe("The engine answered 502.");
  });
});

/* ---------------------------------------------------------- generateStream */

describe("generateStream", () => {
  const request = { intent: "a 3.3V LDO board" };
  const done = { event: "run.done", t_s: 4.2, result: { status: "FEASIBLE" } };

  it("parses frames, calls onFrame in order, and resolves with run.done's result", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        line({ event: "run.accepted", t_s: 0 }),
        line({ event: "stage.start", stage: "propose", t_s: 0.1 }),
        line(done),
      ])
    );
    const seen: string[] = [];
    const result = await generateStream("http://x", request, (f) =>
      seen.push(f.event)
    );
    expect(seen).toEqual(["run.accepted", "stage.start", "run.done"]);
    expect(result).toEqual({ status: "FEASIBLE" });
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("reassembles a frame split mid-JSON across reads", async () => {
    const whole = line({ event: "stage.done", stage: "place", t_s: 2 });
    const cut = Math.floor(whole.length / 2);
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        line({ event: "run.accepted" }),
        whole.slice(0, cut),
        whole.slice(cut),
        line(done),
      ])
    );
    const seen: StreamFrame[] = [];
    await generateStream("http://x", request, (f) => seen.push(f));
    expect(seen.map((f) => f.event)).toEqual([
      "run.accepted",
      "stage.done",
      "run.done",
    ]);
    expect(seen[1].stage).toBe("place");
  });

  it("survives a split landing inside a multi-byte scenario: one byte at a time", async () => {
    // The decoder is created with {stream: true}; feeding every byte separately
    // is the harshest chunking a transport can produce.
    const text =
      line({ event: "run.accepted" }) +
      line({ event: "model.call", stage: "propose", ok: true }) +
      line(done);
    const chunks = Array.from(text, (ch) => ch);
    mockFetch.mockResolvedValueOnce(streamResponse(chunks));
    const seen: string[] = [];
    const result = await generateStream("http://x", request, (f) => seen.push(f.event));
    expect(seen).toEqual(["run.accepted", "model.call", "run.done"]);
    expect(result).toEqual({ status: "FEASIBLE" });
  });

  it("accepts CRLF line endings", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        `${JSON.stringify({ event: "run.accepted" })}\r\n`,
        `${JSON.stringify(done)}\r\n`,
      ])
    );
    const seen: string[] = [];
    const result = await generateStream("http://x", request, (f) => seen.push(f.event));
    expect(seen).toEqual(["run.accepted", "run.done"]);
    expect(result).toEqual({ status: "FEASIBLE" });
  });

  it("processes a final line with no trailing newline", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        line({ event: "run.accepted" }),
        JSON.stringify(done), // unterminated
      ])
    );
    const result = await generateStream("http://x", request, () => {});
    expect(result).toEqual({ status: "FEASIBLE" });
  });

  it("skips malformed and blank lines without dying", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        "\n\n",
        "this is not json\n",
        line({ notAnEvent: true }),
        line({ event: 42 }),
        line({ event: "run.accepted" }),
        line(done),
      ])
    );
    const seen: string[] = [];
    await generateStream("http://x", request, (f) => seen.push(f.event));
    expect(seen).toEqual(["run.accepted", "run.done"]);
  });

  it("throws the run.error's classified failure and does NOT re-run", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        line({ event: "run.accepted" }),
        line({
          event: "run.error",
          status: 502,
          error: "GOOGLE_API_KEY is not set",
        }),
      ])
    );
    const error = await failure(generateStream("http://x", request, () => {}));
    expect(error.kind).toBe("setup");
    expect(error.status).toBe(502);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("run.error defaults to status 500 when the frame carries none", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([line({ event: "run.error", error: "it broke" })])
    );
    const error = await failure(generateStream("http://x", request, () => {}));
    expect(error.kind).toBe("server");
    expect(error.status).toBe(500);
  });

  it("still delivers frames received after run.error before throwing", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        line({ event: "run.error", status: 500, error: "boom" }),
        line({ event: "stage.done", stage: "review" }),
      ])
    );
    const seen: string[] = [];
    await failure(generateStream("http://x", request, (f) => seen.push(f.event)));
    expect(seen).toEqual(["run.error", "stage.done"]);
  });

  /* ------------------------------------------------------ addressable runs */

  it("names the run on the way out, so a lost response is still addressable", async () => {
    // The point of a client-chosen id: this client knows the run's name before
    // the engine has answered anything at all. LiteLLM accepts a caller's id
    // the same way, on `x-litellm-call-id`.
    mockFetch.mockResolvedValueOnce(streamResponse([line(done)]));
    await generateStream("http://x", request, () => {}, undefined, undefined, "run_chosen");
    const headers = (mockFetch.mock.calls[0][1] as { headers: Record<string, string> })
      .headers;
    expect(headers["X-Kaleo-Run-Id"]).toBe("run_chosen");
  });

  it("carries the run id onto the error when the stream closes unfinished", async () => {
    // This is the whole §6 gap in docs/paid-run-safety.md: the run is still
    // going and still being paid for, and until it had a name there was
    // nothing the client could do but guess.
    mockFetch.mockResolvedValueOnce(streamResponse([line({ event: "run.accepted" })]));
    const error = await failure(
      generateStream("http://x", request, () => {}, undefined, undefined, "run_mine")
    );
    expect(error.runId).toBe("run_mine");
    expect(error.message).toMatch(/run_mine/);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("prefers the engine's run id over the one this client proposed", async () => {
    // An older engine, or a proxy that dropped the request header, mints its
    // own. Polling ours would then ask about a run that does not exist.
    const body = streamResponse([line({ event: "run.accepted" })]);
    const answered = new Response(body.body, {
      status: 200,
      headers: { "X-Kaleo-Run-Id": "run_theirs" },
    });
    mockFetch.mockResolvedValueOnce(answered);
    const error = await failure(
      generateStream("http://x", request, () => {}, undefined, undefined, "run_ours")
    );
    expect(error.runId).toBe("run_theirs");
  });

  it("reads run.cancelled as cancelled, never as a server fault", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        line({ event: "run.cancelled", run_id: "run_z", detail: "stopped on request" }),
      ])
    );
    const error = await failure(generateStream("http://x", request, () => {}));
    expect(error.kind).toBe("cancelled");
    expect(error.runId).toBe("run_z");
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("mints a distinct run id per call", () => {
    expect(newRunId()).not.toBe(newRunId());
    expect(newRunId().startsWith("run_")).toBe(true);
  });

  it("a stream that closes with neither run.done nor run.error throws and never re-POSTs", async () => {
    mockFetch.mockResolvedValueOnce(
      streamResponse([
        line({ event: "run.accepted" }),
        line({ event: "stage.start", stage: "place" }),
      ])
    );
    const error = await failure(generateStream("http://x", request, () => {}));
    expect(error.kind).toBe("server");
    expect(error.message).toMatch(/closed the stream/);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("an empty 200 stream also counts as closed-before-done, one POST only", async () => {
    mockFetch.mockResolvedValueOnce(streamResponse([]));
    const error = await failure(generateStream("http://x", request, () => {}));
    expect(error.kind).toBe("server");
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("falls back to one-shot /generate on a 404 — the only status allowed a second POST", async () => {
    mockFetch.mockResolvedValueOnce(new Response("not found", { status: 404 }));
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { status: "FEASIBLE" }));
    const result = await generateStream("http://x", request, () => {});
    expect(result).toEqual({ status: "FEASIBLE" });
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(String(mockFetch.mock.calls[0][0])).toBe("http://x/generate/stream");
    expect(String(mockFetch.mock.calls[1][0])).toBe("http://x/generate");
  });

  it.each([400, 413, 500, 502, 503])(
    "a pre-stream %i answers plain JSON and triggers no second POST",
    async (status) => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse(status, { error: `refused with ${status}`, status })
      );
      const error = await failure(generateStream("http://x", request, () => {}));
      expect(error.status).toBe(status);
      expect(error.message).toBe(`refused with ${status}`);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    }
  );

  it("a 200 with no body throws server — the run already started, so no retry", async () => {
    mockFetch.mockResolvedValueOnce({
      status: 200,
      ok: true,
      body: null,
      json: async () => ({}),
    } as unknown as Response);
    const error = await failure(generateStream("http://x", request, () => {}));
    expect(error.kind).toBe("server");
    expect(error.message).toMatch(/no stream/);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("a rejected fetch surfaces as offline with the cause in detail", async () => {
    mockFetch.mockRejectedValueOnce(new TypeError("Load failed"));
    const error = await failure(generateStream("http://x", request, () => {}));
    expect(error.kind).toBe("offline");
    expect(error.detail).toBe("Load failed");
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("POSTs the normalized request, not the raw draft", async () => {
    mockFetch.mockResolvedValueOnce(streamResponse([line(done)]));
    await generateStream(
      "http://x",
      {
        intent: "  board  ",
        datasheets: { "": "https://x/orphan.pdf" },
        time_limit_s: 9000,
        ground: false,
      },
      () => {}
    );
    const sent = JSON.parse(String(mockFetch.mock.calls[0][1]?.body));
    expect(sent).toEqual({
      intent: "board",
      datasheets: {},
      time_limit_s: MAX_TIME_LIMIT_S,
      review: true,
    });
  });

  it("a run.done with no result field resolves to an empty result, not a crash", async () => {
    mockFetch.mockResolvedValueOnce(streamResponse([line({ event: "run.done" })]));
    const result = await generateStream("http://x", request, () => {});
    expect(result).toEqual({});
  });

  // Greptile P1 on #22. The engine had already sent the board; only the
  // transport died afterwards. Throwing here reported a finished run as
  // failed and dropped the board from history, which is the user's one copy.
  it("keeps a terminal run.done when the stream errors after it", async () => {
    const encoder = new TextEncoder();
    const done = { event: "run.done", result: { status: "FEASIBLE" }, t_s: 9 };
    // Delivered on pull, not enqueued up front: controller.error() discards a
    // queue, so an eager enqueue would never hand run.done to the consumer and
    // the test would pass for the wrong reason.
    const chunks = [line({ event: "run.accepted", t_s: 0 }), line(done)];
    let sent = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent < chunks.length) {
          controller.enqueue(encoder.encode(chunks[sent++]));
          return;
        }
        controller.error(new Error("connection reset"));
      },
    });
    mockFetch.mockResolvedValueOnce(new Response(stream, { status: 200 }));

    const result = await generateStream("http://x", request, () => {});
    expect(result).toEqual({ status: "FEASIBLE" });
  });

  it("still reports a stream error that arrives before any terminal frame", async () => {
    const encoder = new TextEncoder();
    let delivered = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!delivered) {
          delivered = true;
          controller.enqueue(encoder.encode(line({ event: "run.accepted", t_s: 0 })));
          return;
        }
        controller.error(new Error("connection reset"));
      },
    });
    mockFetch.mockResolvedValueOnce(new Response(stream, { status: 200 }));

    await expect(generateStream("http://x", request, () => {})).rejects.toThrow();
  });

});

/* ------------------------------------------------------------------ health */

describe("health", () => {
  it("returns ok only for ok:true", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    expect(await health("http://x")).toEqual({ ok: true, detail: "" });
  });

  it("a 200 without ok:true is not healthy", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { status: "fine" }));
    const result = await health("http://x");
    expect(result.ok).toBe(false);
    expect(result.detail).toMatch(/without ok:true/);
  });

  it("a non-200 reports the status instead of throwing", async () => {
    mockFetch.mockResolvedValueOnce(new Response("", { status: 503 }));
    expect(await health("http://x")).toEqual({ ok: false, detail: "answered 503" });
  });

  it("an unreachable engine reports the reason instead of throwing", async () => {
    mockFetch.mockRejectedValueOnce(new Error("connection refused"));
    expect(await health("http://x")).toEqual({
      ok: false,
      detail: "connection refused",
    });
  });
});

/* -------------------------------------------------------------- parseFrame */

describe("parseFrame", () => {
  it("parses a valid frame and trims whitespace/CR", () => {
    expect(parseFrame('  {"event":"run.accepted","t_s":0}\r')).toEqual({
      event: "run.accepted",
      t_s: 0,
    });
  });

  it("returns null for blanks, non-JSON, non-objects, and missing event", () => {
    expect(parseFrame("")).toBeNull();
    expect(parseFrame("   \r")).toBeNull();
    expect(parseFrame("not json")).toBeNull();
    expect(parseFrame("42")).toBeNull();
    expect(parseFrame("null")).toBeNull();
    expect(parseFrame('"event"')).toBeNull();
    expect(parseFrame("[1,2]")).toBeNull();
    expect(parseFrame('{"t_s":1}')).toBeNull();
    expect(parseFrame('{"event":7}')).toBeNull();
  });
});

/* -------------------------------------------------------------------- auth */

describe("bearer token", () => {
  it("authHeaders sends nothing for an absent, empty, or whitespace token", () => {
    expect(authHeaders()).toEqual({});
    expect(authHeaders("")).toEqual({});
    expect(authHeaders("   ")).toEqual({});
  });

  it("authHeaders builds the trimmed Authorization header", () => {
    expect(authHeaders(" tok-1 ")).toEqual({ Authorization: "Bearer tok-1" });
  });

  it("generate carries the token; a 401 is kind auth", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { error: "unauthorized" }));
    const error = await failure(
      generate("http://x", { intent: "a board" }, undefined, "tok-1")
    );
    expect(error.kind).toBe("auth");
    expect(error.status).toBe(401);
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer tok-1"
    );
  });

  it("no token configured means no Authorization header at all", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { status: "feasible" }));
    await generate("http://x", { intent: "a board" });
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect(init.headers as Record<string, string>).not.toHaveProperty(
      "Authorization"
    );
  });

  it("the stream carries the token, and the 404 fallback re-sends it", async () => {
    mockFetch
      .mockResolvedValueOnce(jsonResponse(404, { error: "not found" }))
      .mockResolvedValueOnce(jsonResponse(200, { status: "feasible" }));
    const frames: StreamFrame[] = [];
    const result = await generateStream(
      "http://x",
      { intent: "a board" },
      (frame) => frames.push(frame),
      undefined,
      "tok-2"
    );
    expect(result.status).toBe("feasible");
    expect(mockFetch).toHaveBeenCalledTimes(2);
    for (const call of mockFetch.mock.calls) {
      const init = call[1] as RequestInit;
      expect((init.headers as Record<string, string>).Authorization).toBe(
        "Bearer tok-2"
      );
    }
  });

  it("a 401 on the stream route is kind auth and starts nothing else", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { error: "unauthorized" }));
    const error = await failure(
      generateStream("http://x", { intent: "a board" }, () => {}, undefined, "bad")
    );
    expect(error.kind).toBe("auth");
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("health passes the token through", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    await health("http://x", "tok-3");
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer tok-3"
    );
  });
});

/* -------------------------------------------------------------- step runs */

describe("startSteps", () => {
  it("posts the summary mode alongside the step-run fields", async () => {
    // The step flow is the same run under approval gates, so it is asked for
    // the same summary; `startSteps` forwards whatever normalization kept.
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, { session: "s1", step: "propose", next: [] })
    );
    await startSteps("http://x", {
      intent: "a 3.3V LDO board",
      summary: "structured",
      kicad_live: true,
    });
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    const body = JSON.parse(String(init.body));
    expect(body.summary).toBe("structured");
    expect(body.kicad_live).toBe(true);
  });

  it("sends an Idempotency-Key only when one was given", async () => {
    // The header the engine dedupes a start by (service/steps.py::start_once).
    // Absent unless a caller supplies one, so nothing older changes shape.
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, { session: "s1", step: "propose", next: [] })
    );
    await startSteps("http://x", { intent: "a 3.3V LDO board" }, undefined, "", "press-1");
    const withKey = (mockFetch.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(withKey["Idempotency-Key"]).toBe("press-1");

    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, { session: "s2", step: "propose", next: [] })
    );
    await startSteps("http://x", { intent: "a 3.3V LDO board" });
    const without = (mockFetch.mock.calls[1][1] as RequestInit).headers as Record<string, string>;
    expect("Idempotency-Key" in without).toBe(false);
  });
});

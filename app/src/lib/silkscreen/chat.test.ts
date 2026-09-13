import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: (...a: unknown[]) => fetchMock(...a) }));

const { ask } = await import("./chat");
const { SilkscreenError } = await import("./client");

/** An NDJSON body, delivered in whatever chunks the test asks for. */
function stream(chunks: string[], status = 200) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok: status < 400,
    status,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { done: false, value: encoder.encode(chunks[i++]) }
            : { done: true, value: undefined },
      }),
    },
  };
}

const line = (frame: Record<string, unknown>) => `${JSON.stringify(frame)}\n`;

beforeEach(() => fetchMock.mockReset());

describe("asking Ada from the terminal", () => {
  it("returns her reply", async () => {
    fetchMock.mockResolvedValue(
      stream([line({ event: "chat.done", assistant: "Because U3 has no footprint." })]),
    );
    const outcome = await ask("Why did that fail?", { baseUrl: "http://127.0.0.1:8081" });
    expect(outcome.assistant).toBe("Because U3 has no footprint.");
    expect(outcome.ranBoard).toBe(false);
  });

  it("reports that a board ran, so a paid run is never a silent slow reply", async () => {
    // The hazard this module exists for: /chat/stream hands the orchestrator
    // a `generate` callable, so a typed sentence can become a paid pipeline.
    fetchMock.mockResolvedValue(
      stream([
        line({ event: "chat.accepted" }),
        line({ event: "stage.start", stage: "schematic" }),
        line({ event: "chat.done", assistant: "Done.", result: { board: {} } }),
      ]),
    );
    const outcome = await ask("Make me a buck converter", { baseUrl: "http://x" });
    expect(outcome.ranBoard).toBe(true);
  });

  it("counts a result as a run even with no stage frame seen", async () => {
    fetchMock.mockResolvedValue(
      stream([line({ event: "chat.done", assistant: "", result: { board: {} } })]),
    );
    expect((await ask("go", { baseUrl: "http://x" })).ranBoard).toBe(true);
  });

  it("streams a line per frame instead of going quiet", async () => {
    const lines: string[] = [];
    fetchMock.mockResolvedValue(
      stream([
        line({ event: "run.accepted" }),
        line({ event: "chat.done", assistant: "ok" }),
      ]),
    );
    await ask("Hi", { baseUrl: "http://x", onLine: (l) => lines.push(l) });
    expect(lines.length).toBeGreaterThan(0);
  });

  it("survives a frame split across two chunks", async () => {
    const whole = line({ event: "chat.done", assistant: "split fine" });
    fetchMock.mockResolvedValue(stream([whole.slice(0, 12), whole.slice(12)]));
    expect((await ask("Hi", { baseUrl: "http://x" })).assistant).toBe("split fine");
  });

  it("skips a malformed frame rather than failing the turn", async () => {
    fetchMock.mockResolvedValue(
      stream(["{not json\n", line({ event: "chat.done", assistant: "still here" })]),
    );
    expect((await ask("Hi", { baseUrl: "http://x" })).assistant).toBe("still here");
  });

  it("raises rather than printing an empty line when the engine is unreachable", async () => {
    fetchMock.mockImplementationOnce(() => {
      throw new Error("connection refused");
    });
    let thrown: unknown;
    try {
      await ask("Hi", { baseUrl: "http://x" });
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(SilkscreenError);
    expect((thrown as Error).message).toMatch(/Could not reach/);
  });

  it("names a build with no chat route, instead of a bare 404", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, body: null });
    await expect(ask("Hi", { baseUrl: "http://x" })).rejects.toThrow(/chat\/stream/);
  });

  it("surfaces an error frame", async () => {
    fetchMock.mockResolvedValue(
      stream([line({ event: "chat.error", error: "quota exhausted", status: 429 })]),
    );
    await expect(ask("Hi", { baseUrl: "http://x" })).rejects.toThrow(/quota exhausted/);
  });

  it("refuses a stream that ends before she answers, rather than inventing one", async () => {
    // Something happened on the engine; claiming success would hide it and
    // returning empty would invite a silent retry of a paid turn.
    fetchMock.mockResolvedValue(stream([line({ event: "chat.accepted" })]));
    await expect(ask("Hi", { baseUrl: "http://x" })).rejects.toThrow(/before Hardy answered/);
  });

  it("refuses an empty question without touching the network", async () => {
    await expect(ask("   ", { baseUrl: "http://x" })).rejects.toThrow(/Nothing was asked/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends the question as the intent, on the chat route", async () => {
    fetchMock.mockResolvedValue(stream([line({ event: "chat.done", assistant: "ok" })]));
    await ask("Why?", { baseUrl: "http://127.0.0.1:8081", sessionId: "term-abc" });
    const [url, init] = fetchMock.mock.calls[0] as [string, { body: string }];
    expect(url).toBe("http://127.0.0.1:8081/chat/stream");
    expect(JSON.parse(init.body)).toMatchObject({ intent: "Why?", session_id: "term-abc" });
  });
});

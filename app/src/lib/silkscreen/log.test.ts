import { beforeEach, describe, expect, it } from "vitest";

import {
  LOG_CAPACITY,
  MAX_ENTRY_BYTES,
  MAX_EXPORT_BYTES,
  clearLog,
  getSnapshot,
  logError,
  logEvent,
  logServer,
  record,
  scrubText,
  subscribe,
  toNdjson,
  toText,
  type LogEntry,
} from "./log";

beforeEach(() => {
  clearLog();
});

function lastData(): Record<string, unknown> {
  const { entries } = getSnapshot();
  return entries[entries.length - 1].data as Record<string, unknown>;
}

describe("ring bound", () => {
  it("drops the oldest past capacity and counts them", () => {
    for (let i = 0; i < LOG_CAPACITY + 7; i++) logEvent("t", `line ${i}`);
    const { entries, dropped } = getSnapshot();
    expect(entries.length).toBe(LOG_CAPACITY);
    expect(dropped).toBe(7);
    // Oldest gone, newest kept.
    expect(entries[0].msg).toBe("line 7");
    expect(entries[entries.length - 1].msg).toBe(`line ${LOG_CAPACITY + 6}`);
  });

  it("clear empties the buffer and resets dropped", () => {
    for (let i = 0; i < LOG_CAPACITY + 1; i++) logEvent("t", "x");
    clearLog();
    expect(getSnapshot()).toEqual({ entries: [], dropped: 0 });
  });
});

describe("key-based redaction (layer 1)", () => {
  it("drops secret-named keys wherever they sit", () => {
    logEvent("t", "m", {
      api_key: "sk-live-1234",
      nested: { access_token: "abc", ok: 1 },
      Authorization: "Bearer zzz",
    });
    const data = lastData();
    expect(data.api_key).toBe("[redacted]");
    expect((data.nested as Record<string, unknown>).access_token).toBe(
      "[redacted]",
    );
    expect((data.nested as Record<string, unknown>).ok).toBe(1);
    expect(data.Authorization).toBe("[redacted]");
    expect(JSON.stringify(data)).not.toContain("sk-live-1234");
  });

  it("replaces kicad_pcb with its length instead of the board text", () => {
    logServer("run.done", "done", { result: { kicad_pcb: "x".repeat(5000) } });
    const data = lastData();
    expect((data.result as Record<string, unknown>).kicad_pcb).toBe(
      "[kicad_pcb: 5000 chars]",
    );
  });
});

describe("scrubText (layer 2)", () => {
  it("scrubs credential query params from a URL, keeping the rest readable", () => {
    const url =
      "https://ds.example/ldo.pdf?page=3&X-Amz-Signature=SECRETSIG&x=1";
    const out = scrubText(url);
    expect(out).not.toContain("SECRETSIG");
    expect(out).toContain("X-Amz-Signature=[redacted]");
    expect(out).toContain("page=3");
    expect(out).toContain("x=1");
  });

  it("scrubs fragment tokens and bearer values", () => {
    expect(scrubText("https://a/#access_token=tok123")).not.toContain("tok123");
    expect(scrubText("sent Authorization: Bearer abc.def-ghi")).toBe(
      "sent Authorization: Bearer [redacted]",
    );
  });

  it("is applied to every stored string: msg and walked data alike", () => {
    logError("net.fail", "request with Bearer topsecret failed", {
      detail: "retry https://x/y?api_key=k123 later",
    });
    const { entries } = getSnapshot();
    const entry = entries[entries.length - 1];
    expect(entry.msg).toContain("Bearer [redacted]");
    expect(entry.msg).not.toContain("topsecret");
    expect((entry.data as Record<string, unknown>).detail).toContain(
      "api_key=[redacted]",
    );
    expect(JSON.stringify(entry.data)).not.toContain("k123");
  });
});

describe("record hardening", () => {
  it("never throws on circular or hostile data", () => {
    const cyc: Record<string, unknown> = {};
    cyc.self = cyc;
    expect(() => record({ msg: "cyc", data: cyc })).not.toThrow();
    expect((lastData() as { self: unknown }).self).toBe("[Circular]");
    expect(() =>
      record({
        msg: "getter",
        data: {
          get boom(): never {
            throw new Error("no");
          },
        },
      }),
    ).not.toThrow();
  });

  it("truncates an oversized entry and records the original size", () => {
    logEvent("t", "wide", {
      rows: Array.from({ length: 40 }, (_, i) => ({
        // Under the per-value cap so only the entry-level backstop can fire.
        text: `row ${i} ${"y".repeat(1000)}`,
      })),
    });
    const data = lastData();
    expect(data.truncated).toBe(true);
    expect(data.original_bytes as number).toBeGreaterThan(MAX_ENTRY_BYTES);
    expect(typeof data.preview).toBe("string");
  });
});

describe("exports", () => {
  it("NDJSON round-trips entries after a meta line and never leaks bytes", () => {
    logEvent("run.start", "started", { intent: "an LDO board" });
    const lines = toNdjson().trimEnd().split("\n");
    expect(lines.length).toBe(2);
    const meta = JSON.parse(lines[0]);
    expect(meta.app).toBe("kaleo");
    expect(meta.capacity).toBe(LOG_CAPACITY);
    const row = JSON.parse(lines[1]);
    expect(row.event).toBe("run.start");
    expect(row.bytes).toBeUndefined();
  });

  it("text export carries counts in the header", () => {
    logEvent("a", "one");
    logError("b", "two");
    const text = toText();
    expect(text).toContain("# kaleo log");
    expect(text).toContain("2 entries");
    expect(text).toContain("1 error");
  });

  it("says so when the export had to omit entries to fit the budget", () => {
    // Synthesized rows, passed explicitly: filling the live buffer to 2 MB
    // through record() would dominate the test run for no extra coverage.
    const big = "z".repeat(4000);
    const rows: LogEntry[] = Array.from({ length: 600 }, (_, i) => ({
      seq: i + 1,
      t: 0,
      level: "info",
      src: "app",
      event: "bulk",
      msg: big,
      data: null,
    }));
    const nd = toNdjson(rows);
    expect(nd.length).toBeLessThan(MAX_EXPORT_BYTES + 4096);
    const meta = JSON.parse(nd.slice(0, nd.indexOf("\n")));
    expect(meta.truncated).toBe(true);
    expect(meta.omitted_entries).toBeGreaterThan(0);
    // The newest lines are the ones kept.
    expect(nd.trimEnd().endsWith(`"seq":600,"data":null}`)).toBe(true);

    const text = toText(rows);
    expect(text).toContain("# truncated: first");

    // A small export must NOT claim truncation.
    expect(JSON.parse(toNdjson(rows.slice(0, 2)).split("\n")[0]).truncated)
      .toBeUndefined();
    expect(toText(rows.slice(0, 2))).not.toContain("# truncated");
  });
});

describe("subscribe", () => {
  it("notifies once per microtask batch and stops after unsubscribe", async () => {
    let calls = 0;
    const off = subscribe(() => calls++);
    logEvent("a", "1");
    logEvent("a", "2");
    expect(calls).toBe(0); // batched — not synchronous
    await Promise.resolve();
    expect(calls).toBe(1);
    const seen = getSnapshot();
    expect(seen.entries.length).toBe(2);
    // Snapshot identity is stable until the buffer changes again.
    expect(getSnapshot()).toBe(seen);
    off();
    logEvent("a", "3");
    await Promise.resolve();
    expect(calls).toBe(1);
  });
});

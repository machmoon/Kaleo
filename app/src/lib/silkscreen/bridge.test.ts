// Tests for the cross-window run hand-off.
//
// The suite runs under Node, which has neither `window` nor `localStorage`;
// both are stubbed per test and removed after, because the bridge (via
// `safeLocalStorage`) must also degrade to a clean no-op when storage is
// missing entirely — that case is asserted, not worked around.

import { afterEach, describe, expect, it, vi } from "vitest";

import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import type { RunHistoryEntry } from "@/hooks/useSilkscreenRun";
import {
  MAX_PUBLISHED_CHARS,
  publishRun,
  readPublishedRun,
  subscribePublishedRun,
} from "./bridge";

type Store = Map<string, string>;

function stubStorage(): Store {
  const store: Store = new Map();
  const localStorage = {
    getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
  const listeners = new Set<(event: unknown) => void>();
  (globalThis as Record<string, unknown>).window = {
    localStorage,
    addEventListener: (_type: string, fn: (event: unknown) => void) =>
      listeners.add(fn),
    removeEventListener: (_type: string, fn: (event: unknown) => void) =>
      listeners.delete(fn),
    __emit: (event: unknown) => listeners.forEach((fn) => fn(event)),
  };
  (globalThis as Record<string, unknown>).localStorage = localStorage;
  return store;
}

afterEach(() => {
  delete (globalThis as Record<string, unknown>).window;
  delete (globalThis as Record<string, unknown>).localStorage;
});

function entry(overrides: Partial<RunHistoryEntry> = {}): RunHistoryEntry {
  return {
    id: "run-1",
    intent: "a 3.3 V LDO board",
    at: 1_000,
    request: {
      intent: "a 3.3 V LDO board",
      datasheets: {},
      time_limit_s: 20,
      review: true,
      ground: false,
      debug: false,
    },
    result: { intent: "a 3.3 V LDO board" } as RunHistoryEntry["result"],
    frames: [{ event: "run.done" } as RunHistoryEntry["frames"][number]],
    progress: { stages: [], feed: [], status: "done" } as unknown as RunHistoryEntry["progress"],
    startedAt: 0,
    finishedAt: 1_000,
    elapsedS: 1,
    ...overrides,
  };
}

describe("publishRun / readPublishedRun", () => {
  it("round-trips an entry, dropping the frame log", () => {
    stubStorage();
    publishRun(entry());
    const read = readPublishedRun();
    expect(read).not.toBeNull();
    expect(read!.id).toBe("run-1");
    expect(read!.result).toEqual({ intent: "a 3.3 V LDO board" });
    // Frames deliberately do not cross the boundary.
    expect(read!.frames).toEqual([]);
  });

  it("returns null for junk, half-shaped, or absent values", () => {
    const store = stubStorage();
    expect(readPublishedRun()).toBeNull();
    store.set(KALEO_STORAGE_KEYS.LAST_RUN, "not json {");
    expect(readPublishedRun()).toBeNull();
    store.set(KALEO_STORAGE_KEYS.LAST_RUN, JSON.stringify({ id: "x" }));
    expect(readPublishedRun()).toBeNull();
  });

  it("republishing the same id replaces what the other window will read", () => {
    stubStorage();
    publishRun(entry({ at: 1_000, result: { kicad_pcb: "(placed)" } as RunHistoryEntry["result"] }));
    publishRun(
      entry({ at: 2_000, result: { kicad_pcb: "(routed)" } as RunHistoryEntry["result"] })
    );
    const read = readPublishedRun()!;
    expect(read.id).toBe("run-1");
    expect(read.at).toBe(2_000);
    expect(read.result).toEqual({ kicad_pcb: "(routed)" });
  });

  it("drops a payload too big for storage whole, and says so", () => {
    const store = stubStorage();
    publishRun(entry());
    const small = store.get(KALEO_STORAGE_KEYS.LAST_RUN);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    publishRun(
      entry({
        result: { kicad_pcb: "x".repeat(MAX_PUBLISHED_CHARS + 1) } as RunHistoryEntry["result"],
      })
    );
    // Not truncated, not half-written: the previous announcement still stands.
    expect(store.get(KALEO_STORAGE_KEYS.LAST_RUN)).toBe(small);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("is a silent no-op without storage (no window at all)", () => {
    expect(() => publishRun(entry())).not.toThrow();
    expect(readPublishedRun()).toBeNull();
  });
});

describe("subscribePublishedRun", () => {
  it("fires only for the bridge key, and unsubscribes cleanly", () => {
    const store = stubStorage();
    const onRun = vi.fn();
    const unsubscribe = subscribePublishedRun(onRun);

    const emit = (key: string) =>
      (window as unknown as { __emit: (e: unknown) => void }).__emit({ key });

    emit("some_other_key");
    expect(onRun).not.toHaveBeenCalled();

    store.set(
      KALEO_STORAGE_KEYS.LAST_RUN,
      JSON.stringify((({ frames: _f, ...rest }) => rest)(entry()))
    );
    emit(KALEO_STORAGE_KEYS.LAST_RUN);
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0][0].id).toBe("run-1");

    unsubscribe();
    emit(KALEO_STORAGE_KEYS.LAST_RUN);
    expect(onRun).toHaveBeenCalledTimes(1);
  });
});

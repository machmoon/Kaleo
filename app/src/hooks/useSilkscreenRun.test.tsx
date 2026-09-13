// @vitest-environment jsdom
//
// The run state machine, driven end to end with a mocked client. The client's
// own behaviour (frame parsing, fallbacks, error kinds) is covered in
// `client.test.ts`; here `generateStream` is a hand-cranked promise so each
// test controls exactly when frames arrive and how the run ends. The money
// rule is asserted directly: one `start()` is at most one client call.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

vi.mock("@/hooks/useEngineHealth", () => ({
  useEngineHealth: () => ({
    baseUrl: "http://mock",
    ok: true,
    detail: "",
    checking: false,
    lastCheckedAt: null,
    recheck: () => {},
  }),
}));

vi.mock("@/lib/silkscreen/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/silkscreen/client")>();
  return { ...actual, generateStream: vi.fn() };
});

import { SilkscreenError, generateStream } from "@/lib/silkscreen/client";
import type { GenerateRequest, RunResult, StreamFrame } from "@/lib/silkscreen/types";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import { useSilkscreenRunState } from "./useSilkscreenRun";

const mockGenerateStream = vi.mocked(generateStream);

interface Driver {
  emit: (frame: StreamFrame) => void;
  resolve: (result: RunResult) => void;
  reject: (error: unknown) => void;
  request: () => GenerateRequest;
  signal: () => AbortSignal | undefined;
}

/** Arm the mock for the NEXT call and hand back the crank. */
function arm(): Driver {
  let onFrame: ((f: StreamFrame) => void) | null = null;
  let resolvePromise: ((r: RunResult) => void) | null = null;
  let rejectPromise: ((e: unknown) => void) | null = null;
  let capturedRequest: GenerateRequest | null = null;
  let capturedSignal: AbortSignal | undefined;
  mockGenerateStream.mockImplementationOnce(
    (_base, request, frameCb, signal) =>
      new Promise<RunResult>((res, rej) => {
        capturedRequest = request;
        capturedSignal = signal;
        onFrame = frameCb;
        resolvePromise = res;
        rejectPromise = rej;
      })
  );
  return {
    emit: (frame) => onFrame?.(frame),
    resolve: (result) => resolvePromise?.(result),
    reject: (error) => rejectPromise?.(error),
    request: () => {
      if (!capturedRequest) throw new Error("generateStream was never called");
      return capturedRequest;
    },
    signal: () => capturedSignal,
  };
}

function render(options?: Parameters<typeof useSilkscreenRunState>[0]) {
  return renderHook(() => useSilkscreenRunState(options));
}

async function finishRun(
  hook: ReturnType<typeof render>,
  driver: Driver,
  result: RunResult = { status: "FEASIBLE" }
) {
  act(() => {
    driver.emit({ event: "run.accepted", t_s: 0 });
    driver.emit({ event: "run.done", t_s: 1, result });
    driver.resolve(result);
  });
  await waitFor(() => expect(hook.result.current.status).toBe("done"));
}

beforeEach(() => {
  mockGenerateStream.mockReset();
  // The storage bridge publishes every finished run; without this, a run from
  // one test would be adopted into the next test's fresh hook on mount.
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the cross-window hand-off", () => {
  // The dashboard's only source of a board. A step run (the app's default
  // mode) publishes from `place` onward under ONE id per engine session, so
  // adoption has to grow that entry rather than collect copies of it.
  const published = (at: number, result: RunResult) =>
    JSON.stringify({
      id: "steps-s1",
      intent: "a toy car",
      at,
      request: {
        intent: "a toy car",
        datasheets: {},
        time_limit_s: 20,
        review: false,
        ground: false,
        debug: false,
      },
      result,
      progress: { stages: [], feed: [], status: "done" },
      startedAt: 0,
      finishedAt: at,
      elapsedS: 1,
    });

  const announce = () =>
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: KALEO_STORAGE_KEYS.LAST_RUN })
      );
    });

  it("adopts a run published before this window existed", () => {
    localStorage.setItem(KALEO_STORAGE_KEYS.LAST_RUN, published(1_000, { kicad_pcb: "(placed)" }));
    const hook = render();
    expect(hook.result.current.history).toHaveLength(1);
    expect(hook.result.current.history[0].result.kicad_pcb).toBe("(placed)");
  });

  it("a later step replaces the entry it belongs to instead of adding a second one", () => {
    localStorage.setItem(KALEO_STORAGE_KEYS.LAST_RUN, published(1_000, { kicad_pcb: "(placed)" }));
    const hook = render();
    localStorage.setItem(KALEO_STORAGE_KEYS.LAST_RUN, published(2_000, { kicad_pcb: "(routed)" }));
    announce();
    expect(hook.result.current.history).toHaveLength(1);
    expect(hook.result.current.history[0].result.kicad_pcb).toBe("(routed)");
    // Announced while this window sat idle, so it is also what is on screen.
    expect(hook.result.current.result?.kicad_pcb).toBe("(routed)");
  });

  it("ignores a repeat of an announcement it already has", () => {
    localStorage.setItem(KALEO_STORAGE_KEYS.LAST_RUN, published(2_000, { kicad_pcb: "(routed)" }));
    const hook = render();
    const before = hook.result.current.history[0];
    localStorage.setItem(KALEO_STORAGE_KEYS.LAST_RUN, published(1_000, { kicad_pcb: "(stale)" }));
    announce();
    expect(hook.result.current.history[0]).toBe(before);
  });
});

describe("useSilkscreenRunState", () => {
  it("runs the happy path: starting -> running on first frame -> done, with history", async () => {
    const driver = arm();
    const hook = render();

    act(() => hook.result.current.start({ intent: "a 3.3V LDO board" }));
    expect(hook.result.current.status).toBe("starting");
    expect(mockGenerateStream).toHaveBeenCalledTimes(1);

    act(() => driver.emit({ event: "run.accepted", t_s: 0 }));
    expect(hook.result.current.status).toBe("running");
    expect(hook.result.current.progress.status).toBe("accepted");

    act(() => driver.emit({ event: "stage.start", stage: "place", t_s: 0.5 }));
    expect(hook.result.current.progress.currentStage).toBe("place");
    expect(
      hook.result.current.stages.find((s) => s.id === "place")?.status
    ).toBe("running");

    const runResult: RunResult = { status: "FEASIBLE", duration_s: 1.2 };
    act(() => {
      driver.emit({ event: "stage.done", stage: "place", t_s: 1 });
      driver.emit({ event: "run.done", t_s: 1.2, result: runResult });
      driver.resolve(runResult);
    });

    await waitFor(() => expect(hook.result.current.status).toBe("done"));
    expect(hook.result.current.result).toEqual(runResult);
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.frames).toHaveLength(4);
    expect(hook.result.current.history).toHaveLength(1);
    expect(hook.result.current.history[0].intent).toBe("a 3.3V LDO board");
    expect(hook.result.current.history[0].frames).toHaveLength(4);
    expect(
      hook.result.current.stages.find((s) => s.id === "place")?.status
    ).toBe("done");
  });

  it("blocks a double start: two clicks, exactly one client call", async () => {
    const driver = arm();
    const hook = render();
    act(() => {
      hook.result.current.start({ intent: "board" });
      hook.result.current.start({ intent: "board again" });
    });
    expect(mockGenerateStream).toHaveBeenCalledTimes(1);
    expect(driver.request().intent).toBe("board");
    await finishRun(hook, driver);
  });

  it("blocks a start while a run is mid-flight, then allows one after it finishes", async () => {
    const first = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "one" }));
    act(() => hook.result.current.start({ intent: "two" }));
    expect(mockGenerateStream).toHaveBeenCalledTimes(1);
    await finishRun(hook, first);

    const second = arm();
    act(() => hook.result.current.start({ intent: "two" }));
    expect(mockGenerateStream).toHaveBeenCalledTimes(2);
    await finishRun(hook, second);
    expect(hook.result.current.history).toHaveLength(2);
  });

  it("refuses an empty intent locally: error kind 'request', zero client calls", () => {
    const hook = render();
    act(() => hook.result.current.start({ intent: "   " }));
    expect(hook.result.current.status).toBe("error");
    expect(hook.result.current.error?.kind).toBe("request");
    expect(mockGenerateStream).not.toHaveBeenCalled();
  });

  // Greptile P1 on #22. cancel() reports "cancelled" at once so the button
  // stops saying "running", but the reader takes a beat to notice the abort.
  // While the guard stayed held, the UI looked idle and accepted a new prompt
  // that start() then dropped on the floor, with no run and no error.
  it("accepts a new run submitted immediately after cancel", async () => {
    const first = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "first board" }));
    act(() => first.emit({ event: "run.accepted", t_s: 0 }));

    act(() => hook.result.current.cancel());
    expect(hook.result.current.status).toBe("cancelled");

    // The first run's reader has NOT rejected yet -- this is the whole window.
    const second = arm();
    act(() => hook.result.current.start({ intent: "second board" }));

    expect(mockGenerateStream).toHaveBeenCalledTimes(2);
    expect(second.request().intent).toBe("second board");
    expect(hook.result.current.status).toBe("starting");
    act(() => second.emit({ event: "run.accepted", t_s: 0 }));
    expect(hook.result.current.status).toBe("running");

    // The abandoned run rejecting late must not disturb the live one.
    act(() =>
      first.reject(new SilkscreenError("offline", "Could not reach the engine."))
    );
    await waitFor(() => expect(hook.result.current.status).toBe("running"));
    expect(hook.result.current.error).toBeNull();
  });

  it("cancel aborts the signal and lands on cancelled, not error", async () => {
    const driver = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "board" }));
    act(() => driver.emit({ event: "run.accepted", t_s: 0 }));

    act(() => hook.result.current.cancel());
    expect(hook.result.current.status).toBe("cancelled");
    expect(driver.signal()?.aborted).toBe(true);

    // The reader notices the abort a beat later and surfaces it as an
    // "offline" rejection; that must not overwrite the user's own cancel.
    act(() =>
      driver.reject(new SilkscreenError("offline", "Could not reach the engine."))
    );
    await waitFor(() =>
      expect(mockGenerateStream.mock.results[0]).toBeDefined()
    );
    expect(hook.result.current.status).toBe("cancelled");
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.history).toHaveLength(0);
  });

  it("frames arriving after cancel are ignored", async () => {
    const driver = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "board" }));
    act(() => driver.emit({ event: "run.accepted", t_s: 0 }));
    const framesBefore = hook.result.current.frames.length;
    act(() => hook.result.current.cancel());
    act(() => driver.emit({ event: "stage.start", stage: "route", t_s: 2 }));
    expect(hook.result.current.frames).toHaveLength(framesBefore);
  });

  it("cancel with nothing in flight is a no-op", () => {
    const hook = render();
    act(() => hook.result.current.cancel());
    expect(hook.result.current.status).toBe("idle");
  });

  it("a SilkscreenError keeps its kind; the UI switches on it", async () => {
    const driver = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "board" }));
    act(() =>
      driver.reject(
        new SilkscreenError("setup", "GOOGLE_API_KEY is not set", { status: 502 })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(hook.result.current.error?.kind).toBe("setup");
    expect(hook.result.current.history).toHaveLength(0);
  });

  it("a bare Error is classified as server; a TimeoutError as timeout", async () => {
    const first = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "board" }));
    act(() => first.reject(new Error("something exploded")));
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(hook.result.current.error?.kind).toBe("server");

    const second = arm();
    act(() => hook.result.current.start({ intent: "board" }));
    const timeout = new Error("The operation timed out");
    timeout.name = "TimeoutError";
    act(() => second.reject(timeout));
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(hook.result.current.error?.kind).toBe("timeout");
  });

  it("reset returns everything to idle and aborts anything in flight", async () => {
    const driver = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "board" }));
    act(() => driver.emit({ event: "run.accepted", t_s: 0 }));
    act(() => hook.result.current.reset());
    expect(hook.result.current.status).toBe("idle");
    expect(hook.result.current.result).toBeNull();
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.frames).toHaveLength(0);
    expect(hook.result.current.startedAt).toBeNull();
    expect(driver.signal()?.aborted).toBe(true);

    // And a fresh start works after the reset.
    const next = arm();
    act(() => hook.result.current.start({ intent: "board" }));
    expect(mockGenerateStream).toHaveBeenCalledTimes(2);
    await finishRun(hook, next);
  });

  it("selectRun refuses while a run is live, and works once it is not", async () => {
    const first = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "first board" }));
    await finishRun(hook, first, { status: "OPTIMAL" });
    const pastId = hook.result.current.history[0].id;

    const second = arm();
    act(() => hook.result.current.start({ intent: "second board" }));
    act(() => hook.result.current.selectRun(pastId));
    expect(hook.result.current.viewingId).toBeNull();
    expect(hook.result.current.viewingHistory).toBe(false);

    await finishRun(hook, second, { status: "FEASIBLE" });
    act(() => hook.result.current.selectRun(pastId));
    expect(hook.result.current.viewingHistory).toBe(true);
    expect(hook.result.current.result).toEqual({ status: "OPTIMAL" });
    expect(hook.result.current.status).toBe("done");
    expect(hook.result.current.submitted?.intent).toBe("first board");

    act(() => hook.result.current.selectRun(null));
    expect(hook.result.current.viewingHistory).toBe(false);
    expect(hook.result.current.result).toEqual({ status: "FEASIBLE" });
  });

  it("history is capped at historyLimit, newest first", async () => {
    const hook = render({ historyLimit: 2 });
    for (const intent of ["one", "two", "three"]) {
      const driver = arm();
      act(() => hook.result.current.start({ intent }));
      await finishRun(hook, driver);
    }
    expect(hook.result.current.history.map((h) => h.intent)).toEqual([
      "three",
      "two",
    ]);
  });

  it("unmount mid-run aborts the stream and a late resolution neither throws nor warns", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const driver = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "board" }));
    act(() => driver.emit({ event: "run.accepted", t_s: 0 }));

    hook.unmount();
    expect(driver.signal()?.aborted).toBe(true);

    driver.emit({ event: "stage.start", stage: "place", t_s: 1 });
    driver.resolve({ status: "FEASIBLE" });
    await Promise.resolve();
    await Promise.resolve();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("updateRequest clamps the time limit and ignores a non-numeric one", () => {
    const hook = render();
    act(() => hook.result.current.updateRequest({ time_limit_s: 9999 }));
    expect(hook.result.current.request.time_limit_s).toBe(60);
    act(() => hook.result.current.updateRequest({ time_limit_s: 1 }));
    expect(hook.result.current.request.time_limit_s).toBe(5);
    act(() => hook.result.current.updateRequest({ time_limit_s: Number.NaN }));
    expect(hook.result.current.request.time_limit_s).toBe(5);
  });

  it("removing the last datasheet also drops the ground flag", () => {
    const hook = render();
    act(() => {
      hook.result.current.setDatasheet("AMS1117", "https://x/ds.pdf");
      hook.result.current.updateRequest({ ground: true });
    });
    expect(hook.result.current.request.ground).toBe(true);
    act(() => hook.result.current.removeDatasheet("AMS1117"));
    expect(hook.result.current.request.datasheets).toEqual({});
    expect(hook.result.current.request.ground).toBe(false);
  });

  it("ground and debug reach the wire only when true", async () => {
    const driver = arm();
    const hook = render();
    act(() =>
      hook.result.current.start({ intent: "board", ground: false, debug: false })
    );
    expect("ground" in driver.request()).toBe(false);
    expect("debug" in driver.request()).toBe(false);
    await finishRun(hook, driver);

    const next = arm();
    act(() =>
      hook.result.current.start({ intent: "board", ground: true, debug: true })
    );
    expect(next.request().ground).toBe(true);
    expect(next.request().debug).toBe(true);
    await finishRun(hook, next);
  });

  it("carries the summary mode into the request, and prose by default", async () => {
    // The whole point of the control: the mode the page folded in is the mode
    // the engine is asked for. Absent, the draft's own default answers, which
    // is prose -- exactly what every run before this field produced.
    const driver = arm();
    const hook = render();
    act(() => hook.result.current.start({ intent: "board" }));
    expect(driver.request().summary).toBe("prose");
    await finishRun(hook, driver);

    const next = arm();
    act(() =>
      hook.result.current.start({ intent: "board", summary: "structured" })
    );
    expect(next.request().summary).toBe("structured");
    await finishRun(hook, next);
  });

  it("records the mode a run was asked for, so history and a restore keep it", async () => {
    // The draft carries it like `review`: the history entry is the request as
    // submitted, and restoring that request brings the mode back with it.
    const driver = arm();
    const hook = render();
    act(() =>
      hook.result.current.start({ intent: "board", summary: "structured" })
    );
    await finishRun(hook, driver);
    expect(hook.result.current.history[0].request.summary).toBe("structured");

    act(() => hook.result.current.updateRequest({ summary: "prose" }));
    act(() => hook.result.current.restoreRequest(hook.result.current.history[0].id));
    expect(hook.result.current.request.summary).toBe("structured");
  });

  it("a retry carries the same mode: the page folds it in on every start", async () => {
    // The overlay's Retry button is the same `submit` closure as the first
    // press (RunFailure's onRetry), so the second run is asked for the mode
    // the first one was. Here that is the second `start()` after a failure.
    const first = arm();
    const hook = render();
    act(() =>
      hook.result.current.start({ intent: "board", summary: "structured" })
    );
    act(() => first.reject(new SilkscreenError("server", "boom")));
    await waitFor(() => expect(hook.result.current.status).toBe("error"));

    const second = arm();
    act(() =>
      hook.result.current.start({ intent: "board", summary: "structured" })
    );
    expect(second.request().summary).toBe("structured");
    await finishRun(hook, second);
  });

  it("canStart follows intent and business", async () => {
    const driver = arm();
    const hook = render();
    expect(hook.result.current.canStart).toBe(false);
    act(() => hook.result.current.updateRequest({ intent: "board" }));
    expect(hook.result.current.canStart).toBe(true);
    act(() => hook.result.current.start());
    expect(hook.result.current.canStart).toBe(false);
    await finishRun(hook, driver);
    expect(hook.result.current.canStart).toBe(true);
  });
});

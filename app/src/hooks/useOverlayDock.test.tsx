// @vitest-environment jsdom
//
// The dock hook, driven with a scripted window. The window API is mocked at
// the module boundary, the way the other hook tests mock `@tauri-apps/api/core`
// and `@tauri-apps/plugin-http`, so each test controls the monitor, the
// window's size and position, and when `moved` / `resized` events arrive.
// The rules asserted are the ones that make the bar not twitchy: one move per
// decision, a drag pins it, a resize is not a drag, a new run unpins it.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Point = { x: number; y: number };
type Handler<T> = (event: { payload: T }) => void;

const scripted = vi.hoisted(() => ({
  position: { x: 840, y: 54 } as Point,
  size: { width: 1200, height: 108 },
  scale: 2,
  monitor: {
    name: "built-in",
    position: { x: 0, y: 0 },
    size: { width: 2880, height: 1800 },
    workArea: { position: { x: 0, y: 50 }, size: { width: 2880, height: 1610 } },
    scaleFactor: 2,
  } as unknown as null | Record<string, unknown>,
  moved: null as Handler<Point> | null,
  resized: null as Handler<{ width: number; height: number }> | null,
  setPosition: vi.fn(async (p: Point) => {
    scripted.position = { x: p.x, y: p.y };
  }),
}));

vi.mock("@tauri-apps/api/window", () => ({
  PhysicalPosition: class {
    constructor(
      public x: number,
      public y: number
    ) {}
  },
  currentMonitor: vi.fn(async () => scripted.monitor),
  primaryMonitor: vi.fn(async () => scripted.monitor),
  getCurrentWindow: () => ({
    outerSize: async () => scripted.size,
    scaleFactor: async () => scripted.scale,
    outerPosition: async () => scripted.position,
    setPosition: scripted.setPosition,
    onMoved: async (handler: Handler<Point>) => {
      scripted.moved = handler;
      return () => {
        scripted.moved = null;
      };
    },
    onResized: async (handler: Handler<{ width: number; height: number }>) => {
      scripted.resized = handler;
      return () => {
        scripted.resized = null;
      };
    },
  }),
}));

import { BOTTOM_MARGIN, TOP_OFFSET_PX, dockOriginFor } from "@/lib/overlay-dock";
import type { StepName, StepResponse } from "@/lib/silkscreen/types";
import { DOCK_MOVE_INTERVAL_MS, dockInputFrom, useOverlayDock } from "./useOverlayDock";
import type { StepRun } from "./useStepRun";

const response = (step: StepName, shown = true): StepResponse =>
  ({ step, shown_in_kicad: shown, next: [], session: "s", events: [] }) as unknown as StepResponse;

function stepRun(over: Partial<StepRun>): StepRun {
  return {
    status: "idle",
    session: null,
    history: [],
    running: null,
    failedStep: null,
    available: [],
    error: null,
    elapsedS: 0,
    start: vi.fn(),
    approve: vi.fn(),
    cancel: vi.fn(),
    reset: vi.fn(),
    ...over,
  };
}

const idle = stepRun({});
const proposing = stepRun({ status: "running", running: "propose" });
const placedWaiting = stepRun({
  status: "waiting",
  history: [response("propose"), response("place")],
});
const reviewing = stepRun({
  status: "running",
  running: "review",
  history: [response("propose"), response("place"), response("route")],
});

const TOP: Point = { x: 840, y: TOP_OFFSET_PX };
const BOTTOM: Point = { x: 840, y: 50 + 1610 - 108 - BOTTOM_MARGIN * 2 };

/** Let the scheduled move fire and its awaits settle. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(DOCK_MOVE_INTERVAL_MS + 1);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  scripted.position = { ...TOP };
  scripted.size = { width: 1200, height: 108 };
  scripted.monitor = {
    name: "built-in",
    position: { x: 0, y: 0 },
    size: { width: 2880, height: 1800 },
    workArea: { position: { x: 0, y: 50 }, size: { width: 2880, height: 1610 } },
    scaleFactor: 2,
  };
  scripted.setPosition.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("dockInputFrom", () => {
  it("reads the last response and treats an unknown live status as in flight", () => {
    expect(dockInputFrom(placedWaiting, "idle")).toEqual({
      stepStatus: "waiting",
      runningStep: null,
      lastStep: "place",
      lastShownInKicad: true,
      stepCount: 2,
      runInFlight: false,
    });
    expect(dockInputFrom(idle, "connecting").runInFlight).toBe(true);
    expect(dockInputFrom(idle, "done").runInFlight).toBe(false);
  });
});

describe("useOverlayDock", () => {
  it("leaves a bar already at the top alone on mount", async () => {
    const { result } = renderHook(() => useOverlayDock(idle, "idle"));
    await settle();
    expect(result.current).toBe("top");
    expect(scripted.setPosition).not.toHaveBeenCalled();
  });

  it("moves to the bottom while a KiCad step runs, and back up when it waits for Send", async () => {
    const { result, rerender } = renderHook(
      ({ steps }: { steps: StepRun }) => useOverlayDock(steps, "idle"),
      { initialProps: { steps: proposing } }
    );
    await settle();
    expect(result.current).toBe("bottom");
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    expect(scripted.setPosition.mock.calls[0][0]).toMatchObject(BOTTOM);

    rerender({ steps: placedWaiting });
    await settle();
    // Waiting for approval: bar returns to the top so Send stays readable.
    expect(result.current).toBe("top");
    expect(scripted.setPosition).toHaveBeenCalledTimes(2);
    expect(scripted.setPosition.mock.calls[1][0]).toMatchObject(TOP);

    rerender({ steps: reviewing });
    await settle();
    expect(result.current).toBe("top");
    expect(scripted.setPosition).toHaveBeenCalledTimes(2);
  });

  it("never moves more than once per interval, and keeps only the last decision", async () => {
    const { rerender } = renderHook(
      ({ steps }: { steps: StepRun }) => useOverlayDock(steps, "idle"),
      { initialProps: { steps: proposing } }
    );
    // The first move fires straight away (running → bottom).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    // A decision change right behind it waits out the interval.
    rerender({ steps: reviewing });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DOCK_MOVE_INTERVAL_MS / 2);
    });
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    // Another top decision before it fires replaces it: one move, to top.
    rerender({ steps: placedWaiting });
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(2);
    expect(scripted.setPosition.mock.calls[1][0]).toMatchObject(TOP);
    // Already at top — reviewing does not move again.
    rerender({ steps: reviewing });
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(2);
  });

  it("a drag pins the bar until the next run starts", async () => {
    const { rerender } = renderHook(
      ({ steps, run }: { steps: StepRun; run: string }) => useOverlayDock(steps, run),
      { initialProps: { steps: proposing, run: "idle" } }
    );
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    // The window echoes our own move: not a drag.
    act(() => scripted.moved?.({ payload: BOTTOM }));
    await settle();
    // The engineer drags it somewhere else.
    scripted.position = { x: 10, y: 900 };
    act(() => scripted.moved?.({ payload: { x: 10, y: 900 } }));
    rerender({ steps: reviewing, run: "idle" });
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    expect(scripted.position).toEqual({ x: 10, y: 900 });
    // Approving further steps of the same run keeps the pin.
    rerender({ steps: placedWaiting, run: "idle" });
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    // A new live run releases it: the bar goes back to its dock.
    rerender({ steps: idle, run: "running" });
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(2);
    expect(scripted.setPosition.mock.calls[1][0]).toMatchObject(TOP);
  });

  it("a new step run releases the pin too", async () => {
    const { rerender } = renderHook(
      ({ steps }: { steps: StepRun }) => useOverlayDock(steps, "idle"),
      { initialProps: { steps: idle } }
    );
    await settle();
    scripted.position = { x: 10, y: 900 };
    act(() => scripted.moved?.({ payload: { x: 10, y: 900 } }));
    rerender({ steps: proposing });
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    expect(scripted.setPosition.mock.calls[0][0]).toMatchObject(BOTTOM);
  });

  it("re-docks after the window changes height, and does not mistake that for a drag", async () => {
    renderHook(() => useOverlayDock(proposing, "idle"));
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
    act(() => scripted.moved?.({ payload: BOTTOM }));
    await settle();
    // useOverlayHeight grows the bar; macOS reports that as a move too.
    scripted.size = { width: 1200, height: 400 };
    scripted.position = { x: 840, y: BOTTOM.y - 100 };
    act(() => scripted.moved?.({ payload: { x: 840, y: BOTTOM.y - 100 } }));
    act(() => scripted.resized?.({ payload: { width: 1200, height: 400 } }));
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(2);
    expect(scripted.setPosition.mock.calls[1][0]).toMatchObject({
      x: 840,
      y: 50 + 1610 - 400 - BOTTOM_MARGIN * 2,
    });
  });

  it("a pinned bar is not re-docked by a resize", async () => {
    renderHook(() => useOverlayDock(proposing, "idle"));
    await settle();
    act(() => scripted.moved?.({ payload: BOTTOM }));
    await settle();
    scripted.position = { x: 10, y: 900 };
    act(() => scripted.moved?.({ payload: { x: 10, y: 900 } }));
    await settle();
    scripted.size = { width: 1200, height: 400 };
    act(() => scripted.resized?.({ payload: { width: 1200, height: 400 } }));
    await settle();
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);
  });

  it("uses the primary monitor when there is no current one, and warns rather than throws", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { currentMonitor, primaryMonitor } = await import("@tauri-apps/api/window");
    vi.mocked(currentMonitor).mockResolvedValueOnce(null);
    renderHook(() => useOverlayDock(proposing, "idle"));
    await settle();
    expect(primaryMonitor).toHaveBeenCalled();
    expect(scripted.setPosition).toHaveBeenCalledTimes(1);

    scripted.setPosition.mockRejectedValueOnce(new Error("window.set_position not allowed"));
    const { rerender } = renderHook(
      ({ steps }: { steps: StepRun }) => useOverlayDock(steps, "idle"),
      { initialProps: { steps: proposing } }
    );
    scripted.position = { ...BOTTOM };
    rerender({ steps: reviewing });
    await settle();
    expect(warn).toHaveBeenCalledWith(
      "[kaleo overlay] dock move failed:",
      expect.objectContaining({ message: "window.set_position not allowed" })
    );
  });

  // ------------------------------------------------ the origin for a coming size
  //
  // `useOverlaySize` sends the window's new size; the dock is still the only
  // thing that knows where a window of that size belongs, so it answers here
  // and the two travel in one native call instead of two (the collapse jump).

  it("answers where a window of a coming size belongs", async () => {
    renderHook(() => useOverlayDock(idle, "idle"));
    await settle();
    // The pill is 132 logical, 264 physical on this scale-2 monitor, so its
    // top-left is (2880 - 264) / 2 — not the 600px bar's, which is the whole
    // 234px jump the atomic call exists to remove.
    expect(dockOriginFor({ width: 132, height: 58 })).toEqual({
      x: (2880 - 264) / 2,
      y: TOP_OFFSET_PX,
    });
    // …and the answer is synchronous: awaiting a monitor query would put the
    // resize a frame behind the content again.
  });

  it("answers nothing before it has read the monitor, so nothing is guessed", () => {
    renderHook(() => useOverlayDock(idle, "idle"));
    expect(dockOriginFor({ width: 132, height: 58 })).toBeNull();
  });

  it("does not mistake the move it just authorised for a drag", async () => {
    const { rerender } = renderHook(
      ({ steps }: { steps: StepRun }) => useOverlayDock(steps, "idle"),
      { initialProps: { steps: idle } }
    );
    await settle();
    const origin = dockOriginFor({ width: 132, height: 58 });
    expect(origin).not.toBeNull();
    // The atomic resize moves the window, and macOS reports that as a `moved`.
    scripted.position = { ...origin! };
    act(() => scripted.moved?.({ payload: origin! }));
    scripted.size = { width: 264, height: 116 };
    act(() => scripted.resized?.({ payload: { width: 264, height: 116 } }));
    // If that had pinned the bar, the next decision would never be applied.
    rerender({ steps: proposing });
    await settle();
    expect(scripted.setPosition).toHaveBeenCalled();
  });

  it("keeps a dragged bar's centre rather than re-docking it", async () => {
    renderHook(() => useOverlayDock(idle, "idle"));
    await settle();
    scripted.position = { x: 10, y: 900 };
    act(() => scripted.moved?.({ payload: { x: 10, y: 900 } }));
    // Pinned: the dock has no say over where it is, but narrowing 1200 → 264
    // physical must not slide it 468 px left either.
    expect(dockOriginFor({ width: 132, height: 58 })).toEqual({ x: 10 + 468, y: 900 });
  });

  it("stops answering once it unmounts", async () => {
    const { unmount } = renderHook(() => useOverlayDock(idle, "idle"));
    await settle();
    expect(dockOriginFor({ width: 132, height: 58 })).not.toBeNull();
    unmount();
    expect(dockOriginFor({ width: 132, height: 58 })).toBeNull();
  });

  it("unsubscribes on unmount", async () => {
    const { unmount } = renderHook(() => useOverlayDock(idle, "idle"));
    await settle();
    expect(scripted.moved).not.toBeNull();
    unmount();
    expect(scripted.moved).toBeNull();
    expect(scripted.resized).toBeNull();
  });
});

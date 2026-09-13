// @vitest-environment jsdom
//
// The window-sizing hook, driven with a scripted `invoke`. Every assertion
// here is a defect the overlay motion audit or the launcher research found:
// grow late and content is clipped; shrink early and a transparent strip of
// KiCad flashes through the card; resize per frame and the dock chases the
// bar; remember a failed invoke and the overlay stays clipped for the rest of
// the session; and sequence anything off the invoke promise and you have
// sequenced off a resize that has only been *queued*.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

import { setDockOriginResolver } from "@/lib/overlay-dock";
import { OVERLAY_PILL_WIDTH, type OverlayState } from "@/lib/overlay-size";
import { SHRINK_DELAY_MS, useOverlaySize } from "./useOverlaySize";

/** Every call so far, as `{ width, height }`, oldest first. */
const sent = () => {
  const calls = invoke.mock.calls as unknown as [
    string,
    { width: number; height: number },
  ][];
  return calls.map(([, args]) => ({ width: args.width, height: args.height }));
};

/** Mount already in `state`, with the launch resize forgotten — the window is
 *  created as the bar, so any other opening state is a real grow. */
const grown = (state: OverlayState) => {
  const view = render("bar");
  act(() => view.rerender({ state, content: undefined }));
  invoke.mockClear();
  return view;
};

const render = (state: OverlayState, contentHeight?: number) =>
  renderHook(
    ({ state: s, content }: { state: OverlayState; content?: number }) =>
      useOverlaySize(s, content),
    { initialProps: { state, content: contentHeight } }
  );

beforeEach(() => {
  vi.useFakeTimers();
  invoke.mockReset();
  invoke.mockImplementation(async () => undefined);
});

afterEach(() => {
  setDockOriginResolver(null);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useOverlaySize", () => {
  it("costs no resize at launch, because the window is created as the bar", () => {
    render("bar");
    expect(invoke).not.toHaveBeenCalled();
  });

  it("grows on the frame the state changes", () => {
    const view = render("bar");
    act(() => view.rerender({ state: "running", content: undefined }));
    // No timer advanced: the content is already laid out below the window's
    // bottom edge, and clipped content is not hidden content.
    expect(sent()).toEqual([{ width: 600, height: 293 }]);
  });

  it("holds a shrink for SHRINK_DELAY_MS", () => {
    const view = grown("running");
    act(() => view.rerender({ state: "bar", content: undefined }));
    expect(invoke).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS - 1));
    expect(invoke).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1));
    expect(sent()).toEqual([{ width: 600, height: 58 }]);
  });

  it("a grow arriving during the wait cancels the pending shrink", () => {
    const view = grown("running");
    act(() => view.rerender({ state: "bar", content: undefined }));
    act(() => vi.advanceTimersByTime(100));
    expect(invoke).not.toHaveBeenCalled();

    act(() => view.rerender({ state: "running-feed", content: undefined }));
    expect(sent()).toEqual([{ width: 600, height: 461 }]);

    // The shrink to 58 must never arrive late and clip the feed.
    act(() => vi.advanceTimersByTime(10_000));
    expect(sent()).toEqual([{ width: 600, height: 461 }]);
  });

  it("returning to the current size cancels the shrink outright", () => {
    // Collapse-to-empty between keystrokes: the state flickers away and back
    // inside the delay, and the window never moves.
    const view = grown("running");
    act(() => view.rerender({ state: "bar", content: undefined }));
    act(() => vi.advanceTimersByTime(100));
    act(() => view.rerender({ state: "running", content: undefined }));
    act(() => vi.advanceTimersByTime(10_000));
    expect(invoke).not.toHaveBeenCalled();
  });

  it("sends one resize per state change, not one per render", () => {
    const view = render("bar");
    act(() => view.rerender({ state: "running", content: undefined }));
    act(() => view.rerender({ state: "running", content: undefined }));
    act(() => view.rerender({ state: "running", content: undefined }));
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it("does not resize the X axis for anything but a pill↔bar change", () => {
    const view = render("bar");
    for (const state of ["running", "result", "failure", "cancelled"] as const) {
      act(() => view.rerender({ state, content: undefined }));
    }
    expect(sent().every((size) => size.width === 600)).toBe(true);
  });

  it("narrows to the pill on a delay and widens back immediately", () => {
    const view = render("bar");

    act(() => view.rerender({ state: "pill", content: undefined }));
    expect(invoke).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));
    expect(sent()).toEqual([{ width: OVERLAY_PILL_WIDTH, height: 58 }]);

    act(() => view.rerender({ state: "bar", content: undefined }));
    expect(sent()).toEqual([
      { width: OVERLAY_PILL_WIDTH, height: 58 },
      { width: 600, height: 58 },
    ]);
  });

  it("treats the listening pill as a width-only grow", () => {
    const view = render("bar");
    act(() => view.rerender({ state: "pill", content: undefined }));
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));
    invoke.mockClear();

    act(() => view.rerender({ state: "pill-listening", content: undefined }));
    expect(sent()).toEqual([{ width: 280, height: 58 }]);
  });

  it("follows the measurement for the content-driven panels only", () => {
    const view = render("bar");
    act(() => view.rerender({ state: "steps", content: 380 }));
    expect(sent()).toEqual([{ width: 600, height: 380 }]);

    act(() => view.rerender({ state: "steps", content: 460 }));
    expect(sent()).toEqual([
      { width: 600, height: 380 },
      { width: 600, height: 460 },
    ]);

    // A measurement on a fixed bar state raises it, because the card stacks:
    // an engine-down banner above a running block is taller than `running`'s
    // constant, and the page supplies a height only when it knows blocks are
    // stacked. It can only raise, never shrink (`overlay-size.test.ts`).
    invoke.mockClear();
    act(() => view.rerender({ state: "running", content: 340 }));
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));
    expect(sent()).toEqual([{ width: 600, height: 340 }]);

    // The pill is the exception: never stacked, so still strictly fixed.
    invoke.mockClear();
    act(() => view.rerender({ state: "pill", content: 999 }));
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));
    expect(sent()).toEqual([{ width: 132, height: 58 }]);
  });

  it("forgets the key when the invoke rejects, so the overlay is not clipped for the session", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    invoke.mockRejectedValueOnce(new Error("no window"));

    const view = render("bar");
    act(() => view.rerender({ state: "running", content: undefined }));
    expect(invoke).toHaveBeenCalledTimes(1);
    await act(async () => {
      await Promise.resolve();
    });
    expect(warn).toHaveBeenCalled();

    // The window never took 293, so it is still the 58px bar: going back to
    // `bar` is not a shrink and must send nothing…
    invoke.mockClear();
    act(() => view.rerender({ state: "bar", content: undefined }));
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));
    expect(invoke).not.toHaveBeenCalled();

    // …and asking for 293 again must re-send it rather than dedupe against a
    // size the window never took, which is what left the overlay clipped for
    // the rest of the session.
    act(() => view.rerender({ state: "running", content: undefined }));
    expect(sent()).toEqual([{ width: 600, height: 293 }]);
    warn.mockRestore();
  });

  it("keeps a newer size when an older invoke rejects", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    let reject: ((error: unknown) => void) | undefined;
    invoke.mockImplementationOnce(
      () =>
        new Promise<undefined>((_resolve, r) => {
          reject = r as (error: unknown) => void;
        })
    );

    const view = render("bar");
    act(() => view.rerender({ state: "running", content: undefined }));
    act(() => view.rerender({ state: "running-feed", content: undefined }));
    expect(invoke).toHaveBeenCalledTimes(2);

    act(() => reject?.(new Error("late")));
    await act(async () => {
      await Promise.resolve();
    });

    // 461 is what the window is now; the stale rejection must not roll that
    // back and re-send it.
    invoke.mockClear();
    act(() => view.rerender({ state: "running-feed", content: undefined }));
    expect(invoke).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it("never awaits the invoke promise to sequence the next resize", () => {
    // `set_window_height` resolves when the resize is *queued*, not applied —
    // in the pinned tao it is a fire-and-forget dispatch onto the main GCD
    // queue. A hook that awaited it would stall here forever.
    invoke.mockImplementation(() => new Promise<undefined>(() => {}));

    const view = render("bar");
    act(() => view.rerender({ state: "running", content: undefined }));
    act(() => view.rerender({ state: "running-feed", content: undefined }));
    act(() => view.rerender({ state: "result", content: undefined }));
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));

    expect(sent()).toEqual([
      { width: 600, height: 293 },
      { width: 600, height: 461 },
      { width: 600, height: 300 },
    ]);
  });

  // ---------------------------------------------- size and origin together
  //
  // `set_size` is anchored at the window's top-left, so narrowing the bar to
  // the pill slides it ~234 px left and the dock's separate re-centre lands up
  // to 400 ms later. The two must be one call; these pin that they are.

  it("sends the size and the origin in a single command", () => {
    setDockOriginResolver(() => ({ x: 1308, y: 54 }));
    const view = render("bar");
    act(() => view.rerender({ state: "pill", content: undefined }));
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));

    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith("set_window_frame", {
      width: OVERLAY_PILL_WIDTH,
      height: 58,
      x: 1308,
      y: 54,
    });
  });

  it("asks for the origin of the size it is about to send, not the current one", () => {
    const asked: { width: number; height: number }[] = [];
    setDockOriginResolver((size) => {
      asked.push(size);
      return { x: 0, y: 0 };
    });
    const view = render("bar");
    act(() => view.rerender({ state: "pill", content: undefined }));
    act(() => vi.advanceTimersByTime(SHRINK_DELAY_MS));
    expect(asked).toEqual([{ width: OVERLAY_PILL_WIDTH, height: 58 }]);
  });

  it("resizes without moving when the dock cannot say where, rather than twice", () => {
    const view = render("bar");
    act(() => view.rerender({ state: "running", content: undefined }));
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith("set_window_frame", {
      width: 600,
      height: 293,
      x: null,
      y: null,
    });
  });

  it("does not resize after unmount", () => {
    const view = grown("running");
    act(() => view.rerender({ state: "bar", content: undefined }));
    view.unmount();
    act(() => vi.advanceTimersByTime(10_000));
    expect(invoke).not.toHaveBeenCalled();
  });
});

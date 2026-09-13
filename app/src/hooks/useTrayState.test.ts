// @vitest-environment jsdom
//
// The tray hook against a scripted `invoke` and `listen`. What this pins: the
// menu bar is told the mic and window state on mount and on every change (and
// nothing else — no report on an unrelated re-render); a click on the tray's
// "Ada listening" item reaches the *current* handler; the listener is removed
// on unmount; and a failed report is a warning, never a throw.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.hoisted(() => vi.fn(async () => undefined));
const unlisten = vi.hoisted(() => vi.fn());
const handlers = vi.hoisted(() => new Map<string, () => void>());
const listen = vi.hoisted(() =>
  vi.fn(async (event: string, handler: () => void) => {
    handlers.set(event, handler);
    return unlisten;
  })
);
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen }));

import { TRAY_TOGGLE_EVENT, useTrayState } from "./useTrayState";

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

const reports = () =>
  (invoke.mock.calls as unknown as [string, { listening: boolean; visible: boolean }][])
    .filter(([command]) => command === "tray_set_state")
    .map(([, payload]) => payload);

beforeEach(() => {
  invoke.mockClear();
  unlisten.mockClear();
  listen.mockClear();
  handlers.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useTrayState", () => {
  it("reports the mic and window state on mount and on change only", () => {
    const { rerender } = renderHook(
      (props: { listening: boolean; visible: boolean }) =>
        useTrayState({ ...props, onToggleListening: () => undefined }),
      { initialProps: { listening: false, visible: true } }
    );
    expect(reports()).toEqual([{ listening: false, visible: true }]);

    // Same facts, new render: the menu bar is not told again.
    rerender({ listening: false, visible: true });
    expect(reports()).toHaveLength(1);

    rerender({ listening: true, visible: true });
    rerender({ listening: true, visible: false });
    expect(reports()).toEqual([
      { listening: false, visible: true },
      { listening: true, visible: true },
      { listening: true, visible: false },
    ]);
  });

  it("listens for the tray's toggle and calls the current handler", async () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(
      ({ onToggleListening }: { onToggleListening: () => void }) =>
        useTrayState({ listening: false, visible: true, onToggleListening }),
      { initialProps: { onToggleListening: first } }
    );
    await flush();
    expect(listen).toHaveBeenCalledTimes(1);
    expect(listen.mock.calls[0][0]).toBe(TRAY_TOGGLE_EVENT);
    expect(TRAY_TOGGLE_EVENT).toBe("tray-ada-toggle");

    handlers.get(TRAY_TOGGLE_EVENT)?.();
    expect(first).toHaveBeenCalledTimes(1);

    // A new handler, no new listener: the click reaches the latest one.
    rerender({ onToggleListening: second });
    handlers.get(TRAY_TOGGLE_EVENT)?.();
    expect(listen).toHaveBeenCalledTimes(1);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("removes the listener on unmount", async () => {
    const { unmount } = renderHook(() =>
      useTrayState({ listening: false, visible: true, onToggleListening: () => undefined })
    );
    await flush();
    expect(unlisten).not.toHaveBeenCalled();
    unmount();
    expect(unlisten).toHaveBeenCalledTimes(1);
  });

  it("warns rather than throws when the report fails", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    invoke.mockRejectedValueOnce(new Error("no tray"));
    expect(() =>
      renderHook(() =>
        useTrayState({ listening: true, visible: true, onToggleListening: () => undefined })
      )
    ).not.toThrow();
    await flush();
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain("[kaleo tray]");
  });
});

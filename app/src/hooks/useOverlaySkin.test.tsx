// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import { useOverlaySkin } from "./useOverlaySkin";

/** What the *other* window's write looks like in this one. */
function storageEvent(key: string | null) {
  window.dispatchEvent(new StorageEvent("storage", { key }));
}

beforeEach(() => localStorage.clear());

describe("useOverlaySkin", () => {
  it("starts on the stored skin", () => {
    localStorage.setItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN, "terminal");
    expect(renderHook(() => useOverlaySkin()).result.current.skin).toBe("terminal");
  });

  it("follows a change made in the settings window", () => {
    // The whole reason this is a storage event and not a context: settings
    // and the overlay are separate webviews, so picking a skin has to reach
    // the overlay without a restart.
    const { result } = renderHook(() => useOverlaySkin());
    expect(result.current.skin).toBe("plain");
    act(() => {
      localStorage.setItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN, "orb");
      storageEvent(KALEO_STORAGE_KEYS.OVERLAY_SKIN);
    });
    expect(result.current.skin).toBe("orb");
  });

  it("follows the terminal's Ada switch", () => {
    const { result } = renderHook(() => useOverlaySkin());
    expect(result.current.adaInTerminal).toBe(true);
    act(() => {
      localStorage.setItem(KALEO_STORAGE_KEYS.TERMINAL_ADA, "0");
      storageEvent(KALEO_STORAGE_KEYS.TERMINAL_ADA);
    });
    expect(result.current.adaInTerminal).toBe(false);
  });

  it("re-reads everything when the whole store is cleared", () => {
    // `key === null` means cleared. Ignoring it would leave the overlay on a
    // skin whose stored value no longer exists.
    localStorage.setItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN, "terminal");
    const { result } = renderHook(() => useOverlaySkin());
    act(() => {
      localStorage.clear();
      storageEvent(null);
    });
    expect(result.current.skin).toBe("plain");
  });

  it("ignores unrelated keys", () => {
    const { result } = renderHook(() => useOverlaySkin());
    act(() => {
      localStorage.setItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN, "orb");
      storageEvent("something_else_entirely");
    });
    expect(result.current.skin).toBe("plain");
  });
});

describe("changing the skin from the overlay itself", () => {
  it("updates immediately, because `storage` does not fire in the writing window", () => {
    // The trap this covers: the terminal skin replaces the bar, and the bar
    // is where the settings button lives. "Back to the bar" has to work from
    // inside the overlay, and it would silently do nothing if this hook only
    // listened for the event.
    localStorage.setItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN, "terminal");
    const { result } = renderHook(() => useOverlaySkin());
    expect(result.current.skin).toBe("terminal");
    act(() => result.current.setSkin("plain"));
    expect(result.current.skin).toBe("plain");
    expect(localStorage.getItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN)).toBe("plain");
  });
});

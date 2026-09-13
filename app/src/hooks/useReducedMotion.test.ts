// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { motionAttr, useReducedMotion } from "./useReducedMotion";

type Listener = () => void;

function fakeMatchMedia(matches: boolean) {
  const listeners = new Set<Listener>();
  const list = {
    matches,
    addEventListener: (_: string, cb: Listener) => listeners.add(cb),
    removeEventListener: (_: string, cb: Listener) => listeners.delete(cb),
  };
  const mm = vi.fn(() => list as unknown as MediaQueryList);
  Object.defineProperty(window, "matchMedia", { value: mm, configurable: true });
  return {
    flip(next: boolean) {
      list.matches = next;
      listeners.forEach((cb) => cb());
    },
  };
}

afterEach(() => {
  // @ts-expect-error test cleanup
  delete window.matchMedia;
});

describe("useReducedMotion", () => {
  it("is false where matchMedia cannot be asked", () => {
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(false);
    expect(motionAttr(false)).toBe("animated");
  });

  it("reads the media query and follows changes", () => {
    const media = fakeMatchMedia(true);
    const { result } = renderHook(() => useReducedMotion());
    expect(result.current).toBe(true);
    expect(motionAttr(result.current)).toBe("still");
    act(() => media.flip(false));
    expect(result.current).toBe(false);
  });
});

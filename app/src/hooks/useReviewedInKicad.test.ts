// @vitest-environment jsdom
//
// The nag that asks whether the engineer actually looked. It can only observe
// that the overlay lost focus, so the rules that matter are: a fresh artifact
// is unread, leaving marks it read, and a newer artifact is unread again.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useReviewedInKicad } from "./useReviewedInKicad";

const leave = () => act(() => void window.dispatchEvent(new Event("blur")));

describe("useReviewedInKicad", () => {
  it("nothing to review reads as reviewed", () => {
    const { result } = renderHook(() => useReviewedInKicad(null));
    expect(result.current).toBe(true);
  });

  it("a fresh artifact is unread until the engineer leaves the overlay", () => {
    const { result } = renderHook(() => useReviewedInKicad("s1:1"));
    expect(result.current).toBe(false);
    leave();
    expect(result.current).toBe(true);
  });

  it("a newer artifact is unread again, even after an earlier one was read", () => {
    const { result, rerender } = renderHook(
      ({ marker }: { marker: string | null }) => useReviewedInKicad(marker),
      { initialProps: { marker: "s1:1" as string | null } }
    );
    leave();
    expect(result.current).toBe(true);
    rerender({ marker: "s1:2" });
    expect(result.current).toBe(false);
    leave();
    expect(result.current).toBe(true);
  });

  it("leaving while there is nothing to review does not pre-approve the next artifact", () => {
    const { result, rerender } = renderHook(
      ({ marker }: { marker: string | null }) => useReviewedInKicad(marker),
      { initialProps: { marker: null as string | null } }
    );
    leave();
    rerender({ marker: "s1:1" });
    expect(result.current).toBe(false);
  });
});

// @vitest-environment jsdom
//
// The level bus, and the one thing it must never do: report a level for a
// microphone nobody is measuring.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearMicLevel,
  MIC_LEVEL_STALE_MS,
  micLevelSnapshot,
  normaliseMicPeak,
  publishMicLevel,
  useMicLevel,
} from "./useMicLevel";

describe("normaliseMicPeak", () => {
  it("is zero at and below the silence floor, and one at full scale", () => {
    expect(normaliseMicPeak(0)).toBe(0);
    expect(normaliseMicPeak(3)).toBe(0);
    expect(normaliseMicPeak(48)).toBe(1);
    expect(normaliseMicPeak(128)).toBe(1);
  });

  it("rises monotonically across the speaking range", () => {
    const peaks = [4, 8, 12, 20, 30, 40, 47];
    const levels = peaks.map(normaliseMicPeak);
    for (let i = 1; i < levels.length; i += 1) {
      expect(levels[i]).toBeGreaterThan(levels[i - 1]);
    }
    expect(levels.every((l) => l > 0 && l < 1)).toBe(true);
  });

  it("refuses to invent a level from a non-number", () => {
    expect(normaliseMicPeak(Number.NaN)).toBe(0);
  });
});

describe("useMicLevel", () => {
  beforeEach(() => {
    clearMicLevel();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    clearMicLevel();
  });

  const now = () => Date.now();

  it("reports no signal until something publishes", () => {
    const { result } = renderHook(() => useMicLevel(true, { now }));
    expect(result.current.hasSignal).toBe(false);
    expect(result.current.level).toBe(0);
  });

  it("follows a published peak while active", () => {
    const { result } = renderHook(() => useMicLevel(true, { now }));
    act(() => {
      publishMicLevel(40, Date.now());
      vi.advanceTimersByTime(40);
    });
    expect(result.current.hasSignal).toBe(true);
    expect(result.current.level).toBeGreaterThan(0.5);
  });

  it("is deaf while inactive — the microphone is not open, so there is no level", () => {
    // This is the honesty rule at the data layer: a consumer that is not in a
    // microphone-open state gets nothing, whatever the bus is carrying.
    const { result } = renderHook(() => useMicLevel(false, { now }));
    act(() => {
      publishMicLevel(64, Date.now());
      vi.advanceTimersByTime(200);
    });
    expect(result.current.hasSignal).toBe(false);
    expect(result.current.level).toBe(0);
  });

  it("goes stale rather than holding the last reading forever", () => {
    const { result } = renderHook(() => useMicLevel(true, { now }));
    act(() => {
      publishMicLevel(40, Date.now());
      vi.advanceTimersByTime(40);
    });
    expect(result.current.hasSignal).toBe(true);
    act(() => {
      vi.advanceTimersByTime(MIC_LEVEL_STALE_MS + 100);
    });
    expect(result.current.hasSignal).toBe(false);
    expect(result.current.level).toBe(0);
  });

  it("clearMicLevel drops the signal at once — a closed mic is not a quiet one", () => {
    const { result } = renderHook(() => useMicLevel(true, { now }));
    act(() => {
      publishMicLevel(40, Date.now());
      vi.advanceTimersByTime(40);
    });
    expect(result.current.hasSignal).toBe(true);
    act(() => {
      clearMicLevel();
      vi.advanceTimersByTime(40);
    });
    expect(result.current.hasSignal).toBe(false);
    expect(micLevelSnapshot()).toBeNull();
  });

  it("passes levels straight through with no smoothing loop under reduced motion", () => {
    const { result } = renderHook(() => useMicLevel(true, { still: true, now }));
    act(() => {
      publishMicLevel(48, Date.now());
    });
    // No timers advanced: the value arrived on the publish itself.
    expect(result.current.hasSignal).toBe(true);
    expect(result.current.level).toBe(1);
  });
});

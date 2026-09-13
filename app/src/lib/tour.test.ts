// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  TOUR_CAPTION,
  TOUR_CAPTION_KEY,
  TOUR_STOPS,
  TOUR_STORAGE_KEY,
  acceptTour,
  currentStop,
  dismissTour,
  nextStop,
  offerTour,
  readTour,
  resetTour,
  resolveTarget,
  startTour,
  subscribeTour,
  targetSelector,
} from "./tour";

beforeEach(() => {
  localStorage.clear();
  resetTour();
});

describe("tour state", () => {
  it("starts idle and reads garbage as idle", () => {
    expect(readTour()).toEqual({ kind: "idle" });
    localStorage.setItem(TOUR_STORAGE_KEY, "{not json");
    expect(readTour()).toEqual({ kind: "idle" });
    localStorage.setItem(TOUR_STORAGE_KEY, JSON.stringify({ kind: "active", index: 99 }));
    expect(readTour()).toEqual({ kind: "idle" });
  });

  it("offer is a consent gate; accept is what starts drawing", () => {
    offerTour("settings");
    expect(readTour()).toEqual({ kind: "offered", reason: "settings" });
    acceptTour("settings");
    expect(readTour()).toMatchObject({ kind: "active", index: 0 });
  });

  it("Take the tour writes the strip caption first, then the first stop", () => {
    startTour("first-run");
    expect(localStorage.getItem(TOUR_CAPTION_KEY)).toBe(TOUR_CAPTION);
    expect(currentStop(readTour())?.id).toBe(TOUR_STOPS[0].id);
  });

  it("next walks the stops and completes; the caption is cleared at the end", () => {
    startTour();
    for (let i = 1; i < TOUR_STOPS.length; i += 1) {
      nextStop();
      expect(readTour()).toMatchObject({ kind: "active", index: i });
    }
    nextStop();
    expect(readTour()).toEqual({ kind: "done", end: "completed" });
    expect(localStorage.getItem(TOUR_CAPTION_KEY)).toBeNull();
  });

  it("dismiss ends it from any stop", () => {
    startTour();
    dismissTour();
    expect(readTour()).toEqual({ kind: "done", end: "dismissed" });
    expect(nextStop().kind).toBe("done");
  });

  it("tells same-window subscribers and hears the other window's storage event", () => {
    const seen = vi.fn();
    const off = subscribeTour(seen);
    startTour();
    expect(seen).toHaveBeenCalledWith(expect.objectContaining({ kind: "active" }));
    localStorage.setItem(TOUR_STORAGE_KEY, JSON.stringify({ kind: "done", end: "skipped" }));
    window.dispatchEvent(new StorageEvent("storage", { key: TOUR_STORAGE_KEY }));
    expect(seen).toHaveBeenLastCalledWith({ kind: "done", end: "skipped" });
    off();
    startTour();
    expect(seen).toHaveBeenCalledTimes(2);
  });
});

describe("targets", () => {
  it("every stop names a route and the live one uses the integrations testid", () => {
    for (const stop of TOUR_STOPS) expect(stop.route).toMatch(/^\//);
    expect(TOUR_STOPS.find((s) => s.id === "integrations")?.target).toEqual({ testid: "integration-group" });
  });

  it("builds selectors from identity attributes, escaping quotes and backslashes", () => {
    expect(targetSelector({ testid: "integration-card" })).toBe('[data-testid="integration-card"]');
    expect(targetSelector({ testid: "finding", attrs: { "data-ref": 'C"1\\' } })).toBe(
      '[data-testid="finding"][data-ref="C\\"1\\\\"]',
    );
    expect(targetSelector({ testid: "row", within: "list" })).toBe('[data-testid="list"] [data-testid="row"]');
    expect(targetSelector({ testid: "row" })).not.toMatch(/nth-child/);
  });

  it("resolves through the injected query and treats a zero rect as missing", () => {
    const box = { top: 10, left: 20, width: 30, height: 40 };
    const el = { getBoundingClientRect: () => box };
    expect(resolveTarget({ testid: "x" }, () => el)).toEqual(box);
    expect(resolveTarget({ testid: "x" }, () => ({ getBoundingClientRect: () => ({ ...box, width: 0 }) }))).toBeNull();
    expect(resolveTarget({ testid: "x" }, () => null)).toBeNull();
    expect(resolveTarget(null, () => el)).toBeNull();
  });
});

// Sizing from state, not from measurement. The properties worth pinning are
// the ones that would silently clip the overlay or leave a transparent strip
// over KiCad: every state has a size, the pill is the only narrow shape, the
// two content-driven panels are the only ones that read a measurement, and
// the collapsed height is still the number the window is created at.

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  MEASURED_STATES,
  OVERLAY_COLLAPSED_HEIGHT,
  OVERLAY_MAX_HEIGHT,
  OVERLAY_PILL_LISTENING_WIDTH,
  OVERLAY_PILL_WIDTH,
  OVERLAY_SIZES,
  OVERLAY_WIDTH,
  isMeasured,
  isShrink,
  sizeFor,
  sizeKey,
  type OverlayState,
} from "./overlay-size";

const STATES = Object.keys(OVERLAY_SIZES) as OverlayState[];

describe("OVERLAY_SIZES", () => {
  it("covers every state with a usable, integral size", () => {
    expect(STATES.length).toBeGreaterThan(0);
    for (const state of STATES) {
      const size = OVERLAY_SIZES[state];
      expect(Number.isInteger(size.width), state).toBe(true);
      expect(Number.isInteger(size.height), state).toBe(true);
      expect(size.height, state).toBeGreaterThanOrEqual(OVERLAY_COLLAPSED_HEIGHT);
      expect(size.height, state).toBeLessThanOrEqual(OVERLAY_MAX_HEIGHT);
      expect(size.width, state).toBeLessThanOrEqual(OVERLAY_WIDTH);
    }
  });

  it("pins each state's size, so a change to one is a change on purpose", () => {
    expect(OVERLAY_SIZES).toEqual({
      pill: { width: OVERLAY_PILL_WIDTH, height: 58 },
      "pill-listening": { width: OVERLAY_PILL_LISTENING_WIDTH, height: 58 },
      bar: { width: 600, height: 58 },
      "desk-caption": { width: 600, height: 108 },
      "engine-down": { width: 600, height: 110 },
      "ada-caption": { width: 600, height: 134 },
      running: { width: 600, height: 293 },
      "running-feed": { width: 600, height: 461 },
      result: { width: 600, height: 300 },
      failure: { width: 600, height: 240 },
      cancelled: { width: 600, height: 104 },
      steps: { width: 600, height: 240 },
      deliver: { width: 600, height: 240 },
    });
  });

  it("makes the pill the only narrow shape — width is a discrete state", () => {
    // Anything that is not a pill is the window's native width, so a state
    // change inside the bar never touches the X axis.
    for (const state of STATES) {
      const expected = state.startsWith("pill")
        ? OVERLAY_SIZES[state].width
        : OVERLAY_WIDTH;
      expect(OVERLAY_SIZES[state].width, state).toBe(expected);
    }
    expect(OVERLAY_PILL_WIDTH).toBeLessThan(OVERLAY_WIDTH);
    expect(OVERLAY_PILL_LISTENING_WIDTH).toBeLessThan(OVERLAY_WIDTH);
  });

  it("keeps the pill↔bar swap height-neutral", () => {
    // The expand/collapse is, on the height axis, a no-op: it is a width
    // animation plus a content swap.
    expect(OVERLAY_SIZES.pill.height).toBe(OVERLAY_SIZES.bar.height);
    expect(OVERLAY_SIZES["pill-listening"].height).toBe(OVERLAY_SIZES.bar.height);
    // …and the listening pill is a width change only.
    expect(OVERLAY_SIZES["pill-listening"].width).not.toBe(OVERLAY_SIZES.pill.width);
  });

  it("opening the activity feed only ever grows the window", () => {
    expect(isShrink(OVERLAY_SIZES.running, OVERLAY_SIZES["running-feed"])).toBe(
      false
    );
  });
});

describe("sizeFor", () => {
  it("returns each state's size", () => {
    for (const state of STATES) {
      expect(sizeFor(state), state).toEqual(OVERLAY_SIZES[state]);
    }
  });

  it("ignores a measurement for the pill, which is never stacked", () => {
    // The pill renders one row of controls and no stackable blocks, so a
    // measurement is not evidence about its height -- and keeping it fixed
    // keeps the pill<->bar swap a pure width change.
    for (const state of ["pill", "pill-listening"] as const) {
      expect(sizeFor(state, 5_000), state).toEqual(OVERLAY_SIZES[state]);
      expect(sizeFor(state, 1), state).toEqual(OVERLAY_SIZES[state]);
    }
  });

  it("never lets a short measurement shrink a fixed bar state", () => {
    // A measurement below the constant is not evidence the constant is wrong:
    // each constant is the ceiling of a measured range, and undershooting is
    // the silent-clipping bug this module exists to prevent.
    for (const state of STATES) {
      if (isMeasured(state)) continue;
      expect(sizeFor(state, 1), state).toEqual(OVERLAY_SIZES[state]);
    }
  });

  it("only the step and deliver panels are measured", () => {
    expect([...MEASURED_STATES].sort()).toEqual(["deliver", "steps"]);
  });

  it("lets a measurement raise a measured state to its content", () => {
    expect(sizeFor("steps", 420)).toEqual({ width: 600, height: 420 });
    expect(sizeFor("deliver", 419.2)).toEqual({ width: 600, height: 420 });
  });

  it("never lets a measurement push a measured state below its floor or above the ceiling", () => {
    expect(sizeFor("steps", 10)).toEqual(OVERLAY_SIZES.steps);
    expect(sizeFor("steps", 0)).toEqual(OVERLAY_SIZES.steps);
    expect(sizeFor("deliver", 10_000)).toEqual({
      width: 600,
      height: OVERLAY_MAX_HEIGHT,
    });
  });

  it("falls back to the floor on a measurement that is not one", () => {
    expect(sizeFor("steps", Number.NaN)).toEqual(OVERLAY_SIZES.steps);
    expect(sizeFor("steps", Number.POSITIVE_INFINITY)).toEqual(OVERLAY_SIZES.steps);
    expect(sizeFor("steps", undefined)).toEqual(OVERLAY_SIZES.steps);
  });

  it("returns a fresh object, so a caller cannot mutate the table", () => {
    const size = sizeFor("pill");
    size.width = 1;
    expect(OVERLAY_SIZES.pill.width).toBe(OVERLAY_PILL_WIDTH);
  });

  it("answers an unknown state with the window's launch shape, never nothing", () => {
    const size = sizeFor("not-a-state" as OverlayState);
    expect(size).toEqual({ width: OVERLAY_WIDTH, height: OVERLAY_COLLAPSED_HEIGHT });
  });
});

describe("isShrink", () => {
  const at = (width: number, height: number) => ({ width, height });

  it("is true when either axis gets smaller", () => {
    expect(isShrink(at(600, 300), at(600, 58))).toBe(true);
    expect(isShrink(at(600, 58), at(132, 58))).toBe(true);
    expect(isShrink(at(600, 300), at(132, 400))).toBe(true);
  });

  it("is false for a grow on both axes, or no change at all", () => {
    expect(isShrink(at(132, 58), at(600, 58))).toBe(false);
    expect(isShrink(at(600, 58), at(600, 300))).toBe(false);
    expect(isShrink(at(600, 58), at(600, 58))).toBe(false);
  });

  it("calls collapsing to the pill a shrink, and expanding a grow", () => {
    expect(isShrink(sizeFor("bar"), sizeFor("pill"))).toBe(true);
    expect(isShrink(sizeFor("pill"), sizeFor("bar"))).toBe(false);
  });
});

describe("sizeKey", () => {
  it("is width by height, and distinguishes the axes", () => {
    expect(sizeKey({ width: 600, height: 58 })).toBe("600x58");
    expect(sizeKey({ width: 58, height: 600 })).not.toBe(
      sizeKey({ width: 600, height: 58 })
    );
  });
});

describe("the constants that live in two files", () => {
  // The comment on OVERLAY_COLLAPSED_HEIGHT has always said it must match
  // `app.height`; nothing checked it. A silent 4px disagreement here is a bar
  // that launches clipped, which is exactly the class of bug the overlay
  // sizing exists to prevent.
  const conf = JSON.parse(
    readFileSync(
      path.resolve(__dirname, "../../src-tauri/tauri.conf.json"),
      "utf8"
    )
  ) as { app: { windows: { width: number; height: number }[] } };

  it("OVERLAY_COLLAPSED_HEIGHT is app.height in tauri.conf.json", () => {
    expect(conf.app.windows[0].height).toBe(OVERLAY_COLLAPSED_HEIGHT);
  });

  it("OVERLAY_WIDTH is app.width in tauri.conf.json", () => {
    expect(conf.app.windows[0].width).toBe(OVERLAY_WIDTH);
  });
});

describe("a stacked card is honoured above its dominant block's constant", () => {
  it("raises a fixed state when the caller measured something taller", () => {
    // The case: an engine-down banner above a result. `result` alone is 300,
    // and the stack is taller; before this, the measurement was discarded for
    // every state outside MEASURED_STATES and the bottom of the card clipped.
    expect(sizeFor("result", 340).height).toBe(340);
    expect(sizeFor("result", 340).width).toBe(OVERLAY_WIDTH);
  });

  it("never shrinks a fixed state below its own floor", () => {
    // A short measurement is not evidence the constant is wrong -- the
    // constant is the ceiling of a measured range, and undershooting clips.
    expect(sizeFor("result", 100).height).toBe(OVERLAY_SIZES.result.height);
    expect(sizeFor("bar", 10).height).toBe(OVERLAY_SIZES.bar.height);
  });

  it("still ignores a measurement nobody supplied", () => {
    expect(sizeFor("result").height).toBe(OVERLAY_SIZES.result.height);
  });

  it("caps a stacked card at the ceiling so it cannot eat the screen", () => {
    expect(sizeFor("result", 5000).height).toBe(OVERLAY_MAX_HEIGHT);
  });
});

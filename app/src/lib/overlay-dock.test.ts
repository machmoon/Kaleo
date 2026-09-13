import { readFileSync } from "node:fs";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  OVERLAY_COLLAPSED_HEIGHT,
  OVERLAY_WIDTH,
} from "@/hooks/useOverlayHeight";

import {
  BOTTOM_MARGIN,
  TOP_OFFSET_PX,
  centredOrigin,
  dockOriginFor,
  dockPosition,
  dockReason,
  isRunStart,
  isSamePosition,
  overlayDock,
  releasesPin,
  setDockOriginResolver,
  type DockGeometry,
  type OverlayDockInput,
} from "./overlay-dock";
import { STEP_DESCRIPTORS, STEP_ORDER } from "./silkscreen/steps";

const idle: OverlayDockInput = {
  stepStatus: "idle",
  runningStep: null,
  lastStep: null,
  lastShownInKicad: false,
  stepCount: 0,
  runInFlight: false,
};

const kicadSteps = STEP_ORDER.filter((s) => STEP_DESCRIPTORS[s].where === "kicad");
const otherSteps = STEP_ORDER.filter((s) => STEP_DESCRIPTORS[s].where !== "kicad");

describe("overlayDock", () => {
  it("the idle pill and a finished live run stay at the top", () => {
    expect(dockReason(idle)).toBe("idle");
    expect(overlayDock(idle)).toBe("top");
  });

  it("a live run is read in the bar, so the bar stays at the top", () => {
    const input = { ...idle, runInFlight: true };
    expect(dockReason(input)).toBe("live-run");
    expect(overlayDock(input)).toBe("top");
  });

  it("docks to the bottom only while a KiCad-shown step is running", () => {
    for (const step of kicadSteps) {
      const input: OverlayDockInput = { ...idle, stepStatus: "running", runningStep: step };
      expect(dockReason(input)).toBe("kicad-running");
      expect(overlayDock(input)).toBe("bottom");
    }
  });

  it("stays at the top while waiting for Send/Approve so the controls stay readable", () => {
    for (const step of kicadSteps) {
      const input: OverlayDockInput = {
        ...idle,
        stepStatus: "waiting",
        lastStep: step,
        lastShownInKicad: true,
        stepCount: 1,
      };
      expect(dockReason(input)).toBe("kicad-waiting");
      expect(overlayDock(input)).toBe("top");
    }
  });

  it("stays at the top when the engine could not show the step in KiCad", () => {
    const input: OverlayDockInput = {
      ...idle,
      stepStatus: "waiting",
      lastStep: "place",
      lastShownInKicad: false,
      stepCount: 2,
    };
    expect(dockReason(input)).toBe("step-not-shown");
    expect(overlayDock(input)).toBe("top");
  });

  it("overlay-facing steps keep the bar at the top, running or waiting", () => {
    expect(otherSteps).toEqual(expect.arrayContaining(["review", "order", "case"]));
    for (const step of otherSteps) {
      const running: OverlayDockInput = { ...idle, stepStatus: "running", runningStep: step };
      expect(dockReason(running)).toBe("step-overlay");
      expect(overlayDock(running)).toBe("top");
      const waiting: OverlayDockInput = {
        ...idle,
        stepStatus: "waiting",
        lastStep: step,
        // Even when the engine says it showed it (the bridge reports through the same flag).
        lastShownInKicad: true,
        stepCount: 4,
      };
      expect(dockReason(waiting)).toBe("step-overlay");
      expect(overlayDock(waiting)).toBe("top");
    }
  });

  it("a finished or failed step flow is read in the bar", () => {
    const done: OverlayDockInput = { ...idle, stepStatus: "done", lastStep: "case", stepCount: 6 };
    expect(dockReason(done)).toBe("step-overlay");
    expect(overlayDock(done)).toBe("top");
    const error: OverlayDockInput = {
      ...idle,
      stepStatus: "error",
      lastStep: "route",
      lastShownInKicad: true,
      stepCount: 2,
    };
    expect(dockReason(error)).toBe("step-error");
    expect(overlayDock(error)).toBe("top");
  });

  it("a running step with no name is not treated as a KiCad one", () => {
    const input: OverlayDockInput = { ...idle, stepStatus: "running", runningStep: null };
    expect(overlayDock(input)).toBe("top");
  });
});

describe("releasesPin", () => {
  const proposing: OverlayDockInput = { ...idle, stepStatus: "running", runningStep: "propose" };
  const waiting: OverlayDockInput = {
    ...idle,
    stepStatus: "waiting",
    lastStep: "propose",
    lastShownInKicad: true,
    stepCount: 1,
  };
  const placing: OverlayDockInput = { ...waiting, stepStatus: "running", runningStep: "place" };

  it("a new step run releases the pin", () => {
    expect(isRunStart(proposing)).toBe(true);
    expect(releasesPin(idle, proposing)).toBe(true);
  });

  it("a new live run releases the pin", () => {
    expect(releasesPin(idle, { ...idle, runInFlight: true })).toBe(true);
  });

  it("approving the next step of the same run keeps the pin", () => {
    expect(isRunStart(placing)).toBe(false);
    expect(releasesPin(waiting, placing)).toBe(false);
    expect(releasesPin(placing, { ...placing, stepStatus: "waiting", stepCount: 2 })).toBe(false);
  });

  it("finishing, failing or resetting a run keeps the pin", () => {
    expect(releasesPin(proposing, waiting)).toBe(false);
    expect(releasesPin(waiting, { ...waiting, stepStatus: "error" })).toBe(false);
    expect(releasesPin(waiting, idle)).toBe(false);
    expect(releasesPin({ ...idle, runInFlight: true }, idle)).toBe(false);
  });

  it("does not release twice for one start", () => {
    expect(releasesPin(proposing, proposing)).toBe(false);
    expect(releasesPin({ ...idle, runInFlight: true }, { ...idle, runInFlight: true })).toBe(false);
  });
});

describe("dockPosition", () => {
  // A retina MacBook display: 2880×1800 physical, menu bar 50 px, Dock 140 px.
  const mac: DockGeometry = {
    monitor: { x: 0, y: 0, width: 2880, height: 1800 },
    workArea: { x: 0, y: 50, width: 2880, height: 1610 },
    window: { width: 1200, height: 108 },
    scaleFactor: 2,
  };

  it("top is where window.rs put the bar at launch", () => {
    expect(dockPosition("top", mac)).toEqual({ x: (2880 - 1200) / 2, y: TOP_OFFSET_PX });
  });

  it("bottom keeps the centre line and clears the Dock by the margin", () => {
    const expectedY = 50 + 1610 - 108 - BOTTOM_MARGIN * 2;
    expect(dockPosition("bottom", mac)).toEqual({ x: 840, y: expectedY });
  });

  it("follows a secondary monitor's own origin and work area", () => {
    const second: DockGeometry = {
      monitor: { x: 2880, y: -200, width: 1920, height: 1080 },
      workArea: { x: 2880, y: -200, width: 1920, height: 1040 },
      window: { width: 600, height: 300 },
      scaleFactor: 1,
    };
    expect(dockPosition("top", second)).toEqual({ x: 2880 + 660, y: -200 + TOP_OFFSET_PX });
    expect(dockPosition("bottom", second)).toEqual({
      x: 3540,
      y: -200 + 1040 - 300 - BOTTOM_MARGIN,
    });
  });

  it("a bar taller than the work area keeps its top on screen", () => {
    const tiny: DockGeometry = { ...mac, window: { width: 1200, height: 5000 } };
    expect(dockPosition("bottom", tiny).y).toBe(mac.workArea.y);
  });

  it("treats a missing scale factor as 1 rather than producing NaN", () => {
    const broken: DockGeometry = { ...mac, scaleFactor: Number.NaN };
    expect(dockPosition("bottom", broken).y).toBe(50 + 1610 - 108 - BOTTOM_MARGIN);
  });
});

describe("isSamePosition", () => {
  it("tolerates the odd pixel of rounding and nothing more", () => {
    expect(isSamePosition({ x: 10, y: 20 }, { x: 11, y: 18 })).toBe(true);
    expect(isSamePosition({ x: 10, y: 20 }, { x: 13, y: 20 })).toBe(false);
    expect(isSamePosition({ x: 10, y: 20 }, { x: 10, y: 400 })).toBe(false);
  });
});

// The launch position lives in Rust and the dock's copy of it lives here, and
// nothing in either language compares them: `dockPosition` is tested against
// the symbol, so both halves of a mismatch pass. They drifted once already —
// window.rs was raised 54 -> 58 by a sweep that was really raising
// `OVERLAY_COLLAPSED_HEIGHT` (an unrelated quantity that happened to share the
// value), and the bar hopped 4 px on its first dock apply. This test is the
// comparison, done the way `config/hardening.test.ts` does it: read the
// shipped file, not a copy of it.
describe("the Rust window constants", () => {
  const rust = readFileSync(
    new URL("../../src-tauri/src/window.rs", import.meta.url),
    "utf8"
  );

  const constant = (name: string): number => {
    const match = rust.match(new RegExp(`const ${name}\\s*:\\s*\\w+\\s*=\\s*([0-9.]+)`));
    if (!match) throw new Error(`window.rs no longer declares ${name}`);
    return Number(match[1]);
  };

  it("place the window where the top dock will put it back", () => {
    expect(constant("TOP_OFFSET")).toBe(TOP_OFFSET_PX);
  });

  it("agree with the frontend on the bar's width", () => {
    expect(constant("OVERLAY_WIDTH")).toBe(OVERLAY_WIDTH);
  });

  // TOP_OFFSET is a position in physical pixels; OVERLAY_COLLAPSED_HEIGHT is a
  // height in logical pixels. Nothing requires them to be equal, and the fact
  // that they were is what made the drift invisible — so if a later change
  // makes them equal again, this records that it is a coincidence and not a
  // rule anything may rely on.
  it("do not tie the top offset to the collapsed height", () => {
    expect(OVERLAY_COLLAPSED_HEIGHT).toBe(58);
    expect(TOP_OFFSET_PX).toBe(54);
  });
});

describe("the size/origin registry", () => {
  afterEach(() => setDockOriginResolver(null));

  it("answers nothing when no dock is mounted, so a resize does not guess a position", () => {
    expect(dockOriginFor({ width: 132, height: 58 })).toBeNull();
  });

  it("hands the registered dock the size and returns its origin", () => {
    const seen: { width: number; height: number }[] = [];
    setDockOriginResolver((size) => {
      seen.push(size);
      return { x: 1174, y: 54 };
    });
    expect(dockOriginFor({ width: 132, height: 58 })).toEqual({ x: 1174, y: 54 });
    expect(seen).toEqual([{ width: 132, height: 58 }]);
  });

  it("warns rather than throws when the dock cannot answer", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    setDockOriginResolver(() => {
      throw new Error("no monitor");
    });
    // A resize that cannot be anchored must still be a resize: the window ends
    // up in the wrong place, which is what it does today, not unresized.
    expect(dockOriginFor({ width: 132, height: 58 })).toBeNull();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it("does not let a retiring dock clear its replacement's registration", () => {
    const first = () => ({ x: 1, y: 1 });
    const second = () => ({ x: 2, y: 2 });
    setDockOriginResolver(first);
    setDockOriginResolver(second);
    // StrictMode order: the replacement registers, *then* the old instance's
    // cleanup runs. Clearing unconditionally would leave every later resize
    // anchored at the window's top-left again — the whole defect, restored.
    setDockOriginResolver(null, first);
    expect(dockOriginFor({ width: 132, height: 58 })).toEqual({ x: 2, y: 2 });
    setDockOriginResolver(null, second);
    expect(dockOriginFor({ width: 132, height: 58 })).toBeNull();
  });
});

describe("centredOrigin", () => {
  it("keeps a narrowing window's visual centre where it is", () => {
    // 600 → 132 logical at scale 2 is 1200 → 264 physical: the window's
    // top-left must move right by half the difference, not stay put.
    expect(centredOrigin({ x: 840, y: 54 }, 1200, 264)).toEqual({ x: 1308, y: 54 });
  });

  it("is symmetric, so widening puts it back exactly", () => {
    const narrow = centredOrigin({ x: 840, y: 54 }, 1200, 264);
    expect(centredOrigin(narrow, 264, 1200)).toEqual({ x: 840, y: 54 });
  });

  it("never changes y: a width change is not a vertical move", () => {
    expect(centredOrigin({ x: 0, y: 900 }, 264, 1200).y).toBe(900);
  });

  it("rounds to a whole physical pixel", () => {
    expect(Number.isInteger(centredOrigin({ x: 10, y: 10 }, 133, 20).x)).toBe(true);
  });
});

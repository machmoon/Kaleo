import { describe, expect, it } from "vitest";

import {
  barContent,
  isOverlayExpanded,
  overlayReason,
  type OverlayModeInput,
} from "./overlay-mode";

const idle: OverlayModeInput = {
  expanded: false,
  busy: false,
  stepsActive: false,
  status: "idle",
};

describe("overlayReason", () => {
  it("is the pill when nothing is happening", () => {
    expect(overlayReason(idle)).toBeNull();
    expect(isOverlayExpanded(idle)).toBe(false);
  });

  it("opens for the arrow, and closes again when it is released", () => {
    expect(overlayReason({ ...idle, expanded: true })).toBe("user");
    expect(overlayReason({ ...idle, expanded: false })).toBeNull();
  });

  it("never collapses over a run in flight, whatever the arrow says", () => {
    expect(overlayReason({ ...idle, busy: true })).toBe("busy");
    expect(overlayReason({ ...idle, busy: true, expanded: false })).toBe("busy");
    expect(overlayReason({ ...idle, expanded: true, stepsActive: true })).toBe("steps");
    // ...but an explicit collapse puts it away. Only `busy` overrides that.
    expect(overlayReason({ ...idle, expanded: false, stepsActive: true })).toBeNull();
  });

  it("keeps a result or failure on screen until dismissed", () => {
    for (const status of ["done", "error", "cancelled"]) {
      expect(overlayReason({ ...idle, expanded: true, status })).toBe("result");
      expect(overlayReason({ ...idle, expanded: false, status })).toBeNull();
    }
  });

  it("ranks the paid reasons above the cosmetic one", () => {
    expect(
      overlayReason({ expanded: true, busy: true, stepsActive: true, status: "done" })
    ).toBe("busy");
    expect(overlayReason({ expanded: true, busy: false, stepsActive: true, status: "done" })).toBe(
      "steps"
    );
  });
});

describe("barContent", () => {
  it("is the field until the microphone is actually open", () => {
    expect(barContent({ micOpen: false })).toBe("field");
    expect(barContent({ micOpen: true })).toBe("listening");
  });

  // The speech duck closes the microphone for the length of every reply, so
  // micOpen goes false mid-sentence. Without this the field would drop back
  // into the strip and out again on every spoken line.
  it("holds the swap while I am talking, even though the mic is shut", () => {
    expect(barContent({ micOpen: false, speaking: true })).toBe("listening");
  });

  it("a hidden overlay claims nothing, whatever the microphone is doing", () => {
    // Hidden by the global shortcut: there is nobody to tell, and a state
    // nobody can see is the one thing that must not be asserted.
    expect(barContent({ micOpen: true, hidden: true })).toBe("field");
    expect(barContent({ micOpen: false, speaking: true, hidden: true })).toBe("field");
  });
});

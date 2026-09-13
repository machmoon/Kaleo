// @vitest-environment jsdom
//
// The overlay's discrete state, on its own.
//
// `overlayStateFor` is the one place the page decides what shape it is, and
// the sizing hook trusts it completely — a wrong answer here is a window that
// is the wrong size with no measurement left to correct it. So it is pure and
// it is tested without a React tree, the convention `overlay-mode.ts` and
// `overlay-dock.ts` already set for the overlay's other two decisions.
//
// The page module is imported for it, which drags the whole overlay's import
// graph in; the mocks below are only there to keep that graph from reaching
// Tauri at import time.

import { describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({
    setSize: vi.fn(),
    setPosition: vi.fn(),
    onMoved: vi.fn(async () => () => {}),
    onResized: vi.fn(async () => () => {}),
    scaleFactor: vi.fn(async () => 1),
    outerSize: vi.fn(async () => ({ width: 600, height: 58 })),
  }),
  currentMonitor: vi.fn(async () => null),
  availableMonitors: vi.fn(async () => []),
}));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/plugin-fs", () => ({ readFile: vi.fn(), writeFile: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: vi.fn(async () => null) }));

import { heldDoors, overlayStateFor, type OverlayStateInput } from "./index";

/** Everything off: the full bar, idle, nothing in it. */
const idle: OverlayStateInput = {
  open: true,
  listening: false,
  busy: false,
  stepsActive: false,
  deliverOpen: false,
  status: "idle",
  hasResult: false,
  hasError: false,
  engineDown: false,
  deskCaption: false,
  commandNote: false,
};

const at = (patch: Partial<OverlayStateInput>) => overlayStateFor({ ...idle, ...patch });

describe("overlayStateFor", () => {
  it("is the bare bar when nothing is on screen", () => {
    expect(at({})).toEqual({ state: "bar", measured: false });
  });

  it("is the pill when the bar is closed", () => {
    expect(at({ open: false })).toEqual({ state: "pill", measured: false });
  });

  it("distinguishes the listening pill, which is a different width", () => {
    expect(at({ open: false, listening: true })).toEqual({
      state: "pill-listening",
      measured: false,
    });
  });

  it("does not split the bar on listening, because that swap is height-neutral", () => {
    // ListeningPanel is `h-9`, exactly the Input it stands in for, so the
    // window does not change and `overlay-size.ts` has one `bar` constant.
    // The pill is the opposite case, above: there it is a width change.
    expect(at({ listening: true })).toEqual({ state: "bar", measured: false });
  });

  it("does not let a run hide behind the pill", () => {
    // `open` already encodes this (isOverlayExpanded pins the bar open for a
    // run), so this asserts the two derivations cannot disagree: a busy
    // overlay that is somehow closed still reports a pill, and it is
    // `isOverlayExpanded` — not this function — that guarantees it never is.
    expect(at({ open: false, busy: true }).state).toBe("pill");
  });

  describe("the run states", () => {
    it("is one state whether or not a stage has ticked", () => {
      // `overlay-size.ts` sizes `running` for the taller of the two (the "no
      // events yet" note), so the first stage frame is not its own resize.
      expect(at({ busy: true })).toEqual({ state: "running", measured: false });
    });

    // The activity disclosure used to make the running card its own, taller
    // state. The raw feed is the dashboard console's job now, so a run has
    // exactly one size on the strip.

    it("is fixed, not measured — the checklist is a constant seven rows", () => {
      expect(at({ busy: true }).measured).toBe(false);
    });

    it("keeps the step flow out of the one-shot run block", () => {
      // `busy` is true for both machines; only the one-shot run draws
      // RunProgress, and the step panel draws itself.
      expect(at({ busy: true, stepsActive: true }).state).toBe("steps");
    });
  });

  describe("the settled states", () => {
    it("is fixed for the result, whose constant is the audit's ceiling", () => {
      expect(at({ status: "done", hasResult: true })).toEqual({
        state: "result",
        measured: false,
      });
    });

    it("is fixed for the failure, likewise", () => {
      expect(at({ status: "error", hasError: true })).toEqual({
        state: "failure",
        measured: false,
      });
    });

    it("does not measure the cancelled row, which is fixed copy", () => {
      expect(at({ status: "cancelled" })).toEqual({ state: "cancelled", measured: false });
    });

    it("ignores a status with nothing to show for it", () => {
      // `done` with no result renders no block at all, so the bar is a bar.
      expect(at({ status: "done", hasResult: false }).state).toBe("bar");
      expect(at({ status: "error", hasError: false }).state).toBe("bar");
    });
  });

  describe("the panels", () => {
    it("measures the step panel", () => {
      expect(at({ stepsActive: true })).toEqual({ state: "steps", measured: true });
    });

    it("lets the deliver panel win, since it stacks on top of the step panel", () => {
      expect(at({ stepsActive: true, deliverOpen: true })).toEqual({
        state: "deliver",
        measured: true,
      });
    });

    it("does not count the command note as its own block while a step run is open", () => {
      // The note is handed to StepPanel and the standalone banner is hidden;
      // counting it would make this a two-block stack that is not on screen.
      expect(at({ stepsActive: true, commandNote: true })).toEqual({
        state: "steps",
        measured: true,
      });
    });
  });

  describe("the banners", () => {
    it("is fixed for a desk caption, which the page clamps to three lines", () => {
      expect(at({ deskCaption: true })).toEqual({ state: "desk-caption", measured: false });
    });

    it("is fixed for an Ada caption, clamped to four", () => {
      expect(at({ commandNote: true })).toEqual({ state: "ada-caption", measured: false });
    });

    it("does not measure the engine-down banner on its own", () => {
      expect(at({ engineDown: true })).toEqual({ state: "engine-down", measured: false });
    });
  });

  describe("stacking — the case the audit's one-row-per-state table cannot express", () => {
    it("measures when two blocks are on screen at once", () => {
      // The table has a target for a result and a target for the engine-down
      // banner, and none for the two together. A fixed target for the
      // dominant one alone would clip the other.
      expect(at({ status: "done", hasResult: true, engineDown: true })).toEqual({
        state: "result",
        measured: true,
      });
    });

    it("measures a cancelled run under an engine-down banner", () => {
      // Both fixed on their own; their sum is not in the table.
      expect(at({ status: "cancelled", engineDown: true })).toEqual({
        state: "cancelled",
        measured: true,
      });
    });

    it("measures a run with a desk caption pinned above it", () => {
      // A desk caption is never cleared (audit §3.11), so it rides above
      // every later state for the life of the session.
      expect(at({ busy: true, deskCaption: true })).toEqual({
        state: "running",
        measured: true,
      });
    });
  });
});

// The doors a sentence typed at a live run can actually go through.
//
// These are not cosmetic: offering a door the service refuses is how a UI
// starts telling people "no" again, one 400 at a time. `heldDoors` mirrors the
// service's own guards (`service/amend.py`), and each case below is one of
// them.
describe("heldDoors", () => {
  const done = [] as const;

  it("offers nothing during the first phase, because there is nothing to address", () => {
    // `POST /steps` (plan + propose) is synchronous and the client has no
    // session id until it returns — and it is the most model-expensive phase
    // there is. Neither a note nor a cancel can reach it. The local hold is
    // the only mechanism, which is why it exists.
    expect(heldDoors({ session: null, done, cancelled: false })).toEqual({
      note: false,
      caseStep: false,
      restart: false,
    });
  });

  it("offers nothing on a cancelled session, which answers 409", () => {
    expect(heldDoors({ session: "s1", done, cancelled: true })).toEqual({
      note: false,
      caseStep: false,
      restart: false,
    });
  });

  it("offers the case gate — the one step in the API that reads free text", () => {
    expect(heldDoors({ session: "s1", done: ["place", "route"], cancelled: false })).toEqual({
      note: true,
      caseStep: true,
      restart: true,
    });
  });

  it("withdraws the case gate once the case has run, as the service does", () => {
    // The service refuses a note for a step already in `done`: its note would
    // never be read. Mirroring that here means the button is absent rather
    // than present-and-400.
    expect(heldDoors({ session: "s1", done: ["place", "case"], cancelled: false })).toEqual({
      note: true,
      caseStep: false,
      restart: true,
    });
  });
});

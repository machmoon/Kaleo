import { describe, expect, it } from "vitest";
import {
  CARDS_BY_STEP,
  INITIAL_STATE,
  SETUP_CARDS,
  SETUP_STEPS,
  hydrate,
  needsSetup,
  progress,
  reduce,
  remaining,
  skippedSentence,
  type SetupState,
} from "./machine";

const walk = (state: SetupState, ...actions: Parameters<typeof reduce>[1][]) =>
  actions.reduce(reduce, state);

describe("the setup step machine", () => {
  it("has six steps, one dot each, and every card lives on exactly one step", () => {
    expect(SETUP_STEPS).toEqual(["hello", "appearance", "engine", "accounts", "permissions", "done"]);
    const placed = SETUP_STEPS.flatMap((s) => CARDS_BY_STEP[s]);
    expect([...placed].sort()).toEqual([...SETUP_CARDS].sort());
  });

  it("continues forward and stops at done", () => {
    let s = INITIAL_STATE;
    for (const step of SETUP_STEPS) {
      expect(s.step).toBe(step);
      s = reduce(s, { type: "continue" });
    }
    expect(s.step).toBe("done");
    // `done` draws no dots (no footer), so it sits outside the counted route
    // and clamps to 0 — the four counted screens are appearance…permissions.
    expect(progress(s)).toEqual({ index: 0, total: 4 });
  });

  it("counts only the screens that draw a dot", () => {
    // The first screen with a dot row is the first screen counted. Six dots on
    // `appearance` with the second lit promised a screen the user had already
    // passed on a row they had never seen.
    expect(progress({ ...INITIAL_STATE, step: "hello" }).total).toBe(4);
    expect(progress({ ...INITIAL_STATE, step: "appearance" })).toEqual({ index: 0, total: 4 });
    expect(progress({ ...INITIAL_STATE, step: "permissions" })).toEqual({ index: 3, total: 4 });
  });

  it("back never goes below hello", () => {
    expect(reduce(INITIAL_STATE, { type: "back" })).toBe(INITIAL_STATE);
  });

  it("records the cards walked past as skipped, by card id not step id", () => {
    const s = walk(
      INITIAL_STATE,
      { type: "continue" },
      { type: "continue" },
      { type: "continue" },
      { type: "complete", card: "google" },
      { type: "continue" },
    );
    expect(s.step).toBe("permissions");
    expect(s.skipped).toEqual(["kicad", "stripe", "microsoft"]);
    expect(remaining(s)).toEqual(["notifications", "voice"]);
  });

  it("walking back puts that step's cards back in play", () => {
    const s = walk(
      INITIAL_STATE,
      { type: "jump", step: "accounts" },
      { type: "continue" },
      { type: "back" },
    );
    expect(s.step).toBe("accounts");
    expect(s.skipped).toEqual([]);
  });

  it("Set Up Later skips only this screen and moves one step on", () => {
    const s = walk(INITIAL_STATE, { type: "jump", step: "accounts" }, { type: "complete", card: "google" }, { type: "skip" });
    expect(s.step).toBe("permissions");
    expect(s.skipped).toEqual(["stripe", "microsoft"]);
    expect(s.finished).toBe(false);
    const again = reduce(s, { type: "skip" });
    expect(again.step).toBe("done");
    expect(again.skipped).toEqual(["stripe", "microsoft", "notifications", "voice"]);
    expect(reduce(again, { type: "skip" })).toEqual(again);
  });

  it("exit is Set Up Later plus finished", () => {
    const s = reduce(INITIAL_STATE, { type: "exit" });
    expect(s.finished).toBe(true);
    expect(remaining(s)).toEqual([]);
  });

  it("autoAdvance only fires from the step it was armed on", () => {
    const engine = reduce(INITIAL_STATE, { type: "jump", step: "engine" });
    expect(reduce(engine, { type: "autoAdvance", from: "engine" }).step).toBe("accounts");
    const accounts = reduce(engine, { type: "continue" });
    // The probe answered after the user already pressed Continue.
    expect(reduce(accounts, { type: "autoAdvance", from: "engine" })).toBe(accounts);
  });

  it("completing a card un-skips it, and uncompleting is idempotent", () => {
    const s = walk(INITIAL_STATE, { type: "jump", step: "accounts" }, { type: "continue" });
    expect(s.skipped).toContain("google");
    const done = reduce(s, { type: "complete", card: "google" });
    expect(done.skipped).not.toContain("google");
    expect(done.completed).toEqual(["google"]);
    expect(reduce(done, { type: "uncomplete", card: "stripe" })).toBe(done);
  });

  it("needsSetup is true for anything but completed: true", () => {
    expect(needsSetup(undefined)).toBe(true);
    expect(needsSetup({})).toBe(true);
    expect(needsSetup({ completed: "true" })).toBe(true);
    expect(needsSetup({ completed: true })).toBe(false);
  });

  it("reads the store's fresh defaults (empty remaining, empty skipped) as nothing completed", () => {
    const s = hydrate({ step: "accounts", skipped: [], remaining: [] });
    expect(s.completed).toEqual([]);
    expect(remaining(s)).toEqual([...SETUP_CARDS]);
    // Skipped alone is enough to trust remaining again.
    expect(hydrate({ step: "done", skipped: ["google"], remaining: [] }).completed).toEqual([
      "kicad",
      "stripe",
      "microsoft",
      "notifications",
      "voice",
    ]);
  });

  it("hydrates from the store and refuses garbage", () => {
    expect(hydrate({ step: "banana", skipped: "x", remaining: 3 })).toEqual(INITIAL_STATE);
    const s = hydrate({ step: "permissions", skipped: ["google", "nope"], remaining: ["notifications", "voice"] });
    expect(s.step).toBe("permissions");
    expect(s.skipped).toEqual(["google"]);
    expect(s.completed).toEqual(["kicad", "stripe", "microsoft"]);
    expect(remaining(s)).toEqual(["notifications", "voice"]);
  });

  it("writes the skipped sentence in catalogue order, or nothing", () => {
    expect(skippedSentence([])).toBe("");
    expect(skippedSentence(["stripe", "google"])).toBe("Skipped: Google, Billing. Find them in Settings.");
  });
});

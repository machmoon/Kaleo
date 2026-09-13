// @vitest-environment jsdom
//
// The gate as a truth table and the copy as a contract. The copy tests are
// the ones with teeth: "Routing finished" over an open net, or a case
// outcome announced before anyone pressed Case, is a banner that lies.

import { beforeEach, describe, expect, it } from "vitest";
import { NOTIFY_DEFAULT_ENABLED, NOTIFY_DEFAULT_OS, loadNotifySettings } from "./settings";
import { milestoneCopy, notifyMilestone, sendTestNotification, shouldNotify } from "./notify";

beforeEach(() => {
  window.localStorage.clear();
});

describe("shouldNotify", () => {
  it.each([
    [false, "always", false, false],
    [false, "always", true, false],
    [false, "not_focused", false, false],
    [false, "never", false, false],
    [true, "always", false, true],
    [true, "always", true, true],
    [true, "not_focused", false, true],
    [true, "not_focused", true, false],
    [true, "never", false, false],
    [true, "never", true, false],
  ] as const)("enabled=%s os=%s focused=%s → %s", (enabled, os, focused, expected) => {
    expect(shouldNotify({ enabled, os }, focused)).toBe(expected);
  });

  it("is off by default, and 'only when in another app' by default", () => {
    expect(NOTIFY_DEFAULT_ENABLED).toBe(false);
    expect(NOTIFY_DEFAULT_OS).toBe("not_focused");
    expect(loadNotifySettings()).toEqual({ enabled: false, os: "not_focused" });
  });
});

describe("milestoneCopy", () => {
  it("placed", () => {
    expect(milestoneCopy({ kind: "placed", parts: 11, boardMm: [18.25, 18] })).toEqual({
      title: "Placement is in KiCad",
      body: "11 parts on 18.25 × 18 mm",
    });
    expect(milestoneCopy({ kind: "placed", parts: 1, boardMm: null })).toEqual({
      title: "Placement is in KiCad",
      body: "1 part placed",
    });
  });

  it("routed, clean", () => {
    expect(milestoneCopy({ kind: "routed", routed: 7, total: 7, unrouted: 0 })).toEqual({
      title: "Routing finished",
      body: "7 of 7 nets routed, 0 left as ratsnest",
    });
  });

  it("routed with open nets never says plain 'finished'", () => {
    const copy = milestoneCopy({ kind: "routed", routed: 5, total: 7, unrouted: 2 })!;
    expect(copy.title).toBe("Routing finished with open nets");
    expect(copy.body).toBe("5 of 7 nets routed, 2 left as ratsnest");
  });

  it("reviewed", () => {
    expect(milestoneCopy({ kind: "reviewed", findings: 4, blockers: 1 })).toEqual({
      title: "Review finished",
      body: "4 findings, 1 blocking",
    });
    expect(milestoneCopy({ kind: "reviewed", findings: 0, blockers: 0 })).toEqual({
      title: "Review finished",
      body: "The critic found nothing to flag.",
    });
    expect(milestoneCopy({ kind: "reviewed", findings: 0, blockers: 0, status: "ok" })).toEqual({
      title: "Review finished",
      body: "The critic found nothing to flag.",
    });
  });

  it("a critic that answered nothing never gets the clean-board banner", () => {
    // The raiser should send `review_failed`; a `reviewed` carrying the
    // failed status is answered the same way rather than as "nothing to flag".
    const viaReviewed = milestoneCopy({ kind: "reviewed", findings: 0, blockers: 0, status: "failed" });
    expect(viaReviewed).toEqual({
      title: "Review failed",
      body: "The critic answered nothing readable — nothing is known about this board",
    });
    expect(milestoneCopy({ kind: "review_failed", detail: null })).toEqual(viaReviewed);
    expect(milestoneCopy({ kind: "review_failed", detail: "  model answered with no JSON " })).toEqual({
      title: "Review failed",
      body: "model answered with no JSON — nothing is known about this board",
    });
    for (const copy of [viaReviewed, milestoneCopy({ kind: "review_failed", detail: "x" })]) {
      expect(`${copy!.title} ${copy!.body}`).not.toMatch(/nothing to flag|nothing to say|finished/i);
    }
  });

  it("a skipped review is not news", () => {
    expect(milestoneCopy({ kind: "reviewed", findings: 0, blockers: 0, status: "skipped" })).toBeNull();
  });

  it("a settled background job says press-to-collect and nothing about the outcome", () => {
    expect(milestoneCopy({ kind: "case_done", state: "settled" })).toEqual({
      title: "Case finished in the background",
      body: "Press Case to collect it",
    });
    expect(milestoneCopy({ kind: "sourcing_done", state: "settled" })).toEqual({
      title: "Sourcing finished in the background",
      body: "Press Sourcing to collect it",
    });
  });

  it("an uncollected case has no banner: running is not news and failed is the collecting step's to say", () => {
    expect(milestoneCopy({ kind: "case_done", state: "running" })).toBeNull();
    expect(milestoneCopy({ kind: "case_done", state: "failed" })).toBeNull();
    expect(milestoneCopy({ kind: "sourcing_done", state: "running" })).toBeNull();
  });

  it("a collected failure carries the engine's own sentence", () => {
    const warning = "the case designed in the background failed: kernel clause clash";
    expect(milestoneCopy({ kind: "case_failed", warning })).toEqual({
      title: "Case failed in the background",
      body: warning,
    });
    expect(milestoneCopy({ kind: "sourcing_failed", warning: "  " })).toBeNull();
  });

  it("run_done, clean and with open nets", () => {
    expect(
      milestoneCopy({ kind: "run_done", routed: 9, total: 9, unrouted: 0, blockers: 0 })
    ).toEqual({ title: "Board generated", body: "9/9 nets routed, 0 blockers" });
    expect(
      milestoneCopy({ kind: "run_done", routed: 8, total: 9, unrouted: 1, blockers: 1 })
    ).toEqual({ title: "Board generated with open nets", body: "8/9 nets routed, 1 blocker" });
  });

  it("run_failed names the step and the message", () => {
    expect(milestoneCopy({ kind: "run_failed", step: "Routing", message: "engine 500" })).toEqual({
      title: "Routing failed",
      body: "engine 500",
    });
    expect(milestoneCopy({ kind: "run_failed", step: "", message: "" })).toEqual({
      title: "Run failed",
      body: "No reason was given.",
    });
  });

  it("engine_up carries no URL", () => {
    const copy = milestoneCopy({ kind: "engine_up", baseUrl: "http://127.0.0.1:8081" })!;
    expect(copy.title).toBe("Engine is back");
    expect(copy.body).not.toContain("127.0.0.1");
    expect(copy.body).not.toMatch(/https?:\/\//);
  });

  it("no banner ever carries a URL", () => {
    const all = [
      milestoneCopy({ kind: "placed", parts: 2, boardMm: [1, 2] }),
      milestoneCopy({ kind: "routed", routed: 1, total: 2, unrouted: 1 }),
      milestoneCopy({ kind: "reviewed", findings: 1, blockers: 0 }),
      milestoneCopy({ kind: "review_failed", detail: "critic timed out" }),
      milestoneCopy({ kind: "case_done", state: "settled" }),
      milestoneCopy({ kind: "run_done", routed: 1, total: 1, unrouted: 0, blockers: 0 }),
      milestoneCopy({ kind: "engine_up", baseUrl: "http://localhost:8081" }),
    ];
    for (const copy of all) {
      expect(copy).not.toBeNull();
      expect(`${copy!.title} ${copy!.body}`).not.toMatch(/https?:\/\//);
    }
  });
});

describe("outside Tauri", () => {
  it("notifyMilestone is a silent no-op", () => {
    expect(() =>
      notifyMilestone({ kind: "routed", routed: 1, total: 1, unrouted: 0 })
    ).not.toThrow();
  });

  it("sendTestNotification turns the switch on, then reports it could not send here", async () => {
    expect(loadNotifySettings().enabled).toBe(false);
    const result = await sendTestNotification();
    expect(loadNotifySettings().enabled).toBe(true);
    expect(result.sent).toBe(false);
    expect(result.reason).not.toBe("");
  });
});

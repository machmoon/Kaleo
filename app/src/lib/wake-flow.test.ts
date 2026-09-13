import { describe, expect, it } from "vitest";

import { wakeAction } from "./wake-flow";

describe("wakeAction", () => {
  it("starts a board from the sentence when nothing is open", () => {
    expect(
      wakeAction({ utterance: " make me a 3.3 V LDO board ", busy: false, stepsStatus: "idle" })
    ).toEqual({ kind: "start", intent: "make me a 3.3 V LDO board" });
  });

  it("only the name, nothing open: listen for the sentence", () => {
    expect(wakeAction({ utterance: "", busy: false, stepsStatus: "idle" })).toEqual({
      kind: "listen",
    });
  });

  it("while a stage waits, the sentence is a reply to the run, never a second board", () => {
    for (const stepsStatus of ["waiting", "done", "error"] as const) {
      expect(wakeAction({ utterance: "approve", busy: false, stepsStatus })).toEqual({
        kind: "command",
        text: "approve",
      });
    }
    expect(wakeAction({ utterance: "", busy: false, stepsStatus: "waiting" })).toEqual({
      kind: "command",
      text: "",
    });
  });

  it("a run in flight takes the sentence as a command rather than dropping it", () => {
    // It used to answer `ignore` — "say it again when this run is done" —
    // which is the app refusing a sentence it heard perfectly well. The
    // guarantee that mattered is unchanged and is one layer up: a `command`
    // is interpreted, and a sentence that describes a board is held for a
    // human to confirm. Nothing here starts a run.
    expect(wakeAction({ utterance: "an LDO", busy: true, stepsStatus: "idle" })).toEqual({
      kind: "command",
      text: "an LDO",
    });
    expect(wakeAction({ utterance: "an LDO", busy: false, stepsStatus: "running" })).toEqual({
      kind: "command",
      text: "an LDO",
    });
    // And it is never `start`, which is the kind that POSTs /generate.
    for (const busy of [true, false]) {
      expect(
        wakeAction({ utterance: "a buck converter", busy, stepsStatus: "running" }).kind
      ).not.toBe("start");
    }
  });
});

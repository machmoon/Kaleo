// What gets said at each turn. Pure strings, pinned here rather than in the
// page, for the same reason `summarize.test.ts` pins the digest: these are
// the only words a person across the room gets, and a sentence that quietly
// borrowed another moment's wording would be a lie about where the work is.

import { describe, expect, it } from "vitest";
import type { StepResponse } from "@/lib/silkscreen/types";
import {
  armedLine,
  refusalLine,
  shorten,
  stageFailedLine,
  stageReadyLine,
  unknownCommandLine,
} from "./moments";

function response(patch: Partial<StepResponse> = {}): StepResponse {
  return {
    session: "s1",
    step: "place",
    stage: "placed",
    intent: "a 3.3V LDO board",
    files: {},
    next: ["route"],
    shown_in_kicad: true,
    events: [],
    duration_s: 2,
    ...patch,
  } as StepResponse;
}

describe("stageReadyLine", () => {
  it("names the stage, where it is, and the words that approve the next one", () => {
    const line = stageReadyLine(response(), ["route"]);
    expect(line).toContain("The placement is done");
    expect(line).toContain("open in KiCad");
    expect(line).toContain("route it");
    // The confirm step is promised because it is real: a spoken command arms,
    // and a human commits. Saying "and it will run" would be a lie.
    expect(line).toContain("confirm");
  });

  it("does not send anyone to KiCad for a stage KiCad never received", () => {
    const line = stageReadyLine(response({ shown_in_kicad: false }), ["route"]);
    expect(line).toContain("could not open it in KiCad");
    expect(line).not.toContain("It is open in KiCad");
  });

  it("puts the stages that land in the overlay in the overlay", () => {
    const line = stageReadyLine(
      response({ step: "review", shown_in_kicad: false, next: ["sourcing"] }),
      ["sourcing"]
    );
    expect(line).toContain("here on the strip");
  });

  it("says a finished run is finished, and that nothing was ordered", () => {
    expect(stageReadyLine(response({ step: "order", next: [] }), [])).toBe(
      "Every stage has run. Nothing was ordered."
    );
  });
});

describe("stageFailedLine", () => {
  it("names the stage and carries the engine's own reason", () => {
    expect(stageFailedLine("route", "no path for net VOUT")).toBe(
      "I could not finish the copper. no path for net VOUT"
    );
  });

  it("says the plain fact when there is no reason to give, rather than inventing one", () => {
    expect(stageFailedLine("place", null)).toBe("I could not finish the placement.");
    expect(stageFailedLine(null, "")).toBe("I could not finish that stage.");
  });

  it("caps a machine error so a stack trace cannot become a minute of speech", () => {
    const line = stageFailedLine("route", "x".repeat(500));
    expect(line.length).toBeLessThan(220);
    expect(line.endsWith("…")).toBe(true);
  });
});

describe("refusalLine", () => {
  it("says it heard them and why nothing happened", () => {
    const line = refusalLine("a run is in flight");
    expect(line).toContain("I heard you");
    expect(line).toContain("a run is in flight");
  });
});

describe("armedLine", () => {
  it("asks for the confirmation rather than announcing an action", () => {
    expect(armedLine("route")).toBe("route the copper? Confirm it.");
    expect(armedLine("restart")).toContain("throw this run away");
  });
});

describe("unknownCommandLine", () => {
  it("answers a sentence it did not follow with the words it would follow", () => {
    const line = unknownCommandLine(["route", "review"]);
    expect(line).toContain("did not follow");
    expect(line).toContain("route it");
    expect(line).toContain("review it");
    expect(line).toContain("start over");
  });

  it("says no stage is waiting when none is, instead of offering nothing", () => {
    expect(unknownCommandLine([])).toContain("No stage is waiting");
  });
});

describe("shorten", () => {
  it("leaves a short line alone and flattens whitespace", () => {
    expect(shorten("  two   words\n")).toBe("two words");
  });
});

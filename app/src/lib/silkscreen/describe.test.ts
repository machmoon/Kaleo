import { describe, expect, it } from "vitest";

import {
  describeFrame,
  initialRunProgress,
  reduceFrame,
  reduceFrames,
  type RunProgress,
  type StageState,
} from "./describe";
import { PIPELINE_STAGES, planStages, stageById, stageForEvent } from "./stages";

const stage = (state: RunProgress, id: string): StageState =>
  state.stages.find((s) => s.id === id) as StageState;

describe("stage vocabulary", () => {
  it("matches the engine's stage names in pipeline order", () => {
    expect(PIPELINE_STAGES.map((s) => s.id)).toEqual([
      "read",
      "propose",
      "validate",
      "place",
      "schematic",
      "route",
      "review",
    ]);
    // `validate` is ours; every other row must be addressable by the string
    // the engine puts on its stage frames.
    for (const s of PIPELINE_STAGES) {
      if (s.id === "validate") expect(s.event).toBeNull();
      else expect(stageForEvent(s.event)).toBe(s);
    }
    expect(stageForEvent("validate")).toBeNull();
    expect(stageById("nope")).toBeNull();
  });

  it("leaves out the stages a plan cannot reach", () => {
    const ids = planStages({ datasheets: false, review: false }).map((s) => s.id);
    expect(ids).toEqual(["propose", "validate", "place", "route"]);
    expect(planStages({ datasheets: true }).map((s) => s.id)).toContain("read");
    // No output path over HTTP, so the schematic stage can never emit.
    expect(planStages({ output: false }).map((s) => s.id)).not.toContain("schematic");
    expect(planStages({ output: true }).map((s) => s.id)).toContain("schematic");
  });
});

describe("describeFrame", () => {
  it("says what happened, specifically", () => {
    expect(describeFrame({ event: "run.accepted", t_s: 0 })).toBe(
      "Run accepted, the pipeline is starting."
    );
    expect(describeFrame({ event: "stage.start", stage: "place", time_limit_s: 20 })).toBe(
      "Placing parts (20 s solver budget)."
    );
    expect(
      describeFrame({
        event: "stage.done",
        stage: "propose",
        parts: 11,
        nets: 9,
        repair_rounds: 2,
      })
    ).toBe("Proposed 11 parts across 9 nets after 2 repair rounds.");
    expect(
      describeFrame({
        event: "stage.done",
        stage: "place",
        solver_status: "FEASIBLE",
        board_mm: [30, 20.5],
        wirelength_mm: 412.7,
        warnings: 1,
      })
    ).toBe("Placed on a 30.0 × 20.5 mm board, FEASIBLE, 413 mm of wire, 1 warning.");
    expect(
      describeFrame({
        event: "stage.done",
        stage: "route",
        tracks: 24,
        vias: 3,
        routed_nets: 8,
        unrouted_nets: 1,
      })
    ).toBe("Routed 8 of 9 nets: 24 tracks, 3 vias, 1 left unrouted.");
    // Older engine: no `status` on the frame, so the empty list keeps the
    // reading it always had.
    expect(describeFrame({ event: "stage.done", stage: "review", findings: 0 })).toBe(
      "Review finished with no findings."
    );
    expect(
      describeFrame({ event: "read.part", part: "AMS1117", index: 1, total: 2, cached: true })
    ).toBe("AMS1117: facts already cached (1 of 2).");
    expect(
      describeFrame({ event: "model.call", stage: "propose", model: "gemini-3.5", elapsed_s: 2.44, chars: 12034, ok: true })
    ).toBe("gemini-3.5 answered (propose) in 2.4 s, 12,034 characters.");
  });

  it("drops a clause the frame never carried rather than inventing a zero", () => {
    expect(describeFrame({ event: "stage.start", stage: "place" })).toBe("Placing parts.");
    expect(describeFrame({ event: "model.call", ok: true })).toBe("The model answered.");
    expect(describeFrame({ event: "stage.done", stage: "place" })).toBe("Placement solved.");
  });

  it("returns null for an unknown event and never renders it raw", () => {
    expect(describeFrame({ event: "stage.teleport", stage: "warp", payload: { a: 1 } })).toBeNull();
    expect(describeFrame({ event: "wibble" })).toBeNull();
    // Prototype keys are event names like any other, and must not resolve to
    // an inherited function.
    expect(describeFrame({ event: "toString" })).toBeNull();
    expect(describeFrame({ event: "constructor" })).toBeNull();
  });

  it("survives malformed frames", () => {
    for (const bad of [null, undefined, 42, "run.done", [], {}, { event: 7 }, { event: null }]) {
      expect(describeFrame(bad)).toBeNull();
    }
    // A known event whose fields are all the wrong type still produces a sentence.
    expect(
      describeFrame({ event: "stage.done", stage: "read", parts: "x", pins: null, requirements: {} })
    ).toBe("Facts for 0 parts (read or cached): 0 pins, 0 requirements.");
  });

  it("names a stage it has never heard of instead of dropping it", () => {
    expect(describeFrame({ event: "stage.start", stage: "thermal" })).toBe("Starting thermal.");
    expect(describeFrame({ event: "stage.done", stage: "thermal" })).toBe("Finished thermal.");
    expect(describeFrame({ event: "stage.start" })).toBeNull();
  });
});

describe("reduceFrame", () => {
  const plan = { datasheets: false, review: true, route: true, output: false };

  it("starts with nothing ticked and the unreachable stages already explained", () => {
    const s = initialRunProgress(plan);
    expect(s.status).toBe("idle");
    expect(s.currentStage).toBeNull();
    expect(stage(s, "propose").status).toBe("pending");
    expect(stage(s, "read").status).toBe("skipped");
    expect(stage(s, "schematic").status).toBe("skipped");
    expect(stage(s, "schematic").note).toMatch(/writing files/);
  });

  it("ticks a stage only from its own frames", () => {
    let s = initialRunProgress(plan);
    s = reduceFrame(s, { event: "run.accepted", t_s: 0 });
    expect(s.status).toBe("accepted");

    s = reduceFrame(s, { event: "stage.start", stage: "propose", t_s: 0.2 });
    expect(s.status).toBe("running");
    expect(s.currentStage).toBe("propose");
    expect(stage(s, "propose").status).toBe("running");

    // A later stage starting says nothing about an earlier one finishing.
    s = reduceFrame(s, { event: "stage.start", stage: "place", t_s: 5 });
    expect(stage(s, "propose").status).toBe("running");

    s = reduceFrame(s, { event: "stage.done", stage: "place", t_s: 9, board_mm: [10, 10] });
    expect(stage(s, "place").status).toBe("done");
    expect(stage(s, "place").durationS).toBe(4);
    expect(stage(s, "place").summary).toContain("10.0 × 10.0 mm");
  });

  it("drives the synthetic validate row from propose frames only", () => {
    let s = initialRunProgress(plan);
    s = reduceFrame(s, { event: "stage.start", stage: "propose", t_s: 0 });
    expect(stage(s, "validate").status).toBe("pending");

    s = reduceFrame(s, { event: "propose.round", round: 1, errors: 3, first_error: "C1.1 missing", t_s: 2 });
    expect(stage(s, "validate").status).toBe("running");
    expect(s.repairRounds).toBe(1);

    s = reduceFrame(s, { event: "stage.done", stage: "propose", parts: 4, nets: 3, repair_rounds: 1, t_s: 6 });
    expect(stage(s, "validate").status).toBe("done");
    expect(stage(s, "validate").summary).toBe("Validated after 1 repair round.");
  });

  it("counts model round-trips and their time", () => {
    let s = initialRunProgress(plan);
    s = reduceFrames(s, [
      { event: "model.call", stage: "propose", model: "g", ok: true, elapsed_s: 1.5, chars: 10 },
      { event: "model.retry", provider: "a", error: "429", elapsed_s: 0.2 },
      { event: "model.call", stage: "review", ok: false, elapsed_s: 0.5 },
    ]);
    expect(s.modelCalls).toBe(2);
    expect(s.modelFailures).toBe(1);
    expect(s.modelRetries).toBe(1);
    expect(s.modelSecondsTotal).toBe(2);
    expect(s.modelCallLog[0]).toMatchObject({ stage: "propose", ok: true, chars: 10 });
  });

  it("counts unknown and malformed frames without changing anything else", () => {
    const start = initialRunProgress(plan);
    const s = reduceFrames(start, [
      { event: "stage.teleport", stage: "warp" },
      "not a frame",
      null,
      { nope: true },
    ]);
    expect(s.unknownFrames).toBe(4);
    expect(s.feed).toHaveLength(0);
    expect(s.stages.map((x) => x.status)).toEqual(start.stages.map((x) => x.status));
  });

  it("closes the checklist honestly when the run ends", () => {
    let s = initialRunProgress(plan);
    s = reduceFrames(s, [
      { event: "run.accepted", t_s: 0 },
      { event: "stage.start", stage: "propose", t_s: 0.1 },
      { event: "stage.done", stage: "propose", parts: 2, nets: 2, repair_rounds: 0, t_s: 3 },
      { event: "stage.start", stage: "route", t_s: 3.1 },
      { event: "run.done", t_s: 9, result: { kicad_pcb: "(kicad_pcb)" } },
    ]);
    expect(s.status).toBe("done");
    expect(s.result?.kicad_pcb).toBe("(kicad_pcb)");
    expect(s.currentStage).toBeNull();
    // Started, never reported: said out loud, not quietly ticked.
    expect(stage(s, "route").status).toBe("unreported");
    // Never started and now never will.
    expect(stage(s, "place").status).toBe("skipped");
    expect(stage(s, "review").status).toBe("skipped");
    expect(s.elapsedS).toBe(9);
  });

  it("records a run.error with its status", () => {
    let s = initialRunProgress(plan);
    s = reduceFrames(s, [
      { event: "run.accepted", t_s: 0 },
      { event: "run.error", status: 502, error: "Gemini is unavailable", t_s: 4 },
    ]);
    expect(s.status).toBe("error");
    expect(s.error).toEqual({ status: 502, message: "Gemini is unavailable" });
    expect(s.feed[s.feed.length - 1].text).toBe("Run failed (502): Gemini is unavailable");
  });

  it("appends one feed line per describable frame, with unique keys", () => {
    let s = initialRunProgress(plan);
    s = reduceFrames(s, [
      { event: "run.accepted", t_s: 0 },
      { event: "model.response", stage: "propose", chars: 12, truncated: true },
      { event: "unknown.thing" },
    ]);
    expect(s.feed.map((l) => l.event)).toEqual(["run.accepted", "model.response"]);
    expect(new Set(s.feed.map((l) => l.seq)).size).toBe(s.feed.length);
  });

  it("does not mutate the state it was given", () => {
    const before = initialRunProgress(plan);
    const snapshot = JSON.stringify(before.stages.map((x) => x.status));
    reduceFrame(before, { event: "stage.start", stage: "propose", t_s: 1 });
    expect(JSON.stringify(before.stages.map((x) => x.status))).toBe(snapshot);
  });
});

describe("describeFrame: what became of the critic", () => {
  const done = (extra: Record<string, unknown>) =>
    describeFrame({ event: "stage.done", stage: "review", findings: 0, blockers: 0, ...extra });

  it("an ok review with no findings is the clean line", () => {
    expect(done({ status: "ok" })).toBe("The critic found nothing to flag.");
    expect(done({ review: { status: "ok", ran: true, detail: null, note: "" } })).toBe(
      "The critic found nothing to flag."
    );
  });

  it("a failed review never reads as clean, with or without a detail", () => {
    expect(done({ status: "failed" })).toBe(
      "Review failed: the critic answered nothing readable — nothing is known about this board."
    );
    expect(done({ status: "failed", detail: "model answered with no JSON" })).toBe(
      "Review failed: model answered with no JSON — nothing is known about this board."
    );
    expect(
      done({ review: { status: "failed", ran: true, detail: "critic timed out", note: "" } })
    ).toBe("Review failed: critic timed out — nothing is known about this board.");
    for (const text of [done({ status: "failed" }), done({ status: "failed", detail: "x" })]) {
      expect(text).not.toMatch(/no findings|nothing to flag/);
    }
  });

  it("a skipped review says so", () => {
    expect(done({ status: "skipped" })).toBe(
      "Review skipped — nothing is known about this board."
    );
    expect(
      done({ review: { status: "skipped", ran: false, detail: "review=false", note: "" } })
    ).toBe("Review skipped: review=false — nothing is known about this board.");
  });

  it("no block and no status is the older engine's own wording", () => {
    expect(done({})).toBe("Review finished with no findings.");
    // A numeric `status` is not the critic's verdict.
    expect(done({ status: 200 })).toBe("Review finished with no findings.");
  });

  it("findings still count when the critic answered", () => {
    expect(done({ status: "ok", findings: 2, blockers: 1 })).toBe(
      "Review raised 2 findings, 1 blocker."
    );
  });

  it("review.failed is described in the failure words and never dropped", () => {
    expect(describeFrame({ event: "review.failed", stage: "review", detail: "quota exhausted" })).toBe(
      "Review failed: quota exhausted — nothing is known about this board."
    );
    expect(describeFrame({ event: "review.failed", stage: "review" })).toBe(
      "Review failed: the critic answered nothing readable — nothing is known about this board."
    );
    const long = "x".repeat(400);
    expect(describeFrame({ event: "review.failed", detail: long })!.length).toBeLessThan(260);
  });
});

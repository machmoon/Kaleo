// Hostile-input tests for the frame reducer.
//
// `describe.test.ts` covers the sentences and the well-behaved run; this file
// feeds the reducer the runs a broken engine, a flaky transport, or a replayed
// log could produce: frames out of order, duplicated terminals, garbage that
// is not a frame at all, and a 10,000-frame seeded fuzz. The reducer's own
// contract is the yardstick: never throw, tick only from what arrived, count
// what it does not understand.

import { describe, expect, it } from "vitest";
import {
  initialRunProgress,
  reduceFrame,
  reduceFrames,
  type RunProgress,
} from "./describe";

function run(frames: readonly unknown[]): RunProgress {
  return reduceFrames(initialRunProgress(), frames);
}

function stage(state: RunProgress, id: string) {
  const found = state.stages.find((s) => s.id === id);
  if (!found) throw new Error(`no stage ${id}`);
  return found;
}

describe("out-of-order and malformed stage frames", () => {
  it("stage.done without stage.start still ticks the stage, with no invented duration", () => {
    const state = run([
      { event: "run.accepted", t_s: 0 },
      { event: "stage.done", stage: "place", t_s: 3 },
    ]);
    const place = stage(state, "place");
    expect(place.status).toBe("done");
    expect(place.startedAtS).toBeNull();
    expect(place.durationS).toBeNull();
    expect(place.finishedAtS).toBe(3);
  });

  it("a later stage starting never infers that an earlier one finished", () => {
    const state = run([
      { event: "stage.start", stage: "propose", t_s: 1 },
      { event: "stage.start", stage: "route", t_s: 2 },
    ]);
    expect(stage(state, "propose").status).toBe("running");
    expect(stage(state, "route").status).toBe("running");
    expect(state.currentStage).toBe("route");
  });

  it("stage.done for a stage that is not the current one leaves currentStage alone", () => {
    const state = run([
      { event: "stage.start", stage: "route", t_s: 1 },
      { event: "stage.done", stage: "place", t_s: 2 },
    ]);
    expect(state.currentStage).toBe("route");
    expect(stage(state, "place").status).toBe("done");
  });

  it("a stage.start naming an unknown stage is dropped from the checklist, not appended", () => {
    const before = run([{ event: "run.accepted", t_s: 0 }]);
    const after = reduceFrame(before, {
      event: "stage.start",
      stage: "quantum-anneal",
      t_s: 1,
    });
    expect(after.stages.map((s) => s.id)).toEqual(before.stages.map((s) => s.id));
    expect(after.currentStage).toBeNull();
    // It is still a known event with a sentence, so it lands in the feed.
    expect(after.feed[after.feed.length - 1]?.text).toBe("Starting quantum-anneal.");
    expect(after.unknownFrames).toBe(0);
  });

  it("a stage frame with no stage name changes nothing but is not counted unknown", () => {
    const before = run([{ event: "run.accepted", t_s: 0 }]);
    const after = reduceFrame(before, { event: "stage.start", t_s: 1 });
    expect(after.stages).toEqual(before.stages);
    expect(after.unknownFrames).toBe(0);
    expect(after.feed).toHaveLength(before.feed.length);
  });

  it("negative and backwards t_s never move the engine clock backwards", () => {
    const state = run([
      { event: "run.accepted", t_s: 5 },
      { event: "stage.start", stage: "place", t_s: 2 },
      { event: "model.call", ok: true, t_s: -10 },
    ]);
    expect(state.elapsedS).toBe(5);
  });
});

describe("terminal-frame hostility", () => {
  it("duplicate run.done stays done and keeps a result", () => {
    const state = run([
      { event: "run.accepted", t_s: 0 },
      { event: "run.done", t_s: 3, result: { status: "FEASIBLE" } },
      { event: "run.done", t_s: 4, result: { status: "OPTIMAL" } },
    ]);
    expect(state.status).toBe("done");
    // Last write wins on the result; the point is that nothing crashed or
    // reverted to a pre-terminal status.
    expect(state.result).toEqual({ status: "OPTIMAL" });
  });

  it("run.error then more frames: status stays error, but counters keep counting", () => {
    const state = run([
      { event: "run.accepted", t_s: 0 },
      { event: "run.error", t_s: 2, status: 500, error: "boom" },
      { event: "model.call", ok: true, elapsed_s: 1 },
      { event: "model.retry", provider: "gemini" },
      { event: "propose.round", round: 1, errors: 2 },
    ]);
    expect(state.status).toBe("error");
    expect(state.error).toEqual({ status: 500, message: "boom" });
    expect(state.modelCalls).toBe(1);
    expect(state.modelRetries).toBe(1);
    expect(state.repairRounds).toBe(1);
  });

  // Regression test for the terminal-guard gap this suite originally caught:
  // a duplicated or replayed `run.accepted` after `run.done` used to move
  // status back to "accepted", and a stage.start after that promoted it to
  // "running". done/error are terminal now; a late frame cannot resurrect.
  it("a replayed run.accepted must not regress a finished run", () => {
    const state = run([
      { event: "run.accepted", t_s: 0 },
      { event: "run.done", t_s: 3, result: {} },
      { event: "run.accepted", t_s: 3.1 },
    ]);
    expect(state.status).toBe("done");
  });

  it("run.error settles running stages as unreported, pending as skipped", () => {
    const state = run([
      { event: "run.accepted", t_s: 0 },
      { event: "stage.start", stage: "propose", t_s: 1 },
      { event: "run.error", t_s: 2, status: 502, error: "provider died" },
    ]);
    expect(stage(state, "propose").status).toBe("unreported");
    expect(stage(state, "place").status).toBe("skipped");
    expect(state.currentStage).toBeNull();
  });

  it("run.error with no message still produces a message", () => {
    const state = run([{ event: "run.error" }]);
    expect(state.error?.message).toBe("The run failed.");
    expect(state.error?.status).toBeNull();
  });

  it("run.done with a non-object result yields an empty result object", () => {
    for (const result of [null, "text", 42, [1, 2, 3]]) {
      const state = run([{ event: "run.done", result }]);
      expect(state.status).toBe("done");
      expect(state.result).toEqual({});
    }
  });
});

describe("propose.round hostility", () => {
  it("propose.round after propose finished never un-ticks the validate row", () => {
    const state = run([
      { event: "stage.start", stage: "propose", t_s: 1 },
      { event: "propose.round", round: 1, errors: 3, t_s: 2 },
      { event: "stage.done", stage: "propose", t_s: 3 },
      { event: "propose.round", round: 2, errors: 1, t_s: 4 },
    ]);
    expect(stage(state, "validate").status).toBe("done");
    // The stray round still counts — the tally is of frames received.
    expect(state.repairRounds).toBe(2);
  });

  it("propose.round on a plan whose validate row is skipped leaves it running-able but honest", () => {
    // With no datasheets the read row is skipped; validate itself always plans.
    const state = run([
      { event: "propose.round", round: 1, errors: 1, t_s: 1 },
    ]);
    expect(stage(state, "validate").status).toBe("running");
    expect(state.repairRounds).toBe(1);
  });
});

describe("garbage that is not a frame", () => {
  const garbage: unknown[] = [
    null,
    undefined,
    42,
    "run.done",
    true,
    [],
    ["run.done"],
    {},
    { t_s: 1 },
    { event: 42 },
    { event: null },
    { event: { name: "run.done" } },
  ];

  it("counts each as unknown and changes nothing else", () => {
    const before = run([
      { event: "run.accepted", t_s: 0 },
      { event: "stage.start", stage: "place", t_s: 1 },
    ]);
    let state = before;
    for (const junk of garbage) state = reduceFrame(state, junk);
    expect(state.unknownFrames).toBe(garbage.length);
    expect(state.status).toBe(before.status);
    expect(state.stages).toEqual(before.stages);
    expect(state.feed).toEqual(before.feed);
  });

  it("prototype-name events cannot reach Object.prototype", () => {
    for (const name of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
      const state = run([{ event: name }]);
      expect(state.unknownFrames).toBe(1);
      expect(state.feed).toHaveLength(0);
    }
  });

  it("an unknown event name is counted, never rendered", () => {
    const state = run([
      { event: "run.accepted", t_s: 0 },
      { event: "telemetry.blob", payload: "x".repeat(10000) },
      { event: "stage.start", stage: "place", t_s: 1 },
    ]);
    expect(state.unknownFrames).toBe(1);
    expect(state.feed.map((l) => l.event)).toEqual(["run.accepted", "stage.start"]);
  });
});

/* ------------------------------------------------------------------- fuzz */

/** mulberry32 — a tiny seeded PRNG so a failure is replayable from the seed. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const STAGES = ["read", "propose", "place", "schematic", "route", "review", "bogus", ""];
const EVENTS = [
  "run.accepted",
  "stage.start",
  "stage.done",
  "read.part",
  "propose.round",
  "model.call",
  "model.response",
  "model.retry",
  "ground.part",
  "run.done",
  "run.error",
  "made.up",
  "constructor",
  "__proto__",
];

function randomFrame(rnd: () => number): unknown {
  const roll = rnd();
  if (roll < 0.05) return null;
  if (roll < 0.08) return rnd();
  if (roll < 0.11) return "not a frame";
  if (roll < 0.13) return [{ event: "run.done" }];
  if (roll < 0.15) return { t_s: rnd() * 100 };
  const event = EVENTS[Math.floor(rnd() * EVENTS.length)];
  const frame: Record<string, unknown> = { event };
  if (rnd() < 0.8) frame.t_s = rnd() < 0.1 ? -rnd() * 10 : rnd() * 300;
  if (rnd() < 0.7) frame.stage = STAGES[Math.floor(rnd() * STAGES.length)];
  if (rnd() < 0.5) frame.ok = rnd() < 0.5;
  if (rnd() < 0.4) frame.elapsed_s = rnd() < 0.1 ? "NaNish" : rnd() * 20;
  if (rnd() < 0.3) frame.round = Math.floor(rnd() * 10);
  if (rnd() < 0.3) frame.errors = Math.floor(rnd() * 10) - 2;
  if (rnd() < 0.2) frame.status = Math.floor(rnd() * 600);
  if (rnd() < 0.2) frame.error = "e".repeat(Math.floor(rnd() * 400));
  if (rnd() < 0.2)
    frame.result = rnd() < 0.5 ? { status: "FEASIBLE" } : "not an object";
  if (rnd() < 0.1) frame.chars = rnd() * 1e6;
  return frame;
}

const VALID_RUN_STATUS = new Set(["idle", "accepted", "running", "done", "error"]);
const VALID_STAGE_STATUS = new Set([
  "pending",
  "running",
  "done",
  "skipped",
  "unreported",
]);

describe("10k-frame fuzz (seed 0xC0FFEE)", () => {
  it("never throws, keeps counters monotone, and stays internally consistent", () => {
    const rnd = mulberry32(0xc0ffee);
    let state = initialRunProgress();
    let previous = state;
    // Plain-JS checks with a throw-on-violation: 10k iterations of dozens of
    // `expect` calls is minutes of matcher overhead, and a violation here is
    // reported with the iteration number so the seed makes it replayable.
    const check = (i: number, condition: boolean, what: string) => {
      if (!condition) throw new Error(`invariant broke at frame ${i}: ${what}`);
    };
    for (let i = 0; i < 10_000; i += 1) {
      const frame = randomFrame(rnd);
      state = reduceFrame(previous, frame);

      // Counters only ever grow.
      check(i, state.unknownFrames >= previous.unknownFrames, "unknownFrames shrank");
      check(i, state.modelCalls >= previous.modelCalls, "modelCalls shrank");
      check(i, state.modelFailures >= previous.modelFailures, "modelFailures shrank");
      check(i, state.modelRetries >= previous.modelRetries, "modelRetries shrank");
      check(i, state.repairRounds >= previous.repairRounds, "repairRounds shrank");
      // The engine clock never runs backwards.
      check(i, state.elapsedS >= previous.elapsedS, "elapsedS went backwards");
      check(i, Number.isFinite(state.elapsedS), "elapsedS not finite");
      // Caps hold.
      check(i, state.feed.length <= 500, "feed over cap");
      check(i, state.modelCallLog.length <= 200, "modelCallLog over cap");
      // Statuses stay inside their vocabularies.
      check(i, VALID_RUN_STATUS.has(state.status), `bad status ${state.status}`);
      for (const s of state.stages) {
        check(i, VALID_STAGE_STATUS.has(s.status), `bad stage status ${s.status}`);
      }
      // Feed seq is strictly increasing even after the cap drops old lines.
      const feed = state.feed;
      if (feed.length >= 2) {
        check(
          i,
          feed[feed.length - 1].seq > feed[feed.length - 2].seq,
          "feed seq not increasing"
        );
      }
      // Immutability: the reducer returned a new object.
      check(i, state !== previous, "reducer returned the same object");
      previous = state;
    }
    // Full seq sweep once at the end, not per iteration.
    for (let j = 1; j < state.feed.length; j += 1) {
      expect(state.feed[j].seq).toBeGreaterThan(state.feed[j - 1].seq);
    }
    // The fuzz genuinely exercised the machine, not just the garbage branch.
    expect(state.unknownFrames).toBeGreaterThan(100);
    expect(state.modelCalls).toBeGreaterThan(100);
  });

  // Same terminal guard, under fuzz: statuses only move forward.
  it("run status only moves forward under fuzz", () => {
    const order: Record<string, number> = {
      idle: 0,
      accepted: 1,
      running: 2,
      done: 3,
      error: 3,
    };
    const rnd = mulberry32(0xc0ffee);
    let state = initialRunProgress();
    for (let i = 0; i < 10_000; i += 1) {
      const next = reduceFrame(state, randomFrame(rnd));
      expect(order[next.status]).toBeGreaterThanOrEqual(order[state.status]);
      state = next;
    }
  });

  it("reducing a frame does not mutate the previous state (deep spot-check)", () => {
    const rnd = mulberry32(1234);
    const base = run([
      { event: "run.accepted", t_s: 0 },
      { event: "stage.start", stage: "place", t_s: 1 },
      { event: "model.call", ok: true, elapsed_s: 1.5 },
    ]);
    const snapshot = JSON.parse(JSON.stringify(base));
    for (let i = 0; i < 500; i += 1) reduceFrame(base, randomFrame(rnd));
    expect(JSON.parse(JSON.stringify(base))).toEqual(snapshot);
  });
});

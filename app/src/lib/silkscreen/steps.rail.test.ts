// The rail's pure half: status precedence, the engine chip, the receipt
// grammar, and the clock. These live in their own file so the rail's helpers
// can be pinned without touching `steps.test.ts`, which pins everything else.

import { describe, expect, it } from "vitest";

import {
  formatClock,
  railRows,
  receiptLine,
  stageCalls,
  stageModel,
  unreceivedStep,
} from "./steps";
import type { RailInput } from "./steps";
import type { StepName, StepResponse, StepStatusResponse } from "./types";

function step(partial: Partial<StepResponse> & { step: StepName }): StepResponse {
  return {
    session: "s1",
    stage: "proposed",
    intent: "a 3.3V LDO board",
    files: {},
    next: [],
    shown_in_kicad: false,
    events: [],
    duration_s: 1,
    ...partial,
  };
}

function input(partial: Partial<RailInput>): RailInput {
  return { history: [], running: null, available: [], status: "waiting", ...partial };
}

function row(rows: ReturnType<typeof railRows>, id: StepName) {
  const found = rows.find((r) => r.id === id);
  if (!found) throw new Error(`no row for ${id}`);
  return found;
}

const placed = step({
  step: "place",
  parts: [
    { ref: "U1", footprint: "SOT-223" },
    { ref: "C1", footprint: "0603" },
    { ref: "C2", footprint: "0603" },
    { ref: "R1", footprint: "0603" },
    { ref: "J1", footprint: "Conn" },
  ],
  board_mm: [24, 18],
  status: "optimal",
  duration_s: 3.2,
  next: ["route"],
  shown_in_kicad: true,
});

const routed = step({
  step: "route",
  routing: { routed: ["VIN", "VOUT", "GND"], unrouted: {}, tracks: 14, vias: 2 },
  duration_s: 8,
  next: ["review"],
});

describe("railRows: status precedence", () => {
  it("the latest finished step with work still available is in review, not merely done", () => {
    const rows = railRows(input({ history: [placed], available: ["route"] }));
    expect(row(rows, "place").rail).toBe("review");
    expect(row(rows, "route").rail).toBe("ready");
    expect(row(rows, "propose").rail).toBe("queued");
  });

  it("an earlier finished step is done once a later one has answered", () => {
    const rows = railRows(input({ history: [placed, routed], available: ["review"] }));
    expect(row(rows, "place").rail).toBe("done");
    expect(row(rows, "route").rail).toBe("review");
  });

  it("nothing left to approve makes the last row done rather than in review", () => {
    const rows = railRows(input({ history: [placed, routed], available: [], status: "done" }));
    expect(row(rows, "route").rail).toBe("done");
  });

  it("the running step is running and a background step says it started itself", () => {
    const latest = { ...placed, next: ["route", "sourcing"] as StepName[], background: ["sourcing"] as StepName[] };
    const rows = railRows(
      input({ history: [latest], running: "route", available: ["route", "sourcing"], status: "running" })
    );
    expect(row(rows, "route").rail).toBe("running");
    expect(row(rows, "route").method).toContain("A* over a 0.25 mm grid");
    expect(row(rows, "sourcing").rail).toBe("background");
    expect(row(rows, "sourcing").method).toBe("looking up parts — started on its own");
  });

  it("a failure marks the step that was in flight, from failedStep once running is cleared", () => {
    const rows = railRows(
      input({ history: [placed], running: null, failedStep: "route", available: ["route"], status: "error" })
    );
    expect(row(rows, "route").rail).toBe("failed");
    expect(row(rows, "place").rail).toBe("review");
  });

  it("a step the engine finished but this client never received is not done", () => {
    const status: StepStatusResponse = {
      session: "s1",
      stage: "placed",
      intent: "a 3.3V LDO board",
      files: {},
      done: ["propose", "place"],
      next: ["route"],
      kicad_live: false,
    };
    const marker = unreceivedStep(status, "place");
    const rows = railRows(input({ history: [marker], available: ["route"] }));
    expect(row(rows, "place").rail).toBe("unreceived");
    expect(row(rows, "place").receipt).toBeNull();
    expect(row(rows, "place").durationS).toBeNull();
    expect(row(rows, "place").engine).toBeNull();
  });
});

describe("railRows: the KiCad boundary", () => {
  it("names KiCad only when the engine says it showed the stage there", () => {
    const rows = railRows(input({ history: [placed], available: ["route"] }));
    expect(row(rows, "place").whereLabel).toBe("KiCad");
    expect(row(rows, "place").notShown).toBeNull();
  });

  it("relays the engine's own reason verbatim, and chips the row as on disk", () => {
    const detail = "the bridge did not start: no pcbnew socket on 4242";
    const rows = railRows(
      input({
        history: [{ ...placed, shown_in_kicad: false, shown_detail: detail }],
        available: ["route"],
      })
    );
    expect(row(rows, "place").notShown).toBe(detail);
    expect(row(rows, "place").whereLabel).toBe("on disk");
  });

  it("says nothing about KiCad for a stage the bridge was never asked to show", () => {
    const rows = railRows(
      input({ history: [{ ...placed, shown_in_kicad: false, shown_detail: null }], available: ["route"] })
    );
    expect(row(rows, "place").notShown).toBeNull();
    expect(row(rows, "place").whereLabel).toBeNull();
  });

  it("an overlay stage is never 'not shown in KiCad'", () => {
    const review = step({
      step: "review",
      findings: [{ severity: "note", title: "n" }],
      shown_in_kicad: false,
      shown_detail: "the bridge did not start",
      next: ["sourcing"],
    });
    const rows = railRows(input({ history: [review], available: ["sourcing"] }));
    expect(row(rows, "review").notShown).toBeNull();
  });
});

describe("stageModel: the chip is a measurement or an admission", () => {
  it("uses the model a model.call frame names, when the engine sends one", () => {
    const response = step({
      step: "propose",
      events: [{ event: "model.call", model: "gemini-3.7-flash", stage: "propose" }],
    });
    expect(stageModel(response)).toEqual({
      label: "gemini-3.7-flash",
      kind: "model",
      named: true,
    });
  });

  it("refuses to name a model the wire did not name", () => {
    const chip = stageModel(step({ step: "propose" }));
    expect(chip).toEqual({ label: "model", kind: "model", named: false });
  });

  it("names the solver the placement actually used", () => {
    expect(stageModel(placed)).toEqual({ label: "CP-SAT", kind: "cp-sat", named: true });
    expect(stageModel({ ...placed, status: "fallback" })).toEqual({
      label: "shelf pack",
      kind: "shelf-pack",
      named: true,
    });
  });

  it("names the router, the case kernel and the exporter from what came back", () => {
    expect(stageModel(routed)?.label).toBe("A* router");
    expect(
      stageModel(step({ step: "case", enclosure: { engine: "kernel" } }))?.label
    ).toBe("build123d");
    // There is one case engine. Anything else is an engine this client
    // cannot vouch for, and an unvouched name is not a chip.
    expect(stageModel(step({ step: "case", enclosure: { engine: "laser" } }))).toBeNull();
    expect(stageModel(step({ step: "case", enclosure: {} }))).toBeNull();
    expect(stageModel(step({ step: "order" }))?.label).toBe("order gate");
    expect(
      stageModel(step({ step: "order", files: { model_glb: "/tmp/b.glb" } }))?.label
    ).toBe("kicad-cli");
  });
});

describe("stageCalls", () => {
  it("counts the model calls the stage reported", () => {
    const response = step({
      step: "propose",
      events: [
        { event: "stage.start", stage: "propose" },
        { event: "model.call", model: "m" },
        { event: "model.call", model: "m" },
      ],
    });
    expect(stageCalls(response)).toBe(2);
  });

  it("is null, never zero, when the stage reported no call at all", () => {
    expect(stageCalls(step({ step: "propose" }))).toBeNull();
  });
});

describe("receiptLine: measured terms only", () => {
  it("places parts on a measured board", () => {
    expect(receiptLine(placed)).toBe("5 parts on 24.0 × 18.0 mm, optimal");
  });

  it("routes nets with tracks and vias, and names what is left open", () => {
    expect(receiptLine(routed)).toBe("3 of 3 nets, 14 tracks, 2 vias");
    const partial = {
      ...routed,
      routing: { routed: ["VIN"], unrouted: { GND: "no path" }, tracks: 4, vias: 1 },
    };
    expect(receiptLine(partial)).toBe("1 of 2 nets, 4 tracks, 1 via, 1 left as ratsnest");
  });

  it("counts findings, parts and nets, and kernel clauses", () => {
    expect(receiptLine(step({ step: "propose", parts: 5, nets: 4 }))).toBe("5 parts, 4 nets");
    expect(
      receiptLine(step({ step: "review", findings: [{ severity: "blocker" }], blockers: ["b"] }))
    ).toBe("1 finding, 1 blocking");
    expect(receiptLine(step({ step: "review", findings: [] }))).toBe("no findings");
    expect(
      receiptLine(
        step({
          step: "case",
          enclosure: {
            engine: "kernel",
            kernel: {
              passed: true,
              warnings: [],
              clauses: [
                { name: "lid_mates", passed: true, margin_mm: 0.4, detail: "" },
                { name: "min_wall", passed: true, margin_mm: 1.2, detail: "" },
              ],
            },
          },
        })
      )
    ).toBe("2 of 2 checks pass");
  });

  it("says a case built without a kernel report was not checked", () => {
    expect(receiptLine(step({ step: "case", enclosure: { engine: "kernel" } }))).toBe(
      "no acceptance check ran"
    );
  });

  it("counts MPNs separately from the distributor confirmations", () => {
    const sourcing = step({
      step: "sourcing",
      sourcing: {
        parts: [
          {
            ref: "U1",
            value: "AMS1117",
            kind: "device",
            package: "SOT-223",
            mpn: "AMS1117-3.3",
            mpn_status: "proposed",
            datasheet_status: "verified",
          },
          {
            ref: "C1",
            value: "10u",
            kind: "capacitor",
            package: "0603",
            mpn: null,
            mpn_status: "none",
            datasheet_status: "none",
          },
        ],
      },
    });
    expect(receiptLine(sourcing)).toBe("1 of 2 MPNs, 0 confirmed, 1 datasheet PDF");
  });

  it("says an un-orderable board is un-orderable, with the blocker count", () => {
    expect(
      receiptLine(step({ step: "order", order: { orderable: false, manifest: { blocker_count: 2 } } }))
    ).toBe("not orderable, 2 blockers");
    expect(receiptLine(step({ step: "order", order: { orderable: true, issues: [] } }))).toBe(
      "prepared, no issues"
    );
  });
});

describe("formatClock", () => {
  it("is m:ss, and never negative or NaN", () => {
    expect(formatClock(0)).toBe("0:00");
    expect(formatClock(9.6)).toBe("0:09");
    expect(formatClock(65)).toBe("1:05");
    expect(formatClock(3600)).toBe("60:00");
    expect(formatClock(-4)).toBe("0:00");
    expect(formatClock(Number.NaN)).toBe("0:00");
  });
});

describe("stageModel: a model call never takes credit from a deterministic engine", () => {
  const call = { event: "model.call", model: "gemini-3.7-flash", stage: "enclosure" };

  it("keeps the case chipped with the kernel that measured it", () => {
    const cased = step({
      step: "case",
      enclosure: { engine: "kernel" },
      events: [call],
    });
    expect(stageModel(cased)).toEqual({ label: "build123d", kind: "kernel", named: true });
    // The calls the case did make are still counted; only the chip is the kernel's.
    expect(stageCalls(cased)).toBe(1);
  });

  it("keeps the placer and the router chipped with the solver", () => {
    expect(stageModel({ ...placed, events: [call] })?.label).toBe("CP-SAT");
    expect(stageModel({ ...routed, events: [call] })?.label).toBe("A* router");
  });

  it("names the model on the stages a model really does", () => {
    expect(stageModel(step({ step: "review", events: [call] }))).toEqual({
      label: "gemini-3.7-flash",
      kind: "model",
      named: true,
    });
    expect(stageModel(step({ step: "sourcing", events: [call] }))).toEqual({
      label: "gemini-3.7-flash + probe",
      kind: "probe",
      named: true,
    });
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { SilkscreenError, advanceStep, startSteps } from "./client";
import {
  UNRECEIVED_SUMMARY,
  availableSteps,
  caseDetails,
  findingCitation,
  findingProvenance,
  formatMarginMm,
  headline,
  headlineFix,
  policyFlag,
  runReceipt,
  armCommand,
  interpretCommand,
  isUnreceivedStep,
  marginAxis,
  orderDetails,
  reconcileHistory,
  sourcingDetails,
  stepRows,
  stepsExhausted,
  summarizeStep,
} from "./steps";
import type { SourcingEntry, StepResponse, StepStatusResponse } from "./types";

const mockFetch = vi.mocked(tauriFetch);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function step(partial: Partial<StepResponse> & { step: StepResponse["step"] }): StepResponse {
  return {
    session: "s1",
    stage: "proposed",
    intent: "a toy car",
    files: {},
    next: [],
    shown_in_kicad: false,
    events: [],
    duration_s: 1,
    ...partial,
  };
}

afterEach(() => {
  mockFetch.mockReset();
});

describe("summarizeStep", () => {
  it("describes each step in one sentence from its own fields", () => {
    expect(summarizeStep(step({ step: "propose", parts: 6, nets: 5, repair_rounds: 1 }))).toBe(
      "Proposed 6 parts and 5 nets, after 1 repair round."
    );
    expect(
      summarizeStep(
        step({
          step: "place",
          parts: [{ ref: "U1", footprint: "SOIC-8" }],
          board_mm: [23.0, 14.1],
          status: "feasible",
        })
      )
    ).toBe("Placed 1 parts on a 23.0 × 14.1 mm board (feasible).");
    expect(
      summarizeStep(
        step({
          step: "route",
          routing: { tracks: 43, vias: 8, routed: ["GND", "VIN"], unrouted: { MOT: "no path" } },
        })
      )
    ).toBe("Routed 2 of 3 nets with 43 tracks and 8 vias, 1 left as ratsnest.");
    expect(summarizeStep(step({ step: "review", findings: [], blockers: [] }))).toBe(
      "The critic found nothing to flag."
    );
    expect(
      summarizeStep(step({ step: "order", order: { orderable: false, manifest: { blocker_count: 2 } } }))
    ).toBe("Not orderable: 2 blockers to fix first.");
    expect(summarizeStep(step({ step: "order", order: { orderable: true, issues: [] } }))).toBe(
      "Fab order prepared with no issues."
    );
    expect(
      summarizeStep(
        step({
          step: "order",
          order: { orderable: true, issues: [{ severity: "warning", title: "Thin board" }] },
        })
      )
    ).toBe("Fab order prepared; 1 issue to read first.");
    expect(
      summarizeStep(
        step({ step: "case", enclosure: { fit: { margins_mm: { x: 1, y: 1, z: 0.5 } } } })
      )
    ).toBe("Case fits with margins x +1.0, y +1.0, z +0.5 mm.");
    expect(summarizeStep(step({ step: "case", enclosure: null }))).toMatch(/failed/);
  });

  it("counts sourcing as proposals, and datasheets as the only verified thing", () => {
    expect(
      summarizeStep(
        step({
          step: "sourcing",
          sourcing: { parts: bomParts, verified: 1, proposed: 2, unresolved: 1, warnings: [] },
        })
      )
    ).toBe("Sourced 2 of 3 parts; 1 datasheet verified.");
    // A model that never answered: the engine sends the rows with statuses none.
    expect(
      summarizeStep(
        step({ step: "sourcing", sourcing: { parts: [bomParts[2]], verified: 0, proposed: 0, unresolved: 1 } })
      )
    ).toBe("Sourced 0 of 1 parts; 0 datasheets verified.");
  });
});

const CLAUSES = [
  { name: "valid_topology", passed: true, margin_mm: 0, detail: "BRepCheck clean" },
  { name: "lid_mates", passed: true, margin_mm: 0.1, detail: "lip gap 0.1 mm within slack" },
  { name: "headroom", passed: true, margin_mm: 1.4, detail: "1.4 mm above U1" },
];

describe("summarizeStep for the case", () => {
  it("states the kernel verdict with its count when every clause passed", () => {
    const enclosure = { engine: "kernel", kernel: { passed: true, clauses: CLAUSES, warnings: [] } };
    expect(summarizeStep(step({ step: "case", enclosure }))).toBe(
      "Case verified: 3 of 3 checks pass."
    );
  });

  it("names every failing clause with its signed margin", () => {
    const enclosure = {
      engine: "kernel",
      kernel: {
        passed: false,
        clauses: [
          CLAUSES[0],
          { name: "lid_mates", passed: false, margin_mm: -0.2, detail: "lip binds" },
          { name: "headroom", passed: false, margin_mm: -1.1, detail: "U1 hits the lid" },
        ],
        warnings: [],
      },
    };
    expect(summarizeStep(step({ step: "case", enclosure }))).toBe(
      "Case built, 2 checks failed: lid_mates (−0.20 mm), headroom (−1.10 mm)."
    );
  });

  it("never claims a check that did not run", () => {
    // An engine this client does not know: no kernel report, so no check.
    expect(
      summarizeStep(step({ step: "case", enclosure: { engine: "laser" as never, kernel: null } }))
    ).toBe("Case generated; no acceptance check ran.");
    expect(
      summarizeStep(
        step({
          step: "case",
          enclosure: {
            engine: "laser" as never,
            kernel: null,
            fit: { margins_mm: { x: 1, y: 1, z: -0.5 } },
          },
        })
      )
    ).toBe("Case collides with the board: margins x +1.0, y +1.0, z -0.5 mm.");
    // A kernel engine that sent no report is not a passed report.
    expect(
      summarizeStep(step({ step: "case", enclosure: { engine: "kernel", kernel: null } }))
    ).toBe("Case built, kernel check did not run.");
    // A verdict without clauses claims nothing.
    expect(
      summarizeStep(
        step({ step: "case", enclosure: { engine: "kernel", kernel: { passed: true, clauses: [], warnings: [] } } })
      )
    ).toBe("Case built, kernel ran no checks.");
    // An old engine sending only the fit receipt: a negative margin is not a fit.
    expect(
      summarizeStep(
        step({ step: "case", enclosure: { fit: { margins_mm: { x: 1, y: -0.4, z: 2 } } } })
      )
    ).toBe("Case collides with the board: margins x +1.0, y -0.4, z +2.0 mm.");
    expect(summarizeStep(step({ step: "case", enclosure: {} }))).toBe(
      "Case generated; no acceptance check ran."
    );
  });
});

describe("caseDetails", () => {
  it("is null for any step but the case, and for a failed case", () => {
    expect(caseDetails(undefined)).toBeNull();
    expect(caseDetails(step({ step: "order" }))).toBeNull();
    expect(caseDetails(step({ step: "case", enclosure: null }))).toBeNull();
  });

  it("reads the frozen shape whole, files and brief included", () => {
    const details = caseDetails(
      step({
        step: "case",
        files: { case: "/tmp/steps/s1/enclosure.scad" },
        enclosure: {
          engine: "kernel",
          kernel: {
            passed: false,
            clauses: [{ name: "lid_mates", passed: false, margin_mm: -0.2, detail: "lip binds" }],
            warnings: ["U1 height defaulted to 3.0 mm"],
            thin_band_mm: 0.2,
          },
          files: {
            scad: "/tmp/steps/s1/enclosure.scad",
            step: "/tmp/steps/s1/enclosure.step",
            base_stl: "/tmp/steps/s1/enclosure-base.stl",
            lid_stl: null,
            snapshots: ["/tmp/steps/s1/snap-iso.png"],
          },
          brief: "Snap lid, USB-C on the left.",
          fit: { margins_mm: { x: 1, y: 1, z: 0.5 } },
          warnings: ["U1 height defaulted from the class table"],
        },
      })
    );
    expect(details).toEqual({
      engine: "kernel",
      kernel: {
        passed: false,
        clauses: [{ name: "lid_mates", passed: false, marginMm: -0.2, detail: "lip binds" }],
        warnings: ["U1 height defaulted to 3.0 mm"],
        thinBandMm: 0.2,
      },
      margins: { x: 1, y: 1, z: 0.5 },
      step: "/tmp/steps/s1/enclosure.step",
      scad: "/tmp/steps/s1/enclosure.scad",
      baseStl: "/tmp/steps/s1/enclosure-base.stl",
      lidStl: null,
      snapshots: ["/tmp/steps/s1/snap-iso.png"],
      reveal: "/tmp/steps/s1/enclosure.step",
      brief: "Snap lid, USB-C on the left.",
      warnings: ["U1 height defaulted from the class table"],
    });
  });

  it("parses a malformed block to nulls rather than throwing or inventing", () => {
    const details = caseDetails(
      step({
        step: "case",
        enclosure: {
          engine: "laser" as never,
          kernel: "yes" as never,
          files: "nope" as never,
          brief: "   ",
          fit: { margins_mm: { x: 1, y: 1 } },
          warnings: ["real", "", 3 as never],
        },
      })
    );
    expect(details).toEqual({
      engine: null,
      kernel: null,
      margins: null,
      step: null,
      scad: null,
      baseStl: null,
      lidStl: null,
      snapshots: [],
      reveal: null,
      brief: null,
      warnings: ["real"],
    });
  });

  it("keeps a nameless clause out and reads a non-boolean pass as failed", () => {
    const details = caseDetails(
      step({
        step: "case",
        enclosure: {
          engine: "kernel",
          kernel: {
            passed: true,
            clauses: [
              { name: "", passed: true, margin_mm: 1, detail: "" },
              { name: "min_wall", passed: "true", margin_mm: "0.3", detail: 7 },
            ] as never,
            warnings: null as never,
          },
        },
      })
    );
    expect(details?.kernel).toEqual({
      passed: true,
      clauses: [{ name: "min_wall", passed: false, marginMm: null, detail: "" }],
      warnings: [],
      // No band came with the report, so the client has none to judge by.
      thinBandMm: null,
    });
    expect(summarizeStep(step({ step: "case", enclosure: { kernel: { passed: true, clauses: [{ name: "min_wall", passed: "true", margin_mm: "0.3" }] } as never } }))).toBe(
      "Case built, 1 check failed: min_wall."
    );
  });

  it("falls back to the step envelope's case file and reveals whichever file exists", () => {
    const details = caseDetails(
      step({
        step: "case",
        files: { case: "/tmp/steps/s1/enclosure-base.stl" },
        enclosure: { engine: "kernel", kernel: null, files: null },
      })
    );
    expect(details?.step).toBeNull();
    expect(details?.scad).toBe("/tmp/steps/s1/enclosure-base.stl");
    expect(details?.reveal).toBe("/tmp/steps/s1/enclosure-base.stl");
    expect(formatMarginMm(-0.2)).toBe("−0.20");
    expect(formatMarginMm(0)).toBe("+0.00");
  });
});

const bomParts: SourcingEntry[] = [
  {
    ref: "U1",
    value: "AMS1117-3.3",
    kind: "device",
    package: "SOT-223-3_TabPin2",
    manufacturer: "Advanced Monolithic Systems",
    mpn: "AMS1117-3.3",
    mpn_status: "proposed",
    datasheet_url: "https://example.test/ds.pdf",
    datasheet_status: "verified",
    model3d: "${KISYS3DMOD}/Package_TO_SOT_SMD.3dshapes/SOT-223.step",
    model3d_note: null,
    note: null,
  },
  {
    ref: "C1",
    value: "10uF",
    kind: "capacitor",
    package: "C_0805",
    manufacturer: "Samsung",
    mpn: "CL21A106KAYNNNE",
    mpn_status: "proposed",
    datasheet_url: "https://example.test/viewer",
    datasheet_status: "not_pdf",
    model3d: "${KISYS3DMOD}/Capacitor_SMD.3dshapes/C_0805_2012Metric.step",
  },
  {
    ref: "Y1",
    value: "8MHz",
    kind: "crystal",
    package: "Crystal_1210",
    mpn: null,
    mpn_status: "none",
    datasheet_status: "none",
    model3d: null,
    model3d_note: "no KiCad library model for a 2-pin 1210 crystal",
  },
];

describe("sourcingDetails", () => {
  it("is null without a sourcing block, on any step", () => {
    expect(sourcingDetails(undefined)).toBeNull();
    expect(sourcingDetails(step({ step: "route" }))).toBeNull();
    expect(sourcingDetails(step({ step: "sourcing" }))).toBeNull();
    expect(sourcingDetails(step({ step: "order", order: { orderable: true } }))).toBeNull();
  });

  it("carries the rows, the engine's counts, the file and the warnings from either step", () => {
    const block = { parts: bomParts, verified: 1, proposed: 2, unresolved: 1, warnings: ["model gave up"] };
    const sourced = sourcingDetails(
      step({ step: "sourcing", files: { bom: "/t/b-bom.csv" }, sourcing: block })
    );
    expect(sourced).toEqual({
      parts: bomParts,
      verified: 1,
      proposed: 2,
      unresolved: 1,
      named: 2,
      confirmed: 0,
      bom: "/t/b-bom.csv",
      warnings: ["model gave up"],
    });
    // The order step carries the same block; the table follows it.
    const ordered = sourcingDetails(
      step({ step: "order", files: { bom: "/t/b-bom.csv", order: "/t/b-order.zip" }, sourcing: block, order: {} })
    );
    expect(ordered?.parts.map((p) => p.ref)).toEqual(["U1", "C1", "Y1"]);
    expect(ordered?.bom).toBe("/t/b-bom.csv");
  });

  it("counts from the rows when the engine sent no counts, never a quiet zero", () => {
    const details = sourcingDetails(step({ step: "sourcing", sourcing: { parts: bomParts } }));
    expect(details).toMatchObject({ verified: 1, proposed: 2, unresolved: 1, bom: null, warnings: [] });
  });
});

describe("orderDetails", () => {
  it("is null for any step but order", () => {
    expect(orderDetails(undefined)).toBeNull();
    expect(orderDetails(step({ step: "route" }))).toBeNull();
  });

  it("lists issues blockers first, keeps the gate's order within a severity, and names the files", () => {
    const details = orderDetails(
      step({
        step: "order",
        files: {
          board: "/t/b.kicad_pcb",
          order: "/t/b-order.zip",
          order_manifest: "/t/b-order.json",
          model_glb: "/t/b.glb",
        },
        warnings: ["step export failed: kicad-cli exited 3: x"],
        order: {
          orderable: false,
          issues: [
            { code: "n1", severity: "note", title: "Note one" },
            { code: "w1", severity: "warning", title: "Warn one" },
            { code: "b1", severity: "blocker", title: "Block one" },
            { code: "b2", severity: "blocker", title: "Block two" },
            { code: "n2", severity: "note", title: "Note two" },
          ],
        },
      })
    );
    expect(details).not.toBeNull();
    expect(details!.orderable).toBe(false);
    expect(details!.issues.map((i) => i.code)).toEqual(["b1", "b2", "w1", "n1", "n2"]);
    expect(details!.zip).toBe("/t/b-order.zip");
    expect(details!.manifest).toBe("/t/b-order.json");
    expect(details!.model).toBe("/t/b.glb");
    expect(details!.step).toBeNull();
    expect(details!.warnings).toEqual(["step export failed: kicad-cli exited 3: x"]);
  });

  it("has no model and no zip when the engine sent none, never a made-up path", () => {
    const details = orderDetails(step({ step: "order", order: { orderable: true } }));
    expect(details).toEqual({
      orderable: true,
      issues: [],
      zip: null,
      manifest: null,
      model: null,
      step: null,
      warnings: [],
    });
  });
});

describe("stepRows", () => {
  it("marks done, running and available from the engine's own next list", () => {
    const history = [
      step({ step: "propose", next: ["place"], shown_in_kicad: true }),
      step({ step: "place", stage: "placed", next: ["route"] }),
    ];
    const rows = stepRows(history, "route");
    const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
    expect(byId.propose.status).toBe("done");
    expect(byId.propose.shown).toBe(true);
    expect(byId.place.status).toBe("done");
    expect(byId.route.status).toBe("running");
    expect(byId.review.status).toBe("pending");
    expect(rows.map((r) => r.id)).toEqual([
      "propose",
      "place",
      "route",
      "review",
      "sourcing",
      "order",
      "case",
    ]);
  });

  it("shows a step the engine started on its own as preparing, still approvable", () => {
    const history = [
      step({
        step: "place",
        stage: "placed",
        next: ["route", "sourcing", "case"],
        background: ["sourcing", "case"],
      }),
    ];
    const byId = Object.fromEntries(stepRows(history, null).map((r) => [r.id, r]));
    expect(byId.case.status).toBe("preparing");
    expect(byId.case.summary).toBeNull();
    expect(byId.case.preparing).toBe("Designing in the background…");
    expect(byId.sourcing.status).toBe("preparing");
    expect(byId.sourcing.preparing).toBe("Looking up parts in the background…");
    expect(byId.route.status).toBe("available");
    expect(byId.route.preparing).toBeNull();
    // The engine decides what may run; preparing never hides the button.
    expect(availableSteps(history)).toEqual(["route", "sourcing", "case"]);
    // Once the design has settled the engine stops listing it.
    const settled = [step({ step: "route", stage: "routed", next: ["review", "order", "case"] })];
    expect(stepRows(settled, null).find((r) => r.id === "case")?.status).toBe("available");
    // An older engine that never sends `background` reads exactly as before.
    const old = [step({ step: "place", stage: "placed", next: ["route"] })];
    expect(stepRows(old, null).find((r) => r.id === "case")?.status).toBe("pending");
  });

  it("offers exactly what the latest response allows, in pipeline order", () => {
    const history = [
      step({ step: "route", stage: "routed", next: ["case", "order", "review"] }),
    ];
    expect(availableSteps(history)).toEqual(["review", "order", "case"]);
    expect(stepsExhausted(history)).toBe(false);
    expect(stepsExhausted([step({ step: "case", next: [] })])).toBe(true);
    expect(stepsExhausted([])).toBe(false);
  });

  it("marks a step the engine finished without answering as done, with no invented result", () => {
    const history = [step({ step: "propose", next: ["place"] })];
    const status: StepStatusResponse = {
      session: "s1",
      stage: "placed",
      intent: "a toy car",
      files: { placed_board: "/tmp/s1/board.placed.kicad_pcb" },
      done: ["place"],
      next: ["route"],
      kicad_live: true,
    };
    const reconciled = reconcileHistory(history, status);
    expect(reconciled.history.map((r) => r.step)).toEqual(["propose", "place"]);
    expect(reconciled.available).toEqual(["route"]);
    const marker = reconciled.history[1];
    expect(isUnreceivedStep(marker)).toBe(true);
    expect(isUnreceivedStep(history[0])).toBe(false);
    expect(summarizeStep(marker)).toBe(UNRECEIVED_SUMMARY);
    expect(marker.events).toEqual([]);
    expect(marker.shown_in_kicad).toBe(false);
    // The checklist and the offer read the marker like any other response.
    const byId = Object.fromEntries(stepRows(reconciled.history, null).map((r) => [r.id, r]));
    expect(byId.place.status).toBe("done");
    expect(byId.place.summary).toBe(UNRECEIVED_SUMMARY);
    expect(byId.route.status).toBe("available");
    expect(availableSteps(reconciled.history)).toEqual(["route"]);
    // Already-heard steps are never duplicated, and a second reconcile is a no-op.
    expect(reconcileHistory(reconciled.history, status).history).toHaveLength(2);
  });
});

describe("step client", () => {
  it("starts a session with the live-bridge flag and one request", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, step({ step: "propose", next: ["place"] }))
    );
    const result = await startSteps("http://e", {
      intent: "a toy car",
      time_limit_s: 5,
      kicad_live: true,
    });
    expect(result.session).toBe("s1");
    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockFetch.mock.calls[0];
    expect(String(url)).toBe("http://e/steps");
    const body = JSON.parse(String(init?.body));
    expect(body.intent).toBe("a toy car");
    expect(body.kicad_live).toBe(true);
    expect(body.review).toBeUndefined();
  });

  it("advances one step and surfaces a 409 as a request error, never retrying", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(409, { error: "step 'route' needs the run to be 'placed'" })
    );
    await expect(advanceStep("http://e", "s1", "route")).rejects.toMatchObject({
      kind: "request",
      status: 409,
    });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(String(mockFetch.mock.calls[0][0])).toBe("http://e/steps/s1/route");
  });

  it("reports an unreachable engine as offline", async () => {
    mockFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    await expect(advanceStep("http://e", "s1", "place")).rejects.toBeInstanceOf(
      SilkscreenError
    );
  });
});

describe("interpretCommand", () => {
  it("reads an available step's name as approval of that step", () => {
    const available = ["review", "order", "case"] as const;
    expect(interpretCommand("sg alr lets order", available)).toEqual({ kind: "approve", step: "order" });
    expect(interpretCommand("Design the enclosure now", available)).toEqual({ kind: "approve", step: "case" });
    expect(interpretCommand("review it", available)).toEqual({ kind: "approve", step: "review" });
    expect(interpretCommand("source the parts", ["sourcing", "order"])).toEqual({
      kind: "approve",
      step: "sourcing",
    });
    expect(interpretCommand("get me a bom", ["sourcing", "order"])).toEqual({
      kind: "approve",
      step: "sourcing",
    });
  });

  it("matches restart phrases as whole words, never as substrings", () => {
    expect(interpretCommand("check it against the datasheet", ["review"])).toEqual({
      kind: "approve",
      step: "review",
    });
    expect(interpretCommand("run it again", ["order"])).toEqual({ kind: "restart" });
    expect(interpretCommand("Restart.", ["order"])).toEqual({ kind: "restart" });
    // Neither is a restart, and neither is a parse failure either: they fall
    // through to `request`, carrying the sentence whole so the caller can
    // offer it back. The restart guard is what is under test, not the fall-through.
    expect(interpretCommand("scrappy little board", ["place"])).toEqual({
      kind: "request",
      text: "scrappy little board",
    });
    expect(interpretCommand("new boards are nice", ["place"])).toEqual({
      kind: "request",
      text: "new boards are nice",
    });
  });

  it("treats bare agreement as the first available step", () => {
    expect(interpretCommand("ok go", ["route"])).toEqual({ kind: "approve", step: "route" });
    expect(interpretCommand("yeah", ["place"])).toEqual({ kind: "approve", step: "place" });
  });

  it("never approves a step that is not available, and never starts a run by itself", () => {
    // Naming a stage out of turn is `later`, which names the stage asked for
    // so the caller can say which one comes first. It is emphatically not an
    // approval of the stage that *is* available.
    expect(interpretCommand("route it", ["place"])).toEqual({ kind: "later", step: "route" });
    // A board description is a request, kept whole. It is still not a run:
    // nothing in this module starts anything, and the page arms it.
    expect(interpretCommand("a 3.3V regulator board", ["place"])).toEqual({
      kind: "request",
      text: "a 3.3V regulator board",
    });
    expect(interpretCommand("border patrol", ["order"])).toEqual({
      kind: "request",
      text: "border patrol",
    });
    // Bare agreement with nothing to agree to. "go" is not a board, so it is
    // `none` rather than a request — the one input that must not become one.
    expect(interpretCommand("go", [])).toEqual({ kind: "none" });
    expect(interpretCommand("   ", ["place"])).toEqual({ kind: "none" });
  });

  it("recognises a restart", () => {
    expect(interpretCommand("start over with a new board", ["order"])).toEqual({ kind: "restart" });
  });
});

describe("marginAxis", () => {
  const kernel = (
    clauses: Array<[string, boolean, number | null]>,
    thinBandMm: number | null = 0.2
  ) => ({
    passed: clauses.every(([, passed]) => passed),
    clauses: clauses.map(([name, passed, marginMm]) => ({
      name,
      passed,
      marginMm,
      detail: "",
    })),
    warnings: [],
    thinBandMm,
  });

  it("keeps every clause, in the kernel's order", () => {
    const axis = marginAxis(kernel([["b", true, 1], ["a", true, 2], ["c", false, -1]]));
    expect(axis.ticks.map((t) => t.name)).toEqual(["b", "a", "c"]);
  });

  it("puts zero inside the domain even when nothing is negative", () => {
    const axis = marginAxis(kernel([["a", true, 1], ["b", true, 3]]));
    expect(axis.loMm).toBe(0);
    expect(axis.hiMm).toBe(3);
    expect(axis.zeroX).toBe(0);
    // A clause a third of the way along the domain sits a third of the way across.
    expect(axis.ticks[0].x).toBeCloseTo(1 / 3, 10);
  });

  it("places a failing clause left of zero and a passing one right of it", () => {
    const axis = marginAxis(kernel([["fail", false, -0.2], ["pass", true, 0.6]]));
    const zero = axis.zeroX!;
    expect(axis.ticks[0].x!).toBeLessThan(zero);
    expect(axis.ticks[1].x!).toBeGreaterThan(zero);
  });

  it("calls a pass inside the band thin, and a fail is never thin", () => {
    const axis = marginAxis(
      kernel([["tight", true, 0.02], ["roomy", true, 5], ["broken", false, -9]])
    );
    expect(axis.ticks.map((t) => t.tone)).toEqual(["thin", "clear", "fail"]);
    expect(axis.thin.map((t) => t.name)).toEqual(["tight"]);
    expect(axis.fail.map((t) => t.name)).toEqual(["broken"]);
    expect(axis.clearCount).toBe(1);
  });

  it("marks nothing thin when the engine sent no band", () => {
    const axis = marginAxis(kernel([["tight", true, 0.02]], null));
    expect(axis.ticks[0].tone).toBe("clear");
    expect(axis.bandMm).toBeNull();
  });

  it("survives a report where every margin is identical", () => {
    const axis = marginAxis(kernel([["a", true, 1], ["b", true, 1]]));
    expect(axis.ticks.every((t) => t.x === 0.5 || t.x === 1)).toBe(true);
    expect(Number.isFinite(axis.ticks[0].x!)).toBe(true);
  });

  it("keeps an unmeasured clause without placing it", () => {
    const axis = marginAxis(kernel([["unmeasured", false, null], ["a", true, 1]]));
    expect(axis.ticks[0].x).toBeNull();
    expect(axis.ticks[0].tone).toBe("fail");
  });
});

describe("interpretCommand restart phrases", () => {
  it("a bare 'again' is not a restart: a named stage wins, and nothing else is guessed", () => {
    expect(interpretCommand("route it again", ["route"])).toEqual({ kind: "approve", step: "route" });
    expect(interpretCommand("check again", ["review"])).toEqual({ kind: "approve", step: "review" });
    expect(interpretCommand("again", ["route"])).toEqual({ kind: "request", text: "again" });
    expect(interpretCommand("look again", ["route"])).toEqual({
      kind: "request",
      text: "look again",
    });
  });

  it("the two-word restarts still restart, as whole words", () => {
    for (const text of ["do it again", "start fresh", "from scratch", "over again"]) {
      expect(interpretCommand(text, ["route"])).toEqual({ kind: "restart" });
    }
    expect(interpretCommand("scratchy", ["route"])).toEqual({ kind: "request", text: "scratchy" });
    // After a failed stage "try again" means a retry, not a fresh board — so
    // it must not restart. It is a request, which the page offers back.
    expect(interpretCommand("try again", ["route"])).toEqual({
      kind: "request",
      text: "try again",
    });
  });
});

describe("armCommand", () => {
  it("arms an approval and a restart alike, echoing the sentence trimmed", () => {
    expect(armCommand({ kind: "approve", step: "route" }, "  route it ")).toEqual({
      step: "route",
      source: "route it",
    });
    expect(armCommand({ kind: "restart" }, "start over\n")).toEqual({
      step: "restart",
      source: "start over",
    });
  });

  it("arms nothing for the three kinds whose consequence the page decides", () => {
    // A request is not armed here because what it arms depends on whether a
    // run is in flight, which this module cannot see.
    expect(armCommand({ kind: "request", text: "hello" }, "hello")).toBeNull();
    expect(armCommand({ kind: "later", step: "route" }, "route it")).toBeNull();
    expect(armCommand({ kind: "none" }, "")).toBeNull();
  });
});

describe("headline", () => {
  const input = (partial: Partial<Parameters<typeof headline>[0]>) => ({
    status: "waiting" as const,
    running: null,
    latest: undefined,
    elapsedS: 0,
    error: null,
    ...partial,
  });

  it("speaks in the first person while a stage runs", () => {
    expect(headline(input({ status: "running", running: "route", elapsedS: 12.4 }))).toBe(
      "I am routing copper. 12 s so far."
    );
  });

  it("names where a shown stage is, and asks nothing more once it has been looked at", () => {
    const latest = step({ step: "place", next: ["route"], shown_in_kicad: true });
    expect(headline(input({ latest }))).toBe("Placement is in KiCad.");
    expect(headline(input({ latest, reviewed: false }))).toBe(
      "Placement is in KiCad. Have you looked?"
    );
  });

  it("separates a bridge that failed from one that was never asked, and never nags either", () => {
    const failed = step({
      step: "place",
      next: ["route"],
      shown_in_kicad: false,
      shown_detail: "placement was not shown in KiCad: the API server is off",
    });
    const unasked = step({ step: "place", next: ["route"], shown_in_kicad: false });
    // `reviewed: false` is the nag's condition, and it must not fire here.
    expect(headline(input({ latest: failed, reviewed: false }))).toBe(
      "Placement is on disk. I could not show it in KiCad."
    );
    expect(headline(input({ latest: unasked, reviewed: false }))).toBe(
      "Placement is on disk; I did not show it in KiCad."
    );
    expect(headlineFix(input({ latest: failed }))).toBe(
      "placement was not shown in KiCad: the API server is off"
    );
    // Nothing invented for a bridge nobody asked to start.
    expect(headlineFix(input({ latest: unasked }))).toBeNull();
    expect(headlineFix(input({ latest: step({ step: "review", next: ["order"] }) }))).toBeNull();
  });

  it("carries the review's measurement into the sentence", () => {
    const latest = step({
      step: "review",
      next: ["order"],
      findings: [{ severity: "blocker", title: "x" }, { severity: "note", title: "y" }],
      blockers: ["x"],
    });
    expect(headline(input({ latest }))).toBe("Review is here. 2 findings, 1 blocking.");
  });

  it("lets an armed command take the sentence over, and says what is left when nothing is", () => {
    const latest = step({ step: "place", next: ["route"], shown_in_kicad: true });
    expect(headline(input({ latest, armed: { step: "route", source: "sg route it" } }))).toBe(
      "Confirm: route copper?"
    );
    expect(headline(input({ latest, armed: { step: "restart", source: "start over" } }))).toBe(
      "Confirm: start over?"
    );
    expect(headline(input({ latest, available: [] }))).toBe(
      "Every stage has run. Nothing was ordered."
    );
  });
});

describe("finding provenance", () => {
  it("reads a rule's finding as measured and a critic's as suggested", () => {
    expect(findingProvenance({ severity: "note", origin: "proven", rule: "clearance" })).toEqual({
      origin: "measured",
      label: "MEASURED",
      source: "rule clearance, measured on the board",
      measured: true,
      evidence: null,
      rule: "clearance",
    });
    const suggested = findingProvenance({ severity: "blocker" });
    expect(suggested.measured).toBe(false);
    expect(suggested.label).toBe("SUGGESTED");
    expect(suggested.source).toBe("critic, not measured");
  });

  it("quotes a citation with its page, and reports nothing where nothing was cited", () => {
    expect(findingCitation({ severity: "note", citation: "AMS1117 datasheet p. 9: 22 µF" })).toEqual({
      text: "AMS1117 datasheet p. 9: 22 µF",
      page: "9",
    });
    expect(findingCitation({ severity: "note", citation: "the datasheet, pages 4–5" })?.page).toBe(
      "4–5"
    );
    // A citation with no page is still a citation; the page is never invented.
    expect(findingCitation({ severity: "note", citation: "the datasheet" })?.page).toBeNull();
    expect(findingCitation({ severity: "note", citation: "  " })).toBeNull();
    expect(findingCitation({ severity: "note" })).toBeNull();
  });

  it("flags a severity in the policy vocabulary, and an unknown one as itself", () => {
    expect(policyFlag("blocker")).toEqual({ label: "BLOCKER", tone: "bad" });
    expect(policyFlag("marginal")).toEqual({ label: "MARGINAL", tone: "warn" });
    expect(policyFlag("note")).toEqual({ label: "NOTE", tone: "muted" });
    expect(policyFlag("catastrophic")).toEqual({ label: "CATASTROPHIC", tone: "muted" });
  });
});

describe("runReceipt", () => {
  const routed = step({
    step: "route",
    files: { board: "/tmp/b.kicad_pcb" },
    routing: { routed: ["VBUS", "GND"], unrouted: { VOUT: "no path at 0.25 mm clearance" } },
  });

  it("puts what was measured under Verified and everything else under Not verified", () => {
    const receipt = runReceipt([routed]);
    expect(receipt.verified).toContain("2 of 3 nets routed");
    expect(receipt.notVerified).toContain("1 net left as ratsnest: VOUT");
    expect(receipt.notVerified).toContain("ERC and DRC are yours to run in KiCad");
  });

  it("never lets a model's finding read as a measurement", () => {
    const critiqued = step({
      step: "review",
      findings: [
        { severity: "blocker", title: "a" },
        { severity: "note", title: "b", origin: "proven", rule: "clearance" },
      ],
      blockers: ["a"],
    });
    const receipt = runReceipt([critiqued]);
    expect(receipt.verified).toContain("1 of 2 findings measured by a rule");
    expect(receipt.notVerified).toContain("1 finding from the critic, suggested, not measured");
  });

  it("says an empty finding list is not a measurement", () => {
    expect(runReceipt([step({ step: "review", findings: [] })]).notVerified).toContain(
      "the critic raised no findings, which is not a measurement"
    );
  });

  it("names the stages that never ran, and ends with the submission sentence", () => {
    const receipt = runReceipt([routed]);
    expect(receipt.notDone[0]).toBe(
      "Never run: Schematic, Placement, Review, Sourcing, Order, Case."
    );
    expect(receipt.notDone[receipt.notDone.length - 1]).toBe(
      "Nothing is submitted. Sending the package to a fab is yours to do."
    );
    // Three groups, and the closing sentence is the last line of the last one.
    expect(Object.keys(receipt)).toEqual(["verified", "notVerified", "notDone"]);
  });
});

// ---------------------------------------------------------------- background jobs

import {
  backgroundFailure,
  backgroundJobs,
  backgroundNote,
  casePayload,
  routeDetails,
} from "./steps";

describe("background jobs", () => {
  const placed = step({
    step: "place",
    stage: "placed",
    next: ["route", "sourcing", "case"],
    background: ["sourcing", "case"],
  });

  it("is running while the engine lists it, settled once it stops, never failed on silence", () => {
    const running = backgroundJobs([], ["sourcing", "case"], [placed]);
    expect(running).toEqual([
      { step: "sourcing", state: "running", warning: null },
      { step: "case", state: "running", warning: null },
    ]);
    // The engine's list emptied and no step collected either: finished,
    // outcome unknown. Not "done", and not "failed" — nothing said so.
    const settled = backgroundJobs(["sourcing", "case"], [], [placed]);
    expect(settled.map((j) => [j.step, j.state])).toEqual([
      ["sourcing", "settled"],
      ["case", "settled"],
    ]);
    expect(settled.every((j) => j.warning === null)).toBe(true);
  });

  it("names a failure only from the engine's own warning, on whichever step collected it", () => {
    const warning =
      "parts were not sourced: the lookup in the background failed (ModelError: 429 RESOURCE_EXHAUSTED); the BOM lists the board's parts with no part numbers";
    // `order` collects the BOM when sourcing was never pressed; its warning
    // is about the sourcing row.
    const history = [placed, step({ step: "order", next: ["sourcing", "case"], warnings: [warning] })];
    expect(backgroundFailure("sourcing", history)).toBe(warning);
    expect(backgroundFailure("case", history)).toBeNull();
    const jobs = backgroundJobs(["sourcing", "case"], [], history);
    expect(jobs).toEqual([
      { step: "sourcing", state: "failed", warning },
      { step: "case", state: "settled", warning: null },
    ]);
    expect(backgroundNote(jobs[0])).toBe(warning);
    expect(backgroundNote(jobs[1])).toContain("press to collect");
    expect(backgroundNote(jobs[1])).toContain("only when collected");
    expect(backgroundNote({ step: "case", state: "running", warning: null })).toBeNull();
    // Both of the engine's case sentences match: `_case_failure` (the job
    // raised) and `NO_CASE_WARNING` (it finished with nothing). Byte-for-byte
    // the strings service/steps.py writes; a rewording there fails here.
    const raised = "the case designed in the background failed: ModelError: 503";
    const nothing = "enclosure generation failed; the board stands without a case";
    for (const warning of [raised, nothing]) {
      const collected = [placed, step({ step: "case", next: [], warnings: [warning] })];
      expect(backgroundFailure("case", collected)).toBe(warning);
      expect(backgroundFailure("sourcing", collected)).toBeNull();
    }
    expect(backgroundNote(undefined)).toBeNull();
  });

  it("drops a job once its step has been collected: the outcome is on the row itself", () => {
    const history = [placed, step({ step: "case", next: ["route"], enclosure: null })];
    expect(backgroundJobs(["sourcing", "case"], [], history).map((j) => j.step)).toEqual(["sourcing"]);
  });

  it("the rows prefer the hook's picture over the envelope's, and carry the note", () => {
    const history = [placed];
    // The envelope alone still says both are running.
    expect(stepRows(history, null).find((r) => r.id === "case")?.status).toBe("preparing");
    // A poll of GET /steps/<id> learnt the case has settled and the BOM is still going.
    const jobs = backgroundJobs(["sourcing", "case"], ["sourcing"], history);
    const rows = stepRows(history, null, jobs);
    const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
    expect(byId.sourcing.status).toBe("preparing");
    expect(byId.sourcing.backgroundNote).toBeNull();
    expect(byId.case.status).toBe("available");
    expect(byId.case.backgroundNote).toContain("Finished designing in the background");
    expect(byId.route.backgroundNote).toBeNull();
    // Rows for steps that never ran in the background carry no note at all.
    expect(stepRows(history, null).every((r) => r.backgroundNote === null)).toBe(true);
  });
});

describe("casePayload", () => {
  it("sends nothing to collect the background design, and only what was set to design afresh", () => {
    expect(casePayload("", false)).toEqual({});
    expect(casePayload("   ", false)).toEqual({});
    expect(casePayload("", true)).toEqual({ enclosure_rigorous: true });
    expect(casePayload("  snap lid, vented ", false)).toEqual({ enclosure_style: "snap lid, vented" });
    expect(casePayload("wall tabs", true)).toEqual({
      enclosure_style: "wall tabs",
      enclosure_rigorous: true,
    });
  });
});

describe("routeDetails", () => {
  it("counts every net the router was asked about and keeps the engine's own completion", () => {
    const details = routeDetails(
      step({
        step: "route",
        routing: {
          tracks: 12,
          vias: 2,
          routed: ["VCC", "GND"],
          unrouted: { SDA: "no path at 0.25 mm pitch", SCL: "budget exhausted" },
          warnings: ["one warning"],
          completion: 0.5,
        },
        warnings: ["a step warning"],
      })
    );
    expect(details).toEqual({
      routed: 2,
      total: 4,
      unrouted: [
        { net: "SDA", reason: "no path at 0.25 mm pitch" },
        { net: "SCL", reason: "budget exhausted" },
      ],
      tracks: 12,
      vias: 2,
      completion: 0.5,
      warnings: ["one warning", "a step warning"],
    });
  });

  it("a refusal that named its nets is 0 of N, never an empty 100%", () => {
    const details = routeDetails(
      step({
        step: "route",
        routing: {
          tracks: 0,
          vias: 0,
          routed: [],
          unrouted: { VCC: "board area is empty; nothing routed", GND: "board area is empty; nothing routed" },
          completion: 0,
        },
      })
    );
    expect(details?.routed).toBe(0);
    expect(details?.total).toBe(2);
    expect(details?.completion).toBe(0);
    // An engine that sends no completion gets null, not a number made up here.
    expect(routeDetails(step({ step: "route", routing: { routed: ["A"] } }))?.completion).toBeNull();
    expect(routeDetails(step({ step: "place" }))).toBeNull();
  });
});

// ---------------------------------------------------------------- review outcome

import {
  REVIEW_CLEAN_LINE,
  mergeBackgroundOutcomes,
  receiptLine,
  reviewDetails,
  reviewFailure,
  reviewOutcome,
  reviewSkipped,
} from "./steps";
import type { ReviewBlock } from "./types";

const REVIEW_OK: ReviewBlock = { status: "ok", ran: true, detail: null, note: "the critic answered" };
const REVIEW_FAILED: ReviewBlock = {
  status: "failed",
  ran: true,
  detail: "the critic answered nothing readable (ModelError: 503)",
  note: "review failed",
};
const REVIEW_SKIPPED: ReviewBlock = { status: "skipped", ran: false, detail: null, note: "review was not requested" };

describe("review outcome", () => {
  it("reads the three statuses, and an older engine's missing block as ok", () => {
    expect(reviewOutcome(REVIEW_OK)).toEqual({ status: "ok", ran: true, detail: null, note: "the critic answered" });
    expect(reviewOutcome(REVIEW_FAILED).status).toBe("failed");
    expect(reviewOutcome(REVIEW_SKIPPED)).toEqual({
      status: "skipped",
      ran: false,
      detail: null,
      note: "review was not requested",
    });
    // No block at all: the reading every caller had before the block existed.
    expect(reviewOutcome(undefined)).toEqual({ status: "ok", ran: true, detail: null, note: "" });
    // A status this build has never heard of is not a failure it can name.
    expect(reviewOutcome({ ...REVIEW_OK, status: "partial" }).status).toBe("ok");
  });

  it("writes the failed sentence with the engine's detail, and nothing for an ok review", () => {
    expect(reviewFailure(REVIEW_FAILED)).toBe(
      "review failed: the critic answered nothing readable (ModelError: 503) — nothing is known about this board"
    );
    // No detail: the note stands in; no note either: a fixed reason, never a blank.
    expect(reviewFailure({ ...REVIEW_FAILED, detail: null })).toBe(
      "review failed: review failed — nothing is known about this board"
    );
    expect(reviewFailure({ ...REVIEW_FAILED, detail: "  ", note: "" })).toBe(
      "review failed: the critic answered nothing readable — nothing is known about this board"
    );
    expect(reviewFailure(REVIEW_OK)).toBeNull();
    expect(reviewFailure(undefined)).toBeNull();
    expect(reviewSkipped(REVIEW_SKIPPED)).toBe(
      "review skipped: review was not requested — nothing is known about this board"
    );
    expect(reviewSkipped(REVIEW_FAILED)).toBeNull();
  });

  it("the details carry the verdict beside the findings, and an empty list is not clean on a failed one", () => {
    const failed = reviewDetails(step({ step: "review", findings: [], blockers: [], review: REVIEW_FAILED }));
    expect(failed?.status).toBe("failed");
    expect(failed?.detail).toBe("the critic answered nothing readable (ModelError: 503)");
    expect(failed?.findings).toEqual([]);
    const clean = reviewDetails(step({ step: "review", findings: [], blockers: [], review: REVIEW_OK }));
    expect(clean?.status).toBe("ok");
    // Older engine: no block, so the reading is ok and the findings stand alone.
    const older = reviewDetails(step({ step: "review", findings: [{ severity: "note", title: "x" }] }));
    expect(older?.status).toBe("ok");
    expect(older?.findings).toHaveLength(1);
  });

  it("says failed, not 'no findings', on the sentence, the receipt clause and the run receipt", () => {
    const failed = step({ step: "review", findings: [], blockers: [], review: REVIEW_FAILED });
    expect(summarizeStep(failed)).toBe(
      "Review failed: the critic answered nothing readable (ModelError: 503) — nothing is known about this board."
    );
    expect(receiptLine(failed)).toBe("review failed, nothing known");
    const receipt = runReceipt([failed]);
    expect(receipt.notVerified).toContain(
      "review failed: the critic answered nothing readable (ModelError: 503) — nothing is known about this board"
    );
    expect(receipt.notVerified).not.toContain("the critic raised no findings, which is not a measurement");
    expect(receipt.verified.some((line) => line.includes("finding"))).toBe(false);

    const skipped = step({ step: "review", findings: [], blockers: [], review: REVIEW_SKIPPED });
    expect(summarizeStep(skipped)).toBe(
      "Review skipped: review was not requested — nothing is known about this board."
    );
    expect(receiptLine(skipped)).toBe("review skipped, nothing known");
    expect(runReceipt([skipped]).notVerified).toContain(
      "review skipped: review was not requested — nothing is known about this board"
    );

    // An ok review with no findings keeps the clean line and the honest receipt clause.
    const clean = step({ step: "review", findings: [], blockers: [], review: REVIEW_OK });
    expect(summarizeStep(clean)).toBe(REVIEW_CLEAN_LINE);
    expect(receiptLine(clean)).toBe("no findings");
    expect(runReceipt([clean]).notVerified).toContain(
      "the critic raised no findings, which is not a measurement"
    );
    // A failed review with findings somehow attached is still failed: the
    // findings are not counted as anything.
    const contradictory = step({
      step: "review",
      findings: [{ severity: "blocker", title: "x" }],
      blockers: ["x"],
      review: REVIEW_FAILED,
    });
    expect(receiptLine(contradictory)).toBe("review failed, nothing known");
  });
});

describe("background outcomes", () => {
  const placed = step({
    step: "place",
    stage: "placed",
    next: ["route", "sourcing", "case"],
    background: ["sourcing", "case"],
  });

  it("reads finished and failed from the engine's own outcome, verbatim", () => {
    const jobs = backgroundJobs(["sourcing", "case"], [], [placed], {
      case: { ok: true, detail: null },
      sourcing: { ok: false, detail: "ModelError: 429 RESOURCE_EXHAUSTED" },
    });
    expect(jobs).toEqual([
      { step: "sourcing", state: "failed", warning: "ModelError: 429 RESOURCE_EXHAUSTED" },
      { step: "case", state: "finished", warning: null },
    ]);
    expect(backgroundNote(jobs[0])).toBe("ModelError: 429 RESOURCE_EXHAUSTED");
    expect(backgroundNote(jobs[1])).toBe("Case design finished in the background — press to collect");
    expect(backgroundNote({ step: "sourcing", state: "finished", warning: null })).toBe(
      "Parts lookup finished in the background — press to collect"
    );
    // The finished line never claims the engine reports only when collected.
    expect(backgroundNote(jobs[1])).not.toContain("only when collected");
  });

  it("a failure with no detail still says it failed, in words", () => {
    const [job] = backgroundJobs(["case"], [], [placed], { case: { ok: false, detail: null } });
    expect(job.state).toBe("failed");
    expect(job.warning).toContain("failed");
    expect(backgroundNote(job)).toBe(job.warning);
  });

  it("the outcome wins over the list, and names a job the client never saw running", () => {
    // A resync after a cancelled step: the case finished before this client
    // ever saw it listed. The outcome alone is enough to show the row.
    const jobs = backgroundJobs([], [], [placed], { case: { ok: true, detail: null } });
    expect(jobs).toEqual([{ step: "case", state: "finished", warning: null }]);
    // Still listed as running: the list is the present tense and wins.
    expect(backgroundJobs([], ["case"], [placed], { case: { ok: true, detail: null } })).toEqual([
      { step: "case", state: "running", warning: null },
    ]);
    // Collected: off the job list, outcome on its own row.
    const collected = [placed, step({ step: "case", next: ["route"], enclosure: null })];
    expect(backgroundJobs([], [], collected, { case: { ok: true, detail: null } })).toEqual([]);
  });

  it("without an outcome the older reading stands: settled, or failed only from a warning", () => {
    const jobs = backgroundJobs(["sourcing", "case"], [], [placed]);
    expect(jobs.map((j) => j.state)).toEqual(["settled", "settled"]);
    expect(backgroundNote(jobs[1])).toContain("only when collected");
    // The two readings mix per job: an outcome for one, the warning path for the other.
    const warning =
      "parts were not sourced: the lookup in the background failed (ModelError: 503); the BOM lists the board's parts with no part numbers";
    const history = [placed, step({ step: "order", next: ["sourcing", "case"], warnings: [warning] })];
    expect(backgroundJobs(["sourcing", "case"], [], history, { case: { ok: true, detail: null } })).toEqual([
      { step: "sourcing", state: "failed", warning },
      { step: "case", state: "finished", warning: null },
    ]);
  });

  it("merges outcomes across envelopes, the newer word winning per job", () => {
    const first = mergeBackgroundOutcomes({}, { case: { ok: true, detail: null } });
    expect(first).toEqual({ case: { ok: true, detail: null } });
    const second = mergeBackgroundOutcomes(first, { sourcing: { ok: false, detail: "429" } });
    expect(second).toEqual({ case: { ok: true, detail: null }, sourcing: { ok: false, detail: "429" } });
    expect(mergeBackgroundOutcomes(second, undefined)).toBe(second);
    // A malformed entry is ignored rather than read as a verdict.
    expect(
      mergeBackgroundOutcomes({}, { case: { ok: "yes" as unknown as boolean, detail: null } })
    ).toEqual({});
    expect(mergeBackgroundOutcomes({}, { case: { ok: false, detail: 5 as unknown as string } })).toEqual({
      case: { ok: false, detail: null },
    });
  });

  it("the rows carry the finished note, and the failed one in the engine's words", () => {
    const jobs = backgroundJobs(["sourcing", "case"], [], [placed], {
      case: { ok: true, detail: null },
      sourcing: { ok: false, detail: "the model never produced valid JSON" },
    });
    const rows = stepRows([placed], null, jobs);
    const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
    expect(byId.case.status).toBe("available");
    expect(byId.case.backgroundNote).toBe("Case design finished in the background — press to collect");
    expect(byId.sourcing.backgroundNote).toBe("the model never produced valid JSON");
  });
});

// @vitest-environment jsdom
//
// The step panel's outcome blocks. The case: the engine line, the kernel's
// clause-by-clause receipt, its warnings, and the two buttons that hand a
// path to the OS — the panel only ever says what the engine reported, an
// engine with no kernel report says no kernel check ran, and no file means
// no button.
// The order: the reasons behind the verdict, the package on disk, the 3D
// model when there is one. The BOM: one row per part, proposals labelled as
// such. A button that cannot open its file says so with the path rather than
// doing nothing.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => ({
  openPath: vi.fn(() => Promise.resolve()),
  revealItemInDir: vi.fn(() => Promise.resolve()),
  openUrl: vi.fn(() => Promise.resolve()),
}));
vi.mock("@tauri-apps/plugin-fs", () => ({ readFile: vi.fn(() => new Promise(() => {})) }));

import { openPath, openUrl, revealItemInDir } from "@tauri-apps/plugin-opener";
import type { StepRun } from "@/hooks/useStepRun";
import { NO_CITATION } from "@/lib/silkscreen/steps";
import type { EnclosureBlock, KernelClause, StepName, StepResponse } from "@/lib/silkscreen/types";
import { StepPanel } from "./StepPanel";

const mockOpenPath = vi.mocked(openPath);
const mockReveal = vi.mocked(revealItemInDir);
const mockOpenUrl = vi.mocked(openUrl);

const STEP_PATH = "/tmp/steps/s1/enclosure.step";
const FILES = {
  step: STEP_PATH,
  base_stl: "/tmp/steps/s1/enclosure-base.stl",
  lid_stl: "/tmp/steps/s1/enclosure-lid.stl",
};

/** Every frozen clause name, in the kernel's order: thirteen of them. */
const CLAUSE_NAMES = [
  "valid_topology",
  "positive_volume",
  "solid_count",
  "bbox",
  "board_clash",
  "headroom",
  "underside",
  "standoff_concentric",
  "cutout_admits_plug",
  "lid_mates",
  "min_wall",
  "overhang",
  "vent_keepout",
] as const;

const FAILING: Record<string, { margin_mm: number; detail: string }> = {
  lid_mates: { margin_mm: -0.2, detail: "lip binds on the base wall" },
  headroom: { margin_mm: -1.1, detail: "tallest part pokes through the lid" },
};

function clauses(): KernelClause[] {
  return CLAUSE_NAMES.map((name) => {
    const fail = FAILING[name];
    return fail
      ? { name, passed: false, margin_mm: fail.margin_mm, detail: fail.detail }
      : { name, passed: true, margin_mm: 0.5, detail: "" };
  });
}

function caseStep(enclosure: EnclosureBlock): StepResponse {
  return {
    session: "s1",
    step: "case",
    stage: "routed",
    intent: "a 3.3V LDO board",
    files: { board: "/tmp/steps/s1/board.kicad_pcb" },
    next: [],
    shown_in_kicad: true,
    events: [],
    duration_s: 4,
    enclosure,
  };
}

function runWith(latest: StepResponse): StepRun {
  return {
    status: "done",
    session: "s1",
    history: [latest],
    running: null,
    failedStep: null,
    available: [],
    error: null,
    elapsedS: 0,
    start: vi.fn(),
    approve: vi.fn(),
    cancel: vi.fn(),
    reset: vi.fn(),
  };
}

const kernelEnclosure: EnclosureBlock = {
  engine: "kernel",
  brief: "A snap-lid box with a USB-C cutout.",
  kernel: {
    passed: false,
    clauses: clauses(),
    warnings: ["overhang check skipped: no print orientation given"],
    // The band the engine measures thinness against, from rules.py.
    thin_band_mm: 0.2,
  },
  files: FILES,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("StepPanel case outcome", () => {
  it("names the kernel engine and the failing count", () => {
    render(<StepPanel run={runWith(caseStep(kernelEnclosure))} onDismiss={() => {}} />);
    const engine = screen.getByTestId("case-engine");
    expect(engine.getAttribute("data-engine")).toBe("kernel");
    expect(engine.textContent).toContain("build123d kernel");
    expect(engine.textContent).toContain("2 of 13 checks failed");
    expect(screen.getByTestId("case-kernel-verdict").getAttribute("data-passed")).toBe("false");
    expect(screen.getByTestId("case-brief").textContent).toBe("A snap-lid box with a USB-C cutout.");
  });

  it("carries every clause as a tick, in the kernel's order, nothing dropped", () => {
    render(<StepPanel run={runWith(caseStep(kernelEnclosure))} onDismiss={() => {}} />);
    const ticks = screen.getAllByTestId("case-clause-tick");
    expect(ticks).toHaveLength(13);
    expect(ticks.map((t) => t.getAttribute("data-clause"))).toEqual([...CLAUSE_NAMES]);
    const byName = (name: string) => ticks.find((t) => t.getAttribute("data-clause") === name)!;
    expect(byName("lid_mates").textContent).toContain("−0.20 mm");
    expect(byName("bbox").textContent).toContain("+0.50 mm");
  });

  it("gives words only to the clauses that failed or came in under the band", () => {
    render(<StepPanel run={runWith(caseStep(kernelEnclosure))} onDismiss={() => {}} />);
    const rows = screen.getAllByTestId("case-clause");
    // Two fail; every other clause sits at +0.50, clear of the 0.20 band.
    expect(rows.map((r) => r.getAttribute("data-clause")).sort()).toEqual([
      "headroom",
      "lid_mates",
    ]);
    const byName = (name: string) => rows.find((r) => r.getAttribute("data-clause") === name)!;
    expect(byName("lid_mates").querySelector('[data-testid="case-clause-margin"]')?.textContent).toBe("−0.20 mm");
    expect(byName("headroom").querySelector('[data-testid="case-clause-margin"]')?.textContent).toBe("−1.10 mm");
    expect(byName("lid_mates").getAttribute("data-tone")).toBe("fail");
    expect(byName("lid_mates").querySelector('[data-testid="case-clause-detail"]')?.textContent).toBe(
      "lip binds on the base wall"
    );
    expect(screen.getByTestId("case-clear-count").textContent).toBe("11 clear by more than 0.20 mm");
  });

  it("a clause that passed inside the band is surfaced, not folded away as a pass", () => {
    const tight = clauses().map((c) =>
      c.name === "min_wall" ? { ...c, passed: true, margin_mm: 0.02, detail: "wall 1.22 mm" } : c
    );
    render(
      <StepPanel
        run={runWith(
          caseStep({
            ...kernelEnclosure,
            kernel: { passed: false, clauses: tight, warnings: [], thin_band_mm: 0.2 },
          })
        )}
        onDismiss={() => {}}
      />
    );
    const thin = screen
      .getAllByTestId("case-clause")
      .find((r) => r.getAttribute("data-clause") === "min_wall")!;
    expect(thin.getAttribute("data-tone")).toBe("thin");
    expect(thin.getAttribute("data-passed")).toBe("true");
    expect(thin.querySelector('[data-testid="case-clause-margin"]')?.textContent).toBe("+0.02 mm");
    expect(screen.getByTestId("case-axis-summary").textContent).toContain(
      "1 inside the 0.20 mm print tolerance"
    );
  });

  it("without a band from the engine it marks nothing thin rather than inventing one", () => {
    const tight = clauses().map((c) =>
      c.name === "min_wall" ? { ...c, passed: true, margin_mm: 0.02, detail: "" } : c
    );
    render(
      <StepPanel
        run={runWith(
          caseStep({ ...kernelEnclosure, kernel: { passed: false, clauses: tight, warnings: [] } })
        )}
        onDismiss={() => {}}
      />
    );
    expect(
      screen.getAllByTestId("case-clause-tick").filter((t) => t.getAttribute("data-tone") === "thin")
    ).toHaveLength(0);
    expect(screen.getByTestId("case-clear-count").textContent).toBe("11 clear");
  });

  it("renders the kernel warning", () => {
    render(<StepPanel run={runWith(caseStep(kernelEnclosure))} onDismiss={() => {}} />);
    const warnings = screen.getAllByTestId("case-kernel-warning");
    expect(warnings).toHaveLength(1);
    expect(warnings[0].textContent).toBe("overhang check skipped: no print orientation given");
    expect(screen.queryByTestId("case-warning")).toBeNull();
  });

  it("renders the stage's own warnings separately from the kernel's", () => {
    render(
      <StepPanel
        run={runWith(caseStep({ ...kernelEnclosure, warnings: ["height defaulted to 12 mm"] }))}
        onDismiss={() => {}}
      />
    );
    expect(screen.getByTestId("case-warning").textContent).toBe("height defaulted to 12 mm");
    expect(screen.getAllByTestId("case-kernel-warning")).toHaveLength(1);
  });

  it("Open 3D model hands the STEP path to the OS; Reveal case files reveals it", () => {
    render(<StepPanel run={runWith(caseStep(kernelEnclosure))} onDismiss={() => {}} />);
    fireEvent.click(screen.getByText("Open 3D model"));
    expect(mockOpenPath).toHaveBeenCalledTimes(1);
    expect(mockOpenPath).toHaveBeenCalledWith(STEP_PATH);

    fireEvent.click(screen.getByText("Reveal case files"));
    expect(mockReveal).toHaveBeenCalledTimes(1);
    expect(mockReveal).toHaveBeenCalledWith(STEP_PATH);
    expect(screen.queryByTestId("case-open-note")).toBeNull();
  });

  it("shows the path instead of a silent no-op when the opener rejects", async () => {
    mockOpenPath.mockRejectedValueOnce(new Error("no handler"));
    render(<StepPanel run={runWith(caseStep(kernelEnclosure))} onDismiss={() => {}} />);
    fireEvent.click(screen.getByText("Open 3D model"));
    const note = await screen.findByTestId("case-open-note");
    expect(note.textContent).toContain("no handler");
    expect(note.textContent).toContain(STEP_PATH);
  });

  it("with no files written, neither button exists", () => {
    render(
      <StepPanel
        run={runWith(
          caseStep({
            ...kernelEnclosure,
            files: { step: null, base_stl: null, lid_stl: null },
          })
        )}
        onDismiss={() => {}}
      />
    );
    expect(screen.queryByText("Open 3D model")).toBeNull();
    expect(screen.queryByText("Reveal case files")).toBeNull();
    expect(screen.queryByTestId("case-open-model")).toBeNull();
    expect(screen.queryByTestId("case-reveal")).toBeNull();
    // The receipt is still there; only the file buttons went.
    expect(screen.getAllByTestId("case-clause-tick")).toHaveLength(13);
  });

  it("with only a case file on the step envelope, reveal exists but the model button does not", () => {
    const response = caseStep({ engine: "kernel", kernel: null, files: null });
    response.files = { ...response.files, case: "/tmp/steps/s1/enclosure-base.stl" };
    render(<StepPanel run={runWith(response)} onDismiss={() => {}} />);
    expect(screen.queryByText("Open 3D model")).toBeNull();
    fireEvent.click(screen.getByText("Reveal case files"));
    expect(mockReveal).toHaveBeenCalledWith("/tmp/steps/s1/enclosure-base.stl");
  });

  it("with an engine it does not know and no kernel, says so and shows no clause rows", () => {
    render(
      <StepPanel
        run={runWith(caseStep({ engine: "laser", kernel: null, files: null }))}
        onDismiss={() => {}}
      />
    );
    const engine = screen.getByTestId("case-engine");
    // One engine exists; an unrecognised name is reported as no engine at all
    // rather than printed as though the panel vouched for it.
    expect(engine.getAttribute("data-engine")).toBe("unknown");
    expect(engine.textContent).toContain("not reported");
    expect(engine.textContent).toContain("no kernel check");
    expect(screen.getByTestId("case-kernel-verdict").getAttribute("data-passed")).toBe("none");
    expect(screen.queryAllByTestId("case-clause")).toHaveLength(0);
    expect(screen.queryByTestId("case-clauses")).toBeNull();
    expect(screen.queryByTestId("case-kernel-warning")).toBeNull();
  });

  it("a fully passing kernel report reads as N of N pass", () => {
    const passing = clauses().map((c) => ({ ...c, passed: true, margin_mm: Math.abs(c.margin_mm) }));
    render(
      <StepPanel
        run={runWith(
          caseStep({ ...kernelEnclosure, kernel: { passed: true, clauses: passing, warnings: [] } })
        )}
        onDismiss={() => {}}
      />
    );
    expect(screen.getByTestId("case-engine").textContent).toContain("13 of 13 checks pass");
    expect(screen.getByTestId("case-kernel-verdict").getAttribute("data-passed")).toBe("true");
    expect(screen.queryAllByTestId("case-clause").filter((r) => r.getAttribute("data-passed") === "false")).toHaveLength(0);
  });

  it("never renders a case outcome for a non-case step", () => {
    const routed: StepResponse = { ...caseStep(kernelEnclosure), step: "route", enclosure: undefined };
    render(<StepPanel run={runWith(routed)} onDismiss={() => {}} />);
    expect(screen.queryByTestId("case-outcome")).toBeNull();
  });

  it("nothing on the panel calls itself an assistant or a helper", () => {
    const { container } = render(
      <StepPanel run={runWith(caseStep(kernelEnclosure))} note="Could not read that." onDismiss={() => {}} />
    );
    const text = (container.textContent ?? "").toLowerCase();
    expect(text).not.toContain("assistant");
    expect(text).not.toContain("helper");
    // Attributes too — a title or aria-label is still copy.
    expect(container.innerHTML.toLowerCase()).not.toMatch(/assistant|helper/);
  });
});

describe("StepPanel hierarchy", () => {
  const waiting = (available: StepName[]): StepRun => ({
    ...runWith(caseStep(kernelEnclosure)),
    status: "waiting",
    available,
  });

  it("leads with what landed and where the run has got to", () => {
    render(<StepPanel run={waiting(["route"])} onDismiss={() => {}} />);
    // The case step was shown, so the headline names where it is.
    expect(screen.getByTestId("step-headline").textContent).toBe("Case is here.");
    // Routing is the third of the seven stages.
    expect(screen.getByText("3 of 7")).toBeTruthy();
  });

  it("says so plainly when nothing is left to approve", () => {
    render(<StepPanel run={waiting([])} onDismiss={() => {}} />);
    expect(screen.getByTestId("step-headline").textContent).toBe(
      "Every stage has run. Nothing was ordered."
    );
  });

  it("the approve buttons come before the case receipt, not after it", () => {
    const { container } = render(<StepPanel run={waiting(["case"])} onDismiss={() => {}} />);
    const approve = screen.getByTestId("step-approve");
    const receipt = screen.getByTestId("case-outcome");
    // Node.compareDocumentPosition: 4 means the receipt follows the button.
    expect(approve.compareDocumentPosition(receipt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container.querySelector('[data-testid="step-rail"]')).toBeTruthy();
  });

});

describe("StepPanel consent", () => {
  const waiting = (available: StepName[]): StepRun => ({
    ...runWith(caseStep(kernelEnclosure)),
    status: "waiting",
    available,
  });

  it("prices the approval on the control that spends it", () => {
    render(<StepPanel run={waiting(["route", "order"])} onDismiss={() => {}} />);
    const buttons = screen.getAllByTestId("step-approve");
    expect(buttons[0].textContent).toBe("Route copper · 1 call");
    // Only the primary carries the price; the alternates are not the offer.
    expect(buttons[1].textContent).toBe("Prepare fab order");
    expect(buttons[0].getAttribute("title")).toContain("not reversible");
  });

  it("an armed stage shows what it heard and commits only on the button", () => {
    const onConfirm = vi.fn();
    const onDisarm = vi.fn();
    const run = waiting(["route"]);
    render(
      <StepPanel
        run={run}
        armed={{ step: "route", source: "sg lets route it" }}
        onConfirm={onConfirm}
        onDisarm={onDisarm}
        onDismiss={() => {}}
      />
    );
    expect(screen.getByTestId("step-armed").getAttribute("data-step")).toBe("route");
    expect(screen.getByTestId("step-armed").textContent).toContain("sg lets route it");
    expect(screen.getByTestId("step-headline").textContent).toBe("Confirm: route copper?");
    // The plain approve button is gone while something is armed: one target.
    expect(screen.queryByTestId("step-approve")).toBeNull();
    // Rendering an armed stage must not have run anything by itself.
    expect(run.approve).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("step-confirm"));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("step-disarm"));
    expect(onDisarm).toHaveBeenCalledTimes(1);
  });

  it("an unread artifact demotes the approval without disabling it", () => {
    const run = waiting(["route"]);
    render(<StepPanel run={run} reviewed={false} onDismiss={() => {}} />);
    expect(screen.getByTestId("step-headline").textContent).toBe("Case is here.");
    const approve = screen.getByTestId("step-approve");
    expect(approve.getAttribute("data-unread")).toBe("true");
    expect(approve.hasAttribute("disabled")).toBe(false);
    fireEvent.click(approve);
    expect(run.approve).toHaveBeenCalledWith("route");
  });

  it("once the engineer has looked away, the nag goes", () => {
    render(<StepPanel run={waiting(["route"])} reviewed onDismiss={() => {}} />);
    expect(screen.getByTestId("step-headline").textContent).toBe("Case is here.");
    expect(screen.getByTestId("step-approve").getAttribute("data-unread")).toBeNull();
  });

  it("never asks whether a stage that was never shown has been reviewed", () => {
    const unshown: StepRun = {
      ...waiting(["case"]),
      history: [{ ...caseStep(kernelEnclosure), step: "place", shown_in_kicad: false, next: ["case"] }],
    };
    render(<StepPanel run={unshown} reviewed={false} onDismiss={() => {}} />);
    const headline = screen.getByTestId("step-headline").textContent ?? "";
    expect(headline).toBe("Placement is on disk; I did not show it in KiCad.");
    expect(headline).not.toContain("looked");
    expect(screen.getByTestId("step-approve").getAttribute("data-unread")).toBeNull();
  });

  it("says that a retry is another paid call", () => {
    const failed: StepRun = {
      ...waiting(["route"]),
      status: "error",
      error: { message: "route failed", detail: "solver timeout" } as StepRun["error"],
    };
    render(<StepPanel run={failed} onDismiss={() => {}} />);
    expect(screen.getByTestId("step-panel").textContent).toContain(
      "Trying again is another engine call."
    );
  });
});

function step(partial: Partial<StepResponse> & { step: StepResponse["step"] }): StepResponse {
  return {
    session: "s1",
    stage: "routed",
    intent: "a toy car",
    files: {},
    next: [],
    shown_in_kicad: false,
    events: [],
    duration_s: 1,
    ...partial,
  };
}

function runOf(history: StepResponse[]): StepRun {
  return {
    status: "waiting",
    session: "s1",
    history,
    running: null,
    failedStep: null,
    available: history[history.length - 1]?.next ?? [],
    error: null,
    elapsedS: 0,
    start: vi.fn(),
    approve: vi.fn(),
    cancel: vi.fn(),
    reset: vi.fn(),
  };
}

const ordered = step({
  step: "order",
  next: ["case"],
  files: {
    board: "/tmp/steps/s1/a-toy-car.kicad_pcb",
    order: "/tmp/steps/s1/a-toy-car-order.zip",
    order_manifest: "/tmp/steps/s1/a-toy-car-order.json",
    model_glb: "/tmp/steps/s1/a-toy-car.glb",
  },
  warnings: [],
  order: {
    orderable: false,
    manifest: { blocker_count: 1 },
    issues: [
      { code: "thin", severity: "note", title: "Board is thin", detail: "0.8 mm" },
      { code: "unrouted-nets", severity: "blocker", title: "Unrouted nets", parts: ["MOT"] },
    ],
  },
});

const sourced = step({
  step: "sourcing",
  next: ["order", "case"],
  files: { board: "/tmp/steps/s1/a-toy-car.kicad_pcb", bom: "/tmp/steps/s1/a-toy-car-bom.csv" },
  sourcing: {
    parts: [
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
      },
      {
        ref: "C1",
        value: "10uF",
        kind: "capacitor",
        package: "C_0805",
        mpn: "CL21A106KAYNNNE",
        mpn_status: "proposed",
        datasheet_url: "https://example.test/viewer",
        datasheet_status: "not_pdf",
        model3d: null,
      },
      { ref: "Y1", value: "8MHz", kind: "crystal", package: "Crystal_1210", mpn_status: "none", datasheet_status: "none" },
    ],
    verified: 1,
    proposed: 2,
    unresolved: 1,
    warnings: [],
  },
});

describe("StepPanel sourcing outcome", () => {
  it("shows nothing sourcing-specific until a response carries the block", () => {
    render(<StepPanel run={runOf([ordered])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("sourcing-outcome")).toBeNull();
  });

  it("lists one row per part with the MPN as a proposal and the datasheet badge by status", () => {
    render(<StepPanel run={runOf([sourced])} onDismiss={() => {}} />);
    const row = screen
      .getAllByTestId("step-row")
      .find((li) => li.getAttribute("data-step") === "sourcing");
    expect(row?.textContent).toContain("2 of 3 MPNs, 0 confirmed, 1 datasheet PDF");
    const rows = screen.getAllByTestId("bom-row");
    expect(rows.map((tr) => tr.getAttribute("data-ref"))).toEqual(["U1", "C1", "Y1"]);
    expect(rows.map((tr) => tr.getAttribute("data-datasheet-status"))).toEqual([
      "verified",
      "not_pdf",
      "none",
    ]);
    expect(rows[0].textContent).toContain("AMS1117-3.3");
    expect(rows[0].textContent).toContain("PDF verified");
    expect(rows[1].textContent).toContain("not a PDF");
    expect(rows[2].textContent).toContain("—");
    expect(rows[2].textContent).toContain("no datasheet");
    expect(screen.getAllByTestId("model3d-badge").map((b) => b.getAttribute("data-ref"))).toEqual(["U1"]);
    const counts = screen.getByTestId("sourcing-counts").textContent ?? "";
    expect(counts).toContain("3 line items");
    expect(counts).toContain("2 proposed");
    expect(counts).toContain("0 confirmed by a distributor");
    expect(counts).toContain("1 of 3 datasheets are PDFs");
  });

  it("opens only a verified datasheet, and reveals the BOM file", () => {
    mockOpenUrl.mockResolvedValue(undefined);
    mockReveal.mockResolvedValue(undefined);
    render(<StepPanel run={runOf([sourced])} onDismiss={() => {}} />);
    const opens = screen.getAllByTestId("datasheet-open");
    expect(opens.map((b) => b.getAttribute("data-ref"))).toEqual(["U1"]);
    fireEvent.click(opens[0]);
    expect(mockOpenUrl).toHaveBeenCalledWith("https://example.test/ds.pdf");
    fireEvent.click(screen.getByTestId("bom-reveal"));
    expect(mockReveal).toHaveBeenCalledWith("/tmp/steps/s1/a-toy-car-bom.csv");
  });

  it("shows the BOM again under the order outcome when the order step carries it", () => {
    const withBom = step({ ...ordered, sourcing: sourced.sourcing, files: { ...ordered.files, bom: "/tmp/steps/s1/a-toy-car-bom.csv" } });
    render(<StepPanel run={runOf([withBom])} onDismiss={() => {}} />);
    expect(screen.getByTestId("order-outcome")).toBeTruthy();
    expect(screen.getByTestId("sourcing-outcome")).toBeTruthy();
    expect(screen.getAllByTestId("bom-row")).toHaveLength(3);
  });
});

describe("StepPanel sourcing line items", () => {
  const verifiedPart = step({
    ...sourced,
    sourcing: {
      parts: [
        {
          ref: "U1",
          value: "AMS1117-3.3",
          kind: "device",
          package: "SOT-223",
          mpn: "AMS1117-3.3",
          mpn_status: "verified",
          distributor: "Mouser",
          distributor_url: "https://www.mouser.test/p/ams1117",
          datasheet_status: "none",
        },
        {
          ref: "C1",
          value: "10uF",
          kind: "capacitor",
          package: "C_0805",
          mpn: "CL21A106KAYNNNE",
          mpn_status: "proposed",
          verify_error: "Mouser has no listing for CL21A106KAYNNNE",
          datasheet_status: "none",
        },
      ],
      verified: 0,
      proposed: 2,
      unresolved: 0,
      warnings: [],
    },
  });

  it("marks a proposal as a dashed badge and a confirmed MPN with the distributor that confirmed it", () => {
    render(<StepPanel run={runOf([verifiedPart])} onDismiss={() => {}} />);
    const badges = screen.getAllByTestId("mpn-badge");
    expect(badges.map((b) => b.getAttribute("data-ref"))).toEqual(["U1", "C1"]);
    expect(badges[0].textContent).toBe("Verified · Mouser");
    expect(badges[0].className).not.toContain("border-dashed");
    expect(badges[1].textContent).toBe("Proposed");
    expect(badges[1].className).toContain("border-dashed");
  });

  it("shows why a proposal was not confirmed, and links only the confirmed one", () => {
    render(<StepPanel run={runOf([verifiedPart])} onDismiss={() => {}} />);
    const errors = screen.getAllByTestId("mpn-verify-error");
    expect(errors.map((e) => e.getAttribute("data-ref"))).toEqual(["C1"]);
    expect(errors[0].textContent).toBe("Mouser has no listing for CL21A106KAYNNNE");
    const links = screen.getAllByTestId("distributor-open");
    expect(links.map((b) => b.getAttribute("data-ref"))).toEqual(["U1"]);
    fireEvent.click(links[0]);
    expect(mockOpenUrl).toHaveBeenCalledWith("https://www.mouser.test/p/ams1117");
  });
});

describe("StepPanel order outcome", () => {
  it("shows nothing order-specific until the order step has run", () => {
    render(<StepPanel run={runOf([step({ step: "route", next: ["order"] })])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("order-outcome")).toBeNull();
  });

  it("lists every issue blockers first, with its severity, under the verdict", () => {
    render(<StepPanel run={runOf([ordered])} onDismiss={() => {}} />);
    const row = screen
      .getAllByTestId("step-row")
      .find((li) => li.getAttribute("data-step") === "order");
    expect(row?.textContent).toContain("not orderable, 1 blocker");
    const issues = screen.getAllByTestId("order-issue");
    expect(issues.map((li) => li.getAttribute("data-sev"))).toEqual(["blocker", "note"]);
    expect(issues[0].textContent).toContain("Blocker");
    expect(issues[0].textContent).toContain("Unrouted nets");
    expect(issues[0].textContent).toContain("MOT");
    expect(issues[1].textContent).toContain("0.8 mm");
    expect(screen.getByTestId("order-not-submitted").textContent).toContain("Nothing is submitted");
  });

  it("reveals the zip and opens the model through the OS", () => {
    mockReveal.mockResolvedValue(undefined);
    mockOpenPath.mockResolvedValue(undefined);
    render(<StepPanel run={runOf([ordered])} onDismiss={() => {}} />);
    fireEvent.click(screen.getByTestId("order-reveal"));
    expect(mockReveal).toHaveBeenCalledWith("/tmp/steps/s1/a-toy-car-order.zip");
    fireEvent.click(screen.getByTestId("order-open-model"));
    expect(mockOpenPath).toHaveBeenCalledWith("/tmp/steps/s1/a-toy-car.glb");
  });

  it("shows the path when no application takes the model, instead of failing silently", async () => {
    mockOpenPath.mockRejectedValueOnce("No application found");
    render(<StepPanel run={runOf([ordered])} onDismiss={() => {}} />);
    fireEvent.click(screen.getByTestId("order-open-model"));
    await waitFor(() => expect(screen.getByTestId("order-open-note")).toBeTruthy());
    const note = screen.getByTestId("order-open-note").textContent ?? "";
    expect(note).toContain("No application found");
    expect(note).toContain("/tmp/steps/s1/a-toy-car.glb");
  });

  it("offers no model button without a glb, and shows the engine's reason", () => {
    const noModel = step({
      ...ordered,
      files: { order: "/tmp/steps/s1/a-toy-car-order.zip" },
      warnings: ["no 3D model exported: kicad-cli not found (install KiCad or set KICAD_CLI)"],
    });
    render(<StepPanel run={runOf([noModel])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("order-open-model")).toBeNull();
    expect(screen.getByTestId("order-reveal")).toBeTruthy();
    expect(screen.getByTestId("order-warning").textContent).toContain("kicad-cli not found");
  });
});

describe("StepPanel 3D board", () => {
  // "Show 3D board" is KiCad's own 3D viewer on the routed board, not the
  // in-app WebGL preview. The preview kept its place beside it under a label
  // that no longer claims to be the answer to "show me the board in 3D".
  it("presses KiCad's 3D viewer and says nothing when it opened", async () => {
    const show3d = vi.fn().mockResolvedValue({ opened: true, detail: null });
    render(<StepPanel run={{ ...runOf([ordered]), show3d }} onDismiss={() => {}} />);
    const button = screen.getByTestId("order-show-3d");
    expect(button.textContent).toContain("Show 3D board");
    fireEvent.click(button);
    await waitFor(() => expect(show3d).toHaveBeenCalledTimes(1));
    // Nothing is rendered in the strip: the board is in KiCad, which is where
    // the engineer was told to look.
    expect(screen.queryByTestId("model-viewer")).toBeNull();
    await waitFor(() => expect(screen.queryByTestId("order-open-note")).toBeNull());
  });

  it("shows KiCad's own reason verbatim when the viewer did not open", async () => {
    const show3d = vi.fn().mockResolvedValue({
      opened: false,
      detail: "KiCad's API server is off: Preferences > Plugins > Enable API server",
    });
    render(<StepPanel run={{ ...runOf([ordered]), show3d }} onDismiss={() => {}} />);
    fireEvent.click(screen.getByTestId("order-show-3d"));
    await waitFor(() =>
      expect(screen.getByTestId("order-open-note").textContent).toContain(
        "Preferences > Plugins > Enable API server"
      )
    );
  });

  it("offers no KiCad button on an engine that has no view3d route", () => {
    render(<StepPanel run={runOf([ordered])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("order-show-3d")).toBeNull();
  });

  it("keeps the in-app preview collapsed until asked, then mounts it reading the exported glb", async () => {
    render(<StepPanel run={runOf([ordered])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("model-viewer")).toBeNull();
    const toggle = screen.getByTestId("order-show-model");
    expect(toggle.textContent).toContain("Preview here");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(toggle);
    const canvas = screen.getByTestId("model-viewer");
    expect(canvas.getAttribute("data-phase")).toBe("loading");
    expect(toggle.textContent).toContain("Hide preview");
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    const { readFile } = await import("@tauri-apps/plugin-fs");
    expect(vi.mocked(readFile)).toHaveBeenCalledWith("/tmp/steps/s1/a-toy-car.glb");
    // The external open stays available beside the inline view.
    expect(screen.getByTestId("order-open-model")).toBeTruthy();

    fireEvent.click(toggle);
    expect(screen.queryByTestId("model-viewer")).toBeNull();
  });

  it("offers no in-app preview toggle without a glb", () => {
    const noModel = step({ ...ordered, files: { order: "/tmp/steps/s1/a-toy-car-order.zip" } });
    render(<StepPanel run={runOf([noModel])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("order-show-model")).toBeNull();
  });
});

describe("StepPanel at the KiCad boundary", () => {
  const placed = (extra: Partial<StepResponse>) =>
    step({
      step: "place",
      stage: "placed",
      next: ["route"],
      files: { placed_board: "/tmp/steps/s1/a-toy-car.placed.kicad_pcb" },
      parts: [{ ref: "U1", footprint: "SOT-223" }],
      board_mm: [20, 15],
      ...extra,
    });
  const rowFor = (id: StepName) =>
    screen.getAllByTestId("step-row").find((row) => row.getAttribute("data-step") === id) as HTMLElement;

  it("relays the engine's reason when a stage did not reach KiCad", () => {
    const detail =
      "placement was not shown in KiCad: KiCad's API server is off: Preferences > Plugins > Enable API server";
    render(
      <StepPanel run={runOf([placed({ shown_in_kicad: false, shown_detail: detail })])} onDismiss={() => {}} />
    );
    expect(screen.getByTestId("step-headline-fix").textContent).toBe(detail);
    // The headline owns the fact; the fix line owns the engine's reason.
    expect(screen.getByTestId("step-headline").textContent).toBe(
      "Placement is on disk. I could not show it in KiCad."
    );
    // The row's where-chip does not vouch for what did not happen.
    const chip = rowFor("place").querySelector('[data-testid="where-chip"]')!;
    expect(chip.getAttribute("data-shown")).toBe("false");
    expect(chip.textContent).toBe("on disk");
  });

  it("distinguishes a bridge that failed from one that was never asked", () => {
    render(<StepPanel run={runOf([placed({ shown_in_kicad: false })])} onDismiss={() => {}} />);
    // No reason came back, so nothing claims the bridge was tried and failed.
    expect(screen.getByTestId("step-headline").textContent).toBe(
      "Placement is on disk; I did not show it in KiCad."
    );
    expect(screen.queryByTestId("step-headline-fix")).toBeNull();
  });

  it("says nothing of the kind when the stage was shown, and the row wears the chip", () => {
    render(<StepPanel run={runOf([placed({ shown_in_kicad: true })])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("step-headline-fix")).toBeNull();
    expect(rowFor("place").textContent).toContain("KiCad");
  });

  it("a stage reviewed here has no KiCad to be missing from", () => {
    render(<StepPanel run={runOf([sourced])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("step-headline-fix")).toBeNull();
  });
});

const reviewed = step({
  step: "review",
  next: ["sourcing", "order", "case"],
  files: { board: "/tmp/steps/s1/a-toy-car.kicad_pcb" },
  blockers: ["VIN has no bulk capacitor"],
  findings: [
    // Deliberately out of order: the panel sorts blockers first.
    {
      id: "f-note",
      severity: "note",
      title: "Silkscreen revision unset",
      detail: "Nobody has said which revision ships.",
      origin: "suggested",
    },
    {
      id: "f-blocker",
      severity: "blocker",
      title: "VIN has no bulk capacitor",
      detail: "U1 needs 10 uF within 10 mm of pin 3.",
      refs: ["U1", "C1"],
      origin: "proven",
      rule: "bulk_cap_distance",
      evidence: "nearest bulk cap is 18.4 mm from U1 pin 3",
      citation: "AMS1117 datasheet p. 9",
      suggested_fix: "Move C1 beside U1.",
    },
  ],
});

describe("StepPanel review outcome", () => {
  it("shows nothing review-specific until the review step has run", () => {
    render(<StepPanel run={runOf([ordered])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("review-outcome")).toBeNull();
    expect(screen.queryAllByTestId("finding")).toHaveLength(0);
  });

  it("counts the findings and the blockers, and how many a rule measured", () => {
    render(<StepPanel run={runOf([reviewed])} onDismiss={() => {}} />);
    const counts = screen.getByTestId("review-counts").textContent ?? "";
    expect(counts).toContain("2 findings");
    expect(counts).toContain("1 blocking");
    expect(counts).toContain("1 measured by a rule");
  });

  it("lists every finding worst first, each carrying its own provenance", () => {
    render(<StepPanel run={runOf([reviewed])} onDismiss={() => {}} />);
    const rows = screen.getAllByTestId("finding");
    expect(rows.map((r) => r.getAttribute("data-sev"))).toEqual(["blocker", "note"]);
    // The rows share one testid, disambiguated by severity and origin.
    expect(rows.map((r) => r.getAttribute("data-origin"))).toEqual(["measured", "suggested"]);

    const flags = screen.getAllByTestId("finding-flag").map((b) => b.textContent);
    expect(flags).toEqual(["BLOCKER", "NOTE"]);
    expect(screen.getByTestId("finding-refs").textContent).toBe("U1 · C1");

    const origins = screen.getAllByTestId("finding-origin");
    expect(origins[0].getAttribute("data-measured")).toBe("true");
    expect(origins[0].textContent).toContain("rule bulk_cap_distance, measured on the board");
    expect(origins[1].getAttribute("data-measured")).toBe("false");
    // A critic's proposal must never read as a check that ran.
    expect(origins[1].textContent).toContain("critic, not measured");

    expect(screen.getByTestId("finding-evidence").textContent).toContain("18.4 mm");
    expect(screen.getByTestId("finding-fix").textContent).toContain("Move C1 beside U1.");
  });

  it("quotes a citation with its page, and says so in words when there is none", () => {
    render(<StepPanel run={runOf([reviewed])} onDismiss={() => {}} />);
    const cites = screen.getAllByTestId("finding-citation");
    expect(cites[0].getAttribute("data-cited")).toBe("true");
    expect(cites[0].getAttribute("data-page")).toBe("9");
    expect(cites[0].textContent).toContain("AMS1117 datasheet p. 9");
    expect(cites[1].getAttribute("data-cited")).toBe("false");
    expect(cites[1].textContent).toBe(NO_CITATION);
  });

  it("says the critic found nothing rather than showing an empty shell of rows", () => {
    render(
      <StepPanel run={runOf([{ ...reviewed, findings: [], blockers: [] }])} onDismiss={() => {}} />
    );
    // An empty finding list must not read as a clean board — the same rule
    // `audit/report.py` states — so the card keeps its one honest sentence
    // and grows no rows at all.
    expect(screen.queryAllByTestId("finding")).toHaveLength(0);
    expect(screen.getByTestId("review-counts").textContent).toBe(
      "The critic found nothing to flag. No rule measured the board."
    );
    expect(screen.getByTestId("review-outcome").getAttribute("data-status")).toBe("ok");
  });

  it("an ok review with no findings, on an engine that says so, keeps the nothing-to-flag line", () => {
    render(
      <StepPanel
        run={runOf([
          { ...reviewed, findings: [], blockers: [], review: { status: "ok", ran: true, detail: null, note: "" } },
        ])}
        onDismiss={() => {}}
      />
    );
    expect(screen.getByTestId("review-outcome").getAttribute("data-status")).toBe("ok");
    expect(screen.getByTestId("review-counts").textContent).toContain("The critic found nothing to flag.");
    expect(screen.getByTestId("review-counts").className).not.toContain("text-destructive");
  });

  it("a failed review is a failure, in red, with the engine's detail, and never the clean-board card", () => {
    render(
      <StepPanel
        run={runOf([
          {
            ...reviewed,
            findings: [],
            blockers: [],
            review: {
              status: "failed",
              ran: true,
              detail: "the critic answered nothing readable (ModelError: 503)",
              note: "review failed",
            },
          },
        ])}
        onDismiss={() => {}}
      />
    );
    const card = screen.getByTestId("review-outcome");
    expect(card.getAttribute("data-status")).toBe("failed");
    expect(card.className).toContain("border-destructive");
    const counts = screen.getByTestId("review-counts");
    expect(counts.className).toContain("text-destructive");
    expect(counts.textContent).toBe(
      "Review failed: the critic answered nothing readable (ModelError: 503) — nothing is known about this board."
    );
    expect(counts.textContent).not.toContain("nothing to flag");
    expect(screen.queryAllByTestId("finding")).toHaveLength(0);
    // The rail's receipt clause and the headline say the same thing.
    expect(screen.getByTestId("step-headline").textContent).toContain("Review failed:");
    expect(screen.getByTestId("step-headline").textContent).not.toContain("nothing to flag");
  });

  it("a skipped review says so, in the warning tone, and is not clean either", () => {
    render(
      <StepPanel
        run={runOf([
          {
            ...reviewed,
            findings: [],
            blockers: [],
            review: { status: "skipped", ran: false, detail: null, note: "review was not requested" },
          },
        ])}
        onDismiss={() => {}}
      />
    );
    const card = screen.getByTestId("review-outcome");
    expect(card.getAttribute("data-status")).toBe("skipped");
    expect(screen.getByTestId("review-counts").textContent).toBe(
      "Review skipped: review was not requested — nothing is known about this board."
    );
    expect(screen.queryAllByTestId("finding")).toHaveLength(0);
  });

  it("settles into place with the shared motion vocabulary rather than popping", () => {
    render(<StepPanel run={runOf([reviewed])} onDismiss={() => {}} />);
    const card = screen.getByTestId("review-outcome");
    expect(card.className).toContain("kv-settle");
    expect(card.className).toContain("kv-settle-group");
  });

  it("sits in the scroll region beside the other receipts, under the stage line", () => {
    render(<StepPanel run={runOf([reviewed])} onDismiss={() => {}} />);
    const scroll = screen.getByTestId("step-scroll");
    expect(scroll.contains(screen.getByTestId("review-outcome"))).toBe(true);
    const receipt = screen.getByTestId("stage-receipt");
    expect(receipt.getAttribute("data-step")).toBe("review");
    expect(scroll.contains(screen.getAllByTestId("step-approve")[0])).toBe(false);
  });
});

describe("StepPanel scroll region", () => {
  it("the receipts scroll inside step-scroll; the buttons and the headline stay outside it", () => {
    const withBom = { ...ordered, sourcing: sourced.sourcing };
    render(<StepPanel run={runOf([withBom])} onDismiss={() => {}} />);
    const scroll = screen.getByTestId("step-scroll");
    expect(scroll.className).toContain("overflow-y-auto");
    expect(scroll.className).toMatch(/max-h-/);
    expect(scroll.contains(screen.getByTestId("order-outcome"))).toBe(true);
    expect(scroll.contains(screen.getByTestId("bom-table"))).toBe(true);
    expect(scroll.contains(screen.getByTestId("order-not-submitted"))).toBe(true);
    expect(scroll.contains(screen.getByTestId("step-approve"))).toBe(false);
    expect(scroll.contains(screen.getByTestId("step-headline"))).toBe(false);
  });

  it("has no scroll region when there is no receipt to scroll", () => {
    // Propose is the one step with nothing to unfold: its counts are the
    // whole story and the drawing is in KiCad. Route used to be here too,
    // until its receipt (the ratsnest, by name) was rendered.
    render(<StepPanel run={runOf([step({ step: "propose", next: ["place"] })])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("step-scroll")).toBeNull();
  });
});

describe("StepPanel armed restart", () => {
  it("is offered once every stage has run too, where the panel itself says to start over", () => {
    const run: StepRun = { ...runOf([ordered]), status: "done", available: [] };
    render(
      <StepPanel run={run} armed={{ step: "restart", source: "start over" }} onConfirm={() => {}} onDismiss={() => {}} />
    );
    expect(screen.getByTestId("step-confirm").getAttribute("data-step")).toBe("restart");
  });

  it("a restart is confirmed like a stage, and nothing is reset by rendering it", () => {
    const onConfirm = vi.fn();
    const onDisarm = vi.fn();
    const run = runOf([ordered]);
    render(
      <StepPanel
        run={run}
        armed={{ step: "restart", source: "try again" }}
        onConfirm={onConfirm}
        onDisarm={onDisarm}
        onDismiss={() => {}}
      />
    );
    expect(screen.getByTestId("step-headline").textContent).toBe("Confirm: start over?");
    expect(screen.getByTestId("step-armed").getAttribute("data-step")).toBe("restart");
    expect(screen.getByTestId("step-armed").textContent).toContain("try again");
    const confirm = screen.getByTestId("step-confirm");
    expect(confirm.textContent).toBe("Start over");
    expect(confirm.getAttribute("title")).toContain("stay on the engine");
    expect(screen.queryByTestId("step-approve")).toBeNull();
    expect(run.reset).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(run.reset).not.toHaveBeenCalled();
  });
});

// The review approval and the Structured/Prose toggle. The panel is
// deliberately not where that rule lives: `useStepRun` attaches the mode (see
// `summaryPayload`), so both approval routes — this button and the page's
// armed confirm — hand over a bare step name and cannot drift apart.
describe("StepPanel review approval", () => {
  const waitingOnReview = (): StepRun => ({
    ...runWith({ ...caseStep(kernelEnclosure), step: "route", next: ["review"] }),
    status: "waiting",
    available: ["review"],
  });

  it("approves review with the step alone: the panel builds no payload of its own", () => {
    const run = waitingOnReview();
    render(<StepPanel run={run} onDismiss={() => {}} />);
    fireEvent.click(screen.getByTestId("step-approve"));
    expect(run.approve).toHaveBeenCalledWith("review");
    // Exactly one argument — a `summary` added here would be a second source
    // of truth beside the hook's, and the two would stop agreeing.
    expect(vi.mocked(run.approve).mock.calls[0]).toHaveLength(1);
  });

  it("the spoken route commits through the page, not through a payload here", () => {
    const run = waitingOnReview();
    const onConfirm = vi.fn();
    render(
      <StepPanel
        run={run}
        armed={{ step: "review", source: "review it" }}
        onConfirm={onConfirm}
        onDisarm={() => {}}
        onDismiss={() => {}}
      />
    );
    fireEvent.click(screen.getByTestId("step-confirm"));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(run.approve).not.toHaveBeenCalled();
  });

  it("a refused agenda is the review step failing, not a quiet review", () => {
    const run: StepRun = {
      ...waitingOnReview(),
      status: "error",
      failedStep: "review",
      error: {
        message: "'summary' must be 'prose' or 'structured'",
      } as StepRun["error"],
    };
    render(<StepPanel run={run} onDismiss={() => {}} />);
    const headline = screen.getByTestId("step-headline");
    expect(headline.className).toContain("text-destructive");
    expect(screen.getByTestId("step-panel").textContent).toContain(
      "'summary' must be 'prose' or 'structured'"
    );
    // The rail marks the stage that failed, so no row claims review is done.
    expect(screen.getByTestId("step-panel").textContent).not.toContain("Review is in");
  });
});

describe("StepPanel route and place receipts", () => {
  it("says N of M nets routed and names every unrouted net verbatim, with its reason", () => {
    const routed = step({
      step: "route",
      next: ["review", "order"],
      routing: {
        tracks: 9,
        vias: 1,
        routed: ["VCC", "GND", "OUT"],
        unrouted: { "/I2C_SDA": "no path at 0.25 mm pitch", "Net-(U1-Pad4)": "budget exhausted" },
        warnings: ["one via cost was raised"],
        completion: 0.6,
      },
    });
    render(<StepPanel run={runOf([routed])} onDismiss={() => {}} />);
    const count = screen.getByTestId("route-count");
    expect(count.textContent).toContain("3 of 5 nets routed");
    expect(count.textContent).toContain("2 left as ratsnest");
    expect(count.getAttribute("data-routed")).toBe("3");
    expect(count.getAttribute("data-total")).toBe("5");
    expect(count.getAttribute("data-completion")).toBe("0.6");
    const nets = screen.getAllByTestId("route-unrouted-net");
    expect(nets.map((n) => n.getAttribute("data-net"))).toEqual(["/I2C_SDA", "Net-(U1-Pad4)"]);
    expect(nets[0].textContent).toContain("/I2C_SDA");
    expect(nets[0].textContent).toContain("no path at 0.25 mm pitch");
    expect(screen.getByTestId("route-warning").textContent).toBe("one via cost was raised");
    // The receipts now scroll for a routed board, and the stage line names the step.
    expect(screen.getByTestId("step-scroll")).not.toBeNull();
    expect(screen.getByTestId("stage-receipt").getAttribute("data-step")).toBe("route");
  });

  it("a fully routed board has no ratsnest list, and a board with no nets says so rather than 100%", () => {
    render(
      <StepPanel
        run={runOf([step({ step: "route", next: [], routing: { routed: ["A", "B"], unrouted: {}, tracks: 4, vias: 0 } })])}
        onDismiss={() => {}}
      />
    );
    expect(screen.getByTestId("route-count").textContent).toContain("2 of 2 nets routed");
    expect(screen.queryByTestId("route-unrouted")).toBeNull();
    cleanup();
    render(
      <StepPanel
        run={runOf([step({ step: "route", next: [], routing: { routed: [], unrouted: {}, completion: 1 } })])}
        onDismiss={() => {}}
      />
    );
    expect(screen.getByTestId("route-count").textContent).toContain("0 of 0 nets routed");
    expect(screen.getByTestId("route-count").textContent).toContain("no nets to route");
  });

  it("lists the placed parts by reference and the placer's warnings", () => {
    const placedStep = step({
      step: "place",
      stage: "placed",
      next: ["route"],
      status: "FEASIBLE",
      board_mm: [18.2, 18.0],
      parts: [
        { ref: "U1", footprint: "SOT-223" },
        { ref: "C1", footprint: "C_0603" },
      ],
      warnings: ["time limit reached, feasible but not proven optimal"],
    });
    render(<StepPanel run={runOf([placedStep])} onDismiss={() => {}} />);
    expect(screen.getByTestId("place-parts").textContent).toContain("2 parts placed");
    expect(screen.getAllByTestId("place-part").map((p) => p.getAttribute("data-ref"))).toEqual(["U1", "C1"]);
    expect(screen.getByTestId("place-warning").textContent).toContain("not proven optimal");
  });
});

describe("StepPanel background jobs", () => {
  const placedStep = step({
    step: "place",
    stage: "placed",
    next: ["route", "sourcing", "case"],
    background: ["sourcing", "case"],
  });

  it("a settled job says it finished and must be pressed to learn the outcome, on its own row", () => {
    const run: StepRun = {
      ...runOf([placedStep]),
      background: [
        { step: "sourcing", state: "running", warning: null },
        { step: "case", state: "settled", warning: null },
      ],
    };
    render(<StepPanel run={run} onDismiss={() => {}} />);
    const note = screen.getByTestId("background-note");
    expect(note.getAttribute("data-step")).toBe("case");
    expect(note.getAttribute("data-state")).toBe("settled");
    expect(note.textContent).toContain("press to collect");
    // The BOM is still running: its row still says so, with no note.
    const sourcingRow = screen
      .getAllByTestId("step-row")
      .find((row) => row.getAttribute("data-step") === "sourcing");
    expect(sourcingRow?.getAttribute("data-status")).toBe("preparing");
    // The case row is approvable — the engine's `next` said so — not "preparing".
    const caseRow = screen.getAllByTestId("step-row").find((row) => row.getAttribute("data-step") === "case");
    expect(caseRow?.getAttribute("data-status")).toBe("available");
  });

  it("a failure the engine reported through another step is shown on the row nobody pressed, in red", () => {
    const warning =
      "parts were not sourced: the lookup in the background failed (ModelError: 503); the BOM lists the board's parts with no part numbers";
    const run: StepRun = {
      ...runOf([placedStep, step({ step: "order", next: ["sourcing", "case"], warnings: [warning] })]),
      background: [{ step: "sourcing", state: "failed", warning }],
    };
    render(<StepPanel run={run} onDismiss={() => {}} />);
    const note = screen.getByTestId("background-note");
    expect(note.getAttribute("data-step")).toBe("sourcing");
    expect(note.getAttribute("data-state")).toBe("failed");
    expect(note.textContent).toBe(warning);
    expect(note.className).toContain("text-destructive");
  });

  it("a job the engine reports finished says so and asks to be pressed, without the older hedge", () => {
    const run: StepRun = {
      ...runOf([placedStep]),
      background: [
        { step: "sourcing", state: "running", warning: null },
        { step: "case", state: "finished", warning: null },
      ],
    };
    render(<StepPanel run={run} onDismiss={() => {}} />);
    const note = screen.getByTestId("background-note");
    expect(note.getAttribute("data-step")).toBe("case");
    expect(note.getAttribute("data-state")).toBe("finished");
    expect(note.textContent).toBe("Case design finished in the background — press to collect");
    expect(note.className).not.toContain("text-destructive");
  });

  it("a job the engine reports failed shows its detail verbatim, in red, on the unpressed row", () => {
    const detail = "ModelError: 429 RESOURCE_EXHAUSTED while designing the case";
    const run: StepRun = {
      ...runOf([placedStep]),
      background: [
        { step: "sourcing", state: "finished", warning: null },
        { step: "case", state: "failed", warning: detail },
      ],
    };
    render(<StepPanel run={run} onDismiss={() => {}} />);
    const notes = screen.getAllByTestId("background-note");
    const failed = notes.find((n) => n.getAttribute("data-step") === "case");
    expect(failed?.getAttribute("data-state")).toBe("failed");
    expect(failed?.textContent).toBe(detail);
    expect(failed?.className).toContain("text-destructive");
    const finished = notes.find((n) => n.getAttribute("data-step") === "sourcing");
    expect(finished?.textContent).toBe("Parts lookup finished in the background — press to collect");
    // Both rows stay pressable: the engine's `next` said so.
    for (const id of ["case", "sourcing"]) {
      const row = screen.getAllByTestId("step-row").find((r) => r.getAttribute("data-step") === id);
      expect(row?.getAttribute("data-status")).toBe("available");
    }
  });

  it("without the hook's picture the envelope's list still reads as preparing, with no note", () => {
    render(<StepPanel run={runOf([placedStep])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("background-note")).toBeNull();
    const caseRow = screen.getAllByTestId("step-row").find((row) => row.getAttribute("data-step") === "case");
    expect(caseRow?.getAttribute("data-status")).toBe("preparing");
  });
});

describe("StepPanel case options", () => {
  const offeringCase = () => runOf([step({ step: "route", next: ["review", "case"] })]);

  it("are shown only while the case step can be pressed", () => {
    render(<StepPanel run={runOf([step({ step: "propose", stage: "proposed", next: ["place"] })])} onDismiss={() => {}} />);
    expect(screen.queryByTestId("case-options")).toBeNull();
    cleanup();
    render(<StepPanel run={offeringCase()} onDismiss={() => {}} />);
    expect(screen.getByTestId("case-options")).not.toBeNull();
    expect(screen.getByTestId("case-options-note").textContent).toContain("collects the design the engine started");
  });

  it("with nothing set, pressing Case sends the bare step and collects the background design", () => {
    const run = offeringCase();
    render(<StepPanel run={run} onDismiss={() => {}} />);
    const button = screen.getAllByTestId("step-approve").find((b) => b.getAttribute("data-step") === "case")!;
    expect(button.getAttribute("data-afresh")).toBeNull();
    fireEvent.click(button);
    expect(run.approve).toHaveBeenCalledWith("case");
    expect(vi.mocked(run.approve).mock.calls[0]).toHaveLength(1);
  });

  it("a style or the rigorous switch designs afresh: the fields ride the approval under the engine's names", () => {
    const run = offeringCase();
    render(<StepPanel run={run} onDismiss={() => {}} />);
    fireEvent.click(screen.getByTestId("case-rigorous"));
    fireEvent.change(screen.getByTestId("case-style"), { target: { value: "  snap lid, vented  " } });
    expect(screen.getByTestId("case-options-note").textContent).toContain("one more model call");
    const button = screen.getAllByTestId("step-approve").find((b) => b.getAttribute("data-step") === "case")!;
    expect(button.getAttribute("data-afresh")).toBe("true");
    expect(button.textContent).toContain("Design the case afresh");
    fireEvent.click(button);
    expect(run.approve).toHaveBeenCalledWith("case", {
      enclosure_style: "snap lid, vented",
      enclosure_rigorous: true,
    });
    // The style field enforces the engine's own limit rather than earning a 400.
    expect(screen.getByTestId("case-style").getAttribute("maxlength")).toBe("500");
  });

  it("a spoken case carries the same fields through the page's confirm", () => {
    const run = offeringCase();
    const onConfirm = vi.fn();
    render(
      <StepPanel
        run={run}
        armed={{ step: "case", source: "do the case" }}
        onConfirm={onConfirm}
        onDismiss={() => {}}
      />
    );
    fireEvent.change(screen.getByTestId("case-style"), { target: { value: "wall tabs" } });
    fireEvent.click(screen.getByTestId("step-confirm"));
    expect(onConfirm).toHaveBeenCalledWith({ enclosure_style: "wall tabs" });
    expect(run.approve).not.toHaveBeenCalled();
  });

  it("other steps still confirm with no payload at all", () => {
    const run = offeringCase();
    const onConfirm = vi.fn();
    render(
      <StepPanel
        run={run}
        armed={{ step: "review", source: "review it" }}
        onConfirm={onConfirm}
        onDismiss={() => {}}
      />
    );
    fireEvent.click(screen.getByTestId("step-confirm"));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm.mock.calls[0][0]).toBeUndefined();
  });
});

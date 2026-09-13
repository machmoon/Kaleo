// Tests for the spoken digest.
//
// The load-bearing assertions are about honesty and register: the digest
// says "by" and "millimeters" because it is written for the ear, it caps at
// three findings and says how many were left out, and it distinguishes "no
// review ran" from "the review found nothing".

import { describe, expect, it } from "vitest";
import { MAX_SPOKEN_FINDINGS, summarizeRun } from "./summarize";
import type { RunResult } from "@/lib/silkscreen/types";

const finding = (severity: string, title: string, detail = "") => ({
  severity,
  title,
  detail,
});

describe("summarizeRun", () => {
  it("speaks board size and status for the ear, not the eye", () => {
    const text = summarizeRun({
      board_mm: [10.6, 9.7],
      status: "feasible",
      findings: [],
    });
    expect(text).toContain("10.6 by 9.7 millimeters");
    expect(text).not.toContain("×");
    expect(text).not.toMatch(/\bmm\b/);
    expect(text).toContain("feasible");
  });

  it("drops trailing zeros a screen would keep", () => {
    const text = summarizeRun({ board_mm: [12.0, 8.25], findings: [] });
    expect(text).toContain("12 by 8.3 millimeters");
  });

  it("falls back to placements board size when board_mm is absent", () => {
    const text = summarizeRun({
      placements: { board_mm: [20, 15], frame: "solver-y-up", parts: [] },
    } as RunResult);
    expect(text).toContain("20 by 15 millimeters");
  });

  it("speaks each finding as severity, title, detail", () => {
    const text = summarizeRun({
      board_mm: [10, 10],
      status: "optimal",
      findings: [
        finding(
          "blocker",
          "VOUT has no bulk capacitor",
          "The regulator will oscillate without it"
        ),
      ],
    });
    expect(text).toContain(
      "Blocker: VOUT has no bulk capacitor. The regulator will oscillate without it."
    );
  });

  it("caps spoken findings and counts the rest", () => {
    const findings = [
      finding("note", "note one"),
      finding("note", "note two"),
      finding("note", "note three"),
      finding("note", "note four"),
      finding("note", "note five"),
    ];
    const text = summarizeRun({ board_mm: [10, 10], findings });
    expect(text).toContain("And 2 more findings in the review.");
    expect(text).not.toContain("note four");
    expect((text.match(/Note:/g) ?? []).length).toBe(MAX_SPOKEN_FINDINGS);
  });

  it("uses singular grammar for exactly one unspoken finding", () => {
    const findings = Array.from({ length: MAX_SPOKEN_FINDINGS + 1 }, (_, i) =>
      finding("note", `note ${i}`)
    );
    const text = summarizeRun({ findings });
    expect(text).toContain("And 1 more finding in the review.");
  });

  it("gives the worst findings the airtime regardless of arrival order", () => {
    const text = summarizeRun({
      findings: [
        finding("note", "a minor note"),
        finding("note", "another minor note"),
        finding("note", "a third minor note"),
        finding("blocker", "the one that matters"),
      ],
    });
    expect(text).toContain("Blocker: the one that matters.");
    // The blocker displaced a note, and the leftover count still holds.
    expect(text).toContain("And 1 more finding in the review.");
  });

  it("distinguishes an absent review from an empty one", () => {
    const noReview = summarizeRun({ board_mm: [10, 10] });
    const cleanReview = summarizeRun({ board_mm: [10, 10], findings: [] });
    expect(noReview).toContain("no review");
    expect(cleanReview).toContain("found nothing");
    expect(noReview).not.toEqual(cleanReview);
  });

  it("never says the review found nothing over a failed or skipped critic", () => {
    // `/generate` now carries `review: {status, ran, detail, note}`
    // (service/steps.py::review_block); an empty `findings` beside
    // `status: "failed"` is not a clean board.
    const failed = summarizeRun({
      board_mm: [10, 10],
      findings: [],
      review: { status: "failed", ran: true, detail: "not JSON", note: "the review failed" },
    });
    expect(failed).not.toContain("found nothing");
    expect(failed).toContain("review failed");
    const skipped = summarizeRun({
      board_mm: [10, 10],
      findings: [],
      review: { status: "skipped", ran: false, detail: null, note: "review skipped" },
    });
    expect(skipped).not.toContain("found nothing");
    expect(skipped).toContain("no review");
    const ok = summarizeRun({
      board_mm: [10, 10],
      findings: [],
      review: { status: "ok", ran: true, detail: null, note: "" },
    });
    expect(ok).toContain("found nothing");
  });

  it("still says something about a nearly-empty result", () => {
    const text = summarizeRun({});
    expect(text.length).toBeGreaterThan(0);
    expect(text).toContain("no review");
  });
});

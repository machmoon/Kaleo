// @vitest-environment jsdom
//
// The run summary's findings line. An empty finding list means one of three
// things, and the block under `review` says which: the critic ran and had
// nothing to flag, the critic never answered (nothing is known about this
// board), or nobody asked it. An older engine sends no block, and only then
// does the empty list keep the reading it always had.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ReviewBlock, RunResult } from "@/lib/silkscreen/types";
import { RunSummary } from "./RunProgress";

afterEach(cleanup);

const OK: ReviewBlock = { status: "ok", ran: true, detail: null, note: "" };
const FAILED: ReviewBlock = {
  status: "failed",
  ran: true,
  detail: "model answered with no JSON",
  note: "the critic did not answer",
};
const SKIPPED: ReviewBlock = { status: "skipped", ran: false, detail: null, note: "review off" };

function summary(result: RunResult, reviewRequested = true) {
  render(
    <RunSummary
      result={result}
      reviewRequested={reviewRequested}
      elapsedS={1}
      onOpenReview={() => {}}
      onNewRun={() => {}}
    />
  );
  return screen.getByTestId("summary-findings").textContent ?? "";
}

describe("RunSummary: an empty finding list", () => {
  it("ok + 0 findings is the clean line", () => {
    const text = summary({ findings: [], blockers: [], review: OK });
    expect(text).toContain("The critic found nothing to flag.");
    expect(screen.getByTestId("summary-review-outcome").dataset.status).toBe("ok");
  });

  it("a failed critic is never phrased as a clean board", () => {
    const text = summary({ findings: [], blockers: [], review: FAILED });
    expect(text).toContain(
      "Review failed: model answered with no JSON — nothing is known about this board."
    );
    expect(text).not.toMatch(/nothing to flag|reported nothing/);
    const line = screen.getByTestId("summary-review-outcome");
    expect(line.dataset.status).toBe("failed");
    expect(line.className).toContain("text-destructive");
  });

  it("a failed critic reads as failed even when the caller stripped the findings", () => {
    const text = summary({ findings: undefined, review: FAILED });
    expect(text).toContain("Review failed:");
    expect(text).not.toContain("carried no review");
  });

  it("a failed critic with no detail falls back to the note, never to zero", () => {
    const text = summary({ findings: [], review: { ...FAILED, detail: null } });
    expect(text).toContain("Review failed: the critic did not answer — nothing is known about this board.");
  });

  it("a skipped review says skipped, in the quiet style", () => {
    const text = summary({ findings: [], review: SKIPPED });
    expect(text).toContain("Review skipped: review off — nothing is known about this board.");
    const line = screen.getByTestId("summary-review-outcome");
    expect(line.dataset.status).toBe("skipped");
    expect(line.className).not.toContain("text-destructive");
  });

  it("no block (older engine) keeps the old wording, by whether review was requested", () => {
    expect(summary({ findings: [] }, true)).toContain(
      "The review ran and reported nothing. Only the checks it runs were run."
    );
    cleanup();
    expect(summary({ findings: [] }, false)).toContain("Review was off for this run, so no checks ran.");
  });

  it("no findings list at all says nothing was checked", () => {
    expect(summary({})).toContain("This response carried no review");
  });

  it("findings still show as counts when the critic answered", () => {
    summary({
      findings: [
        { severity: "blocker", origin: "proven", title: "short", refs: [] },
        { severity: "warning", origin: "suggested", title: "hmm", refs: [] },
      ],
      blockers: ["short"],
      review: OK,
    });
    expect(screen.getAllByTestId("summary-severity")).toHaveLength(2);
    expect(screen.getByTestId("summary-blockers").textContent).toBe("1 blocking");
    expect(screen.queryByTestId("summary-review-outcome")).toBeNull();
  });
});

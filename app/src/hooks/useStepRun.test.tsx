// @vitest-environment jsdom
//
// The approval-gated state machine, driven with a mocked client. The client's
// own behaviour (409 as a request error, offline detection) is covered in
// `steps.test.ts`; here each step call is a hand-cranked promise so a test
// controls exactly when it answers, and the money rule is asserted directly:
// one approval is at most one engine call, and a cancel never re-runs a step.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

vi.mock("@/lib/silkscreen/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/silkscreen/client")>();
  return {
    ...actual,
    startSteps: vi.fn(),
    advanceStep: vi.fn(),
    stepStatus: vi.fn(),
  };
});

import {
  SilkscreenError,
  advanceStep,
  startSteps,
  stepStatus,
} from "@/lib/silkscreen/client";
import { UNRECEIVED_SUMMARY, summarizeStep } from "@/lib/silkscreen/steps";
import type {
  SpecReviewBlock,
  StepName,
  StepResponse,
  StepStatusResponse,
  SummaryMode,
} from "@/lib/silkscreen/types";
import { readPublishedRun } from "@/lib/silkscreen/bridge";
import { stepRunId, useStepRun } from "./useStepRun";

const mockStart = vi.mocked(startSteps);
const mockAdvance = vi.mocked(advanceStep);
const mockStatus = vi.mocked(stepStatus);

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

function statusOf(partial: Partial<StepStatusResponse>): StepStatusResponse {
  return {
    session: "s1",
    stage: "placed",
    intent: "a toy car",
    files: {},
    done: [],
    next: [],
    kicad_live: true,
    ...partial,
  };
}

interface Crank {
  resolve: (response: StepResponse) => void;
  reject: (error: unknown) => void;
}

/**
 * A step call that answers only when the test says so, and rejects the way
 * the http plugin does when its signal is aborted.
 */
function pending(signal: AbortSignal | undefined, crank: Crank): Promise<StepResponse> {
  return new Promise<StepResponse>((res, rej) => {
    crank.resolve = res;
    crank.reject = rej;
    signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
  });
}

function armStart(): Crank {
  const crank: Crank = { resolve: () => {}, reject: () => {} };
  mockStart.mockImplementationOnce((_base, _request, signal) => pending(signal, crank));
  return crank;
}

function armAdvance(): Crank {
  const crank: Crank = { resolve: () => {}, reject: () => {} };
  mockAdvance.mockImplementationOnce((_base, _session, _step, _payload, signal) =>
    pending(signal, crank)
  );
  return crank;
}

function render(summary?: SummaryMode) {
  return renderHook(() => useStepRun({ baseUrl: "http://mock", token: "", summary }));
}

/** Start a run and land its propose step, so `place` is on offer. */
async function proposed(hook: ReturnType<typeof render>) {
  const crank = armStart();
  act(() => hook.result.current.start({ intent: "a toy car", kicad_live: true }));
  act(() => crank.resolve(step({ step: "propose", next: ["place"] })));
  await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
  expect(hook.result.current.available).toEqual(["place"]);
}

beforeEach(() => {
  mockStart.mockReset();
  mockAdvance.mockReset();
  mockStatus.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useStepRun", () => {
  it("sends one request for a double approve", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => {
      hook.result.current.approve("place");
      hook.result.current.approve("place");
    });
    expect(mockAdvance).toHaveBeenCalledTimes(1);
    expect(hook.result.current.status).toBe("running");
    expect(hook.result.current.running).toBe("place");
    act(() => crank.resolve(step({ step: "place", stage: "placed", next: ["route"] })));
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(hook.result.current.available).toEqual(["route"]);
    expect(hook.result.current.history.map((r) => r.step)).toEqual(["propose", "place"]);
  });

  it("re-syncs from the engine after a cancel: the step that ran anyway is marked, next comes from the engine", async () => {
    const hook = render();
    await proposed(hook);
    armAdvance();
    act(() => hook.result.current.approve("place"));
    // The engine finished the step even though the request was abandoned.
    mockStatus.mockResolvedValueOnce(statusOf({ done: ["place"], next: ["route"] }));
    act(() => hook.result.current.cancel());

    await waitFor(() => expect(hook.result.current.available).toEqual(["route"]));
    expect(mockStatus).toHaveBeenCalledTimes(1);
    expect(mockStatus.mock.calls[0][1]).toBe("s1");
    expect(hook.result.current.status).toBe("waiting");
    expect(hook.result.current.running).toBeNull();
    const steps = hook.result.current.history.map((r) => r.step);
    expect(steps).toEqual(["propose", "place"]);
    expect(summarizeStep(hook.result.current.history[1])).toBe(UNRECEIVED_SUMMARY);
    // Nothing was re-run to learn this.
    expect(mockAdvance).toHaveBeenCalledTimes(1);
  });

  it("leaves the offer alone when the cancelled step has not landed yet", async () => {
    const hook = render();
    await proposed(hook);
    armAdvance();
    act(() => hook.result.current.approve("place"));
    mockStatus.mockResolvedValueOnce(statusOf({ stage: "proposed", done: [], next: ["place"] }));
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(hook.result.current.available).toEqual(["place"]);
    expect(hook.result.current.history.map((r) => r.step)).toEqual(["propose"]);
  });

  it("re-syncs after a 409 and keeps the refusal visible", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    mockStatus.mockResolvedValueOnce(statusOf({ done: ["place"], next: ["route"] }));
    act(() =>
      crank.reject(
        new SilkscreenError("request", "step 'place' already ran for this session", {
          status: 409,
        })
      )
    );
    await waitFor(() => expect(hook.result.current.available).toEqual(["route"]));
    expect(hook.result.current.status).toBe("error");
    expect(hook.result.current.error?.status).toBe(409);
    expect(hook.result.current.history.map((r) => r.step)).toEqual(["propose", "place"]);
    expect(summarizeStep(hook.result.current.history[1])).toBe(UNRECEIVED_SUMMARY);
    expect(mockStatus).toHaveBeenCalledTimes(1);
  });

  it("returns to idle when the first step is cancelled, with no session and no history", async () => {
    const hook = render();
    armStart();
    act(() => hook.result.current.start({ intent: "a toy car", kicad_live: true }));
    expect(hook.result.current.status).toBe("running");
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(hook.result.current.status).toBe("idle"));
    expect(hook.result.current.session).toBeNull();
    expect(hook.result.current.history).toEqual([]);
    expect(hook.result.current.available).toEqual([]);
    // There is no session id to ask about.
    expect(mockStatus).not.toHaveBeenCalled();
  });

  it("keeps `available` from the last good response when a step fails", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() =>
      crank.reject(new SilkscreenError("upstream", "Gemini answered 503", { status: 503 }))
    );
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(hook.result.current.error?.kind).toBe("upstream");
    expect(hook.result.current.available).toEqual(["place"]);
    expect(hook.result.current.history.map((r) => r.step)).toEqual(["propose"]);
    expect(mockStatus).not.toHaveBeenCalled();
  });

  it("surfaces a failed status read instead of pretending the list is current", async () => {
    const hook = render();
    await proposed(hook);
    armAdvance();
    act(() => hook.result.current.approve("place"));
    mockStatus.mockRejectedValueOnce(
      new SilkscreenError("request", "no session s1", { status: 404 })
    );
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(hook.result.current.error?.status).toBe(404);
    expect(hook.result.current.available).toEqual(["place"]);
  });

  it("folds a late bridge failure into the row that claimed KiCad had it", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() =>
      crank.resolve(step({ step: "place", stage: "placed", next: ["route"], shown_in_kicad: true }))
    );
    await waitFor(() => expect(hook.result.current.available).toEqual(["route"]));
    expect(hook.result.current.history[1].shown_in_kicad).toBe(true);

    armAdvance();
    act(() => hook.result.current.approve("route"));
    // The bridge failed after `place` answered; the engine's status is the
    // only place that says so.
    mockStatus.mockResolvedValueOnce(
      statusOf({ done: ["place"], next: ["route"], shown_detail: "KiCad closed the board" })
    );
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(hook.result.current.history[1].shown_in_kicad).toBe(false));
    expect(hook.result.current.history[1].shown_detail).toBe("KiCad closed the board");
    expect(hook.result.current.history[1].step).toBe("place");
    expect(hook.result.current.history.map((r) => r.step)).toEqual(["propose", "place"]);
    // The response that never claimed KiCad is left as it was.
    expect(hook.result.current.history[0].shown_in_kicad).toBe(false);
    expect(hook.result.current.history[0].shown_detail).toBeUndefined();
  });

  it("drops a status reply that lands after reset", async () => {
    const hook = render();
    await proposed(hook);
    armAdvance();
    act(() => hook.result.current.approve("place"));
    let answer: (s: StepStatusResponse) => void = () => {};
    mockStatus.mockImplementationOnce(
      () => new Promise<StepStatusResponse>((res) => (answer = res))
    );
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(1));
    act(() => hook.result.current.reset());
    act(() => answer(statusOf({ done: ["place"], next: ["route"] })));
    await act(async () => {});
    expect(hook.result.current.status).toBe("idle");
    expect(hook.result.current.history).toEqual([]);
    expect(hook.result.current.available).toEqual([]);
  });

  it("remembers which step failed, so the rail can mark that row", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    expect(hook.result.current.failedStep).toBeNull();
    act(() =>
      crank.reject(new SilkscreenError("upstream", "Gemini answered 503", { status: 503 }))
    );
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    // `running` is cleared as it fails; without `failedStep` the failure has
    // no row.
    expect(hook.result.current.running).toBeNull();
    expect(hook.result.current.failedStep).toBe("place");
  });

  it("clears the failed step when that step is retried, and on reset", async () => {
    const hook = render();
    await proposed(hook);
    const first = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() => first.reject(new SilkscreenError("upstream", "503", { status: 503 })));
    await waitFor(() => expect(hook.result.current.failedStep).toBe("place"));

    const retry = armAdvance();
    act(() => hook.result.current.approve("place"));
    expect(hook.result.current.failedStep).toBeNull();
    act(() => retry.resolve(step({ step: "place", stage: "placed", next: ["route"] })));
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(hook.result.current.failedStep).toBeNull();

    act(() => hook.result.current.reset());
    expect(hook.result.current.failedStep).toBeNull();
  });

  it("a cancelled step is not a failed one", async () => {
    const hook = render();
    await proposed(hook);
    armAdvance();
    act(() => hook.result.current.approve("place"));
    mockStatus.mockResolvedValueOnce(statusOf({ done: ["place"], next: ["route"] }));
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(hook.result.current.available).toEqual(["route"]));
    expect(hook.result.current.failedStep).toBeNull();
  });
});

// The Structured/Prose toggle is not a preference the app keeps to itself:
// `service/steps.py::_wants_agenda` turns `{"summary": "structured"}` on the
// *review* approval into a spec-review agenda beside the findings. These pin
// the three halves of that — it reaches review, it reaches nothing else, and
// prose sends the request that existed before the toggle did.
describe("useStepRun and the summary mode", () => {
  /** The payload the hook actually put on the wire for the last approval. */
  function sentPayload(): Record<string, unknown> {
    const call = mockAdvance.mock.calls[mockAdvance.mock.calls.length - 1];
    return call![3] as Record<string, unknown>;
  }

  /** Land propose and place so `review` is the step on offer. */
  async function upToReview(hook: ReturnType<typeof render>) {
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() => crank.resolve(step({ step: "place", stage: "placed", next: ["review"] })));
    await waitFor(() => expect(hook.result.current.available).toEqual(["review"]));
  }

  it("structured asks the review step for an agenda", async () => {
    const hook = render("structured");
    await upToReview(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("review"));
    expect(mockAdvance).toHaveBeenLastCalledWith(
      "http://mock",
      "s1",
      "review",
      { summary: "structured" },
      expect.anything(),
      ""
    );
    act(() => crank.resolve(step({ step: "review", stage: "routed", next: [] })));
    await waitFor(() => expect(hook.result.current.status).toBe("done"));
  });

  it("prose sends no summary at all, so an older engine sees the request it always saw", async () => {
    const hook = render("prose");
    await upToReview(hook);
    armAdvance();
    act(() => hook.result.current.approve("review"));
    expect(sentPayload()).toEqual({});
    expect("summary" in sentPayload()).toBe(false);
  });

  it("the default — no mode chosen at all — is prose", async () => {
    const hook = render();
    await upToReview(hook);
    armAdvance();
    act(() => hook.result.current.approve("review"));
    expect(sentPayload()).toEqual({});
  });

  it("no other step carries the mode: nothing else reads it", async () => {
    const hook = render("structured");
    await proposed(hook);
    const place = armAdvance();
    act(() => hook.result.current.approve("place"));
    expect(sentPayload()).toEqual({});
    act(() =>
      place.resolve(step({ step: "place", stage: "placed", next: ["route", "case", "order"] }))
    );
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    for (const other of ["route", "case", "order"] as const) {
      const crank = armAdvance();
      act(() => hook.result.current.approve(other));
      expect(sentPayload()).toEqual({});
      act(() => crank.resolve(step({ step: other, stage: "routed", next: ["review"] })));
      // eslint-disable-next-line no-await-in-loop -- one approval at a time is the point
      await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    }
  });

  it("a caller's own payload still wins over the mode", async () => {
    const hook = render("structured");
    await upToReview(hook);
    armAdvance();
    act(() => hook.result.current.approve("review", { summary: "prose" }));
    expect(sentPayload()).toEqual({ summary: "prose" });
  });

  it("a 400 on the agenda fails the review step rather than passing quietly", async () => {
    const hook = render("structured");
    await upToReview(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("review"));
    act(() =>
      crank.reject(
        new SilkscreenError("request", "'summary' must be 'prose' or 'structured'", {
          status: 400,
        })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(hook.result.current.failedStep).toBe("review");
    expect(hook.result.current.error?.message).toContain("'summary' must be");
    // And nothing pretended a review happened.
    expect(hook.result.current.history.map((r) => r.step)).toEqual(["propose", "place"]);
  });

  it("the agenda the engine answers with rides the review response into history", async () => {
    const agenda: SpecReviewBlock = {
      title: "3.3V rail spec review",
      summary: "Two questions the board cannot settle on its own.",
      items: [
        {
          topic: "Input transient rating",
          why: "U1's absolute maximum is 6 V.",
          minutes: 15,
          blocking: true,
          refs: ["U1"],
        },
      ],
      total_minutes: 15,
    };
    const hook = render("structured");
    await upToReview(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("review"));
    act(() =>
      crank.resolve(step({ step: "review", stage: "routed", next: [], spec_review: agenda }))
    );
    await waitFor(() => expect(hook.result.current.status).toBe("done"));
    const latest = hook.result.current.history[hook.result.current.history.length - 1];
    expect(latest.spec_review).toEqual(agenda);
  });
});

// --------------------------------------------------- the dashboard hand-off
//
// Step mode is the app's DEFAULT, so this is the only path most runs take to
// the review window. What is asserted here is the rule, not the plumbing: a
// board is announced the moment one exists and not before, one session is one
// entry however many steps it grows, and a failed step announces nothing.

describe("useStepRun and the published run", () => {
  const board = "(kicad_pcb (version 20240108))";

  beforeEach(() => localStorage.clear());

  /** Land `place` with a board, the first step that has one. */
  async function placed(hook: ReturnType<typeof render>, next: StepName[] = ["route"]) {
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() =>
      crank.resolve(
        step({
          step: "place",
          stage: "placed",
          next,
          kicad_pcb: board,
          status: "FEASIBLE",
          board_mm: [20, 15],
          placements: { board_mm: [20, 15], parts: [] } as unknown as StepResponse["placements"],
        })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
  }

  it("publishes nothing while the run has only a proposal: there is no board to draw", async () => {
    const hook = render();
    await proposed(hook);
    expect(readPublishedRun()).toBeNull();
    expect(hook.result.current.publishedId).toBeNull();
  });

  it("publishes the board as soon as `place` lands", async () => {
    const hook = render();
    await placed(hook);
    const published = readPublishedRun();
    expect(published).not.toBeNull();
    expect(published!.id).toBe(stepRunId("s1"));
    expect(published!.intent).toBe("a toy car");
    expect(published!.result.kicad_pcb).toBe(board);
    expect(published!.result.status).toBe("FEASIBLE");
    expect(hook.result.current.publishedId).toBe(stepRunId("s1"));
  });

  it("a later step updates the same entry instead of publishing a second run", async () => {
    const hook = render();
    await placed(hook);
    const first = readPublishedRun()!;
    const routed = "(kicad_pcb (version 20240108) (segment))";
    const crank = armAdvance();
    act(() => hook.result.current.approve("route"));
    act(() =>
      crank.resolve(
        step({
          step: "route",
          stage: "routed",
          next: ["review"],
          kicad_pcb: routed,
          routing: { completion: 1, unrouted: {} },
        })
      )
    );
    await waitFor(() => expect(hook.result.current.available).toEqual(["review"]));

    const second = readPublishedRun()!;
    // One entry, one id: the dashboard replaces the run it is showing rather
    // than gaining a second copy of the same board.
    expect(second.id).toBe(first.id);
    expect(second.result.kicad_pcb).toBe(routed);
    expect(second.result.routing?.completion).toBe(1);
    // The identity a caller watches does not flicker as steps land.
    expect(hook.result.current.publishedId).toBe(first.id);
  });

  it("says a review ran only once it has, and carries its findings", async () => {
    const hook = render();
    await placed(hook, ["review"]);
    expect(readPublishedRun()!.request.review).toBe(false);

    const findings: StepResponse["findings"] = [
      { severity: "warning", title: "R1 is unfused" },
    ];
    const crank = armAdvance();
    act(() => hook.result.current.approve("review"));
    act(() =>
      crank.resolve(step({ step: "review", stage: "routed", next: [], findings, blockers: [] }))
    );
    await waitFor(() => expect(hook.result.current.status).toBe("done"));

    const published = readPublishedRun()!;
    expect(published.request.review).toBe(true);
    expect(published.result.findings).toEqual(findings);
    // The board from the earlier step is still there — an update, not a reset.
    expect(published.result.kicad_pcb).toBe(board);
  });

  it("publishes nothing when a step fails", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() => crank.reject(new SilkscreenError("server", "the placer fell over")));
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(readPublishedRun()).toBeNull();
  });

  it("leaves the published board alone when a later step fails", async () => {
    const hook = render();
    await placed(hook);
    const before = readPublishedRun()!;
    const crank = armAdvance();
    act(() => hook.result.current.approve("route"));
    act(() => crank.reject(new SilkscreenError("server", "the router fell over")));
    await waitFor(() => expect(hook.result.current.status).toBe("error"));
    expect(readPublishedRun()).toEqual(before);
  });

  it("a new session publishes under a new id, so the old board stays in history", async () => {
    const hook = render();
    await placed(hook);
    expect(readPublishedRun()!.id).toBe(stepRunId("s1"));

    const crank = armStart();
    act(() => hook.result.current.start({ intent: "a second board" }));
    expect(hook.result.current.publishedId).toBeNull();
    act(() => crank.resolve(step({ step: "propose", session: "s2", next: ["place"] })));
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    const crank2 = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() =>
      crank2.resolve(
        step({ step: "place", session: "s2", stage: "placed", next: [], kicad_pcb: board })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("done"));
    expect(readPublishedRun()!.id).toBe(stepRunId("s2"));
  });

  it("keeps the published board when the overlay is reset: a real board is still on the bench", async () => {
    const hook = render();
    await placed(hook);
    act(() => hook.result.current.reset());
    expect(readPublishedRun()!.result.kicad_pcb).toBe(board);
    expect(hook.result.current.publishedId).toBeNull();
  });

  it("reduces the engine's own frames into the stage rows it publishes", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() =>
      crank.resolve(
        step({
          step: "place",
          stage: "placed",
          next: [],
          kicad_pcb: board,
          events: [
            { event: "stage.start", stage: "place", t_s: 0 },
            { event: "stage.done", stage: "place", t_s: 1.5 },
          ],
        })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("done"));
    const stages = readPublishedRun()!.progress.stages;
    expect(stages.find((s) => s.id === "place")?.status).toBe("done");
    // Nothing was said about routing, so nothing claims it happened.
    expect(stages.find((s) => s.id === "route")?.status).toBe("pending");
  });
});

// ---------------------------------------------------------------- background

import { BACKGROUND_POLL_MS } from "./useStepRun";

const placedWithJobs = () =>
  step({
    step: "place",
    stage: "placed",
    next: ["route", "sourcing", "case"],
    background: ["sourcing", "case"],
  });

/** Start, propose, place — with both background jobs running as the engine says. */
async function placed(hook: ReturnType<typeof render>) {
  await proposed(hook);
  const crank = armAdvance();
  act(() => hook.result.current.approve("place"));
  act(() => crank.resolve(placedWithJobs()));
  await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
}

describe("useStepRun background jobs", () => {
  it("tracks what the envelope lists as running, and clears it on reset", async () => {
    const hook = render();
    await placed(hook);
    expect(hook.result.current.background).toEqual([
      { step: "sourcing", state: "running", warning: null },
      { step: "case", state: "running", warning: null },
    ]);
    act(() => hook.result.current.reset());
    expect(hook.result.current.background).toEqual([]);
  });

  it("polls GET /steps/<id> while a job runs and nothing is pressed, and marks a job that left the list as settled", async () => {
    // Fake timers go in before the run so the poll's interval is a fake one;
    // `shouldAdvanceTime` keeps `waitFor` (real-clock polling) working.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const hook = render();
    await placed(hook);
    try {
      // The engine finished the case; the BOM is still being looked up.
      mockStatus.mockResolvedValue(statusOf({ next: ["route", "sourcing", "case"], background: ["sourcing"] }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS);
      });
      expect(mockStatus).toHaveBeenCalledTimes(1);
      expect(hook.result.current.background).toEqual([
        { step: "sourcing", state: "running", warning: null },
        { step: "case", state: "settled", warning: null },
      ]);
      // Both settled: the poll stops on its own — a GET is free, but a
      // poll with nothing left to learn is noise.
      mockStatus.mockResolvedValue(statusOf({ next: ["route", "sourcing", "case"], background: [] }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS);
      });
      expect(hook.result.current.background?.every((j) => j.state === "settled")).toBe(true);
      const polls = mockStatus.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS * 3);
      });
      expect(mockStatus).toHaveBeenCalledTimes(polls);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a failed poll changes nothing: the jobs stay where the last word left them", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const hook = render();
    await placed(hook);
    try {
      mockStatus.mockRejectedValue(new SilkscreenError("offline", "Could not reach the silkscreen engine."));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS);
      });
      expect(hook.result.current.status).toBe("waiting");
      expect(hook.result.current.error).toBeNull();
      expect(hook.result.current.background?.every((j) => j.state === "running")).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces a background failure the order step reported, on the sourcing job nobody pressed", async () => {
    const hook = render();
    await placed(hook);
    let crank = armAdvance();
    act(() => hook.result.current.approve("route"));
    act(() =>
      crank.resolve(step({ step: "route", stage: "routed", next: ["review", "sourcing", "order", "case"], background: ["sourcing", "case"] }))
    );
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    const warning =
      "parts were not sourced: the lookup in the background failed (ModelError: 503); the BOM lists the board's parts with no part numbers";
    crank = armAdvance();
    act(() => hook.result.current.approve("order"));
    act(() =>
      crank.resolve(
        step({ step: "order", stage: "routed", next: ["review", "sourcing", "case"], background: [], warnings: [warning] })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(hook.result.current.background).toEqual([
      { step: "sourcing", state: "failed", warning },
      { step: "case", state: "settled", warning: null },
    ]);
  });

  it("an older engine that never sends the list has no jobs and never polls", async () => {
    const hook = render();
    await proposed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("place"));
    act(() => crank.resolve(step({ step: "place", stage: "placed", next: ["route", "case"] })));
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(hook.result.current.background).toEqual([]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS * 2);
      });
      expect(mockStatus).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------- background_outcome

import { announce, stepRunResult } from "./useStepRun";
import { notifyMilestone } from "@/lib/notify/notify";

vi.mock("@/lib/notify/notify", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/notify/notify")>();
  return { ...actual, notifyMilestone: vi.fn() };
});

const mockNotify = vi.mocked(notifyMilestone);

describe("useStepRun background outcomes", () => {
  it("reads finished and failed from a poll's background_outcome, and stops polling once no job runs", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const hook = render();
    await placed(hook);
    mockNotify.mockClear();
    try {
      // The case finished on the engine's own word; the BOM is still going.
      mockStatus.mockResolvedValue(
        statusOf({
          next: ["route", "sourcing", "case"],
          background: ["sourcing"],
          background_outcome: { case: { ok: true, detail: null } },
        })
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS);
      });
      expect(hook.result.current.background).toEqual([
        { step: "sourcing", state: "running", warning: null },
        { step: "case", state: "finished", warning: null },
      ]);
      expect(mockNotify).toHaveBeenCalledWith({ kind: "case_done", state: "settled" });
      // The BOM failed, in the engine's words. The case's outcome is no
      // longer repeated on this status and must not be forgotten.
      mockStatus.mockResolvedValue(
        statusOf({
          next: ["route", "sourcing", "case"],
          background: [],
          background_outcome: { sourcing: { ok: false, detail: "ModelError: 429 RESOURCE_EXHAUSTED" } },
        })
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS);
      });
      expect(hook.result.current.background).toEqual([
        { step: "sourcing", state: "failed", warning: "ModelError: 429 RESOURCE_EXHAUSTED" },
        { step: "case", state: "finished", warning: null },
      ]);
      expect(mockNotify).toHaveBeenCalledWith({
        kind: "sourcing_failed",
        warning: "ModelError: 429 RESOURCE_EXHAUSTED",
      });
      // Every started job has an outcome: the poll stops.
      const polls = mockStatus.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS * 3);
      });
      expect(mockStatus).toHaveBeenCalledTimes(polls);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads an outcome off a step envelope, and announces a failure once even when the collecting step repeats it", async () => {
    const hook = render();
    await placed(hook);
    mockNotify.mockClear();
    let crank = armAdvance();
    act(() => hook.result.current.approve("route"));
    const detail = "the case designed in the background failed: ModelError: 503";
    act(() =>
      crank.resolve(
        step({
          step: "route",
          stage: "routed",
          next: ["review", "sourcing", "order", "case"],
          background: ["sourcing"],
          background_outcome: { case: { ok: false, detail } },
        })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(hook.result.current.background).toEqual([
      { step: "sourcing", state: "running", warning: null },
      { step: "case", state: "failed", warning: detail },
    ]);
    expect(mockNotify.mock.calls.filter(([m]) => m.kind === "case_failed")).toHaveLength(1);
    // Pressing `case` collects it; the engine's envelope repeats the failure
    // as a warning, which the older path would announce again.
    crank = armAdvance();
    act(() => hook.result.current.approve("case"));
    act(() =>
      crank.resolve(
        step({
          step: "case",
          stage: "routed",
          next: ["review", "sourcing", "order"],
          background: ["sourcing"],
          background_outcome: { case: { ok: false, detail } },
          enclosure: null,
          warnings: [detail],
        })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(mockNotify.mock.calls.filter(([m]) => m.kind === "case_failed")).toHaveLength(1);
    // Collected: off the job list.
    expect(hook.result.current.background).toEqual([{ step: "sourcing", state: "running", warning: null }]);
  });

  it("an engine without background_outcome degrades to the older reading, settled with the hedge", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const hook = render();
    await placed(hook);
    try {
      mockStatus.mockResolvedValue(statusOf({ next: ["route", "sourcing", "case"], background: [] }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(BACKGROUND_POLL_MS);
      });
      expect(hook.result.current.background).toEqual([
        { step: "sourcing", state: "settled", warning: null },
        { step: "case", state: "settled", warning: null },
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears the remembered outcomes on reset, so a new session cannot inherit a finished case", async () => {
    const hook = render();
    await placed(hook);
    const crank = armAdvance();
    act(() => hook.result.current.approve("route"));
    act(() =>
      crank.resolve(
        step({
          step: "route",
          stage: "routed",
          next: ["review", "case"],
          background: [],
          background_outcome: { case: { ok: true, detail: null }, sourcing: { ok: true, detail: null } },
        })
      )
    );
    await waitFor(() => expect(hook.result.current.status).toBe("waiting"));
    expect(hook.result.current.background?.every((j) => j.state === "finished")).toBe(true);
    act(() => hook.result.current.reset());
    expect(hook.result.current.background).toEqual([]);
    await proposed(hook);
    expect(hook.result.current.background).toEqual([]);
  });
});

describe("useStepRun review verdict", () => {
  const FAILED = { status: "failed", ran: true, detail: "ModelError: 503", note: "review failed" } as const;

  it("folds the review block into the run result the dashboard reads", () => {
    const history = [
      step({ step: "route", stage: "routed" }),
      step({ step: "review", stage: "routed", findings: [], blockers: [], review: FAILED }),
    ];
    expect(stepRunResult(history).review).toEqual(FAILED);
    expect(stepRunResult(history).findings).toEqual([]);
    // Older engine: no block, nothing invented.
    expect(stepRunResult([step({ step: "review", findings: [] })]).review).toBeUndefined();
  });

  it("does not announce '0 findings' for a critic that answered nothing", () => {
    mockNotify.mockClear();
    announce(step({ step: "review", findings: [], blockers: [], review: FAILED }));
    expect(mockNotify.mock.calls.some(([m]) => m.kind === "reviewed")).toBe(false);
    announce(step({ step: "review", findings: [], blockers: [] }));
    expect(mockNotify).toHaveBeenCalledWith({ kind: "reviewed", findings: 0, blockers: 0 });
  });
});

/**
 * The start route's half of "never bill the same press twice".
 *
 * `POST /steps` is the only step call the engine cannot dedupe on its own —
 * the later ones are refused with "already ran" under the session lock — so it
 * carries an `Idempotency-Key`, Stripe's design (`stripe/_api_requestor.py`
 * puts one on every POST). What these tests pin is which key each press goes
 * out under: the same one while a start is unanswered, a new one once it is.
 */
describe("useStepRun start idempotency", () => {
  const keyOf = (call: number) => mockStart.mock.calls[call][4] as string | undefined;

  it("sends a key with the start", async () => {
    const hook = render();
    await proposed(hook);
    expect(keyOf(0)).toBeTruthy();
  });

  it("reuses the key when a cancelled start is started again, so the engine can refuse the repeat", async () => {
    const hook = render();
    const crank = armStart();
    act(() => hook.result.current.start({ intent: "a toy car", kicad_live: true }));
    // Cancelling aborts this client's fetch; the engine has no session yet and
    // keeps reading, planning and proposing to the end.
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(hook.result.current.status).toBe("idle"));
    crank.reject(new DOMException("aborted", "AbortError"));

    armStart();
    act(() => hook.result.current.start({ intent: "a toy car", kicad_live: true }));

    expect(mockStart).toHaveBeenCalledTimes(2);
    expect(keyOf(1)).toBe(keyOf(0));
  });

  it("mints a new key for a different request, which is a different board", async () => {
    const hook = render();
    armStart();
    act(() => hook.result.current.start({ intent: "a toy car", kicad_live: true }));
    act(() => hook.result.current.cancel());
    await waitFor(() => expect(hook.result.current.status).toBe("idle"));

    armStart();
    act(() => hook.result.current.start({ intent: "a lamp", kicad_live: true }));

    expect(keyOf(1)).not.toBe(keyOf(0));
  });

  it("mints a new key once a start has answered, so the same intent runs twice on purpose", async () => {
    const hook = render();
    await proposed(hook);
    act(() => hook.result.current.reset());

    armStart();
    act(() => hook.result.current.start({ intent: "a toy car", kicad_live: true }));

    expect(keyOf(1)).not.toBe(keyOf(0));
  });
});

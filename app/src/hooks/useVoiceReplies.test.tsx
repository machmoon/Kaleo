// @vitest-environment jsdom
//
// The selectivity is the feature. A hook that spoke on every render, or
// re-spoke the same landing after an elapsed-second tick, would be muted
// within a minute — and the mute would take the useful lines with it. So
// most of these tests assert silence.

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { announce } = vi.hoisted(() => ({
  // Typed with the argument so the assertions below can read what was said.
  announce: vi.fn(async (_text: string) => "spoken" as const),
}));
vi.mock("@/lib/speech", async () => {
  const actual = await vi.importActual<typeof import("@/lib/speech/moments")>(
    "@/lib/speech/moments"
  );
  return { ...actual, announce };
});

import type { StepRun } from "@/hooks/useStepRun";
import type { StepResponse } from "@/lib/silkscreen/types";
import { useVoiceReplies } from "./useVoiceReplies";

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

function steps(patch: Partial<StepRun> = {}): StepRun {
  return {
    status: "idle",
    session: null,
    history: [],
    running: null,
    failedStep: null,
    available: [],
    error: null,
    elapsedS: 0,
    start: vi.fn(),
    approve: vi.fn(),
    cancel: vi.fn(),
    reset: vi.fn(),
    ...patch,
  } as StepRun;
}

const Probe = ({ run }: { run: StepRun }) => {
  useVoiceReplies(run);
  return null;
};

beforeEach(() => announce.mockClear());
afterEach(cleanup);

describe("useVoiceReplies", () => {
  it("says nothing while a stage is running", () => {
    render(
      <Probe run={steps({ status: "running", running: "place", elapsedS: 3 })} />
    );
    expect(announce).not.toHaveBeenCalled();
  });

  it("speaks once when a stage lands and waits for approval", () => {
    const run = steps({
      status: "waiting",
      history: [response()],
      available: ["route"],
    });
    const view = render(<Probe run={run} />);
    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce.mock.calls[0][0]).toContain("The placement is done");
    // A re-render with the same landing — an elapsed tick, a parent update —
    // must not repeat itself. Being told twice is the noise that gets a
    // voice muted for good.
    view.rerender(<Probe run={{ ...run, elapsedS: 9 }} />);
    expect(announce).toHaveBeenCalledTimes(1);
  });

  it("speaks the next landing, because that is a new turn", () => {
    const run = steps({
      status: "waiting",
      history: [response()],
      available: ["route"],
    });
    const view = render(<Probe run={run} />);
    view.rerender(
      <Probe
        run={{
          ...run,
          history: [response(), response({ step: "route", next: ["review"] })],
          available: ["review"],
        }}
      />
    );
    expect(announce).toHaveBeenCalledTimes(2);
    expect(announce.mock.calls[1][0]).toContain("The copper is done");
  });

  it("names the failed stage and the engine's reason, once", () => {
    const run = steps({
      status: "error",
      failedStep: "route",
      error: { message: "no path for net VOUT" } as StepRun["error"],
    });
    const view = render(<Probe run={run} />);
    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce.mock.calls[0][0]).toContain("I could not finish the copper");
    expect(announce.mock.calls[0][0]).toContain("no path for net VOUT");
    view.rerender(<Probe run={{ ...run, elapsedS: 4 }} />);
    expect(announce).toHaveBeenCalledTimes(1);
  });

  it("says nothing at all for an idle flow", () => {
    render(<Probe run={steps()} />);
    expect(announce).not.toHaveBeenCalled();
  });
});

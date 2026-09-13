// @vitest-environment jsdom
//
// The run options, and in particular the one control that decides how a
// finished run reports back: a structured agenda somebody can book time
// against, or the prose summary the app writes today.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import type { RunRequestDraft } from "@/contexts";
import { readSummaryMode, summaryFields } from "@/lib/silkscreen/deliver";
import { RunOptions } from "./RunOptions";

const DRAFT: RunRequestDraft = {
  intent: "a 3.3V LDO from 5V",
  datasheets: {},
  time_limit_s: 20,
  review: true,
  ground: false,
  debug: false,
};

function mount(props: Partial<React.ComponentProps<typeof RunOptions>> = {}) {
  return render(<RunOptions request={DRAFT} onChange={vi.fn()} {...props} />);
}

function option(mode: "structured" | "prose"): HTMLElement {
  const found = screen
    .getAllByTestId("run-options-summary-option")
    .find((el) => el.getAttribute("data-mode") === mode);
  if (!found) throw new Error(`no ${mode} option on screen`);
  return found;
}

beforeEach(() => window.localStorage.clear());
afterEach(() => cleanup());

describe("RunOptions summary mode", () => {
  it("starts on prose — what every run does today — and says what each choice means", () => {
    mount();
    expect(option("prose").getAttribute("data-selected")).toBe("true");
    expect(option("structured").getAttribute("data-selected")).toBe("false");
    expect(screen.getByTestId("run-options-summary").textContent).toContain(
      "the way it reads today"
    );
  });

  it("remembers structured across a remount and hands it to the request", () => {
    const onSummaryChange = vi.fn();
    const first = mount({ onSummaryChange });
    fireEvent.click(option("structured"));
    expect(onSummaryChange).toHaveBeenCalledWith("structured");
    expect(option("structured").getAttribute("data-selected")).toBe("true");
    expect(screen.getByTestId("run-options-summary").textContent).toContain("An agenda");
    first.unmount();

    // The choice belongs to the person, not to one board: a fresh mount — a
    // reopened popover, a restored run — comes back on structured.
    mount();
    expect(option("structured").getAttribute("data-selected")).toBe("true");
    expect(readSummaryMode()).toBe("structured");
    // And that is exactly what a submit folds into the request.
    expect(summaryFields(readSummaryMode())).toEqual({ summary: "structured" });
  });

  it("lets a parent own the choice, and never contradicts what it was given", () => {
    const onSummaryChange = vi.fn();
    mount({ summary: "structured", onSummaryChange });
    expect(option("structured").getAttribute("data-selected")).toBe("true");
    fireEvent.click(option("prose"));
    expect(onSummaryChange).toHaveBeenCalledWith("prose");
    // The prop still says structured, so the control still does.
    expect(option("structured").getAttribute("data-selected")).toBe("true");
  });

  it("freezes with the rest of the options while a run is in flight", () => {
    mount({ disabled: true });
    expect(option("structured")).toHaveProperty("disabled", true);
    expect(option("prose")).toHaveProperty("disabled", true);
    fireEvent.click(option("structured"));
    expect(readSummaryMode()).toBe("prose");
  });
});

describe("RunOptions in step mode", () => {
  it("switches the review and grounding controls off and says they apply to one-shot runs only", () => {
    const onChange = vi.fn();
    mount({
      request: { ...DRAFT, datasheets: { U1: "https://example.com/u1.pdf" } },
      onChange,
      stepMode: true,
      onStepModeChange: vi.fn(),
    });
    const review = screen.getByTestId("run-options-review");
    const ground = screen.getByTestId("run-options-ground");
    expect(review).toHaveProperty("disabled", true);
    expect(ground).toHaveProperty("disabled", true);
    expect(review.getAttribute("data-inert")).toBe("true");
    expect(ground.getAttribute("data-inert")).toBe("true");
    expect(screen.getByTestId("run-options-review-note").textContent).toContain("One-shot runs only");
    expect(screen.getByTestId("run-options-review-note").textContent).toContain("Review step");
    expect(screen.getByTestId("run-options-ground-note").textContent).toContain("One-shot runs only");
    // Off means off: a click reaches nothing, the same as the step routes.
    fireEvent.click(review);
    fireEvent.click(ground);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("leaves both live for a one-shot run, with the datasheet rule intact", () => {
    mount({
      request: { ...DRAFT, datasheets: { U1: "https://example.com/u1.pdf" } },
      stepMode: false,
      onStepModeChange: vi.fn(),
    });
    expect(screen.getByTestId("run-options-review")).toHaveProperty("disabled", false);
    expect(screen.getByTestId("run-options-ground")).toHaveProperty("disabled", false);
    expect(screen.getByTestId("run-options-review").getAttribute("data-inert")).toBeNull();
    expect(screen.getByTestId("run-options-review-note").textContent).toContain("Runs the critic pass");
    expect(screen.getByTestId("run-options-ground-note").textContent).toContain("check the review");
  });
});

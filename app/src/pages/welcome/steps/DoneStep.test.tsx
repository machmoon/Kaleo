// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ALL_DEMO_SENTENCE } from "@/lib/setup/service";
import { DONE_TITLE, DoneStep } from "./DoneStep";

afterEach(cleanup);

describe("DoneStep", () => {
  it("offers the tour and Not now, and names what was skipped", () => {
    render(<DoneStep skipped={["google", "stripe"]} demo={false} onTour={vi.fn(async () => undefined)} onNotNow={vi.fn(async () => undefined)} />);
    expect(screen.getByTestId("setup-title").textContent).toBe(DONE_TITLE);
    expect(screen.getByTestId("setup-take-tour").textContent).toBe("Take the tour");
    expect(screen.getByTestId("setup-not-now").textContent).toBe("Not now");
    expect(screen.getByTestId("setup-skipped-line").textContent).toBe("Skipped: Google, Billing. Find them in Settings.");
    expect(screen.queryByTestId("setup-all-demo")).toBeNull();
  });

  it("says nothing was really connected when the engine was in demo mode", () => {
    render(<DoneStep skipped={[]} demo onTour={vi.fn(async () => undefined)} onNotNow={vi.fn(async () => undefined)} />);
    expect(screen.getByTestId("setup-all-demo").textContent).toBe(ALL_DEMO_SENTENCE);
    expect(screen.queryByTestId("setup-skipped-line")).toBeNull();
  });

  it("each button calls its handler once and locks both while it runs", async () => {
    let release!: () => void;
    const onTour = vi.fn(() => new Promise<void>((r) => (release = r)));
    const onNotNow = vi.fn(async () => undefined);
    render(<DoneStep skipped={[]} demo={false} onTour={onTour} onNotNow={onNotNow} />);
    fireEvent.click(screen.getByTestId("setup-take-tour"));
    fireEvent.click(screen.getByTestId("setup-take-tour"));
    fireEvent.click(screen.getByTestId("setup-not-now"));
    expect(onTour).toHaveBeenCalledTimes(1);
    expect(onNotNow).not.toHaveBeenCalled();
    expect((screen.getByTestId("setup-not-now") as HTMLButtonElement).disabled).toBe(true);
    release();
    await waitFor(() => expect((screen.getByTestId("setup-not-now") as HTMLButtonElement).disabled).toBe(false));
  });
});

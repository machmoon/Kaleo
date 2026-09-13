// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

vi.mock("@/lib/settings/store", async () => (await import("@/lib/setup/testing")).makeSettingsStub());

import * as store from "@/lib/settings/store";
import { TOUR_CAPTION_KEY, TOUR_STOPS, offerTour, readTour, resetTour, startTour } from "@/lib/tour";
import { Tour } from "./Tour";

type Stub = ReturnType<typeof import("@/lib/setup/testing").makeSettingsStub>;
const stub = store as unknown as Stub;

const Where = () => <span data-testid="where">{useLocation().pathname}</span>;

function mount(initial = "/workbench", withTarget = true) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Where />
      <Routes>
        <Route path="/workbench" element={<p>workbench</p>} />
        <Route
          path="/integrations"
          element={withTarget ? <section data-testid="integration-group">groups</section> : <p>empty</p>}
        />
      </Routes>
      <Tour />
    </MemoryRouter>,
  );
}

/** jsdom lays nothing out; give the target a box on demand. */
function giveBox(el: Element | null) {
  if (!el) return;
  Object.defineProperty(el, "getBoundingClientRect", {
    value: () => ({ top: 100, left: 40, width: 300, height: 120 }),
    configurable: true,
  });
}

beforeEach(() => {
  localStorage.clear();
  resetTour();
  stub.__reset();
});
afterEach(cleanup);

describe("Tour", () => {
  it("draws nothing while idle", () => {
    mount();
    expect(screen.queryByTestId("tour")).toBeNull();
    expect(screen.queryByTestId("tour-offer")).toBeNull();
  });

  it("the offer is a consent gate: Start begins, Not now records skipped", () => {
    offerTour("settings");
    mount();
    expect(screen.getByTestId("tour-offer")).toBeTruthy();
    fireEvent.click(screen.getByTestId("tour-decline"));
    expect(readTour()).toEqual({ kind: "done", end: "skipped" });
    expect(stub.__values()["tour.completed"]).toBe(true);
  });

  it("stop 1 is caption-only on /workbench; Next navigates to the integrations stop", async () => {
    startTour();
    mount();
    expect(screen.getByTestId("tour").getAttribute("data-stop")).toBe(TOUR_STOPS[0].id);
    expect(screen.getByTestId("tour-card").getAttribute("data-caption-only")).toBe("true");
    expect(screen.queryByTestId("tour-cutout")).toBeNull();
    expect(screen.getByTestId("tour-card").textContent).toContain("Tip 1 of 2");
    fireEvent.click(screen.getByTestId("tour-next"));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/integrations"));
    expect(screen.getByTestId("tour").getAttribute("data-stop")).toBe("integrations");
  });

  it("a live target gets a cut-out; a missing or zero-rect one degrades to caption-only", async () => {
    startTour();
    mount("/integrations");
    fireEvent.click(screen.getByTestId("tour-next"));
    // No layout in jsdom: the target exists but has a zero rect.
    await waitFor(() => expect(screen.getByTestId("tour").getAttribute("data-stop")).toBe("integrations"));
    expect(screen.getByTestId("tour-card").getAttribute("data-caption-only")).toBe("true");
    giveBox(document.querySelector('[data-testid="integration-group"]'));
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(screen.getByTestId("tour-card").getAttribute("data-caption-only")).toBe("false"));
    const cutout = screen.getByTestId("tour-cutout");
    expect(cutout.style.top).toBe("92px");
    expect(cutout.style.width).toBe("316px");
  });

  it("Done on the last stop completes, mirrors tour.completed and clears the strip caption", async () => {
    startTour();
    expect(localStorage.getItem(TOUR_CAPTION_KEY)).not.toBeNull();
    mount("/integrations", false);
    fireEvent.click(screen.getByTestId("tour-next"));
    await waitFor(() => expect(screen.getByTestId("tour-next").textContent).toBe("Done"));
    fireEvent.click(screen.getByTestId("tour-next"));
    expect(readTour()).toEqual({ kind: "done", end: "completed" });
    expect(stub.__values()["tour.completed"]).toBe(true);
    expect(localStorage.getItem(TOUR_CAPTION_KEY)).toBeNull();
    expect(screen.queryByTestId("tour")).toBeNull();
  });

  it("Skip tour ends it from any stop", () => {
    startTour();
    mount();
    fireEvent.click(screen.getByTestId("tour-skip"));
    expect(readTour()).toEqual({ kind: "done", end: "skipped" });
  });
});

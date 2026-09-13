// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

vi.mock("@/lib/settings/store", async () => (await import("@/lib/setup/testing")).makeSettingsStub());
vi.mock("@/lib/notify/notify", () => ({ sendTestNotification: vi.fn(async () => ({ sent: true, reason: "" })) }));
const invoke = vi.fn(async (_cmd: string, _args?: unknown) => undefined);
vi.mock("@tauri-apps/api/core", () => ({ invoke: (cmd: string, args?: unknown) => invoke(cmd, args) }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn(async () => { throw new Error("ECONNREFUSED"); }) }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/plugin-autostart", () => ({ enable: vi.fn(), disable: vi.fn(), isEnabled: vi.fn(async () => false) }));
const app = { customizable: { wakeWord: { isEnabled: false } }, toggleWakeWord: vi.fn() };
vi.mock("@/contexts", async () => {
  const actual = await vi.importActual<typeof import("@/contexts")>("@/contexts");
  return { ...actual, useApp: () => app };
});

import * as store from "@/lib/settings/store";
import { ThemeProvider } from "@/contexts/theme.context";
import { TOUR_CAPTION_KEY, readTour, resetTour } from "@/lib/tour";
import Welcome from "./index";

type Stub = ReturnType<typeof import("@/lib/setup/testing").makeSettingsStub>;
const stub = store as unknown as Stub;

const Where = () => <span data-testid="where">{useLocation().pathname}</span>;

function mount(path = "/welcome") {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <Where />
        <Routes>
          <Route path="/welcome" element={<Welcome />} />
          <Route path="/workbench" element={<p data-testid="workbench">workbench</p>} />
          <Route path="/engine" element={<p data-testid="engine-page">engine</p>} />
        </Routes>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  resetTour();
  stub.__reset();
  invoke.mockClear();
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    // Reduced motion: the step swap is one frame, so the tests need no timers.
    value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }),
  });
  vi.spyOn(console, "info").mockImplementation(() => undefined);
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Welcome", () => {
  it("opens on Hello with no dots and no Back, and persists each step to the store", async () => {
    mount();
    expect(screen.getByTestId("setup-title").textContent).toBe("Hello.");
    expect(screen.queryByTestId("setup-dots")).toBeNull();
    expect(screen.queryByTestId("setup-back")).toBeNull();
    fireEvent.click(screen.getByTestId("setup-continue"));
    await waitFor(() => expect(screen.getByTestId("setup-title").textContent).toBe("Choose your look"));
    expect(stub.__values()["setup.step"]).toBe("appearance");
    expect(stub.__values()["setup.remaining"]).toEqual(["kicad", "google", "stripe", "microsoft", "notifications", "voice"]);
    expect(screen.getByTestId("setup-dots")).toBeTruthy();
    expect(screen.getByTestId("setup-live").textContent).toBe("Step 1 of 4, Choose your look");
  });

  it("resumes from the stored step", () => {
    stub.__reset({ "setup.step": "permissions", "setup.skipped": ["google"], "setup.remaining": ["notifications", "voice"] });
    mount();
    expect(screen.getByTestId("setup-title").textContent).toBe("Permissions");
    expect(screen.getByTestId("setup-continue").textContent).toBe("Finish");
  });

  it("renders no step and leaves for the workbench when the store says finished", async () => {
    stub.__reset({ "setup.completed": true });
    mount();
    expect(screen.queryByTestId("setup-step")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/workbench"));
  });

  it("Run Setup Again arrives with ?jump=appearance and is honoured once", () => {
    mount("/welcome?jump=appearance");
    expect(screen.getByTestId("setup-title").textContent).toBe("Choose your look");
  });

  it("the engine step blocks Continue while nothing answers and links to the Engine page", async () => {
    stub.__reset({ "setup.step": "engine" });
    mount();
    await waitFor(() => expect(screen.getByTestId("engine-card").getAttribute("data-state")).toBe("down"));
    expect((screen.getByTestId("setup-continue") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("engine-different-address"));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/engine"));
  });

  it("Set Up Later skips this screen only, then the next one lands on done naming the skipped cards", async () => {
    stub.__reset({ "setup.step": "accounts" });
    mount();
    fireEvent.click(screen.getByTestId("setup-later"));
    await waitFor(() => expect(screen.getByTestId("setup-title").textContent).toBe("Permissions"));
    expect(screen.getByTestId("setup-footer")).not.toBeNull();
    fireEvent.click(screen.getByTestId("setup-later"));
    await waitFor(() => expect(screen.getByTestId("setup-title").textContent).toBe("You're all set"));
    expect(screen.queryByTestId("setup-footer")).toBeNull();
    expect(screen.getByTestId("setup-skipped-line").textContent).toBe(
      "Skipped: Google, Billing, Microsoft, Notifications, Hey Ada. Find them in Settings.",
    );
  });

  it("Take the tour finishes setup with the skipped cards, writes the strip caption and starts the tour", async () => {
    stub.__reset({ "setup.step": "done", "setup.skipped": ["google"], "setup.remaining": ["stripe"] });
    mount();
    fireEvent.click(screen.getByTestId("setup-take-tour"));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("setup_finish", { skipped: ["google", "stripe"] }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/workbench"));
    expect(stub.__values()["setup.completed"]).toBe(true);
    expect(stub.__values()["setup.version"]).toBe(1);
    expect(localStorage.getItem(TOUR_CAPTION_KEY)).toMatch(/^This is the field\./);
    expect(readTour()).toMatchObject({ kind: "active", index: 0 });
  });

  it("Not now finishes setup without a tour", async () => {
    stub.__reset({ "setup.step": "done" });
    mount();
    fireEvent.click(screen.getByTestId("setup-not-now"));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/workbench"));
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(TOUR_CAPTION_KEY)).toBeNull();
    expect(readTour().kind).toBe("idle");
  });

  it("Shift+Esc, confirmed, ends setup right away", async () => {
    stub.__reset({ "setup.step": "appearance" });
    mount();
    fireEvent.keyDown(window, { key: "Escape", shiftKey: true });
    fireEvent.click(screen.getByTestId("setup-exit-yes"));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("setup_finish", expect.anything()));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/workbench"));
  });
});

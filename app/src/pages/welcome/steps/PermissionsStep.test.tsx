// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/settings/store", async () => (await import("@/lib/setup/testing")).makeSettingsStub());
const sendTest = vi.fn(async () => ({ sent: true, reason: "" }));
vi.mock("@/lib/notify/notify", () => ({ sendTestNotification: () => sendTest() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("@tauri-apps/plugin-autostart", () => ({ enable: vi.fn(), disable: vi.fn(), isEnabled: vi.fn(async () => false) }));

const app = { customizable: { wakeWord: { isEnabled: false } }, toggleWakeWord: vi.fn() };
vi.mock("@/contexts", async () => {
  const actual = await vi.importActual<typeof import("@/contexts")>("@/contexts");
  return { ...actual, useApp: () => app };
});

import { MIC_SETTINGS_URL, PermissionsStep } from "./PermissionsStep";

beforeEach(() => {
  app.customizable.wakeWord.isEnabled = false;
  sendTest.mockClear();
});
afterEach(cleanup);

describe("PermissionsStep", () => {
  it("renders the three rows: notifications, microphone, wake word", () => {
    render(<PermissionsStep setCard={vi.fn()} />);
    expect(screen.getByTestId("permissions-notifications")).toBeTruthy();
    expect(screen.getByTestId("permissions-mic").getAttribute("data-state")).toBe("unasked");
    expect(screen.getByTestId("wake-word-switch")).toBeTruthy();
    // The setup variant carries no Settings header wrapper.
    expect(screen.queryByTestId("wake-word-settings")).toBeNull();
    expect(screen.queryByTestId("notifications-settings")).toBeNull();
  });

  it("the microphone row asks once and reports granted", async () => {
    const requestMic = vi.fn(async () => "granted" as const);
    render(<PermissionsStep setCard={vi.fn()} requestMic={requestMic} />);
    fireEvent.click(screen.getByTestId("mic-allow"));
    await waitFor(() => expect(screen.getByTestId("permissions-mic").getAttribute("data-state")).toBe("granted"));
    expect(requestMic).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("mic-allow")).toBeNull();
  });

  it("a refusal shows the System Settings link, which opens the microphone pane", async () => {
    const requestMic = vi.fn(async () => {
      throw new Error("Microphone permission was refused. Enable Hardy in System Settings → Privacy & Security → Microphone, then click the ear again.");
    });
    const openSettings = vi.fn(async () => undefined);
    render(<PermissionsStep setCard={vi.fn()} requestMic={requestMic} openSettings={openSettings} />);
    fireEvent.click(screen.getByTestId("mic-allow"));
    await waitFor(() => expect(screen.getByTestId("permissions-mic").getAttribute("data-state")).toBe("denied"));
    fireEvent.click(screen.getByTestId("mic-open-settings"));
    expect(openSettings).toHaveBeenCalledWith(MIC_SETTINGS_URL);
    expect(screen.getByTestId("mic-allow").textContent).toBe("Try again");
  });

  it("no microphone at all is its own state, not a refusal", async () => {
    const requestMic = vi.fn(async () => {
      throw new Error("this environment has no microphone access");
    });
    render(<PermissionsStep setCard={vi.fn()} requestMic={requestMic} />);
    fireEvent.click(screen.getByTestId("mic-allow"));
    await waitFor(() => expect(screen.getByTestId("permissions-mic").getAttribute("data-state")).toBe("unavailable"));
    expect(screen.queryByTestId("mic-open-settings")).toBeNull();
  });

  it("reports the notifications and voice cards from their switches", async () => {
    const setCard = vi.fn();
    render(<PermissionsStep setCard={setCard} />);
    expect(setCard).toHaveBeenCalledWith("notifications", false);
    expect(setCard).toHaveBeenCalledWith("voice", false);
    fireEvent.click(screen.getByTestId("notify-test"));
    await waitFor(() => expect(setCard).toHaveBeenCalledWith("notifications", true));
  });
});

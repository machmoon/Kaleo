// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/settings/store", async () => (await import("@/lib/setup/testing")).makeSettingsStub());
const sendTest = vi.fn(async () => ({ sent: true, reason: "" }));
vi.mock("@/lib/notify/notify", () => ({ sendTestNotification: () => sendTest() }));

import * as store from "@/lib/settings/store";
import { Notifications, SENT_LINE } from "./Notifications";

type Stub = ReturnType<typeof import("@/lib/setup/testing").makeSettingsStub>;
const stub = store as unknown as Stub;

beforeEach(() => {
  stub.__reset();
  sendTest.mockClear();
});
afterEach(cleanup);

const mount = (variant: "setup" | "settings" = "settings") =>
  render(
    <MemoryRouter>
      <Notifications variant={variant} />
    </MemoryRouter>,
  );

describe("Notifications", () => {
  it("defaults off with the radios locked, and the settings variant carries its header", () => {
    mount();
    expect(screen.getByTestId("notifications-settings")).toBeTruthy();
    expect(screen.getByTestId("notify-switch").getAttribute("aria-checked")).toBe("false");
    for (const radio of screen.getAllByTestId("notify-os-option")) {
      expect((radio as HTMLInputElement).disabled).toBe(true);
    }
    expect(screen.getByText("Sending a test turns notifications on.")).toBeTruthy();
  });

  it("the setup variant has no header wrapper", () => {
    mount("setup");
    expect(screen.queryByTestId("notifications-settings")).toBeNull();
    expect(screen.getByTestId("notify-test")).toBeTruthy();
  });

  it("the test button is the opt-in and reports Sent, never Delivered", async () => {
    mount();
    fireEvent.click(screen.getByTestId("notify-test"));
    await waitFor(() => expect(screen.getByTestId("notify-result").textContent).toContain(SENT_LINE));
    expect(sendTest).toHaveBeenCalledTimes(1);
    expect(stub.__values()["notify.enabled"]).toBe(true);
    expect(screen.getByTestId("notify-switch").getAttribute("aria-checked")).toBe("true");
    expect(screen.getByTestId("notify-result").textContent).not.toMatch(/Delivered/);
  });

  it("a send the plugin refused is reported in its words", async () => {
    sendTest.mockResolvedValueOnce({ sent: false, reason: "Notifications need the desktop app; this is a browser." });
    mount();
    fireEvent.click(screen.getByTestId("notify-test"));
    await waitFor(() => expect(screen.getByTestId("notify-result").textContent).toMatch(/^Not sent\. Notifications need/));
  });

  it("stores the focus option under notify.os with the plan's copy", () => {
    stub.__reset({ "notify.enabled": true });
    mount();
    const labels = screen.getAllByTestId("notify-os-option").map((el) => el.parentElement?.textContent);
    expect(labels).toEqual(["Always", "Only when you're in another app", "Never"]);
    const never = screen.getAllByTestId("notify-os-option").find((el) => el.getAttribute("data-os") === "never") as HTMLInputElement;
    fireEvent.click(never);
    expect(stub.__values()["notify.os"]).toBe("never");
  });

  it("follows a change made elsewhere", async () => {
    mount();
    await stub.setSetting("notify.enabled", true);
    await waitFor(() => expect(screen.getByTestId("notify-switch").getAttribute("aria-checked")).toBe("true"));
  });
});

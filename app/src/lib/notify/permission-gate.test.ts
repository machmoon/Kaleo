// @vitest-environment jsdom
//
// The seam between this repo's two notification halves.
//
// `notify.ts` sends with `send()`, which is `new window.Notification(...)`.
// That constructor does NOT throw when macOS has denied the app — the banner
// is silently dropped. So the original order (store the switch on, send,
// report "Sent") reported success for a notification nobody could ever
// receive, and the only way to discover it was to keep not getting banners.
// `notifications.ts` owns the permission, and this pins that it is actually
// consulted rather than merely present in the tree.

import { beforeEach, describe, expect, it, vi } from "vitest";

const enableNotifications = vi.fn();
vi.mock("@/lib/notifications", () => ({
  enableNotifications: () => enableNotifications(),
}));

const { sendTestNotification } = await import("./notify");
const { loadNotifySettings } = await import("./settings");

beforeEach(() => {
  window.localStorage.clear();
  enableNotifications.mockReset();
});

/** `isTauriRuntime()` reads this; jsdom has no `__TAURI_INTERNALS__`. */
function pretendDesktop(on: boolean) {
  if (on) {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  } else {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  }
}

describe("the permission gate", () => {
  it("refuses to claim a send when macOS denied the app", async () => {
    pretendDesktop(true);
    enableNotifications.mockResolvedValue({
      enabled: false,
      permission: "denied",
      message: "macOS has notifications turned off for Hardy.",
    });
    const result = await sendTestNotification();
    expect(result.sent).toBe(false);
    expect(result.reason).toMatch(/macOS/);
  });

  it("leaves the switch off when the permission was refused", async () => {
    // A toggle reading "on" over a denied permission is the same lie one
    // control to the left.
    pretendDesktop(true);
    enableNotifications.mockResolvedValue({
      enabled: false,
      permission: "denied",
      message: "denied",
    });
    await sendTestNotification();
    expect(loadNotifySettings().enabled).toBe(false);
  });

  it("asks the permission layer before sending, not after", async () => {
    pretendDesktop(true);
    enableNotifications.mockResolvedValue({
      enabled: false,
      permission: "default",
      message: "not granted",
    });
    await sendTestNotification();
    expect(enableNotifications).toHaveBeenCalledTimes(1);
  });

  it("still stores the preference in a browser, and still says it did not send", async () => {
    // The switch is a preference, and one asked for in a dev server is real
    // — it applies the moment the desktop app runs. Only the *claim* to have
    // sent would be false here.
    pretendDesktop(false);
    const result = await sendTestNotification();
    expect(result.sent).toBe(false);
    expect(result.reason).toMatch(/desktop app/);
    expect(loadNotifySettings().enabled).toBe(true);
    expect(enableNotifications).not.toHaveBeenCalled();
  });
});

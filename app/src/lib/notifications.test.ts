// @vitest-environment jsdom
//
// The guarantees with teeth here are the negative ones: a fresh profile does
// not notify, an unpermitted app does not notify, and merely reading the
// state never raises the macOS prompt. Each of those failing is silent in
// production — nobody files a bug saying "I was asked too early".

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const settings: Record<string, unknown> = {};
let tauri = true;

vi.mock("@/lib/settings/store", () => ({
  isTauriRuntime: () => tauri,
  getSetting: (key: string) => settings[key],
  setSetting: async (key: string, value: unknown) => {
    settings[key] = value;
  },
}));

const sendNotification = vi.fn<(options: { title: string; body?: string }) => void>();
const requestPermission = vi.fn<() => Promise<string>>(async () => "granted");
const isPermissionGranted = vi.fn<() => Promise<boolean>>(async () => false);

vi.mock("@tauri-apps/plugin-notification", () => ({
  sendNotification: (options: { title: string; body?: string }) => sendNotification(options),
  requestPermission: () => requestPermission(),
  isPermissionGranted: () => isPermissionGranted(),
}));

const invoke = vi.fn<(cmd: string) => Promise<unknown>>(async () => false);
vi.mock("@tauri-apps/api/core", () => ({ invoke: (cmd: string) => invoke(cmd) }));

import {
  DENIED_HINT,
  SETTING_KEY,
  UNSUPPORTED_HINT,
  disableNotifications,
  enableNotifications,
  notificationsEnabled,
  notify,
  readPermission,
} from "./notifications";

/** Stand in for the `window.Notification` the plugin's Rust half installs. */
function setWebviewPermission(value: "granted" | "denied" | "default" | null): void {
  const target = window as Window & { Notification?: unknown };
  if (value === null) {
    delete target.Notification;
    return;
  }
  target.Notification = { permission: value };
}

beforeEach(() => {
  for (const key of Object.keys(settings)) delete settings[key];
  tauri = true;
  setWebviewPermission("granted");
  sendNotification.mockClear();
  requestPermission.mockClear();
  requestPermission.mockResolvedValue("granted");
  isPermissionGranted.mockClear();
  isPermissionGranted.mockResolvedValue(false);
  invoke.mockClear();
  invoke.mockResolvedValue(false);
});

afterEach(() => setWebviewPermission(null));

describe("the stored preference", () => {
  it("is off when nothing was ever written, which is the whole default", () => {
    expect(notificationsEnabled()).toBe(false);
  });

  it("treats anything that is not a stored true as off", () => {
    for (const junk of ["true", 1, {}, null, undefined]) {
      settings[SETTING_KEY] = junk;
      expect(notificationsEnabled()).toBe(false);
    }
  });

  it("writes an explicit false when turned off rather than clearing the key", async () => {
    settings[SETTING_KEY] = true;
    await disableNotifications();
    expect(settings[SETTING_KEY]).toBe(false);
    expect(notificationsEnabled()).toBe(false);
  });
});

describe("reading the permission", () => {
  it("never raises the macOS prompt, so opening the pane cannot cost a Don't Allow", async () => {
    setWebviewPermission("default");
    expect(await readPermission()).toBe("default");
    expect(requestPermission).not.toHaveBeenCalled();
  });

  it("reports unsupported rather than denied outside the desktop app", async () => {
    tauri = false;
    expect(await readPermission()).toBe("unsupported");
  });

  it("calls a not-yet-granted plugin answer default, not denied", async () => {
    setWebviewPermission(null);
    isPermissionGranted.mockResolvedValue(false);
    expect(await readPermission()).toBe("default");
  });
});

describe("enabling", () => {
  it("asks for the permission only on enable, and stores true once granted", async () => {
    setWebviewPermission("default");
    const outcome = await enableNotifications();
    expect(requestPermission).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({ enabled: true, permission: "granted", message: "" });
    expect(settings[SETTING_KEY]).toBe(true);
  });

  it("reports a denied permission instead of swallowing it, and leaves the switch off", async () => {
    setWebviewPermission("default");
    requestPermission.mockResolvedValue("denied");
    const outcome = await enableNotifications();
    expect(outcome.enabled).toBe(false);
    expect(outcome.permission).toBe("denied");
    expect(outcome.message).toBe(DENIED_HINT);
    expect(settings[SETTING_KEY]).toBeUndefined();
  });

  it("says the desktop app is needed rather than blaming System Settings in a browser", async () => {
    tauri = false;
    const outcome = await enableNotifications();
    expect(outcome).toEqual({ enabled: false, permission: "unsupported", message: UNSUPPORTED_HINT });
    expect(settings[SETTING_KEY]).toBeUndefined();
  });

  it("does not prompt again when the permission is already granted", async () => {
    const outcome = await enableNotifications();
    expect(requestPermission).not.toHaveBeenCalled();
    expect(outcome.enabled).toBe(true);
  });
});

describe("notify", () => {
  const request = { title: "Board generated", body: "12/12 nets routed", kind: "done" } as const;

  it("is a no-op while the preference is off", async () => {
    expect(await notify(request)).toBe("off");
    expect(sendNotification).not.toHaveBeenCalled();
  });

  it("is a no-op when the permission is denied even though the switch says on", async () => {
    settings[SETTING_KEY] = true;
    setWebviewPermission("denied");
    expect(await notify(request)).toBe("not-permitted");
    expect(sendNotification).not.toHaveBeenCalled();
  });

  it("stays silent while Kaleo is the frontmost app, because the user is already looking", async () => {
    settings[SETTING_KEY] = true;
    invoke.mockResolvedValue(true);
    expect(await notify(request)).toBe("focused");
    expect(sendNotification).not.toHaveBeenCalled();
  });

  it("sends when it is on, permitted and the user is in another app", async () => {
    settings[SETTING_KEY] = true;
    expect(await notify(request)).toBe("sent");
    expect(sendNotification).toHaveBeenCalledWith({
      title: "Board generated",
      body: "12/12 nets routed",
      group: "done",
    });
  });

  it("falls back to the DOM answer when kaleo_has_focus is not registered", async () => {
    settings[SETTING_KEY] = true;
    invoke.mockRejectedValue(new Error("unknown command"));
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    expect(await notify(request)).toBe("focused");
    vi.restoreAllMocks();
  });

  it("returns a reason instead of throwing when the send itself fails", async () => {
    settings[SETTING_KEY] = true;
    sendNotification.mockImplementation(() => {
      throw new Error("plugin not registered");
    });
    await expect(notify(request)).resolves.toBe("send-failed");
  });
});

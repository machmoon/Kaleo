// Notification settings, read and written through the settings store
// (`src/lib/settings/`), the shape `speech/settings.ts` gives voice.
//
// Default OFF. A first launch that fires a macOS banner nobody asked for is
// exactly the behaviour people uninstall over, so the opt-in is an action:
// the Setup Assistant's "Send a test notification" button turns the switch
// on and then sends (`sendTestNotification`), and until then nothing fires.

import { getSetting, setSetting, subscribe } from "@/lib/settings/store";
import type { NotifyOs } from "@/lib/settings/keys";

export type { NotifyOs };

export interface NotifySettings {
  /** The on/off switch for native banners. */
  enabled: boolean;
  /** When a banner may fire relative to Kaleo being the frontmost app. */
  os: NotifyOs;
}

export const NOTIFY_DEFAULT_ENABLED = false;
export const NOTIFY_DEFAULT_OS: NotifyOs = "not_focused";

/** The three choices as the settings radio renders them, in display order. */
export const NOTIFY_OS_OPTIONS: readonly { value: NotifyOs; label: string }[] = Object.freeze([
  { value: "not_focused", label: "Only when you're in another app" },
  { value: "always", label: "Always" },
  { value: "never", label: "Never" },
]);

export function loadNotifySettings(): NotifySettings {
  return {
    enabled: getSetting("notify.enabled"),
    os: getSetting("notify.os"),
  };
}

export function saveNotifyEnabled(enabled: boolean): Promise<void> {
  return setSetting("notify.enabled", enabled);
}

export function saveNotifyOs(os: NotifyOs): Promise<void> {
  return setSetting("notify.os", os);
}

/** Hear either notify setting change, from this window or the other. */
export function subscribeNotifySettings(cb: (settings: NotifySettings) => void): () => void {
  const a = subscribe("notify.enabled", () => cb(loadNotifySettings()));
  const b = subscribe("notify.os", () => cb(loadNotifySettings()));
  return () => {
    a();
    b();
  };
}

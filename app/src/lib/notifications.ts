// Optional macOS banners for a run that finished while you were somewhere
// else. Off until someone turns it on, and silent unless macOS agrees.
//
// WHY THE TAURI PLUGIN AND NOT THE BROWSER `Notification` API
// -----------------------------------------------------------
// Verified in this checkout rather than assumed:
//
//   - `@tauri-apps/plugin-notification` 2.4.0 is already a dependency
//     (`app/package.json`), so the JS half is present.
//   - Its `sendNotification` is literally `new window.Notification(title,
//     options)` (node_modules/@tauri-apps/plugin-notification/dist-js/
//     index.js), and `requestPermission` is
//     `window.Notification.requestPermission()`. So the plugin is NOT a
//     different transport from the "browser API" — it is the browser API,
//     over a `window.Notification` that the plugin's Rust half replaces with
//     one that reaches `UNUserNotificationCenter`.
//   - That Rust half is not installed yet: `src-tauri/Cargo.toml` has no
//     `tauri-plugin-notification`, `src-tauri/src/lib.rs` registers none, and
//     neither capability file grants `notification:default`. Without those
//     three, macOS WKWebView's own `Notification` object is what the page
//     gets, and WKWebView does not implement Web Notifications at all — the
//     constructor is simply absent, so a "just use the browser API" build
//     would be silently dead on macOS.
//
// So: plugin, and the Rust entries are reported as wiring rather than
// guessed at here. Everything below degrades to a no-op — never a throw —
// while that wiring is missing, which is also exactly how it behaves in
// Vitest and in a plain `npm run dev` browser tab.
//
// WHERE THE PREFERENCE LIVES
// --------------------------
// `notify.enabled` in the settings store (`src/lib/settings/keys.ts`), NOT a
// new `KALEO_STORAGE_KEYS` entry. That key already exists, already defaults
// to `false`, is already validated and already crosses between the strip and
// the dashboard webviews. A second key for the same preference is the bug
// `keys.ts` warns about in its own header comment: two sources of truth for
// one switch, drifting apart the first time a user flips one of them.

import { getSetting, isTauriRuntime, setSetting } from "@/lib/settings/store";

/** What the banner is about. Callers name the moment; the copy is theirs. */
export type NotifyKind = "done" | "needs-approval" | "failed";

/**
 * `unsupported` is not a permission macOS ever reports — it is this app
 * saying it cannot ask: a browser tab, or a desktop build whose Rust plugin
 * is not registered. Kept distinct from `denied` so the pane never tells
 * someone to go fix a System Settings switch that has nothing to do with it.
 */
export type NotifyPermission = "granted" | "denied" | "default" | "unsupported";

export interface NotifyRequest {
  title: string;
  body: string;
  kind: NotifyKind;
}

export interface EnableOutcome {
  /** Whether notifications are now on. False whenever macOS did not agree. */
  enabled: boolean;
  permission: NotifyPermission;
  /** Empty when `enabled`; otherwise a sentence for the pane to show. */
  message: string;
}

/** Why a banner did not fire. Returned so a caller can log it; never thrown. */
export type NotifySkip =
  | "sent"
  | "off"
  | "unsupported"
  | "not-permitted"
  | "focused"
  | "send-failed";

export const SETTING_KEY = "notify.enabled" as const;

/**
 * A denied permission is a dead end the app cannot undo from code —
 * `requestPermission()` resolves `denied` immediately forever after — so the
 * only useful thing to say is where the switch actually is.
 */
export const DENIED_HINT =
  "macOS is blocking Hardy's notifications. Open System Settings › Notifications › Hardy, allow them, then turn this back on.";

export const UNSUPPORTED_HINT =
  "Notifications need the desktop app. Nothing will be sent from a browser tab.";

// ------------------------------------------------------------ the preference

/**
 * Absent means off. Anything but a stored `true` — never written, a
 * hand-edited `settings.json`, a half-migrated mirror — reads as off, so the
 * only way to get a banner is an explicit `true` someone put there.
 */
export function notificationsEnabled(): boolean {
  return getSetting(SETTING_KEY) === true;
}

/** Turn them off. Writes `false` explicitly rather than clearing the key. */
export async function disableNotifications(): Promise<void> {
  await setSetting(SETTING_KEY, false);
}

// ------------------------------------------------------------- the permission

interface PermissionCarrier {
  permission?: string;
}

/**
 * The webview's own answer, or null when there is no `Notification` object to
 * ask (WKWebView without the plugin, jsdom, node). Reading `.permission` is
 * a property access — it cannot prompt, which is what makes it safe to call
 * while a settings pane is merely being opened.
 */
function webviewPermission(): NotifyPermission | null {
  if (typeof window === "undefined") return null;
  const ctor = (window as Window & { Notification?: PermissionCarrier }).Notification;
  const value = ctor?.permission;
  return value === "granted" || value === "denied" || value === "default" ? value : null;
}

/**
 * The current permission, WITHOUT asking for it. A settings pane that raises
 * an OS prompt just by being opened teaches people to hit Don't Allow before
 * they have read the switch, and that answer is permanent.
 */
export async function readPermission(): Promise<NotifyPermission> {
  if (!isTauriRuntime()) return "unsupported";
  const fromWebview = webviewPermission();
  if (fromWebview) return fromWebview;
  try {
    const { isPermissionGranted } = await import("@tauri-apps/plugin-notification");
    // Boolean, so a false here is "not granted" — which is `default` until
    // something says otherwise. Guessing `denied` would put the System
    // Settings hint in front of someone who has never been asked.
    return (await isPermissionGranted()) ? "granted" : "default";
  } catch {
    return "unsupported";
  }
}

/**
 * The one place that may prompt. Only ever reached from a click on the
 * switch, and it refuses to store `true` unless macOS actually granted —
 * a toggle reading "on" over a denied permission is a lie the user only
 * discovers by never getting a banner.
 */
export async function enableNotifications(): Promise<EnableOutcome> {
  let permission = await readPermission();

  if (permission === "unsupported") {
    return { enabled: false, permission, message: UNSUPPORTED_HINT };
  }

  if (permission === "default") {
    try {
      const { requestPermission } = await import("@tauri-apps/plugin-notification");
      const answer = await requestPermission();
      permission = answer === "granted" ? "granted" : answer === "denied" ? "denied" : "default";
    } catch {
      return { enabled: false, permission: "unsupported", message: UNSUPPORTED_HINT };
    }
  }

  if (permission !== "granted") {
    return { enabled: false, permission, message: DENIED_HINT };
  }

  await setSetting(SETTING_KEY, true);
  return { enabled: true, permission, message: "" };
}

// ---------------------------------------------------------------- the sending

/**
 * Is Kaleo frontmost? `kaleo_has_focus` is asked first because it knows about
 * both windows; `document.hasFocus()` alone answers for one 58 px panel, which
 * is not the same question. The command may not be registered — the fallback
 * is not an error path.
 */
async function kaleoIsFrontmost(): Promise<boolean> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const answer = await invoke<unknown>("kaleo_has_focus");
    if (typeof answer === "boolean") return answer;
  } catch {
    // No such command in this build.
  }
  try {
    return typeof document !== "undefined" && document.hasFocus();
  } catch {
    return false;
  }
}

/**
 * Send a banner if every gate agrees. Returns why it did not, and never
 * throws: this is called from run hooks, and a notification failing is not
 * allowed to take a run down with it.
 */
export async function notify(request: NotifyRequest): Promise<NotifySkip> {
  if (!notificationsEnabled()) return "off";
  if (!isTauriRuntime()) return "unsupported";

  try {
    if ((await readPermission()) !== "granted") return "not-permitted";
    // The whole point of the feature is the run you are not watching.
    if (await kaleoIsFrontmost()) return "focused";

    const { sendNotification } = await import("@tauri-apps/plugin-notification");
    // `group` is advisory: the desktop path forwards title and body to
    // `new window.Notification`, so macOS does not collapse on it today. It
    // is carried anyway so the kind survives to whatever reads these next.
    sendNotification({ title: request.title, body: request.body, group: request.kind });
    return "sent";
  } catch {
    return "send-failed";
  }
}

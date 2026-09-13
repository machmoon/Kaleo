// Native macOS banners for the milestones of a run, fired from the strip.
//
// The rules, all of them load-bearing:
//
//   - Only the `main` webview fires. Both windows run the same hooks over the
//     same run (the dashboard adopts what the strip publishes), so without
//     this every milestone would land twice. The test button is the one
//     exception, since it is pressed in the dashboard.
//   - "Focused" is asked of the Rust side (`kaleo_has_focus`: is either of
//     Kaleo's windows the key window), because `document.hasFocus()` in a
//     58 px non-activating panel is not "Kaleo is frontmost". The DOM answer
//     is the fallback while the command does not exist.
//   - Copy is frozen here (`milestoneCopy`) and honest: never "board ready"
//     over unrouted nets, never a case or sourcing outcome before it has been
//     collected — a background job that has left the engine's list has
//     finished, and only the collecting step's warning says whether it
//     failed. Banners carry no URL: a bearer-gated engine address is not
//     something to paint on a lock screen.
//   - `sendNotification` is synchronous and void in the plugin (it is
//     `new window.Notification(...)` behind Tauri's patched constructor), so
//     it is called, not awaited, and nothing here ever throws into a hook.
//
// The plugin cannot say whether a banner was shown (`docs/overlay-skins.md`
// §Notifications): "Sent" is the strongest word any caller may use.

import { isTauriRuntime } from "@/lib/settings/store";
import { REVIEW_CLEAN_LINE, REVIEW_FAILED_TAIL, type ReviewOutcomeStatus } from "@/lib/silkscreen/steps";
import { loadNotifySettings, saveNotifyEnabled, type NotifySettings } from "./settings";
// The permission layer. Kept in its own module because it is the half that
// talks to macOS, and it is what stops this file storing an "on" switch over
// a permission the OS has already refused.
import { enableNotifications } from "@/lib/notifications";

/** The window label whose hooks are allowed to fire milestone banners. */
export const NOTIFY_WEBVIEW_LABEL = "main";

// ------------------------------------------------------------------ the gate

/**
 * May a banner fire right now? `focused` is whether Kaleo is the frontmost
 * app, from whichever signal the caller has.
 */
export function shouldNotify(settings: NotifySettings, focused: boolean): boolean {
  if (!settings.enabled) return false;
  switch (settings.os) {
    case "always":
      return true;
    case "never":
      return false;
    case "not_focused":
      return !focused;
    default:
      return false;
  }
}

// ------------------------------------------------------------------ the copy

export type BackgroundStep = "case" | "sourcing";

export type Milestone =
  | { kind: "placed"; parts: number; boardMm: readonly [number, number] | null }
  | { kind: "routed"; routed: number; total: number; unrouted: number }
  /**
   * The critic answered. `status` is what became of it (`reviewOutcome`);
   * absent means an older engine, which never distinguished a critic that
   * said nothing from one that never answered, so it reads as `ok`. A
   * `failed` status here never produces the clean-board line: the raiser
   * should send `review_failed` instead, and if it does not, the copy says
   * failed anyway.
   */
  | { kind: "reviewed"; findings: number; blockers: number; status?: ReviewOutcomeStatus }
  /** The critic did not answer. `detail` is the engine's own sentence, or null. */
  | { kind: "review_failed"; detail: string | null }
  /**
   * A background job changed state. Only `settled` (left the engine's
   * `background` list, outcome unreported) produces copy: `running` is not
   * news and `failed` is announced by the collecting step through
   * `*_failed`, with the engine's own sentence.
   */
  | { kind: "case_done" | "sourcing_done"; state: "running" | "settled" | "failed" }
  | { kind: "case_failed" | "sourcing_failed"; warning: string }
  | { kind: "run_done"; routed: number; total: number; unrouted: number; blockers: number }
  | { kind: "run_failed"; step: string; message: string }
  | { kind: "engine_up"; baseUrl: string };

export interface NotificationCopy {
  title: string;
  body: string;
}

const BACKGROUND_LABEL: Record<BackgroundStep, string> = { case: "Case", sourcing: "Sourcing" };

function backgroundStepOf(kind: Milestone["kind"]): BackgroundStep {
  return kind.startsWith("case") ? "case" : "sourcing";
}

function nets(routed: number, total: number, sep: " of " | "/"): string {
  return `${routed}${sep}${total} nets routed`;
}

function reviewFailedCopy(detail: string | null): NotificationCopy {
  const why = detail?.trim() || "The critic answered nothing readable";
  return { title: "Review failed", body: `${why} — ${REVIEW_FAILED_TAIL}` };
}

/**
 * The frozen banner text for a milestone, or null when it must not be
 * announced at all.
 */
export function milestoneCopy(milestone: Milestone): NotificationCopy | null {
  switch (milestone.kind) {
    case "placed": {
      const parts = `${milestone.parts} part${milestone.parts === 1 ? "" : "s"}`;
      const body = milestone.boardMm
        ? `${parts} on ${milestone.boardMm[0]} × ${milestone.boardMm[1]} mm`
        : `${parts} placed`;
      return { title: "Placement is in KiCad", body };
    }
    case "routed": {
      const { routed, total, unrouted } = milestone;
      return {
        title: unrouted > 0 ? "Routing finished with open nets" : "Routing finished",
        body: `${nets(routed, total, " of ")}, ${unrouted} left as ratsnest`,
      };
    }
    case "reviewed": {
      const { findings, blockers, status } = milestone;
      // A critic that answered nothing has no count: "0 findings" is the
      // clean-board reading this outcome exists to refuse.
      if (status === "failed") return reviewFailedCopy(null);
      // A review nobody asked for is not news.
      if (status === "skipped") return null;
      return {
        title: "Review finished",
        body:
          findings === 0
            ? REVIEW_CLEAN_LINE
            : `${findings} finding${findings === 1 ? "" : "s"}, ${blockers} blocking`,
      };
    }
    case "review_failed":
      return reviewFailedCopy(milestone.detail);
    case "case_done":
    case "sourcing_done": {
      if (milestone.state !== "settled") return null;
      const label = BACKGROUND_LABEL[backgroundStepOf(milestone.kind)];
      return {
        title: `${label} finished in the background`,
        body: `Press ${label} to collect it`,
      };
    }
    case "case_failed":
    case "sourcing_failed": {
      const label = BACKGROUND_LABEL[backgroundStepOf(milestone.kind)];
      const warning = milestone.warning.trim();
      if (!warning) return null;
      return { title: `${label} failed in the background`, body: warning };
    }
    case "run_done": {
      const { routed, total, unrouted, blockers } = milestone;
      return {
        title: unrouted > 0 ? "Board generated with open nets" : "Board generated",
        body: `${nets(routed, total, "/")}, ${blockers} blocker${blockers === 1 ? "" : "s"}`,
      };
    }
    case "run_failed": {
      const step = milestone.step.trim() || "Run";
      return { title: `${step} failed`, body: milestone.message.trim() || "No reason was given." };
    }
    case "engine_up":
      // No URL in a banner, so the address stays in the app.
      return { title: "Engine is back", body: "The engine answered /healthz." };
    default:
      return null;
  }
}

// -------------------------------------------------------------- the delivery

let labelPromise: Promise<string | null> | null = null;

/** The current webview's label, cached; null outside Tauri or if the API refuses. */
export function currentWebviewLabel(): Promise<string | null> {
  if (!labelPromise) {
    labelPromise = (async () => {
      if (!isTauriRuntime()) return null;
      try {
        const { getCurrentWebviewWindow } = await import("@tauri-apps/api/webviewWindow");
        return getCurrentWebviewWindow().label;
      } catch {
        return null;
      }
    })();
  }
  return labelPromise;
}

/**
 * Is Kaleo frontmost? The Rust command knows about both windows; the DOM
 * fallback is the best this webview can say alone.
 */
export async function kaleoHasFocus(): Promise<boolean> {
  if (isTauriRuntime()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const answer = await invoke<unknown>("kaleo_has_focus");
      if (typeof answer === "boolean") return answer;
    } catch {
      // The command is not there yet (the Rust half lands separately).
    }
  }
  try {
    return typeof document !== "undefined" && document.hasFocus();
  } catch {
    return false;
  }
}

async function send(copy: NotificationCopy): Promise<void> {
  const { sendNotification } = await import("@tauri-apps/plugin-notification");
  // Synchronous and void in the plugin; deliberately not awaited.
  sendNotification({ title: copy.title, body: copy.body });
}

/**
 * Fire a banner for a milestone if the gate allows. Never throws, never
 * awaited: a hook calls this and moves on.
 */
export function notifyMilestone(milestone: Milestone): void {
  const copy = milestoneCopy(milestone);
  if (!copy) return;
  if (!isTauriRuntime()) return;
  void (async () => {
    try {
      if ((await currentWebviewLabel()) !== NOTIFY_WEBVIEW_LABEL) return;
      const settings = loadNotifySettings();
      if (!settings.enabled) return;
      if (!shouldNotify(settings, await kaleoHasFocus())) return;
      await send(copy);
    } catch (error) {
      console.warn("[kaleo notify] could not send a notification:", error);
    }
  })();
}

export interface TestNotificationResult {
  /** True when the plugin was handed the banner. Not "delivered": nothing can say that. */
  sent: boolean;
  /** Why not, in a sentence, when `sent` is false. */
  reason: string;
}

/**
 * The opt-in. Turns `notify.enabled` on, then sends one banner regardless of
 * focus or which window pressed the button — the point is to see whether
 * macOS shows it at all.
 */
export async function sendTestNotification(): Promise<TestNotificationResult> {
  if (!isTauriRuntime()) {
    // Still stored. The switch is a *preference*, and a preference asked for
    // in a browser dev server is real — it applies the moment the desktop
    // app runs. What must not happen is claiming to have sent.
    await saveNotifyEnabled(true);
    return { sent: false, reason: "Notifications need the desktop app; this is a browser." };
  }

  // On the desktop the permission has to be settled BEFORE the switch is
  // stored, because here "on" is falsifiable by the OS. `send` is
  // `new window.Notification(...)`, and that constructor does NOT throw when
  // macOS has denied the app — the banner is simply dropped. So the old
  // order (store true, send, report "Sent") reported success for a
  // notification nobody could receive, and the only way to find out was to
  // keep not getting banners. `enableNotifications` stores `true` itself,
  // and only once macOS has actually granted.
  const outcome = await enableNotifications();
  if (!outcome.enabled) {
    return { sent: false, reason: outcome.message };
  }

  try {
    await send({
      title: "Hardy is set up",
      body: "This is what a finished run looks like.",
    });
    return { sent: true, reason: "" };
  } catch (error) {
    return {
      sent: false,
      reason: (error as Error)?.message || "The notification plugin refused.",
    };
  }
}

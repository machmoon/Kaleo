/**
 * Seam for an on-device "Hey Ada" spotter (Porcupine / cpal in Rust).
 *
 * The webview must not own the always-on mic. Flow:
 *   localWakeStatus → startLocalWake → subscribeLocalWake(ada-wake)
 *   → one command clip → /transcribe → stopLocalWake
 *
 * If status.available is false, the ear falls back to one-shot / PTT —
 * never idle Gemini windows.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

/** Event name Rust's spotter emits. */
export const LOCAL_WAKE_EVENT = "ada-wake";

export interface LocalWakeHit {
  /** Optional phrase the classifier matched ("ada" / "hey ada"). */
  phrase?: string;
  /**
   * If the spotter already captured the command, the page can skip a second
   * clip. Empty/absent means "start one short recording".
   */
  utterance?: string;
}

export interface LocalWakeStatus {
  available: boolean;
  listening: boolean;
  backend: string;
  reason: string;
  platform?: string;
  has_access_key?: boolean;
  keyword_count?: number;
}

export function unavailableLocalWake(reason =
  "no on-device wake — use the mic button to dictate"
): LocalWakeStatus {
  return {
    available: false,
    listening: false,
    backend: "none",
    reason,
  };
}

export function describeLocalWake(available: boolean): string {
  return available
    ? "on-device wake word, then one engine transcript for the command"
    : "no on-device wake — use the mic button to dictate";
}

export async function localWakeStatus(): Promise<LocalWakeStatus> {
  try {
    return await invoke<LocalWakeStatus>("wake_status");
  } catch {
    return unavailableLocalWake();
  }
}

/** Arm the Rust mic + spotter. No-op-ish error when Tauri is missing. */
export async function startLocalWake(): Promise<LocalWakeStatus> {
  return invoke<LocalWakeStatus>("wake_start");
}

/** Drop the Rust mic. Safe to call twice / when never started. */
export async function stopLocalWake(): Promise<void> {
  try {
    await invoke("wake_stop");
  } catch {
    /* ignore */
  }
}

/** Offline / mock: fire one `ada-wake` without speaking (needs KALEO_WAKE_MOCK). */
export async function debugTriggerLocalWake(): Promise<void> {
  await invoke("wake_debug_trigger");
}

/**
 * Listen for `ada-wake` only. Does not start the spotter — pair with
 * startLocalWake via createLocalListener.
 */
export async function subscribeLocalWake(
  onHit: (hit: LocalWakeHit) => void
): Promise<UnlistenFn | null> {
  try {
    return await listen<LocalWakeHit>(LOCAL_WAKE_EVENT, (event) => {
      onHit(event.payload ?? {});
    });
  } catch {
    return null;
  }
}

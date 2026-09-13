// The overlay's half of the menu bar icon (src-tauri/src/tray.rs).
//
// The tray draws two facts it cannot see for itself: whether Ada's microphone
// is open (the glyph fills in, and the "Ada listening" check item is ticked)
// and, on Windows, whether the hide shortcut has blanked the webview. Both
// live in React, so this hook reports them with `tray_set_state` every time
// they change — and that report is the ONLY thing that moves the check mark.
// Clicking the item in the menu does not tick it; it emits `tray-ada-toggle`,
// the page flips the ear, and the next report ticks it if the mic actually
// opened. A mark that followed the click rather than the microphone would be
// a lie about an open mic, which is the one thing this app's indicators must
// never do (see the green-dot note in VoiceControl).
//
// A failed report is a console.warn: a stale menu bar beats a crashed
// overlay, the same rule useOverlayHeight and useOverlayDock apply.

import { useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

/** Emitted by the tray when "Ada listening" is clicked; pinned in tray.rs. */
export const TRAY_TOGGLE_EVENT = "tray-ada-toggle";

export interface TrayStateInput {
  /** The microphone is open right now — the ear's `listening`, not its switch. */
  listening: boolean;
  /** The overlay is on screen (on Windows, not blanked by the hide shortcut). */
  visible: boolean;
  /** What a click on "Ada listening" does: flip the ear's switch. */
  onToggleListening: () => void;
}

export function useTrayState({ listening, visible, onToggleListening }: TrayStateInput): void {
  // Through a ref so the listener is installed once and still calls the
  // current handler — the same pattern the page uses for the speech duck.
  const toggleRef = useRef(onToggleListening);
  useEffect(() => {
    toggleRef.current = onToggleListening;
  }, [onToggleListening]);

  useEffect(() => {
    invoke("tray_set_state", { listening, visible }).catch((error) =>
      console.warn("[kaleo tray] could not report state:", error)
    );
  }, [listening, visible]);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    listen(TRAY_TOGGLE_EVENT, () => toggleRef.current())
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch((error) => console.warn("[kaleo tray] toggle listener:", error));
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);
}

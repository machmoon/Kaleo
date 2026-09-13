// The overlay window is created 600×58 and non-resizable, so the *window*
// never grows on its own — only the content does, and anything below the
// collapsed height is clipped by the webview. Upstream Pluely solved this
// with a Rust command, `set_window_height`, which survived the cleanup
// (registered in lib.rs) but lost its last frontend caller. This hook is the
// caller: it watches the overlay Card's rendered height and keeps the window
// exactly that tall.
//
// The run that "felt like nothing happened" was this: the state machine,
// progress checklist, result card and error card all worked, all rendered —
// from y=58 down, invisible.
//
// @deprecated Superseded by `useOverlaySize` (`@/hooks/useOverlaySize`), which
// sizes the window from the overlay's state instead of following a
// `ResizeObserver`. Measuring is one frame late by construction, so the native
// resize can never share a clock with a CSS transition, and it sends one
// invoke per frame in which the measurement moves — a live run is a stream of
// native resizes, each of which also wakes the dock. See
// `docs/overlay-motion-audit.md` §2.2, §3.2, §3.5 and `src/lib/overlay-size.ts`.
// Kept working, unchanged, only until the last caller has migrated. Do not
// add new callers; do not add features here.

import { useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";

// The size band is now owned by `overlay-size.ts` (pure, no Tauri import), so
// there is exactly one copy of the numbers that have to agree with
// tauri.conf.json and window.rs. Re-exported here so existing importers keep
// working through the migration.
import {
  OVERLAY_COLLAPSED_HEIGHT,
  OVERLAY_MAX_HEIGHT,
  OVERLAY_MIN_WIDTH,
  OVERLAY_WIDTH,
} from "@/lib/overlay-size";

export {
  OVERLAY_COLLAPSED_HEIGHT,
  OVERLAY_MAX_HEIGHT,
  OVERLAY_MIN_WIDTH,
  OVERLAY_WIDTH,
};

/** Clamp a measured content height to the window's allowed band. */
export function overlayHeightFor(contentHeight: number): number {
  if (!Number.isFinite(contentHeight) || contentHeight <= 0) {
    return OVERLAY_COLLAPSED_HEIGHT;
  }
  return Math.max(
    OVERLAY_COLLAPSED_HEIGHT,
    Math.min(OVERLAY_MAX_HEIGHT, Math.ceil(contentHeight))
  );
}

/**
 * The window's width for a measured content width. The full bar is always
 * the window's native width; only the idle pill (`compact`) shrinks the
 * window to fit, so a transparent 600px strip is not lying over KiCad
 * eating clicks while the overlay shows three buttons.
 */
export function overlayWidthFor(contentWidth: number, compact: boolean): number {
  if (!compact) return OVERLAY_WIDTH;
  if (!Number.isFinite(contentWidth) || contentWidth <= 0) return OVERLAY_WIDTH;
  return Math.max(OVERLAY_MIN_WIDTH, Math.min(OVERLAY_WIDTH, Math.ceil(contentWidth)));
}

/**
 * Attach the returned ref to the element whose size the window should track
 * (the overlay Card). One resize invoke per animation frame, and only when the
 * clamped size actually changed. A failed invoke is warned, not thrown: a
 * clipped-but-working overlay beats a crashed one. `compact` says the pill is
 * showing, which is the only time the width follows the content.
 *
 * @deprecated Use `useOverlaySize(state)` instead; see the header.
 */
export function useOverlayHeight<T extends HTMLElement>(compact = false) {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    let last = "";
    let frame = 0;
    const apply = () => {
      frame = 0;
      // Prefer scrollHeight: getBoundingClientRect reports the clipped size
      // when an ancestor is `h-screen overflow-hidden`, which is exactly how
      // the overlay root is laid out — measuring the clipped box kept the
      // window stuck at the collapsed height forever.
      const height = overlayHeightFor(
        Math.max(el.scrollHeight, el.getBoundingClientRect().height)
      );
      const width = overlayWidthFor(
        Math.max(el.scrollWidth, el.getBoundingClientRect().width),
        compact
      );
      const key = `${width}x${height}`;
      if (key === last) return;
      last = key;
      invoke("set_window_height", { height, width }).catch((error) => {
        // The resize did not happen, so the window is still the size it was.
        // Forget the key: `last` is a record of what the *window* is, not of
        // what we asked for, and remembering a failed ask makes every later
        // observation of the same content dedupe against a size the window
        // never took — one dropped invoke would leave the overlay clipped for
        // the rest of the session, silently, which is the exact bug this hook
        // exists to fix. Only clear it if nothing newer has been applied.
        if (last === key) last = "";
        console.warn("[kaleo overlay] set_window_height failed:", error);
      });
    };

    const observer = new ResizeObserver(() => {
      if (frame) return;
      frame = requestAnimationFrame(apply);
    });
    observer.observe(el);
    apply();

    return () => {
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [compact]);

  return ref;
}

// Keeps the native overlay window the size the *state* says it should be.
//
// Two findings from the rebuild's research shape every line of this file.
//
// 1. `invoke("set_window_frame")` resolves when the resize is **queued**, not
//    when it is applied: the pinned `tao` dispatches `set_content_size_async`
//    onto the main GCD queue and returns. So nothing here awaits it to decide
//    when to do anything — it is a request, and the only thing its rejection
//    tells us is that the window did *not* change size.
// 2. Do not measure and follow. The overlay has a small number of discrete
//    states with knowable heights (`overlay-size.ts`), so the size is computed
//    before the frame is painted rather than observed a frame after it. The
//    old `useOverlayHeight` sent one invoke per animation frame in which the
//    measurement moved; this sends one per state change.
//
// The asymmetry is the design: **grow immediately, shrink after a delay.**
// Growing late clips content, which is the "the run felt like nothing
// happened" bug the old hook existed to fix. Shrinking early is worse in the
// other direction: the window is transparent and sits over the engineer's
// KiCad canvas, so a premature shrink flashes a strip of KiCad through the
// card, and a state that collapses and reopens between keystrokes flickers.
// 250 ms is Albert's number for the same trade.

import { useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";

import { dockOriginFor } from "@/lib/overlay-dock";
import {
  isShrink,
  sizeFor,
  sizeKey,
  type OverlaySize,
  type OverlayState,
} from "@/lib/overlay-size";

/** How long a shrink waits before it is sent. Cancelled by any newer size,
 *  which is what makes a grow arriving mid-wait cancel the pending shrink.
 *
 *  Reduced motion, decided rather than defaulted: this delay is **not**
 *  motion. Nothing moves during it — the window simply stays big for a
 *  quarter of a second longer, which is exactly as true under
 *  `prefers-reduced-motion: reduce` as without it, and honouring the media
 *  query here would mean showing reduced-motion users the transparent-strip
 *  flicker the delay exists to prevent. The media query belongs to whatever
 *  CSS transition the card carries, not to this schedule. */
export const SHRINK_DELAY_MS = 250;

/** The size the window is created at (`tauri.conf.json` → 600×58), which is
 *  what `applied` starts out believing, so launching straight into the full
 *  bar costs no resize at all. */
const LAUNCH_SIZE: OverlaySize = sizeFor("bar");

/**
 * Drive the native window from the overlay's state. Returns nothing: there is
 * no ref to attach and nothing to measure — that is the point.
 *
 * `contentHeight` is honoured only for the two content-driven panels (step and
 * deliver); see `sizeFor`.
 */
export function useOverlaySize(state: OverlayState, contentHeight?: number): void {
  const target = sizeFor(state, contentHeight);
  const { width, height } = target;

  // What the *window* is, as far as we know — never what we merely asked for.
  const applied = useRef<OverlaySize>(LAUNCH_SIZE);
  const appliedKey = useRef<string>(sizeKey(LAUNCH_SIZE));
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    const next: OverlaySize = { width, height };
    const key = sizeKey(next);

    // Deduped against the window's actual size, so an unchanged state costs
    // no invoke — and a state that returns to the current size cancels a
    // shrink that was scheduled on the way out (the cleanup below has already
    // run by the time we get here).
    if (key === appliedKey.current) return;

    const send = () => {
      timer.current = undefined;
      const previous = applied.current;
      applied.current = next;
      appliedKey.current = key;
      // Where the window belongs at this size, asked of the dock — which stays
      // the authority on *where*; nothing here repeats its rule. Answered
      // synchronously from the dock's cached monitor geometry, so the resize
      // still goes out on this frame, and `null` when it cannot be answered,
      // in which case the window resizes without moving (the old behaviour).
      //
      // This is the whole fix for the collapse jump: `set_size` is anchored at
      // the window's top-left, so a 600→132 width change slides the pill 234 px
      // left, and the dock's own re-centre lands up to DOCK_MOVE_INTERVAL_MS
      // later. Same size, same origin, one native operation — no intermediate
      // frame exists to see (audit §2.4, §3.4).
      const origin = dockOriginFor(next);
      // Fire and forget, deliberately: the promise resolves when the resize is
      // queued on the main thread, so sequencing anything off it would be
      // sequencing off a lie. Nothing below awaits it.
      invoke("set_window_frame", {
        height: next.height,
        width: next.width,
        x: origin?.x ?? null,
        y: origin?.y ?? null,
      }).catch(
        (error) => {
          // The resize did not happen, so the window is still the size it was.
          // Forget the key: remembering a failed ask makes every later state
          // with the same size dedupe against a size the window never took,
          // and one dropped invoke would leave the overlay clipped for the
          // rest of the session, silently. Only roll back if nothing newer has
          // been applied in the meantime — `invoke` is async, and clearing
          // unconditionally would re-send a size that has since been replaced.
          if (appliedKey.current === key) {
            applied.current = previous;
            appliedKey.current = sizeKey(previous);
          }
          console.warn("[kaleo overlay] set_window_frame failed:", error);
        }
      );
    };

    if (isShrink(applied.current, next)) {
      timer.current = setTimeout(send, SHRINK_DELAY_MS);
      return () => {
        if (timer.current !== undefined) {
          clearTimeout(timer.current);
          timer.current = undefined;
        }
      };
    }

    // A grow supersedes any pending shrink (the cleanup cancelled it) and goes
    // out on this frame, because content that is already laid out below the
    // window's bottom edge is not hidden, it is clipped.
    send();
    return undefined;
  }, [width, height]);
}

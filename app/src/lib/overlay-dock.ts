/**
 * Where the overlay sits on the screen: the top edge or the bottom edge.
 *
 * The bar is always on top, so wherever it is, it is over something. Most of
 * the time that is fine: the engineer is looking at the bar. While a KiCad
 * step is *running* the bar would cover the canvas being written; for that
 * moment only it moves to the bottom edge. When the engine is *waiting* for
 * approval (Send / Approve), the bar stays at the top — that is when the
 * controls must stay readable, not buried and clipped at the Dock. The
 * reasons are listed here, as data, the way overlay-mode.ts lists its
 * reasons, so the page cannot grow a hidden fourth case that parks the bar
 * over the board.
 *
 * "Not too much": the bar has two homes and never wanders. A drag by the
 * engineer wins over both until the next run starts (`releasesPin`); the
 * hook that applies these decisions owns that detection. Bottom placement
 * is always clamped into the work area so a tall panel never loses its foot
 * off the screen.
 */

import { STEP_DESCRIPTORS } from "./silkscreen/steps";
import type { StepName } from "./silkscreen/types";

export type OverlayDock = "top" | "bottom";

export interface OverlayDockInput {
  /** The step flow's status (`useStepRun`). */
  stepStatus: "idle" | "running" | "waiting" | "done" | "error";
  /** The step in flight while `stepStatus` is `running`. */
  runningStep: StepName | null;
  /** The most recent step response's step, if any. */
  lastStep: StepName | null;
  /** Whether the engine reported that response was shown in KiCad. */
  lastShownInKicad: boolean;
  /** How many step responses have arrived this session. */
  stepCount: number;
  /** The live (non-step) run is in flight. */
  runInFlight: boolean;
}

/**
 * Why the overlay sits where it does. Only `kicad-running` docks it to the
 * bottom; every other reason — including waiting for Send/Approve — keeps
 * it at the top so the controls stay on screen.
 */
export type DockReason =
  /** A KiCad-shown step is being computed; the last one is still on screen. */
  | "kicad-running"
  /** A step was just shown in KiCad and the engine is waiting for approval. */
  | "kicad-waiting"
  /** A step finished in KiCad but the engine could not show it there. */
  | "step-not-shown"
  /** An overlay-facing step (review, sourcing, order, case) is running or on screen. */
  | "step-overlay"
  /** The step flow failed; the failure is read in the bar. */
  | "step-error"
  /** The one-shot live run is in flight; its progress is read in the bar. */
  | "live-run"
  /** Nothing is happening, or a live result is on screen. */
  | "idle";

function inKicad(step: StepName | null): boolean {
  return step !== null && STEP_DESCRIPTORS[step].where === "kicad";
}

export function dockReason(input: OverlayDockInput): DockReason {
  switch (input.stepStatus) {
    case "running":
      return inKicad(input.runningStep) ? "kicad-running" : "step-overlay";
    case "waiting":
      if (!inKicad(input.lastStep)) return "step-overlay";
      // The engine says where it put the result. Without the live bridge
      // (`kicad_live` off, or the bridge failed) nothing is under the bar
      // worth moving for, and the summary in the bar is what there is to read.
      return input.lastShownInKicad ? "kicad-waiting" : "step-not-shown";
    case "done":
      return "step-overlay";
    case "error":
      return "step-error";
    case "idle":
      return input.runInFlight ? "live-run" : "idle";
  }
}

export function overlayDock(input: OverlayDockInput): OverlayDock {
  // Only clear the canvas while KiCad is being written. Waiting for approval
  // keeps the bar at the top so Send / Approve are not dumped onto the Dock.
  return dockReason(input) === "kicad-running" ? "bottom" : "top";
}

/** True at the moment a *new* run begins, in either state machine. */
export function isRunStart(input: OverlayDockInput): boolean {
  return (input.stepStatus === "running" && input.stepCount === 0) || input.runInFlight;
}

/**
 * A user drag pins the bar where it was dropped. The pin lasts until the
 * next run starts: approving the next step of the same run is not a new run,
 * so a bar dragged out of the way during review stays out of the way.
 */
export function releasesPin(previous: OverlayDockInput, next: OverlayDockInput): boolean {
  return !isRunStart(previous) && isRunStart(next);
}

// ------------------------------------------------------------- geometry

/**
 * The top dock's offset from the top of the monitor, in *physical* pixels.
 *
 * This is one half of `TOP_OFFSET` in `src-tauri/src/window.rs`, which is
 * where the window is placed at launch; the first "top" dock decision
 * re-applies the same position, so the two numbers must be equal or the bar
 * hops the moment anything triggers a dock apply.
 * `overlay-dock.test.ts` reads window.rs and asserts it, because the last
 * drift went unnoticed for exactly as long as nothing compared them.
 *
 * It is *not* `OVERLAY_COLLAPSED_HEIGHT` (useOverlayHeight.ts) and must not be
 * kept in step with it: that is the window's collapsed *height*, in *logical*
 * pixels. Both were 54 by coincidence until the sweep that raised the height
 * to 58 raised this too and bought a 4 px vertical hop.
 */
export const TOP_OFFSET_PX = 54;
/** The bottom dock's gap above the work area's bottom edge, in logical pixels. */
export const BOTTOM_MARGIN = 16;

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface DockGeometry {
  /** The whole monitor, physical pixels. */
  monitor: Rect;
  /** The monitor minus menu bar, dock and taskbar, physical pixels. */
  workArea: Rect;
  /** The window's outer size, physical pixels. */
  window: { width: number; height: number };
  /** Physical pixels per logical pixel. */
  scaleFactor: number;
}

/**
 * The window's top-left corner for a dock, in physical pixels.
 *
 * Top is exactly where window.rs put the bar at launch (centred, 54 physical
 * pixels down), so the first "top" decision never visibly moves anything.
 * Bottom keeps the same centre line and sits `BOTTOM_MARGIN` logical pixels
 * above the work area's bottom edge, so it clears a macOS Dock or a Windows
 * taskbar rather than hiding behind one. Both docks clamp into the work
 * area so a tall StepPanel never loses its foot (or its headline) off-screen.
 */
export function dockPosition(dock: OverlayDock, g: DockGeometry): { x: number; y: number } {
  const scale = Number.isFinite(g.scaleFactor) && g.scaleFactor > 0 ? g.scaleFactor : 1;
  const margin = BOTTOM_MARGIN * scale;
  const x = Math.round(g.workArea.x + (g.workArea.width - g.window.width) / 2);
  const minY = g.workArea.y;
  const maxY = g.workArea.y + g.workArea.height - g.window.height - margin;
  const clampY = (y: number) => {
    if (maxY < minY) return minY;
    return Math.min(Math.max(y, minY), maxY);
  };
  if (dock === "top") {
    return { x, y: clampY(g.monitor.y + TOP_OFFSET_PX) };
  }
  const bottom = g.workArea.y + g.workArea.height;
  return { x, y: clampY(Math.round(bottom - g.window.height - margin)) };
}

/** True when a reported window position is (within rounding) the one we asked for. */
export function isSamePosition(
  a: { x: number; y: number },
  b: { x: number; y: number },
  tolerance = 2
): boolean {
  return Math.abs(a.x - b.x) <= tolerance && Math.abs(a.y - b.y) <= tolerance;
}

// ------------------------------------------------- one operation, not two
//
// A native window's size is anchored at its *top-left*, so shrinking the bar
// 600 → 132 moves its visual centre 234 px to the left. The dock then puts it
// back — in a second native call, up to DOCK_MOVE_INTERVAL_MS later. The
// pill's start and end positions are identical; the whole visible jump is the
// gap between two operations that should have been one (audit §2.4, §3.4).
//
// It cannot be closed by ordering the two `invoke`s from here: in the pinned
// `tao` (Cargo.lock: 0.34.2) `set_inner_size` goes through
// `util::set_content_size_async` and `set_outer_position` through
// `util::set_frame_top_left_point_async`, each dispatching its own
// fire-and-forget block onto the main GCD queue. Two calls are two queued
// blocks, and the split frame between them is structural.
//
// So the size and the origin travel together, in one `set_window_frame`
// command. The dock stays the authority on *where*: `useOverlaySize` knows the
// new size and nothing else, and asks here for the origin that size should
// have. This registry is how the two hooks meet — the page mounts them as
// siblings (`pages/kaleo/index.tsx`) and neither can be passed to the other.

export interface DockOrigin {
  /** Physical pixels, the frame `dockPosition` and `setPosition` both work in. */
  x: number;
  y: number;
}

/**
 * Answers "where should a window of this *logical* size sit?" synchronously,
 * or `null` when it cannot say (no geometry read yet, no monitor).
 *
 * Synchronous on purpose: `useOverlaySize` sends its resize on the frame the
 * state changes, and awaiting a monitor query first would put the resize a
 * microtask — in practice a frame — behind the content again, which is the
 * bug the whole size path exists to avoid.
 */
export type DockOriginResolver = (size: {
  width: number;
  height: number;
}) => DockOrigin | null;

let originResolver: DockOriginResolver | null = null;

/**
 * Registered by `useOverlayDock` while it is mounted; `null` clears it.
 *
 * Pass the resolver being retired as `owner` when clearing: an instance that
 * unmounts *after* its replacement registered (StrictMode's double-mount, a
 * test rendering two hooks) would otherwise clear a registration that is not
 * its own and leave every later resize unanchored.
 */
export function setDockOriginResolver(
  resolver: DockOriginResolver | null,
  owner?: DockOriginResolver
): void {
  if (resolver === null && owner !== undefined && originResolver !== owner) return;
  originResolver = resolver;
}

/**
 * The origin a window of `size` (logical pixels) should be given, or `null`
 * when nobody can say — in which case the caller must fall back to a size-only
 * resize, i.e. today's behaviour, rather than guess a position.
 */
export function dockOriginFor(size: { width: number; height: number }): DockOrigin | null {
  if (!originResolver) return null;
  try {
    return originResolver(size);
  } catch (error) {
    console.warn("[kaleo overlay] dock origin failed:", error);
    return null;
  }
}

/**
 * The origin that keeps a window's *visual centre* where it is while its width
 * changes. Used only for a bar the engineer has dragged: the dock does not get
 * to move a pinned bar, but a pinned bar must still not lurch sideways when it
 * collapses. Physical pixels in, physical pixels out.
 */
export function centredOrigin(
  current: { x: number; y: number },
  currentWidth: number,
  nextWidth: number
): DockOrigin {
  return { x: Math.round(current.x + (currentWidth - nextWidth) / 2), y: current.y };
}

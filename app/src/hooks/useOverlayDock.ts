// Moves the overlay between its two homes — the top edge, where window.rs
// puts it at launch, and the bottom edge, for the moments in step mode when
// the engineer should be looking at the KiCad canvas the bar would otherwise
// cover. The decision is `overlayDock` in lib/overlay-dock.ts; this hook only
// applies it, through the window API rather than a Rust command, so no
// rebuild of the binary is needed to change where the bar goes.
//
// Three rules keep the bar from feeling twitchy:
//   - it moves only when the decision changes (or the window changed size
//     underneath a decision), never more than once per DOCK_MOVE_INTERVAL_MS;
//   - a drag by the engineer pins it where it was dropped until the next run
//     starts — detected as a `moved` event we did not ask for;
//   - a failed move is a console.warn, like useOverlayHeight: a bar in the
//     wrong place beats a crashed one.

import { useEffect, useRef } from "react";
import {
  PhysicalPosition,
  currentMonitor,
  getCurrentWindow,
  primaryMonitor,
} from "@tauri-apps/api/window";

import {
  centredOrigin,
  dockPosition,
  isSamePosition,
  overlayDock,
  releasesPin,
  setDockOriginResolver,
  type DockGeometry,
  type DockOrigin,
  type OverlayDock,
  type OverlayDockInput,
} from "@/lib/overlay-dock";
import type { StepRun } from "./useStepRun";

/** Never more than one programmatic move in this many milliseconds. */
export const DOCK_MOVE_INTERVAL_MS = 400;

/**
 * Statuses of the live run in which nothing is in flight. The complement, so a
 * status added later reads as "in flight" rather than as idle — the same
 * convention the page uses for its submit control.
 */
const SETTLED: readonly string[] = ["idle", "done", "error", "cancelled"];

/** The pure decision's input, from the two state machines the page holds. */
export function dockInputFrom(steps: StepRun, runStatus: string): OverlayDockInput {
  const last = steps.history[steps.history.length - 1];
  return {
    stepStatus: steps.status,
    runningStep: steps.running,
    lastStep: last?.step ?? null,
    lastShownInKicad: last?.shown_in_kicad ?? false,
    stepCount: steps.history.length,
    runInFlight: !SETTLED.includes(runStatus),
  };
}

type Point = { x: number; y: number };

/**
 * Keeps the overlay window at the edge `overlayDock` chooses for the current
 * step/run state. Returns the current decision, for anything that wants to
 * render differently at the bottom (nothing does yet).
 */
export function useOverlayDock(steps: StepRun, runStatus: string): OverlayDock {
  const input = dockInputFrom(steps, runStatus);
  const dock = overlayDock(input);

  // Everything the event handlers need lives in refs: they are subscribed
  // once, and must see the current state, not the state at subscription.
  const dockRef = useRef<OverlayDock>("top");
  const pinnedRef = useRef(false);
  const previousInputRef = useRef<OverlayDockInput | null>(null);
  /** The position of the move we asked for and have not yet seen echoed. */
  const pendingRef = useRef<Point | null>(null);
  /** Where we last put the window, so a duplicate `moved` is not a drag. */
  const appliedRef = useRef<Point | null>(null);
  /** Until this time a `moved` is treated as fallout from our own move or resize. */
  const settleUntilRef = useRef(0);
  /** When an unexplained `moved` last arrived; a resize right after explains it. */
  const unexplainedMoveAtRef = useRef(0);
  const lastMoveAtRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const applyingRef = useRef(false);
  /** The last monitor read, kept so the origin for a *coming* size can be
   *  answered without awaiting anything (see `dockOriginFor`). */
  const geometryRef = useRef<Omit<DockGeometry, "window"> | null>(null);
  /** The window's current outer position and size, physical pixels. Updated
   *  from the `moved`/`resized` payloads, which arrive with the numbers, so
   *  the cache does not go stale between monitor reads. */
  const positionRef = useRef<Point | null>(null);
  const sizeRef = useRef<{ width: number; height: number } | null>(null);

  const readGeometry = async () => {
    try {
      const win = getCurrentWindow();
      const monitor = (await currentMonitor()) ?? (await primaryMonitor());
      if (!monitor) return;
      const [size, scaleFactor, current] = await Promise.all([
        win.outerSize(),
        win.scaleFactor(),
        win.outerPosition(),
      ]);
      geometryRef.current = {
        monitor: {
          x: monitor.position.x,
          y: monitor.position.y,
          width: monitor.size.width,
          height: monitor.size.height,
        },
        workArea: {
          x: monitor.workArea.position.x,
          y: monitor.workArea.position.y,
          width: monitor.workArea.size.width,
          height: monitor.workArea.size.height,
        },
        scaleFactor,
      };
      sizeRef.current = { width: size.width, height: size.height };
      positionRef.current = { x: current.x, y: current.y };
    } catch (error) {
      console.warn("[kaleo overlay] dock geometry:", error);
    }
  };

  /**
   * Where a window of `size` (logical pixels) belongs — the answer
   * `useOverlaySize` sends down with the size itself, so the resize and the
   * re-anchor are one native operation instead of two (see `overlay-dock.ts`).
   *
   * Returns `null` rather than a guess when the geometry has not been read
   * yet; the caller then resizes without moving, which is what it did before.
   */
  const originFor = (size: { width: number; height: number }): DockOrigin | null => {
    const geometry = geometryRef.current;
    if (!geometry) return null;
    const scale =
      Number.isFinite(geometry.scaleFactor) && geometry.scaleFactor > 0
        ? geometry.scaleFactor
        : 1;
    const physical = {
      width: Math.round(size.width * scale),
      height: Math.round(size.height * scale),
    };
    const current = positionRef.current;
    let target: DockOrigin;
    if (pinnedRef.current) {
      // A dragged bar keeps its place: the dock has no say over where it is.
      // It still must not lurch sideways as it narrows, so it keeps its centre.
      if (!current) return null;
      target = centredOrigin(current, sizeRef.current?.width ?? physical.width, physical.width);
      if (isSamePosition(current, target)) return null;
    } else {
      target = dockPosition(dockRef.current, { ...geometry, window: physical });
    }
    // This move is ours. Record it the way `apply` does, or the `moved` event
    // it produces reads as a drag and pins the bar (`onMoved` below).
    appliedRef.current = target;
    pendingRef.current = target;
    positionRef.current = target;
    sizeRef.current = physical;
    settleUntilRef.current = Date.now() + DOCK_MOVE_INTERVAL_MS;
    lastMoveAtRef.current = Date.now();
    return target;
  };

  const originForRef = useRef(originFor);
  originForRef.current = originFor;

  const apply = async () => {
    if (pinnedRef.current || applyingRef.current) return;
    applyingRef.current = true;
    lastMoveAtRef.current = Date.now();
    try {
      const win = getCurrentWindow();
      const monitor = (await currentMonitor()) ?? (await primaryMonitor());
      if (!monitor) {
        console.warn("[kaleo overlay] dock: no monitor reported; leaving the window where it is");
        return;
      }
      const [size, scaleFactor, current] = await Promise.all([
        win.outerSize(),
        win.scaleFactor(),
        win.outerPosition(),
      ]);
      geometryRef.current = {
        monitor: {
          x: monitor.position.x,
          y: monitor.position.y,
          width: monitor.size.width,
          height: monitor.size.height,
        },
        workArea: {
          x: monitor.workArea.position.x,
          y: monitor.workArea.position.y,
          width: monitor.workArea.size.width,
          height: monitor.workArea.size.height,
        },
        scaleFactor,
      };
      sizeRef.current = { width: size.width, height: size.height };
      positionRef.current = { x: current.x, y: current.y };
      const target = dockPosition(dockRef.current, {
        monitor: {
          x: monitor.position.x,
          y: monitor.position.y,
          width: monitor.size.width,
          height: monitor.size.height,
        },
        workArea: {
          x: monitor.workArea.position.x,
          y: monitor.workArea.position.y,
          width: monitor.workArea.size.width,
          height: monitor.workArea.size.height,
        },
        window: { width: size.width, height: size.height },
        scaleFactor,
      });
      appliedRef.current = target;
      if (isSamePosition(current, target)) return;
      positionRef.current = target;
      pendingRef.current = target;
      settleUntilRef.current = Date.now() + DOCK_MOVE_INTERVAL_MS;
      await win.setPosition(new PhysicalPosition(target.x, target.y));
    } catch (error) {
      console.warn("[kaleo overlay] dock move failed:", error);
    } finally {
      applyingRef.current = false;
    }
  };

  const schedule = () => {
    if (pinnedRef.current) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    const wait = Math.max(0, lastMoveAtRef.current + DOCK_MOVE_INTERVAL_MS - Date.now());
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      void apply();
    }, wait);
  };

  // The handlers are recreated each render but only the first is subscribed;
  // they read refs, so that is fine. This ref lets the subscription call the
  // latest `schedule` without re-subscribing.
  const scheduleRef = useRef(schedule);
  scheduleRef.current = schedule;

  useEffect(() => {
    let cancelled = false;
    const unlisteners: (() => void)[] = [];
    const win = getCurrentWindow();

    const onMoved = ({ payload }: { payload: Point }) => {
      positionRef.current = { x: payload.x, y: payload.y };
      const pending = pendingRef.current;
      if (pending && isSamePosition(payload, pending)) {
        pendingRef.current = null;
        return;
      }
      if (Date.now() < settleUntilRef.current) return;
      if (appliedRef.current && isSamePosition(payload, appliedRef.current)) return;
      // Nobody asked for this move: the engineer dragged the bar. Hold it
      // there. (A resize can report as a move too; `onResized` undoes this
      // when it arrives on the heels of one.)
      unexplainedMoveAtRef.current = Date.now();
      pinnedRef.current = true;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const onResized = ({ payload }: { payload: { width: number; height: number } }) => {
      if (payload) sizeRef.current = { width: payload.width, height: payload.height };
      if (Date.now() - unexplainedMoveAtRef.current < DOCK_MOVE_INTERVAL_MS) {
        // That "drag" was the window growing; not a pin.
        pinnedRef.current = false;
        unexplainedMoveAtRef.current = 0;
      }
      settleUntilRef.current = Date.now() + DOCK_MOVE_INTERVAL_MS;
      // The bar changed height under its dock; at the bottom that means its
      // foot is now off the screen (or floating above it).
      scheduleRef.current();
    };

    // `useOverlaySize` asks here for the origin to send down with a size, so
    // the two become one native call. Registered for as long as this hook is
    // mounted; the geometry read that makes the answer possible starts now.
    const resolver = (size: { width: number; height: number }) =>
      originForRef.current(size);
    setDockOriginResolver(resolver);
    void readGeometry();

    Promise.all([win.onMoved(onMoved), win.onResized(onResized)])
      .then((fns) => {
        if (cancelled) fns.forEach((fn) => fn());
        else unlisteners.push(...fns);
      })
      .catch((error) => console.warn("[kaleo overlay] dock listeners:", error));

    return () => {
      cancelled = true;
      // Only clear our own registration: a second instance mounting before
      // this one unmounts (StrictMode, a test rendering two hooks) has already
      // replaced it, and clearing that would leave the size path unanchored.
      setDockOriginResolver(null, resolver);
      unlisteners.forEach((fn) => fn());
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const previous = previousInputRef.current;
    previousInputRef.current = input;
    // A new run releases a drag pin: the bar goes back to its dock.
    const released = pinnedRef.current && previous !== null && releasesPin(previous, input);
    if (released) pinnedRef.current = false;
    if (dock === dockRef.current && !released) return;
    dockRef.current = dock;
    schedule();
    // The input object is rebuilt every render; its fields are what matter.
  }, [
    dock,
    input.stepStatus,
    input.runningStep,
    input.lastStep,
    input.lastShownInKicad,
    input.stepCount,
    input.runInFlight,
  ]);

  return dock;
}

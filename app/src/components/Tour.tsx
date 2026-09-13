import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui";
import { motionAttr, useReducedMotion } from "@/hooks/useReducedMotion";
import { setSetting } from "@/lib/settings/store";
import {
  TOUR_STOPS,
  acceptTour,
  currentStop,
  nextStop,
  readTour,
  resolveTarget,
  skipTour,
  subscribeTour,
  type TargetRect,
  type TourState,
} from "@/lib/tour";

const CARD_WIDTH = 256;
const PAD = 8;
const GAP = 12;

/** Where the card goes: under the target when it fits, else above, else centred. */
function place(rect: TargetRect | null, viewport: { w: number; h: number }): { top: number; left: number } {
  if (!rect) {
    return { top: Math.max(GAP, viewport.h / 2 - 80), left: Math.max(GAP, (viewport.w - CARD_WIDTH) / 2) };
  }
  const left = Math.min(Math.max(GAP, rect.left), Math.max(GAP, viewport.w - CARD_WIDTH - GAP));
  const below = rect.top + rect.height + PAD + GAP;
  if (below + 160 <= viewport.h) return { top: below, left };
  return { top: Math.max(GAP, rect.top - PAD - GAP - 160), left };
}

/**
 * The dashboard's two Tips cards (`TOUR_STOPS`), plus the consent card.
 *
 * Mounted in `DashboardLayout` only. Each stop's target is resolved from a
 * declarative selector at render time — after navigation, on resize, on
 * scroll — and a target that is missing or has no box degrades to a
 * caption-only card. Nothing here reaches into the strip; that window says
 * its own one sentence through `tour.caption`.
 */
export const Tour = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const still = useReducedMotion();
  const [state, setState] = useState<TourState>(readTour);
  const [rect, setRect] = useState<TargetRect | null>(null);
  const [viewport, setViewport] = useState({ w: window.innerWidth, h: window.innerHeight });

  useEffect(() => subscribeTour(setState), []);

  const stop = currentStop(state);

  // A stop that lives elsewhere: go there.
  useEffect(() => {
    if (!stop?.route || location.pathname === stop.route) return;
    navigate(stop.route);
  }, [stop, location.pathname, navigate]);

  const measure = useCallback(() => {
    setViewport({ w: window.innerWidth, h: window.innerHeight });
    setRect(stop ? resolveTarget(stop.target, (sel) => document.querySelector(sel)) : null);
  }, [stop]);

  useLayoutEffect(() => {
    if (!stop) return;
    measure();
    // The page the stop points at may still be loading its list.
    const retry = window.setTimeout(measure, 300);
    const later = window.setTimeout(measure, 1200);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.clearTimeout(retry);
      window.clearTimeout(later);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [stop, measure, location.pathname]);

  const finish = useCallback((next: TourState) => {
    if (next.kind === "done") void setSetting("tour.completed", true);
  }, []);

  if (state.kind === "offered") {
    return (
      <div
        className="fixed bottom-6 right-6 z-[60] w-[256px] rounded-xl border bg-popover p-4 text-popover-foreground shadow-lg kv-settle"
        role="dialog"
        aria-label="Tour"
        data-testid="tour-offer"
        data-motion={motionAttr(still)}
      >
        <p className="text-sm font-medium">Take the 60-second tour?</p>
        <p className="mt-1 text-xs text-muted-foreground">Two cards in here, one sentence in the strip.</p>
        <div className="mt-3 flex items-center gap-2">
          <Button size="sm" onClick={() => acceptTour(state.reason)} data-testid="tour-accept">
            Start
          </Button>
          <Button size="sm" variant="ghost" onClick={() => finish(skipTour())} data-testid="tour-decline">
            Not now
          </Button>
        </div>
      </div>
    );
  }

  if (state.kind !== "active" || !stop) return null;

  const last = state.index >= TOUR_STOPS.length - 1;
  const pos = place(rect, viewport);

  return (
    <div className="fixed inset-0 z-[60]" data-testid="tour" data-stop={stop.id} data-motion={motionAttr(still)}>
      {rect ? (
        <div
          aria-hidden="true"
          data-testid="tour-cutout"
          className="absolute rounded-lg"
          style={{
            top: rect.top - PAD,
            left: rect.left - PAD,
            width: rect.width + PAD * 2,
            height: rect.height + PAD * 2,
            boxShadow: "0 0 0 9999px rgba(0,0,0,0.45)",
            pointerEvents: "none",
          }}
        />
      ) : (
        <div aria-hidden="true" className="absolute inset-0 bg-black/45" />
      )}
      <div
        role="dialog"
        aria-labelledby="tour-title"
        className="absolute w-[256px] rounded-xl border bg-popover p-4 text-popover-foreground shadow-lg kv-settle"
        style={{ top: pos.top, left: pos.left }}
        data-testid="tour-card"
        data-caption-only={rect ? "false" : "true"}
      >
        <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
          Tip {state.index + 1} of {TOUR_STOPS.length}
        </p>
        <p id="tour-title" className="mt-1 text-sm font-medium">
          {stop.title}
        </p>
        <p className="mt-1 text-xs leading-snug text-muted-foreground">{stop.body}</p>
        <div className="mt-3 flex items-center justify-between gap-2">
          <Button size="sm" variant="ghost" onClick={() => finish(skipTour())} data-testid="tour-skip">
            Skip tour
          </Button>
          <Button size="sm" onClick={() => finish(nextStop())} data-testid="tour-next">
            {last ? "Done" : "Next"}
          </Button>
        </div>
      </div>
    </div>
  );
};

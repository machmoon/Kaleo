import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ErrorBoundary } from "react-error-boundary";
import { listen } from "@tauri-apps/api/event";
import {
  ChevronLeftIcon,
  LayoutDashboardIcon,
} from "lucide-react";
import { Button, Card, DragButton } from "@/components";
import { ErrorLayout } from "@/layouts";
import { useApp } from "@/hooks";
import { useOverlayDock } from "@/hooks/useOverlayDock";
import { useOverlaySize } from "@/hooks/useOverlaySize";
import { useOverlaySkin } from "@/hooks/useOverlaySkin";
import { ask } from "@/lib/silkscreen/chat";
import { isMeasured, sizeFor, type OverlayState } from "@/lib/overlay-size";
import { useRunVoice } from "@/hooks/useRunVoice";
import { useStepRun } from "@/hooks/useStepRun";
import { useIdeaInbox } from "@/hooks/useIdeaInbox";
import { useReviewedInKicad } from "@/hooks/useReviewedInKicad";
import { useWakeWord } from "@/hooks/useWakeWord";
import { useTrayState } from "@/hooks/useTrayState";
import {
  fulfillAdaDecision,
  routeSpokenUtterance,
  wouldStartBoard,
} from "@/lib/ada-path";
import { collectDeskCandidates, enrichWithDeskContext } from "@/lib/desk-context";
import { resolveDesk } from "@/lib/silkscreen/desk";
import {
  announce,
  armedLine,
  refusalLine,
  setSpeechDuck,
} from "@/lib/speech";
import { useVoiceReplies } from "@/hooks/useVoiceReplies";
import {
  STEP_DESCRIPTORS,
  armCommand,
  interpretCommand,
  reviewFailure,
  reviewSkipped,
} from "@/lib/silkscreen/steps";
import type { ArmedCommand } from "@/lib/silkscreen/steps";
import type { AmendResponse, StepName } from "@/lib/silkscreen/types";
import {
  deliverable,
  readSummaryMode,
  summaryFields,
  writeSummaryMode,
} from "@/lib/silkscreen/deliver";
import type { SummaryMode } from "@/lib/silkscreen/types";
import { focusPromptIn } from "@/lib/focus-prompt";
import { safeLocalStorage } from "@/lib/storage/helper";
import { TOUR_CAPTION_KEY } from "@/lib/tour";
import { barContent, isOverlayExpanded } from "@/lib/overlay-mode";
import { useIsSpeaking } from "@/hooks/useIsSpeaking";
import { micIsOpen, micState } from "./components/VoiceControl";
import { useSilkscreenRun } from "@/contexts";
import {
  CompactBar,
  TerminalSkin,
  SpotlightSkin,
  OrbSkin,
  SkinStrip,
  PromptBar,
  RunOptions,
  RunFailure,
  RunProgress,
  StepPanel,
} from "./components";

const STEP_MODE_KEY = "kaleo.stepMode";

/** How long an armed stage waits for a human before it lapses. */
const ARM_DECAY_MS = 15_000;

/**
 * A sentence the bar accepted and has not spent.
 *
 * `parked` — it arrived while something was running, and is being held.
 * `offered` — nothing is running, and it is standing as a question.
 *
 * Two states rather than one because the sentence on screen has to say which:
 * "holding this" and "shall I start this" are different promises, and a strip
 * that showed the second while a solve was still running would be lying about
 * what pressing the button does.
 */
export type HeldRequest = { state: "parked" | "offered"; text: string };

/**
 * Which door a held sentence can be sent through, given what the run is.
 *
 * There are two, and the service enumerated them: a note attached to a step
 * that genuinely reads free text, or a new run from the intent plus the note.
 * `case` is the only step in the whole step API with a free-text field
 * (`enclosure_style`), so the first door exists at exactly one gate and the
 * service answers 400 for any other step on purpose. This function must never
 * offer a door the service would refuse.
 *
 * The first phase is the case that matters most: `POST /steps` is synchronous
 * and there is no session id until it returns, so nothing can be attached to
 * it and nothing can stop it. Then neither door exists and the sentence is
 * held on this strip alone — which is why the local hold was built first and
 * is not replaced by any of this.
 */
export function heldDoors(input: {
  /** A session id exists, i.e. the first phase has returned. */
  session: string | null;
  /** Steps this run has already finished. */
  done: readonly StepName[];
  /** The session is closed to further steps. */
  cancelled: boolean;
}): { note: boolean; caseStep: boolean; restart: boolean } {
  if (!input.session || input.cancelled) {
    // A cancelled session refuses an amend with a 409, and a session that does
    // not exist yet cannot be addressed at all.
    return { note: false, caseStep: false, restart: false };
  }
  return {
    note: true,
    // The service refuses a note for a step that has already run — its note
    // would never be read — so this mirrors that guard rather than finding out
    // by 400.
    caseStep: !input.done.includes("case"),
    restart: true,
  };
}

// On by default: the whole point of the desktop app is that the engineer
// reviews in KiCad. Turning it off is the remembered exception.
function readStepMode(): boolean {
  try {
    return window.localStorage.getItem(STEP_MODE_KEY) !== "0";
  } catch {
    return true;
  }
}

/**
 * Statuses in which no run is in flight. Written as the complement so a status
 * the state machine adds later locks the submit control rather than freeing it.
 */
const SETTLED: string[] = ["idle", "done", "error", "cancelled"];

/**
 * How long the outgoing shape stays mounted during a pill<->bar swap.
 *
 * In step with `--kv-dur-enter` in `src/motion.css`, which is where the number
 * is decided; this is the JS half of the mechanism that file's `.kv-shape-out`
 * documents ("only usable where the implementer keeps the outgoing node
 * mounted for the duration"). A ternary drops the old subtree in the same
 * commit, so without this there is no exit phase to animate at all.
 *
 * It is the *enter* duration and not `--kv-dur-exit`, deliberately: motion.css
 * §2 pairs `.kv-shape-out` and `.kv-shape-in` as mirrored halves of one
 * cross-dissolve between two opaque cards, and halves on different clocks do
 * not sum to an opaque surface — the composite dips and the desktop flashes
 * through. The "exits are faster than entrances" asymmetry belongs to an exit
 * that leaves a gap behind it, which this is not.
 */
export const SHAPE_EXIT_MS = 900;

/**
 * How long the swap waits for the native window to make room before giving up
 * and rendering the wide shape anyway.
 *
 * Not a duration anything animates for — nothing is on screen that is not on
 * screen without it. It is a deadline on a native round trip that normally
 * takes a frame or two, and the failure it bounds is real:
 * `invoke("set_window_frame")` can reject (`useOverlaySize` warns and rolls
 * its key back), in which case no `resize` event is ever coming and a swap
 * gated on one would leave the engineer holding a pill that will not open.
 * A bar clipped to the pill's width is bad; a bar that never arrives is worse,
 * so the deadline resolves in that direction and says so on the console.
 */
export const SHAPE_ROOM_TIMEOUT_MS = 250;

/**
 * Everything the overlay's size depends on, as flat facts.
 *
 * One input object rather than the page's dozen pieces of state, so the
 * derivation below can be exercised without a React tree — the convention
 * `overlay-mode.ts` (`overlayReason`) and `overlay-dock.ts` (`dockReason`)
 * already set.
 *
 * There is deliberately no `hidden` field. The global hide shortcut puts
 * `display:none` on an ancestor, and the right answer for that is to leave
 * the window at whatever size it already had — which falls out for free when
 * hiding does not change the derived state (audit §1.3 state 19, §3.14).
 */
export interface OverlayStateInput {
  /** The full bar rather than the pill; see `isOverlayExpanded`. */
  open: boolean;
  /** The microphone is open (or Ada is talking); see `barContent`. */
  listening: boolean;
  /** A run is in flight in either state machine. */
  busy: boolean;
  /** The step-by-step flow is open. */
  stepsActive: boolean;
  /** The deliver panel is stacked under the step panel. */
  deliverOpen: boolean;
  /** The live run's status. */
  status: string;
  hasResult: boolean;
  hasError: boolean;
  engineDown: boolean;
  deskCaption: boolean;
  commandNote: boolean;
}

/** The size decision for a frame: which state, and whether it needs a measurement. */
export interface OverlaySizing {
  state: OverlayState;
  /**
   * The state's constant cannot be trusted on its own, so a measured height
   * goes with it. True for the two states `overlay-size.ts` itself calls
   * measured (the step and deliver panels), and — the case the audit's
   * one-row-per-state table cannot express — whenever two optional blocks are
   * stacked in the card at once, since the table has a target for each of
   * them alone and none for their sum.
   *
   * Resolved 2026-09-06: `sizeFor` now honours a supplied `contentHeight` for
   * any bar state, raise-only (it can never shrink a state below its
   * constant), and the pill stays strictly fixed because it never stacks.
   *
   * The measurement is therefore supplied *always*, not only when this flag is
   * set, and the flag survives only to say why a state is content-driven. The
   * table is what makes the common case instant; the measurement is a safety
   * net for everything the table cannot enumerate — stacked blocks, and the
   * popover strut (`components/ui/popover.tsx` reserves an open popover's
   * extent inside the card so the window grows to fit it, which no state in
   * the table describes). A net that only ever raises cannot reintroduce the
   * frame-late *shrink* the table exists to avoid.
   */
  measured: boolean;
}

/** Pill states, the two the shape swap crosses between. */
function isPill(state: OverlayState): boolean {
  return state === "pill" || state === "pill-listening";
}

/**
 * The overlay's discrete state, in one place.
 *
 * The card is a stack, so several of the audit's states can be on screen at
 * once (an engine-down banner above a result, a desk caption above a run).
 * `sizeFor` takes one state, so this returns the *dominant* one — the block
 * that decides the shape — chosen from the blocks actually rendered, deepest
 * first. Anything the dominant state does not account for sets `measured`,
 * which is the honest answer for a stack whose height is a sum: a fixed
 * target for the dominant block alone would clip the banners above it, and
 * clipped-but-reported-as-working is the exact bug the sizing lane exists to
 * prevent.
 *
 * The union is `overlay-size.ts`'s, not this file's. Where that module folded
 * two of the audit's rows into one constant — the bar's listening swap (3/4,
 * height-neutral by construction) and the run's "no events yet" note (8/9,
 * sized for the taller so the first stage frame is not its own resize) — this
 * does not reintroduce the distinction.
 */
export function overlayStateFor(input: OverlayStateInput): OverlaySizing {
  if (!input.open) {
    // The pill draws three controls and nothing else; both its shapes are
    // known widths, and neither can carry a banner. Listening *is* a width
    // change here (the chevron becomes a status span), which is why the pill
    // has two states and the bar has one.
    return { state: input.listening ? "pill-listening" : "pill", measured: false };
  }

  // Which optional blocks are on screen, by the card's own conditions. The
  // command note is *hidden* while the step flow is open — it is handed to
  // StepPanel instead — so it is not a block of its own then; the same guard
  // the JSX below uses.
  const blocks: OverlayState[] = [];
  if (input.deskCaption) blocks.push("desk-caption");
  if (input.engineDown) blocks.push("engine-down");
  if (input.commandNote && !input.stepsActive) blocks.push("ada-caption");
  if (input.stepsActive) blocks.push("steps");
  if (input.deliverOpen) blocks.push("deliver");
  // One running state now: the raw feed lives in the dashboard console, so
  // there is no disclosure on the strip to make the card taller.
  if (input.busy && !input.stepsActive) blocks.push("running");
  if (input.status === "done" && input.hasResult) blocks.push("result");
  if (input.status === "error" && input.hasError) blocks.push("failure");
  if (input.status === "cancelled") blocks.push("cancelled");

  // Deepest block wins, read the way the card is built: the panels and the
  // run are the tall blocks, the banners are the thin ones above them.
  const dominant = DOMINANCE.find((candidate) => blocks.includes(candidate));
  if (!dominant) return { state: "bar", measured: false };

  return { state: dominant, measured: isMeasured(dominant) || blocks.length > 1 };
}

/** Tallest-first, so a stack is sized by the block that decides its shape. */
const DOMINANCE: readonly OverlayState[] = [
  "deliver",
  "steps",
  "running-feed",
  "running",
  "result",
  "failure",
  "cancelled",
  "ada-caption",
  "engine-down",
  "desk-caption",
];

/**
 * The card's own height, for the states that genuinely need one.
 *
 * A callback ref, deliberately: `useOverlayHeight`'s effect keyed on
 * `compact` (audit §2.5) re-attached its observer only when that flag
 * flipped, so any refactor that swapped the observed node without flipping it
 * left the observer on a detached element and the window frozen. A callback
 * ref cannot get that wrong — React hands it the new node and `null` for the
 * old one.
 *
 * `scrollHeight` over `getBoundingClientRect`, for the reason the old hook
 * records: the overlay root is `h-screen overflow-hidden`, so the rect is the
 * *clipped* box and measuring it once left the window stuck at 58 forever.
 */
function useContentHeight(): [(el: HTMLDivElement | null) => void, number | undefined] {
  const [height, setHeight] = useState<number | undefined>(undefined);
  const observer = useRef<ResizeObserver | null>(null);

  const ref = useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const next = Math.max(el.scrollHeight, el.getBoundingClientRect().height);
      // Only on a real change: a ResizeObserver that writes state on every
      // callback with the same number is a render loop.
      setHeight((prev) => (prev !== undefined && Math.abs(prev - next) < 1 ? prev : next));
    };
    const next = new ResizeObserver(measure);
    next.observe(el);
    observer.current = next;
    measure();
  }, []);

  useEffect(() => () => observer.current?.disconnect(), []);
  return [ref, height];
}

/**
 * The shape the overlay just left, while it is still fading out.
 *
 * Only the pill<->bar swap gets an exit phase; a block landing mid-run is an
 * arrival, not a substitution, and `.kv-settle` covers it on the way in. What
 * stays mounted is the *surface* — an empty Card at the outgoing shape's size,
 * taken from `sizeFor` so the ghost and the window it is standing in for read
 * the same table — and never the outgoing subtree: re-mounting `PromptBar` or
 * `StepPanel` for the length of a swap would run a second copy of every hook
 * in them, including the microphone.
 *
 * **Ghost the shrink, never the grow**, and the reason is geometric rather
 * than aesthetic. The two shapes are concentric opaque cards of different
 * widths, so at any moment the composite alpha is `a` where only the outgoing
 * covers, `b` where only the incoming does, and `a + b - ab` where both do.
 *
 * Collapsing, the arriving pill is strictly *inside* the departing bar, so
 * every pixel the bar had and the pill does not is covered by the ghost alone.
 * Without it that whole area is gone in one frame — a hard cut of a 600 px
 * surface — which is exactly what the ghost exists to prevent, and why its
 * fade is `.kv-shape-out`'s slow-falling ease (motion.css §2): an early fall
 * *is* the hole.
 *
 * Expanding, the departing pill is strictly *inside* the arriving bar, and the
 * incoming card already paints that area itself. A ghost there preserves
 * nothing — it only adds `a(1 - b)` of extra coverage inside the pill's
 * outline while the bar is still rising, which renders as a denser capsule
 * floating in the middle of the field for the length of the swap. That is not
 * a theoretical concern: it was photographed on screen, and removing the ghost
 * from this direction is what removes it. The bar simply rises uniformly on
 * `.kv-shape-in`'s fast-rising ease, which is the whole of what a grow needs.
 *
 * The asymmetry is the same one `useOverlaySize` already keeps for the window
 * — grow at once, shrink on a delay — read on the surface instead of the
 * frame: the larger shape is the one that persists.
 */
interface ShapeGhost {
  state: OverlayState;
  /** What the card actually measured before it left, or undefined. */
  height: number | undefined;
}

function useShapeGhost(
  state: OverlayState,
  contentHeight: number | undefined
): ShapeGhost | null {
  const [ghost, setGhost] = useState<ShapeGhost | null>(null);
  const previous = useRef(state);
  // The height the card had in the frame *before* the swap. `sizeFor`'s table
  // constant is not it: the bar's row is 58, but a bar carrying a "Run
  // options" line or a banner is taller, and a ghost drawn at the constant
  // drops that band in the first frame of the fade — the surface it is there
  // to hold gets 28 px shorter at exactly the moment it should not move.
  // Photographed in slow motion before this existed.
  const lastHeight = useRef(contentHeight);

  useEffect(() => {
    const was = previous.current;
    const height = lastHeight.current;
    previous.current = state;
    if (!needsGhost(was, state)) return;
    setGhost({ state: was, height });
    const id = window.setTimeout(() => setGhost(null), SHAPE_EXIT_MS);
    return () => window.clearTimeout(id);
  }, [state]);

  // Declared *after* the effect above, and with no dependency array, so on the
  // commit that swaps the shape the effect above still reads the previous
  // frame's measurement and this then catches up. (The ResizeObserver behind
  // `contentHeight` is a frame late by construction, which for once is the
  // behaviour wanted: at swap time it still describes the outgoing card.)
  useEffect(() => {
    lastHeight.current = contentHeight;
  });

  return ghost;
}

/**
 * Does leaving `was` for `state` need the outgoing surface held on screen?
 *
 * Only a pill<->bar crossing, and only in the direction that leaves bare
 * window behind it. See `useShapeGhost` for why the other direction is worse
 * with a ghost than without one.
 */
export function needsGhost(was: OverlayState, state: OverlayState): boolean {
  if (isPill(was) === isPill(state)) return false;
  return sizeFor(was).width > sizeFor(state).width;
}

/**
 * Is the webview already wide enough to lay this shape out at its real width?
 *
 * The bar is `w-full` inside a `w-screen` root, so its width is `100vw` — and
 * the webview is exactly as wide as the native window. That makes the question
 * answerable from the DOM alone, with no Tauri round trip and no physical/
 * logical pixel conversion: `window.innerWidth` *is* the number `w-full`
 * resolves against.
 *
 * The pill is exempt, and that is a layout fact rather than a shortcut: its
 * wrapper is `w-fit`, so it lays out at the intrinsic width of its three
 * controls and a narrow viewport cannot reflow it — at worst the window clips
 * it for a frame, which is not what this guards against. Only the `w-full`
 * shapes resolve their width against the viewport, so only they can be drawn
 * at the wrong one. (This also keeps the idle → listening pill instant: that
 * pair is a *window* width change with no content change at all, since
 * `CompactBar` draws the same three controls either way.)
 *
 * The 1 px of slack is for a fractional device pixel ratio: the Rust side
 * takes a `LogicalSize` and `innerWidth` is rounded, so an exact `>=` can miss
 * by a pixel on a scaled display and hold the swap open for the full deadline
 * every single time.
 */
export function hasRoomFor(state: OverlayState, viewportWidth: number): boolean {
  if (isPill(state)) return true;
  if (!Number.isFinite(viewportWidth) || viewportWidth <= 0) return true;
  return viewportWidth + 1 >= sizeFor(state).width;
}

/**
 * The shape whose *content* is rendered this frame, which is not always the
 * shape the window is being sized to.
 *
 * This is the fix for the expand direction, and the reason it needed one is
 * asymmetry that was never noticed because only one half of it is visible.
 * `useOverlaySize` grows at once and shrinks after `SHRINK_DELAY_MS`, so a
 * *collapse* already happens inside a window that is still 600 px wide: the
 * pill has all the room it needs from the first frame and the cross-dissolve
 * plays over correct geometry. An *expand* has the opposite shape. React
 * commits the bar and the browser lays it out and paints it in the frame the
 * state changed; `invoke("set_window_frame")` goes out from an effect after
 * that paint, and even that is only a request — the pinned `tao` dispatches
 * `set_content_size_async` onto the main GCD queue and returns. So the bar's
 * first painted frames are laid out against a ~132 px viewport: the field
 * collapses to nothing, the controls stack or clip, and then everything
 * reflows to 600 px when the resize lands. That reflow is what reads as the
 * pop, and no easing curve can hide it — a cross-dissolve between a squashed
 * bar and a correct one is a dissolve between two different layouts.
 *
 * So: keep showing the old shape until the viewport can hold the new one. The
 * window resize and the content swap stop being two animations on two clocks
 * and become one ordered pair — grow, then swap; swap, then shrink — which is
 * the same envelope OpenWhispr gets structurally by sizing its native window
 * to a footprint ladder that is always larger than the pill animating inside
 * it (`vendor/openwhispr/src/helpers/windowConfig.js` — `WINDOW_SIZES.BASE`
 * 176x120 around a 40x40 idle pill).
 *
 * Only the width is gated. It is a constant (600) that the window always
 * reaches, whereas the tall states' heights are content-driven and may never
 * match a target exactly — a height gate would spend its deadline on every
 * step-panel frame. Height clipping also degrades gracefully (the card is
 * `max-h-full overflow-y-auto`); a width reflow does not.
 *
 * Not reduced-motion gated, for `SHRINK_DELAY_MS`'s reason: nothing moves
 * during the wait. It is a frame or two of the shape that was already on
 * screen, which is as true with the media query set as without it, and
 * skipping it would show reduced-motion users the one artifact this exists to
 * remove.
 */
export function useShownShape(target: OverlayState): OverlayState {
  const [shown, setShown] = useState<OverlayState>(target);

  useEffect(() => {
    if (target === shown) return;
    const width = () =>
      typeof window === "undefined" ? Number.NaN : window.innerWidth;
    if (hasRoomFor(target, width())) {
      setShown(target);
      return;
    }

    // Every path out of here sets the shape, so there is no state in which the
    // overlay is stuck showing a shape it has left.
    const onResize = () => {
      if (!hasRoomFor(target, width())) return;
      setShown(target);
    };
    window.addEventListener("resize", onResize);
    const id = window.setTimeout(() => {
      console.warn(
        "[kaleo overlay] the window did not make room for the",
        target,
        "shape within",
        SHAPE_ROOM_TIMEOUT_MS,
        "ms; showing it clipped rather than not at all"
      );
      setShown(target);
    }, SHAPE_ROOM_TIMEOUT_MS);
    return () => {
      window.removeEventListener("resize", onResize);
      window.clearTimeout(id);
    };
  }, [target, shown]);

  return shown;
}

/**
 * The floating overlay: one prompt, one run, one result.
 *
 * The shell is Pluely's — a Card in a transparent, undecorated window, with the
 * drag handle and updater the app draws for itself because the OS draws no
 * titlebar. Only the domain inside it is silkscreen's.
 *
 * All run state lives in the provider, deliberately: it owns the only in-flight
 * guard and the only AbortController, and a run costs real money.
 */
const Kaleo = () => {
  const { isHidden } = useApp();
  const run = useSilkscreenRun();
  // Speaks the digest once when the live run completes; see useRunVoice.
  useRunVoice(run);

  // How a finished run reports back. Carried exactly like `stepMode`: page
  // state, seeded from the remembered choice so it survives a remount, written
  // back on every change (RunOptions persists it too, and writing the same
  // value twice costs nothing — this way the page owns the fact rather than
  // depending on a child to record it), and handed to the options popover.
  // Unlike `stepMode` it also joins the request, through `summaryFields`, so
  // every path that starts a run folds it in from the one piece of state.
  const [summaryMode, setSummaryMode] = useState<SummaryMode>(readSummaryMode);
  const changeSummaryMode = useCallback((mode: SummaryMode) => {
    setSummaryMode(mode);
    writeSummaryMode(mode);
  }, []);
  // The step-by-step flow is a second, separate state machine with its own
  // in-flight guard; the toggle decides which one a submit goes to, and the
  // two are never in flight together.
  //
  // The step run reads the same state: the engine's `review` step is what
  // actually turns Structured into a spec-review agenda, so the mode has to
  // reach that approval rather than only the one-shot request. The hook
  // attaches it (see `summaryPayload`), which is why it is declared above the
  // hook and why no button builds a payload of its own.
  const steps = useStepRun({
    baseUrl: run.baseUrl,
    token: run.token,
    summary: summaryMode,
  });
  // Speaks the turns in the step flow — a stage waiting to be approved, a
  // stage that failed — and nothing else. See useVoiceReplies.
  useVoiceReplies(steps);
  const [stepMode, setStepMode] = useState<boolean>(readStepMode);
  const changeStepMode = useCallback((enabled: boolean) => {
    setStepMode(enabled);
    try {
      window.localStorage.setItem(STEP_MODE_KEY, enabled ? "1" : "0");
    } catch {
      /* per-viewer convenience only */
    }
  }, []);
  // Which of the two doors the service would actually accept for a held
  // sentence, right now. Derived from what the engine sent, never assumed.
  const doors = heldDoors({
    session: steps.session,
    done: steps.history.map((entry) => entry.step),
    cancelled: steps.cancelled === true,
  });

  const stepsActive = steps.status !== "idle";

  const busy = !SETTLED.includes(run.status) || steps.status === "running";

  // The overlay opens as the full bar with the text box: Pat tried the idle
  // pill (dot, mic, arrow) and found it too small to be useful. The left
  // arrow still folds it down to the pill for anyone who wants the screen
  // back; a run in flight or a result on screen holds it open regardless.
  // See overlay-mode.ts.
  const [expanded, setExpanded] = useState(true);
  // Which skin is on, live: Settings is a different webview, so this arrives
  // as a `storage` event rather than through a context.
  const { skin, adaInTerminal, setSkin } = useOverlaySkin();
  /**
   * The terminal skin's Ada half.
   *
   * `/chat/stream` can decide to generate a board, which is a paid run — so
   * this streams every frame into the terminal as it arrives rather than
   * going quiet and returning one block at the end, and says plainly when a
   * board actually ran. A paid run must never look like a slow reply.
   */
  const terminalSession = useRef<string>(
    `term-${Math.random().toString(36).slice(2, 10)}`,
  );
  const askFromTerminal = useCallback(
    async (text: string, mode: "ada" | "agent", write: (line: string) => void) => {
      const outcome = await ask(text, {
        baseUrl: run.baseUrl,
        token: run.token,
        onLine: write,
        sessionId: terminalSession.current,
      });
      const parts: string[] = [];
      if (outcome.assistant) parts.push(outcome.assistant);
      if (outcome.ranBoard) {
        parts.push("A board was generated — open the dashboard to see it.");
      } else if (mode === "agent" && !outcome.assistant) {
        parts.push("Nothing to do.");
      }
      return parts.join("\n\n");
    },
    [run.baseUrl, run.token],
  );
  const open = isOverlayExpanded({
    expanded,
    busy,
    stepsActive: steps.status !== "idle",
    status: run.status,
  });
  // The whole overlay, so the focus shortcut can find the prompt field in it
  // whichever shape the bar is in.
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    // cmd+shift+i asks for the text field: unfold the bar, then focus the
    // field once it has rendered. The click this saves is the one that would
    // have left KiCad.
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    listen("focus-text-input", () => {
      setExpanded(true);
      window.setTimeout(() => focusPromptIn(rootRef.current), 100);
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch((error) => console.warn("[kaleo overlay] focus listener:", error));
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);


  // `start` takes an optional override, so it must never be handed straight to
  // onClick — React would pass the click event as the override.
  const [commandNote, setCommandNote] = useState<string | null>(null);
  const [deskCaption, setDeskCaption] = useState<string | null>(null);

  // The tour's one sentence for the strip arrives over the storage bridge
  // (`TOUR_CAPTION_KEY`, written by the dashboard's done step) and rides the
  // existing caption path. Dismiss clears the key, which ends it.
  useEffect(() => {
    const apply = (value: string | null) => {
      if (value) {
        setExpanded(true);
        setCommandNote(value);
      }
    };
    apply(safeLocalStorage.getItem(TOUR_CAPTION_KEY));
    const onStorage = (event: StorageEvent) => {
      if (event.key === TOUR_CAPTION_KEY) apply(event.newValue);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // A sentence never spends money. It arms a stage, and a human commits it.
  // The match is deliberately fuzzy, which is right for choosing and
  // indefensible for committing: "sg lets order" is one token away from
  // "let's not order", and a stray transcript is no tokens away at all.
  const [armed, setArmed] = useState<ArmedCommand | null>(null);

  // Consent does not accumulate. An armed stage that nobody confirmed decays,
  // out loud, rather than waiting all afternoon for a mis-aimed Enter.
  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => {
      setArmed(null);
      setCommandNote("That timed out. Nothing was run.");
    }, ARM_DECAY_MS);
    return () => window.clearTimeout(id);
  }, [armed]);

  // Anything that changes what may run invalidates a pending consent.
  useEffect(() => {
    if (steps.status === "running" || steps.status === "idle") setArmed(null);
  }, [steps.status]);

  /**
   * A sentence the bar took but has not acted on.
   *
   * `parked` means something was running when it arrived: it is held, in the
   * engineer's own words, until the stage lands. `offered` means it is
   * standing as a question — "start a new board from this?" — with a button
   * that spends and a button that does not.
   *
   * It deliberately does **not** decay the way `armed` does. `armed` decays
   * because a fuzzy match on a transcript may be a mishearing, and consent
   * to spend must not accumulate. This is the opposite object: the exact
   * sentence, kept verbatim, and losing it would mean the engineer types it
   * twice. Nothing here runs by itself — the only path from held to spent is
   * `startHeld`, which is a click.
   */
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [held, setHeld] = useState<HeldRequest | null>(null);

  // An idea sent from Slack (slackbot/bridge.py -> service/inbox.py). Taken
  // only while nothing is in flight, and always as a step run whatever the
  // toggle says: a sentence from a phone must reach the approval-gated flow,
  // never a one-shot paid board. Same `steps.start` the prompt bar calls.
  useIdeaInbox({
    baseUrl: run.baseUrl,
    token: run.token,
    busy,
    session: steps.session,
    status: steps.status,
    onIdea: (idea) => {
      setCommandNote(null);
      setHeld(null);
      run.updateRequest({ intent: idea.text });
      steps.start({
        intent: idea.text,
        datasheets: run.request.datasheets,
        time_limit_s: run.request.time_limit_s,
        kicad_live: true,
        ...summaryFields(summaryMode),
      });
    },
  });

  /**
   * The service's own sentence about the last thing we asked it to do with a
   * held note — an amend receipt, or a cancel report.
   *
   * Rendered **verbatim**. The service composes the one honest sentence out of
   * facts this client does not have (whether a step was under the session
   * lock, which background jobs are still going, what a restart would cost),
   * and every rewording I tried here came out rosier than the truth. So the
   * client's job is to put it on screen and to add nothing.
   */
  const [lifecycle, setLifecycle] = useState<
    { headline: string; detail: string | null; kind: "amend" | "cancel" } | null
  >(null);
  /** The last amend receipt, for the one affordance `applies.at_step` licenses. */
  const [receipt, setReceipt] = useState<AmendResponse | null>(null);
  /** A lifecycle POST is in flight. Only ever disables its own two buttons. */
  const [sending, setSending] = useState(false);

  // A parked sentence becomes an offer the moment nothing is in flight. It
  // does not start: the run it would start costs a full pipeline, and the
  // engineer said it twenty seconds ago about a board that has changed
  // underneath them since.
  useEffect(() => {
    if (held?.state !== "parked" || busy) return;
    setHeld({ state: "offered", text: held.text });
  }, [held, busy]);

  // Held text renders in the open card only, so holding it while the strip is
  // a pill would be the "it ignored me" bug wearing a different hat.
  useEffect(() => {
    if (held) setExpanded(true);
  }, [held]);

  /**
   * A sentence, from the keyboard or the mic, while a run is open.
   *
   * `spoken` says the sentence arrived by voice, and it is the only thing
   * that decides whether the answer is spoken: someone typing is looking at
   * the strip and already has the answer in front of them, while someone
   * across the room who said "route it" gets nothing at all unless it is
   * said back. Answering a turn is conversation; announcing it to a person
   * watching the screen is narration.
   *
   * Every branch below ends in something happening. There used to be one
   * that did not — an off-vocabulary sentence was answered with the list of
   * words this app knows, on screen and out loud — and it was the wrong
   * answer to the commonest thing an engineer says mid-run, which is what
   * they want built instead. The vocabulary is unchanged; what changed is
   * that falling off it is now a request rather than a failure.
   */
  const interpret = useCallback(
    (text: string, spoken = false): boolean => {
      const command = interpretCommand(text, steps.available);
      // A restart is armed like an approval: it drops a run money was spent
      // on, and "try again" is one transcript away from "look again".
      const next = armCommand(command, text);
      if (next) {
        setCommandNote(null);
        setHeld(null);
        setArmed(next);
        // The consent step, out loud: it is a question, and a question that
        // is only drawn on screen leaves a hands-free engineer waiting for
        // an answer that never comes, until it decays.
        if (spoken) void announce(armedLine(next.step));
        return true;
      }
      if (command.kind === "later") {
        // They named a real stage out of turn. That has an answer — which
        // stage comes first — and the answer is worth more offered than
        // stated, so the stage that *is* next arms, echoing what they said.
        // Confirming is still a click, and it still says what it costs.
        const soon = STEP_DESCRIPTORS[command.step].action.toLowerCase();
        if (busy) {
          // Mid-run there is nothing available to arm, and the answer is not
          // "no": it is when. The stage they named is still coming.
          setArmed(null);
          const line = `Noted — I will offer ${soon} once this stage lands.`;
          setCommandNote(line);
          if (spoken) void announce(line);
          return true;
        }
        const now = steps.available[0];
        if (now) {
          setHeld(null);
          setArmed({ step: now, source: text.trim() });
          const line = `${soon} comes after ${STEP_DESCRIPTORS[now].action.toLowerCase()}. Confirm and I will do that one first.`;
          setCommandNote(line);
          if (spoken) void announce(line);
          return true;
        }
        setArmed(null);
        const line = `Nothing is waiting to run, so I cannot ${soon} on this run. Describe a board and I will start a new one.`;
        setCommandNote(line);
        if (spoken) void announce(line);
        return false;
      }
      if (command.kind === "request") {
        // The sentence that used to be a dead end. It is a board, or a change
        // to one, and it is accepted either way — the only question is when.
        setArmed(null);
        if (busy) {
          setHeld({ state: "parked", text: command.text });
          const line =
            "Got it — holding that until this stage lands, then I will ask. Nothing extra is spent.";
          setCommandNote(line);
          if (spoken) void announce(line);
          return true;
        }
        setHeld({ state: "offered", text: command.text });
        const line = steps.history.length
          ? "Got it. That is a different board from this one — confirm and I will start it."
          : "Got it. Confirm and I will start that board.";
        setCommandNote(line);
        if (spoken) void announce(line);
        return true;
      }
      // `none`: nothing was actually said — an empty transcript, or a bare
      // "ok" with nothing waiting to agree to. This is the one place the menu
      // is still the right answer, because there is no sentence to act on.
      //
      // It does not use `unknownCommandLine`, which opens "I did not follow
      // that": there was nothing to follow, and telling someone their silence
      // was misunderstood is the same rudeness in a smaller size. The screen
      // and the ear get the same sentence.
      setArmed(null);
      const line = steps.available.length
        ? `I am listening. Next up: ${steps.available
            .map((step) => STEP_DESCRIPTORS[step].action.toLowerCase())
            .join(", ")} — or describe a different board.`
        : "I am listening — describe the board you want.";
      setCommandNote(line);
      if (spoken) void announce(line);
      return false;
    },
    [steps, busy]
  );

  /**
   * Spend the held sentence: this is the one path from a held request to a
   * paid run, and it is a click. It reuses the same two start paths the
   * prompt bar uses, so a board started this way is the same board.
   */
  /** What the engine said, when it refused. Its words, not a paraphrase. */
  const lifecycleFailure = useCallback((error: unknown, what: string) => {
    const message =
      error instanceof Error && error.message ? error.message : `could not ${what}`;
    setLifecycle({ headline: message, detail: null, kind: "amend" });
  }, []);

  /**
   * Send the held sentence to the engine as a note on this run.
   *
   * `step` is `"case"` for the one gate that reads free text and null for a
   * note kept for a restart. Exactly one note is written either way: the door
   * is chosen here, before the POST, rather than by amending twice — the
   * restart intent is the original plus *every* note, so a sentence recorded
   * under both doors would appear twice on the board that replaces this one.
   *
   * Nothing about this changes the run in flight, and the headline the service
   * returns says so in its own words.
   */
  const noteHeld = useCallback(
    async (step: StepName | null) => {
      if (!held || !steps.amend) return;
      setSending(true);
      try {
        const result = await steps.amend(held.text, step);
        setReceipt(result);
        setLifecycle({
          headline: result.headline,
          // Only shown for a note with nowhere to land: it is the service's
          // explanation of *why* no stage will read it, and repeating it over
          // a note the case step will read would be noise.
          detail: result.applies.at_step ? null : result.not_consumed,
          kind: "amend",
        });
        // The sentence is on the engine now, so the strip stops holding it —
        // but the offer to start a new board from it stays, because that is
        // still the only way to act on it.
        setHeld({ state: held.state, text: held.text });
        run.updateRequest({ intent: "" });
      } catch (error) {
        lifecycleFailure(error, "record that");
      } finally {
        setSending(false);
      }
    },
    [held, steps, run, lifecycleFailure]
  );

  /**
   * Stop the run, and say what that did and did not stop.
   *
   * The response is the whole point: a cancel closes the session to further
   * steps — real money, since every remaining step is a model call or a solve
   * — and reaches nothing already started. `still_running` names background
   * work that is *finishing and will be discarded*, and this must never be
   * drawn as stopped.
   */
  const cancelRunOnEngine = useCallback(async () => {
    if (!steps.cancelRun) return;
    setSending(true);
    try {
      const result = await steps.cancelRun();
      setLifecycle({
        headline: result.headline,
        detail: [
          result.still_running.length
            ? `Still finishing, and will be discarded: ${result.still_running.join(", ")}.`
            : null,
          result.in_flight ? `Stops at ${result.aborts_at}.` : null,
          result.not_stoppable,
        ]
          .filter(Boolean)
          .join(" "),
        kind: "cancel",
      });
    } catch (error) {
      lifecycleFailure(error, "cancel that");
    } finally {
      setSending(false);
    }
  }, [steps, lifecycleFailure]);

  const startHeld = useCallback(async () => {
    if (!held) return;
    let intent = held.text;
    const doors = heldDoors({
      session: steps.session,
      done: steps.history.map((entry) => entry.step),
      cancelled: steps.cancelled === true,
    });
    if (doors.restart && steps.amend) {
      setSending(true);
      try {
        // The engine's own amended intent, not this one sentence: it is the
        // original request plus every note typed against this run, in order,
        // which is what someone who has been talking to a run for two minutes
        // means by "start over with this". A receipt already in hand is reused
        // rather than posting a second copy of the same note.
        const result = receipt ?? (await steps.amend(held.text, null));
        setReceipt(result);
        intent = result.restart.intent;
      } catch {
        // The board is still the product: a note the engine would not record
        // must not cost the engineer the run they asked for.
      } finally {
        setSending(false);
      }
      // Close the old session before spending on a new one. It is the only
      // real saving available — the steps that now never run — and the report
      // says plainly what it could not stop.
      if (busy && steps.cancelRun) {
        try {
          const stopped = await steps.cancelRun();
          setLifecycle({
            headline: stopped.headline,
            detail: [
              stopped.still_running.length
                ? `Still finishing, and will be discarded: ${stopped.still_running.join(", ")}.`
                : null,
              stopped.not_stoppable,
            ]
              .filter(Boolean)
              .join(" "),
            kind: "cancel",
          });
        } catch {
          /* the new run is what matters; the old session lapses on its own */
        }
      }
    }
    setHeld(null);
    setReceipt(null);
    setCommandNote(null);
    run.updateRequest({ intent });
    if (!stepMode) {
      run.start({ intent, ...summaryFields(summaryMode) });
      return;
    }
    // `steps.start` clears the previous session itself, so no reset first.
    steps.start({
      intent,
      datasheets: run.request.datasheets,
      time_limit_s: run.request.time_limit_s,
      kicad_live: true,
      ...summaryFields(summaryMode),
    });
  }, [held, run, steps, stepMode, summaryMode, receipt, busy]);

  const confirmArmed = useCallback(
    (payload?: Record<string, unknown>) => {
      if (!armed) return;
      const step = armed.step;
      setArmed(null);
      if (step === "restart") {
        setCommandNote(null);
        steps.reset();
        return;
      }
      // The panel's own inputs for the stage (the case style and rigorous
      // flag), so a spoken "case" carries the same request a click would.
      steps.approve(step, payload);
    },
    [armed, steps]
  );

  const submit = useCallback(() => {
    // Something is already in flight, in either state machine. The sentence
    // is still a sentence: it is parked, said back, and offered the moment
    // the stage lands. This used to be `return` — the text stayed in the
    // field and nothing whatever happened, which is the worst of the three
    // possible answers because it is indistinguishable from a broken key.
    if (busy) {
      const text = run.request.intent.trim();
      if (!text) return;
      if (interpret(text)) run.updateRequest({ intent: "" });
      return;
    }
    if (!stepMode) {
      run.start(summaryFields(summaryMode));
      return;
    }
    // A step run is open: the prompt bar is a conversation with it. "sg lets
    // order" approves the order step; it must never quietly become a second
    // paid board without being asked. That holds once every stage has run
    // too: Enter on a finished run is answered, and a sentence describing a
    // different board is offered rather than started.
    if (steps.status === "waiting" || steps.status === "error" || steps.status === "done") {
      const text = run.request.intent;
      // Understood or not, the draft is spent: the panel now carries the echo.
      if (interpret(text)) run.updateRequest({ intent: "" });
      return;
    }
    setCommandNote(null);
    setHeld(null);
    steps.start({
      intent: run.request.intent,
      datasheets: run.request.datasheets,
      time_limit_s: run.request.time_limit_s,
      kicad_live: true,
      ...summaryFields(summaryMode),
    });
  }, [run, steps, stepMode, summaryMode, interpret, busy]);

  // Local wake → transcript → ada-path. Deixis goes to /desk/resolve (or a
  // caption) and never to /generate. wake-flow still owns board vs command.
  const wake = useWakeWord({
    baseUrl: run.baseUrl,
    token: run.token,
    hidden: isHidden,
    // On-device wake is on. `wake_status` still only reports whether an ONNX
    // file loads rather than whether it recognises anything, so the guarantee
    // behind this flag is measurement, not the status call: the shipped
    // `resources/wake/hey_ada.onnx` scores 0.87 recall on "Hey Ada" spoken by
    // voices it never trained on, and zero false accepts over held-out speech,
    // at the 0.7 threshold the app runs (scratchpad measurement, 2026-09-07).
    // The model it replaces was a collapsed training run that emitted ~0.09
    // for every input — silence, noise and the phrase alike.
    preferLocal: true,
    onWake: (utterance) => {
      void (async () => {
        // BAR LANE: hearing my name no longer unfolds the whole overlay. The
        // strip keeps its size and swaps the field for the listening state
        // (PromptBar/CompactBar). Expansion happens below, and only where
        // there is something to *read* — a note, a caption, a draft — since
        // those render nowhere but the open card.
        const prepared = await enrichWithDeskContext(utterance);
        const routed = routeSpokenUtterance({
          utterance: prepared.utterance,
          snap: prepared.snap,
          busy,
          stepsStatus: steps.status,
        });
        const action = await fulfillAdaDecision(routed, {
          resolveDesk: (text, snap) =>
            resolveDesk(run.baseUrl, {
              utterance: text,
              png_base64: snap.png_base64,
              cursor_x: snap.cursor_x,
              cursor_y: snap.cursor_y,
              width: snap.width,
              height: snap.height,
              candidates: collectDeskCandidates(),
              token: run.token,
            }),
        });
        if (action.kind === "ignore") {
          // They said my name and I dropped the sentence. Silence here is
          // indistinguishable from not having heard them at all, which is
          // the one thing this app must never leave ambiguous.
          void announce(refusalLine(action.reason));
          return;
        }
        if (action.kind === "command") {
          // interpret() answers in commandNote / the step panel, both of
          // which live in the open card. BAR LANE: expand here, not on wake.
          setExpanded(true);
          // Spoken in, spoken back: this sentence arrived by voice.
          void interpret(action.text, true);
          return;
        }
        if (action.kind === "listen") return;
        if (action.kind === "caption") {
          // Same reason: a caption nobody can see is the "it ignored me" bug.
          setExpanded(true);
          setDeskCaption(action.text);
          setCommandNote(action.text);
          // Through announce, not the speaker directly: this is the answer to
          // a spoken question, and it has to obey the same silence switch as
          // everything else — it used to keep talking after the mute.
          void announce(action.text);
          try {
            window.dispatchEvent(
              new CustomEvent("silkscreen:desk", {
                detail: {
                  caption: action.text,
                  abstain: action.abstain === true,
                  target: action.target ?? null,
                },
              })
            );
          } catch {
            /* overlay has no SPA guide; caption-only is honest */
          }
          return;
        }
        if (action.kind === "desk" || !wouldStartBoard(action)) return;
        run.updateRequest({ intent: action.intent });
        if (!stepMode) {
          run.start({ intent: action.intent, ...summaryFields(summaryMode) });
          return;
        }
        setCommandNote(null);
        steps.start({
          intent: action.intent,
          datasheets: run.request.datasheets,
          time_limit_s: run.request.time_limit_s,
          kicad_live: true,
          ...summaryFields(summaryMode),
        });
      })();
    },
  });

  // The ear stops while I talk, and starts again when I stop.
  //
  // Two reasons, both measured rather than theoretical. The `windows` ear
  // pays for four seconds of room audio per call, and a window recorded
  // while I am talking is a model call spent listening to myself. And this
  // machine's speakers reach this machine's microphone: a reply of mine that
  // happens to contain my own name would wake me — one room, one microphone,
  // one speaker is a feedback loop unless somebody yields, and it is me.
  //
  // Registered through a ref so the duck is installed once and still sees the
  // current listener; the resume is skipped when a human took the microphone
  // mid-sentence (announce checks that), because push-to-talk restarts the
  // ear itself and two starts under one recording is worse than none.
  const wakeRef = useRef(wake);
  useEffect(() => {
    wakeRef.current = wake;
  }, [wake]);
  useEffect(() => {
    setSpeechDuck(() => {
      const ear = wakeRef.current;
      if (!ear?.listening) return undefined;
      ear.stop();
      return () => {
        if (wakeRef.current?.enabled) wakeRef.current.start();
      };
    });
    return () => setSpeechDuck(null);
  }, []);

  // The menu bar icon follows the ear: the glyph fills in while the mic is
  // open, and its "Ada listening" item is the same switch as the bar's mic
  // button. See useTrayState for why the check mark waits for this report.
  useTrayState({
    listening: wake.listening,
    visible: !isHidden,
    onToggleListening: () => wake.setEnabled(!wake.enabled),
  });

  // BAR LANE: the machine going away closes the microphone.
  //
  // QA left the app unmuted when the Mac locked and the ear stayed open for
  // minutes — the VAD gate held, so it spent nothing, but "the screen is
  // locked and the laptop is still listening" is the exact thing that makes
  // an always-listening product untrustworthy, and the indicator that would
  // have said so is behind the lock screen.
  //
  // The signal is `visibilitychange` (plus `pagehide`), because those are
  // what a WKWebView actually gets. Deliberately *not* window blur or Tauri
  // focus: this overlay's whole job is to listen while the engineer works in
  // KiCad, so it loses focus constantly and closing on that would leave the
  // ear open for no useful window at all.
  //
  // It mutes rather than pausing, so the control reads `muted` — the truth —
  // instead of `arming`, which would claim a microphone was opening. Coming
  // back is a click, and the note below is why: an ear that went quiet
  // without saying so is the "spent" bug in a different coat.
  useEffect(() => {
    const onAway = () => {
      if (document.visibilityState !== "hidden") return;
      const ear = wakeRef.current;
      if (!ear?.enabled && !ear?.listening) return;
      ear?.setEnabled(false);
      setCommandNote(
        "The screen went away, so I stopped listening — I won’t keep a microphone open on a machine you’ve left. Click the mic when you’re back."
      );
    };
    document.addEventListener("visibilitychange", onAway);
    window.addEventListener("pagehide", onAway);
    return () => {
      document.removeEventListener("visibilitychange", onAway);
      window.removeEventListener("pagehide", onAway);
    };
  }, []);

  const openDashboard = useCallback(async () => {
    try {
      await invoke("open_dashboard");
    } catch (error) {
      console.error("Failed to open the review window:", error);
    }
  }, []);

  // Open the dashboard once per completed LIVE run: the bridge has already
  // handed it the result, so it opens populated, on the board — the moment
  // worth surfacing. History browsing and failures never trigger this.
  const openedRunRef = useRef<string | null>(null);
  const latestRunId = run.history[0]?.id ?? null;
  useEffect(() => {
    if (run.status !== "done" || !run.result || run.viewingHistory) return;
    if (!latestRunId || openedRunRef.current === latestRunId) return;
    openedRunRef.current = latestRunId;
    void openDashboard();
  }, [run.status, run.result, run.viewingHistory, latestRunId, openDashboard]);

  // The same rule for a step run, which is the DEFAULT mode: open the
  // dashboard once, at the moment the run first has a board to show
  // (`useStepRun` publishes from the `place` step onward under one id per
  // session). Once per session and never again — later steps update the
  // window that is already open, and a case or an order landing must not
  // throw a window over the KiCad the engineer is working in. This mirrors
  // the one-shot path above rather than being more shy than it: the overlay
  // opens the review window exactly once per run, either way.
  const openedStepRef = useRef<string | null>(null);
  useEffect(() => {
    const published = steps.publishedId;
    if (!published || openedStepRef.current === published) return;
    openedStepRef.current = published;
    void openDashboard();
  }, [steps.publishedId, openDashboard]);

  // The newest artifact, as an identity that changes when a stage lands. The
  // overlay can only see that the engineer left it, not that they arrived in
  // KiCad, so this is a nag and never a gate. See useReviewedInKicad.
  const latestStep = steps.history[steps.history.length - 1];
  const reviewMarker =
    latestStep && latestStep.shown_in_kicad
      ? `${latestStep.session}:${steps.history.length}`
      : null;
  const reviewed = useReviewedInKicad(reviewMarker);
  // The one-shot result's critic verdict: the failed (or skipped) sentence,
  // or null when the review answered — including with no findings.
  const unreviewed = run.result
    ? (reviewFailure(run.result.review) ?? reviewSkipped(run.result.review))
    : null;

  const engineDown = !busy && run.engine.lastCheckedAt !== null && !run.engine.ok;

  // ------------------------------------------------------------ size and shape
  //
  // The window no longer chases the content. One derivation says what the
  // overlay *is*, `sizeFor` says how big that is, and the hook grows the
  // window at once and delays the shrink so the content transition is not
  // racing a frame change. The measured path survives only for the states
  // the audit calls genuinely content-driven (see `overlayStateFor`).
  //
  // `listening` is read here as well as in PromptBar/CompactBar, from the same
  // pure `barContent`: it is a *width* change on the pill (state #2) and the
  // window has to know about it, but the bar's own swap stays height-neutral
  // by construction (ListeningPanel is `h-9`, exactly the Input it replaces).
  const speaking = useIsSpeaking();
  const listening =
    barContent({
      micOpen: micIsOpen(micState(wake, false)),
      speaking,
      hidden: isHidden,
    }) === "listening";
  const deliverOpen = stepsActive && Boolean(steps.session) && deliverable(steps.history);
  const sizing = overlayStateFor({
    open,
    listening,
    busy,
    stepsActive,
    deliverOpen,
    status: run.status,
    hasResult: Boolean(run.result),
    hasError: Boolean(run.error),
    engineDown,
    deskCaption: Boolean(deskCaption),
    commandNote: Boolean(commandNote),
  });
  const [contentRef, contentHeight] = useContentHeight();
  // Always supplied: see `measured`'s doc comment. Raise-only in `sizeFor`,
  // so a constant that is right stays authoritative and one that is short
  // (stacked blocks, an open popover) is corrected upward instead of clipping.
  useOverlaySize(sizing.state, contentHeight);
  // And where on the screen it sits: the bottom edge while a step is on show
  // in KiCad, the top otherwise. See overlay-dock.ts.
  useOverlayDock(steps, run.status);
  // Which shape is *drawn*. `sizing.state` is what the window is being sized
  // to; this lags it by however long the native grow takes, so the bar is
  // never laid out against a viewport too narrow to hold it. See
  // `useShownShape`. The ghost follows the drawn shape, not the target —
  // otherwise the outgoing surface would start dissolving while the shape it
  // is dissolving *into* is still the one on screen.
  const shownState = useShownShape(sizing.state);
  const shownOpen = !isPill(shownState);
  const ghost = useShapeGhost(shownState, contentHeight);

  return (
    <ErrorBoundary
      fallbackRender={() => <ErrorLayout isCompact />}
      resetKeys={["kaleo-error"]}
    >
      <div
        ref={rootRef}
        className={`relative w-screen h-screen flex overflow-hidden justify-center items-start ${
          isHidden ? "hidden pointer-events-none" : ""
        }`}
      >
        {/* The shape the overlay just left, fading out *under* the one
            arriving. Surface only — an empty Card at the outgoing size, so the
            swap has something to cross-fade from without a second live
            subtree. It is absolutely positioned and so contributes nothing to
            the measured box, and `aria-hidden` because it says nothing.

            "Under" is load-bearing and is why every shape wrapper below is
            `relative`. An absolutely positioned element paints above in-flow
            siblings whatever the source order, so the ghost used to sit on top
            of the shape replacing it: expanding, a blank 132 px capsule hung
            over the middle of the prompt field for the length of the swap;
            collapsing, a blank 600 px card hid the pill's three controls until
            it had finished fading. Photographed both. Making the wrappers
            positioned puts them in the same paint step as the ghost, where
            source order decides — ghost first, incoming over it — so the new
            content is legible from its first frame and the ghost does the one
            job it has, which is to keep the surface opaque underneath while
            the new content rises (motion.css §2). */}
        {ghost ? (
          <div
            aria-hidden
            className="kv-shape-out pointer-events-none absolute left-1/2 top-0 -translate-x-1/2"
            style={{
              width: sizeFor(ghost.state).width,
              height: sizeFor(ghost.state, ghost.height).height,
            }}
          >
            <Card className="h-full w-full" />
          </div>
        ) : null}

        {skin === "spotlight" ? (
          // One field at rest and nothing else. The brief was that the
          // overlay "isnt meant to have so much visual clutter" — every
          // other element here is conditional on a reason existing.
          <div ref={contentRef} className="w-full relative kv-shape-in" key="spotlight">
            <Card className="w-full overflow-hidden p-0">
              <SkinStrip name="Spotlight" onLeave={() => setSkin("plain")} />
              <SpotlightSkin
                value={run.request.intent}
                onChange={(intent) => run.updateRequest({ intent })}
                onSubmit={submit}
                onClose={() => setExpanded(false)}
                busy={busy}
                hidden={isHidden}
                engine={run.engine}
                baseUrl={run.baseUrl}
              />
            </Card>
          </div>
        ) : skin === "orb" ? (
          // Collapses to the orb and nothing else; the field appears on click
          // or on speech. `micOpen` is the ONLY input that may animate it —
          // an orb that pulses at a shut microphone is the green dot's lie in
          // a louder form. See OrbSkin's own comments.
          <div ref={contentRef} className="w-fit relative kv-shape-in" key="orb">
            <Card className="w-fit flex-col items-center overflow-hidden p-0">
              <SkinStrip name="Orb" onLeave={() => setSkin("plain")} />
              <div className="px-3 py-2">
                <OrbSkin
                  engine={run.engine}
                  baseUrl={run.baseUrl}
                  micOpen={wake?.listening ?? false}
                  listening={wake?.enabled ?? false}
                  justHeard={wake?.justHeard ?? false}
                  value={run.request.intent}
                  onChange={(intent) => run.updateRequest({ intent })}
                  onSubmit={submit}
                  busy={busy}
                  canStart={run.canStart}
                  hidden={isHidden}
                  onStopListening={() => wake?.setEnabled(false)}
                />
              </div>
            </Card>
          </div>
        ) : skin === "terminal" ? (
          // The terminal skin replaces the bar rather than sitting inside it:
          // it is a real shell on a pty (`src-tauri/src/pty.rs`), and a shell
          // squeezed into a one-line strip is neither a terminal nor a bar.
          // Ada shares its input line — see `@/lib/terminal-sigil`.
          <div ref={contentRef} className="w-full relative kv-shape-in" key="terminal">
            <Card className="flex h-[320px] w-full flex-col overflow-hidden p-0">
              <SkinStrip name="Terminal" onLeave={() => setSkin("plain")} />
              <div className="min-h-0 flex-1">
                <TerminalSkin adaEnabled={adaInTerminal} onAsk={askFromTerminal} />
              </div>
            </Card>
          </div>
        ) : !shownOpen ? (
          // `shownOpen`, not `open`: the pill holds the slot until the native
          // window is wide enough for the bar to lay out at 600 px, so the
          // dissolve is never between a squashed bar and a correct one. See
          // `useShownShape`.
          //
          // `key` so React remounts on the swap and the enter animation
          // replays; a keyframe animation needs a mount, not a re-render.
          <div ref={contentRef} className="w-fit relative kv-shape-in" key="pill">
            <Card className="w-fit flex-row items-center px-1.5 py-1">
              <CompactBar
                baseUrl={run.baseUrl}
                token={run.token}
                hidden={isHidden}
                wake={wake}
                onExpand={() => setExpanded(true)}
                onTranscript={(text) => {
                  run.updateRequest({
                    intent: run.request.intent.trim()
                      ? `${run.request.intent.trimEnd()} ${text}`
                      : text,
                  });
                  // Open the bar so the transcript can be read and edited.
                  // It lands in the draft only; generating is still a click.
                  setExpanded(true);
                }}
              />
            </Card>
          </div>
        ) : (
        <div ref={contentRef} className="w-full relative kv-shape-in" key="bar">
        <Card
          // `max-h-full overflow-y-auto`: the window is capped at
          // OVERLAY_MAX_HEIGHT and the root is `overflow-hidden`, so content
          // past the cap used to be clipped mid-sentence with nothing on
          // screen to say so. Clipping is invisible; a scrollbar is not.
          // `data-tauri-drag-region` replaces the drag handle that used to sit
          // in the row -- the whole card is the grip now, which is one control
          // fewer and a much larger target.
          className="w-full flex max-h-full flex-col gap-2 overflow-y-auto p-2"
          data-tauri-drag-region
        >
          <div className="flex w-full flex-row items-center gap-1.5">
            <Button
              size="icon"
              variant="ghost"
              title="Collapse (keeps your text)"
              aria-label="Collapse the prompt"
              onClick={() => setExpanded(false)}
              // Only `busy` still pins the bar open: a run in flight has a
              // cancel button that must stay reachable. Steps and results are
              // collapsible on purpose -- an overlay you cannot put away is
              // the clutter, and `setExpanded(true)` still reopens it when
              // something needs an answer.
              disabled={busy}
              data-testid="overlay-collapse"
            >
              <ChevronLeftIcon className="size-4" />
            </Button>
            <PromptBar
              request={run.request}
              onRequestChange={run.updateRequest}
              onSubmit={submit}
              onCancel={() => {
                // Two different things wear the same button. With a session,
                // cancel is a real request that closes the run to further
                // steps — the actual saving — and answers with what it could
                // not stop. Without one (the first phase, or a one-shot run)
                // all that is available is to stop waiting locally.
                if (steps.status === "running" && doors.restart) {
                  void cancelRunOnEngine();
                  return;
                }
                if (steps.status === "running") steps.cancel();
                else run.cancel();
              }}
              // Whether the button can do more than stop this client waiting.
              // The first phase of a step run has no session id until it
              // returns, so nothing can be addressed and the label must not
              // pretend otherwise.
              cancelReaches={doors.restart}
              canStart={run.canStart && steps.status !== "running"}
              busy={busy}
              hidden={isHidden}
              engine={run.engine}
              baseUrl={run.baseUrl}
              stepMode={stepMode}
              onStepModeChange={changeStepMode}
              summary={summaryMode}
              onSummaryChange={changeSummaryMode}
              awaitingApproval={
                steps.status === "waiting" || steps.status === "error" || steps.status === "done"
              }
              onTranscript={
                steps.status === "waiting" || steps.status === "error" || steps.status === "done"
                  ? // Speech is the least deliberate input there is, so it may
                    // select a stage and never fire one.
                    (text) => void interpret(text)
                  : undefined
              }
              nextAction={
                steps.available[0] ? STEP_DESCRIPTORS[steps.available[0]].action : null
              }
              wake={wake}
            />
            {/* Dashboard sits between the ⏎ and the handle: the strip's own
                controls (field, mic, ⏎) are one group, and the two things
                that are not about this sentence — the other window, and
                moving the window — bracket it. Order is fixed by Pat, and
                it is the same left-to-right story in both bar sizes.

                It is deliberately NOT beside the collapse chevron. Grouping
                it with collapse put two unrelated jobs (fold the strip, open
                another window) under one visual heading, and pushed the
                field a control further from the left edge. */}
            <Button
              size="icon"
              variant="ghost"
              title="Open the review window"
              onClick={openDashboard}
              data-testid="open-dashboard"
            >
              <LayoutDashboardIcon className="size-4" />
            </Button>
            {/* The handle stays. `data-tauri-drag-region` on the Card only
                grabs where the Card itself is the element under the cursor --
                the 8px padding and the gaps between blocks -- and in the
                expanded form the children cover nearly all of it, which left
                the window unmovable. */}
            <DragButton />
          </div>

          {deskCaption ? (
            <p
              // `line-clamp-3` is load-bearing now that the height is a constant:
              // `overlay-size.ts` sizes this state for three lines, and a caption
              // the model wrote four lines long would be clipped rather than
              // capped. Capping is visible; clipping is not.
              className="kv-settle line-clamp-3 px-1 text-[11px] leading-tight text-muted-foreground"
              data-testid="desk-caption"
              role="status"
            >
              {deskCaption}
            </p>
          ) : null}

          {engineDown ? (
            // A degraded condition is never an eight pixel circle. Nothing on
            // this strip can run while the engine is unreachable, so the strip
            // says so at the width of the window.
            <div
              className="kv-settle flex items-center justify-between gap-2 rounded-md bg-destructive px-2 py-1.5 text-destructive-foreground"
              data-testid="engine-down-reason"
              role="status"
            >
              <span className="line-clamp-2 min-w-0 text-[11px] leading-tight">
                <span className="font-medium">Engine unreachable</span> at{" "}
                <span className="font-mono">{run.baseUrl}</span>. Start it with{" "}
                <span className="font-mono">PORT=8081 python -m service.app</span>
              </span>
              <Button
                size="sm"
                variant="secondary"
                className="shrink-0"
                onClick={run.engine.recheck}
                disabled={run.engine.checking}
                data-testid="engine-retry"
              >
                {run.engine.checking ? "Checking…" : "Retry"}
              </Button>
            </div>
          ) : null}

          {/* The run options, out of the strip and into the card.
              `RunOptions` is not app settings — it is the solver budget, the
              datasheets, the step-by-step switch and the summary mode, and
              nothing in the dashboard renders it today. So removing the gear
              from the row could not simply delete it: it moved one level down,
              where a form belongs, behind a line of text rather than an icon.
              Closed by default, so the resting card is unchanged. */}
          {optionsOpen ? (
            <div
              className="kv-settle flex flex-col gap-2 rounded-md border border-input/50 p-2"
              data-testid="run-options-panel"
            >
              <RunOptions
                request={run.request}
                onChange={run.updateRequest}
                disabled={busy}
                stepMode={stepMode}
                onStepModeChange={changeStepMode}
                summary={summaryMode}
                onSummaryChange={changeSummaryMode}
              />
            </div>
          ) : null}

          <div className="flex justify-end">
            <Button
              size="sm"
              variant="ghost"
              className="h-6 px-1 text-[11px] text-muted-foreground"
              onClick={() => setOptionsOpen((open) => !open)}
              data-testid="run-options-disclosure"
              aria-expanded={optionsOpen}
              title="Solver budget, datasheets, step-by-step and how the finished run reports back."
            >
              {optionsOpen ? "Hide run options" : "Run options"}
            </Button>
          </div>

          {held ? (
            // The sentence, in the engineer's own words, and what will happen
            // to it. It renders in both modes — the step panel is not mounted
            // for a one-shot run, and this is the block that must never be the
            // one that disappears, because it is holding text somebody typed.
            <div
              className="kv-settle flex flex-col gap-1 rounded-md border border-input/50 px-2 py-1.5"
              data-testid="held-request"
              data-state={held.state}
              role="status"
            >
              <p className="line-clamp-3 min-w-0 text-[11px] leading-tight">
                heard <span className="italic">“{held.text}”</span>
              </p>

              {/* The service's sentence, verbatim. It composes this out of
                  facts the client does not have — whether a step was under
                  the session lock, which background jobs are still going —
                  and every rewording reads rosier than the truth. */}
              {lifecycle ? (
                <p
                  className="text-[11px] text-muted-foreground"
                  data-testid="lifecycle-headline"
                  data-kind={lifecycle.kind}
                >
                  {lifecycle.headline}
                  {lifecycle.detail ? (
                    <span className="block" data-testid="lifecycle-detail">
                      {lifecycle.detail}
                    </span>
                  ) : null}
                </p>
              ) : null}

              <div className="flex flex-wrap items-center gap-1.5">
                {/* The one door inside this run, and it exists at exactly one
                    gate: `case` is the only step in the whole step API with a
                    free-text field. It is offered only where the service would
                    accept it, and what it promises comes back from the service
                    (`applies.at_step`) rather than being claimed here. */}
                {doors.caseStep && !receipt ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={sending}
                    onClick={() => void noteHeld("case")}
                    data-testid="held-note-case"
                    title="Records this for the case step, which reads it when you press Case. It does not change the circuit or the placement, and it spends nothing now."
                  >
                    Use it for the case
                  </Button>
                ) : null}

                {/* What `applies.at_step` licenses, and only when the service
                    said it: a note with a stage that will actually read it. */}
                {receipt?.applies.at_step ? (
                  <span
                    className="text-[11px] text-muted-foreground"
                    data-testid="held-applies"
                    data-step={receipt.applies.at_step}
                  >
                    {receipt.applies.when}
                  </span>
                ) : null}

                {held.state === "offered" || doors.restart ? (
                  <Button
                    size="sm"
                    disabled={sending}
                    onClick={() => void startHeld()}
                    data-testid="held-start"
                    // The one control here that spends. It says so, and it
                    // says what stopping the current run does not stop.
                    title={
                      busy
                        ? "Stops this run and starts a new one from your request plus this note. Work already under way still finishes and is discarded. This calls the model and costs money."
                        : "Starts a new board from your request plus this note — this calls the model and costs money."
                    }
                  >
                    Start over with this
                  </Button>
                ) : (
                  <span className="text-[11px] text-muted-foreground" data-testid="held-waiting">
                    Held here — this run has no session yet, so nothing can be
                    attached to it and nothing is spent.
                  </span>
                )}

                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    // The way to change their mind, and the way to edit: the
                    // sentence goes back into the field rather than being
                    // deleted, so "not that" never costs them their typing.
                    // A note already recorded on the engine stays recorded —
                    // this clears the strip, not the run's transcript, and
                    // saying otherwise would be the one lie available here.
                    run.updateRequest({ intent: held.text });
                    setHeld(null);
                    setReceipt(null);
                    setLifecycle(null);
                    setCommandNote(null);
                  }}
                  data-testid="held-drop"
                  title="Puts the sentence back in the field. Nothing is spent. A note already recorded on the run stays recorded."
                >
                  Not that
                </Button>
              </div>
            </div>
          ) : null}

          {commandNote && !stepsActive ? (
            <div
              className="kv-settle flex items-center justify-between gap-2 rounded-md bg-muted/60 px-2 py-1.5"
              data-testid="ada-caption"
              role="status"
            >
              {/* Four lines, matching this state's constant. The longest copy in
                  the file is the visibility guard's sentence, which wraps to
                  three at 600px; the fourth is headroom, not an invitation. */}
              <span className="line-clamp-4 min-w-0 text-[11px] leading-tight">{commandNote}</span>
              <Button
                size="sm"
                variant="ghost"
                className="shrink-0"
                onClick={() => {
                  setCommandNote(null);
                  safeLocalStorage.removeItem(TOUR_CAPTION_KEY);
                }}
                data-testid="ada-caption-dismiss"
              >
                Dismiss
              </Button>
            </div>
          ) : null}

          {stepsActive ? (
            <div className="kv-settle">
            <StepPanel
              run={steps}
              note={commandNote}
              armed={armed}
              onConfirm={confirmArmed}
              onDisarm={() => setArmed(null)}
              reviewed={reviewed}
              onDismiss={() => {
                setCommandNote(null);
                setArmed(null);
                steps.reset();
              }}
            />
            </div>
          ) : null}

          {/* Delivery used to live here: four rows of email, attendee and
              datetime inputs. Nobody types an attendee list into a 600px
              strip that floats over KiCad, so it moved to the dashboard
              (the ⊞ button), which is where a form belongs. */}

          {busy && !stepsActive ? (
            <div className="kv-settle flex flex-col gap-2 border-t border-input/40 pt-2">
              <RunProgress
                stages={run.stages}
                elapsedS={run.elapsedS}
                onCancel={() => run.cancel()}
              />
              {/* The raw line feed moved to the dashboard console, which is
                  the full debugging surface and always was. The clock and the
                  cancel button are what a floating strip owes a running job. */}
            </div>
          ) : null}

          {run.status === "done" && run.result ? (
            <div className="kv-settle relative border-t border-input/40 pt-2">
              {/* The speaker mute is in the mic menu, and was here too --
                  two switches writing one preference. One is enough. */}
              {unreviewed ? (
                // The critic was asked and answered nothing (or was skipped):
                // the empty finding list it left is not a clean board. Said
                // here in the failure tone, and the summary below is handed
                // the result without that list, so it says "no review" rather
                // than "reported nothing".
                <p
                  className="mb-2 text-[11px] font-medium text-destructive"
                  data-testid="review-failed"
                  data-status={run.result.review?.status}
                >
                  {`${unreviewed[0].toUpperCase()}${unreviewed.slice(1)}.`}
                </p>
              ) : null}
              {/* The summary and the Save button moved to the dashboard,
                  which opens itself when a run finishes. What stays here is
                  the part that is bad news -- the unreviewed warning above --
                  because an error the engineer has to see must not depend on
                  another window being in front. */}
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate text-[11px] text-muted-foreground">
                  Board ready. Open the dashboard to read it.
                </span>
                <div className="flex shrink-0 items-center gap-1">
                  <Button size="sm" variant="secondary" onClick={openDashboard} data-testid="result-open-review">
                    Open
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => run.reset()} data-testid="result-new-run">
                    New run
                  </Button>
                </div>
              </div>
            </div>
          ) : null}

          {run.status === "error" && run.error ? (
            <div className="kv-settle border-t border-input/40 pt-2">
              <RunFailure
                error={run.error}
                baseUrl={run.baseUrl}
                onRetry={submit}
                onDismiss={() => run.reset()}
              />
            </div>
          ) : null}

          {run.status === "cancelled" ? (
            <div className="kv-settle flex items-center justify-between gap-2 border-t border-input/40 pt-2">
              <span className="text-[11px] text-muted-foreground">
                Run cancelled. The engine may have finished the work it had
                already started, but nothing came back.
              </span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => run.reset()}
                data-testid="cancelled-dismiss"
              >
                Dismiss
              </Button>
            </div>
          ) : null}
        </Card>
        </div>
        )}
      </div>
    </ErrorBoundary>
  );
};

export default Kaleo;

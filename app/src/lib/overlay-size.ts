/**
 * The overlay window's size, as a function of which shape the bar is in.
 *
 * This replaces measuring. The old path (`useOverlayHeight`) watched the
 * Card with a `ResizeObserver` and sent one `set_window_height` per animation
 * frame in which the clamped measurement changed — so a live run was a stream
 * of native resizes, each one arriving a frame *after* the content it was
 * meant to make room for, and each one also waking the dock (see
 * `docs/overlay-motion-audit.md` §2.2, §3.2, §3.5).
 *
 * The audit's §4 finding is the whole argument for this module: of the states
 * the overlay actually has, all but two have a height that is knowable from
 * the state alone, because the tall ones already carry a scroll clamp
 * (`ActivityFeed` is `max-h-40`, `step-scroll` is `max-h-[14rem]`). A size
 * that is known before the frame is painted can drive the native resize and a
 * CSS transition off the same number in the same frame; a measured one cannot,
 * because it is by construction one frame late. Every launcher surveyed for
 * this rebuild (coco-app, Albert, Wox, Cerebro, sol) sizes from state.
 *
 * Pure on purpose: no Tauri import, no DOM. `useOverlaySize` is the only thing
 * that talks to the window.
 */

/** The bar's collapsed height; must match `app.height` in tauri.conf.json.
 *  Sized for one row of h-9 controls + card padding + border — the previous
 *  54px left the full bar flush with the window edge and clipped the tops of
 *  the input and Generate button on retina.
 *  `overlay-size.test.ts` reads tauri.conf.json and asserts the two agree, so
 *  the comment is no longer the only thing holding them together. */
export const OVERLAY_COLLAPSED_HEIGHT = 58;
/** The full bar's width; must match `app.width` in tauri.conf.json and window.rs. */
export const OVERLAY_WIDTH = 600;
/** The idle pill can never be narrower than its own controls. */
export const OVERLAY_MIN_WIDTH = 120;
/** Generous ceiling so a long activity feed scrolls instead of eating the screen. */
export const OVERLAY_MAX_HEIGHT = 600;

/** Idle pill: orb + mic + chevron + drag handle, three gaps, padding, borders.
 *  Audit §1.3 state 1. Pinned rather than measured so the pill↔bar swap is not
 *  a measurement race — every pixel of window the pill does not need is a pixel
 *  KiCad gets back (audit §5.5). */
export const OVERLAY_PILL_WIDTH = 132;
/** Listening pill: the chevron is replaced by a `max-w-44` status line
 *  (audit §1.3 state 2). Width-only change; the height is unmoved. */
export const OVERLAY_PILL_LISTENING_WIDTH = 280;

/**
 * The shapes the overlay takes, one per row of the audit's state table that
 * has its own size. Deliberately *exclusive*: `useOverlaySize` is handed one
 * state, so the caller resolves precedence (the audit's own order — steps,
 * then busy, then a settled run, then a banner, then the bare bar) before it
 * calls. A state is not a stack of blocks; if a real combination clips, the
 * fix is a new member here with its own measured constant, not a fallback to
 * measuring, which would put the frame-late resize back.
 *
 * Positional states (docked bottom, drag-pinned) and `hidden` are absent by
 * design. Docking is position, never size (audit §1.3 state 17), and hiding
 * the overlay must keep the last size rather than resize a `display:none`
 * element to 600×58 (audit §1.3 state 19, §3.14) — which it does here for
 * free, because a hidden overlay reports the same state it had.
 */
export type OverlayState =
  /** 1 — the idle pill. */
  | "pill"
  /** 2 — the pill with the mic open or Ada speaking. */
  | "pill-listening"
  /** 3, 4 — the full bar, idle or listening. The listening swap is
   *  height-neutral by construction (`ListeningPanel` is `h-9` like the
   *  `Input` it replaces), which is why there is no separate member. */
  | "bar"
  /** 5 — the desk caption from the wake path, capped at three lines. */
  | "desk-caption"
  /** 6 — the engine-unreachable banner with its Retry button. */
  | "engine-down"
  /** 7 — the Ada caption / command note with its Dismiss. */
  | "ada-caption"
  /** 8, 9 — a one-shot run in flight, feed closed. Sized for the taller of
   *  the two (the "no events yet" note) so the first stage frame is not its
   *  own resize. */
  | "running"
  /** 10 — the same, with the activity feed disclosed. `max-h-40` caps it. */
  | "running-feed"
  /** 11 — the result card. */
  | "result"
  /** 12 — the failure card. */
  | "failure"
  /** 13 — the cancelled row. */
  | "cancelled"
  /** 14, 15 — the step panel, with or without receipts. Content-driven. */
  | "steps"
  /** 16 — the deliver panel stacked under the step panel. Content-driven. */
  | "deliver";

export interface OverlaySize {
  width: number;
  height: number;
}

const bar = (height: number): OverlaySize => ({ width: OVERLAY_WIDTH, height });

/**
 * The window size for each state. Heights are the audit's §4 column, and each
 * one is the *ceiling* of the range the audit measured rather than its
 * midpoint: an overshoot costs a few transparent pixels over KiCad, an
 * undershoot silently clips content, which is the entire bug class this file
 * inherits responsibility for.
 */
export const OVERLAY_SIZES: Readonly<Record<OverlayState, OverlaySize>> =
  Object.freeze({
    pill: { width: OVERLAY_PILL_WIDTH, height: OVERLAY_COLLAPSED_HEIGHT },
    "pill-listening": {
      width: OVERLAY_PILL_LISTENING_WIDTH,
      height: OVERLAY_COLLAPSED_HEIGHT,
    },
    bar: bar(OVERLAY_COLLAPSED_HEIGHT),
    // 58 + gap 8 + three lines at 14. Needs `line-clamp-3` on the paragraph.
    "desk-caption": bar(108),
    // 58 + gap 8 + a 44px banner (32px sm button + py-1.5).
    "engine-down": bar(110),
    // 58 + gap 8 + four lines at 17. The visibility-guard sentence is the
    // longest copy in the page; needs `line-clamp-4`.
    "ada-caption": bar(134),
    // 58 + gap 8 + RunProgress (7 stage rows is a constant) + the two-line
    // "no events yet" note, which is included in the constant so that the
    // note going away is not a resize.
    running: bar(293),
    // …plus the feed's own `max-h-40` (160) and its gap.
    "running-feed": bar(461),
    // Stat grid is two fixed rows; only the findings row varies. Ceiling.
    result: bar(300),
    // Four optional one-to-two-line fields. Ceiling.
    failure: bar(240),
    cancelled: bar(104),
    // The two genuinely content-driven panels start at their floor and are
    // corrected by a measurement; see MEASURED_STATES.
    steps: bar(240),
    deliver: bar(240),
  });

/**
 * The states whose height a constant honestly cannot express: the step panel
 * (seven rows of engine text, plus optional armed/approve/error blocks) and
 * the deliver panel (four rows of engine-written notes and an open-ended
 * agenda). Audit §4: "Must stay measured: 14 and 16."
 *
 * These, and only these, read `sizeFor`'s `contentHeight`. Letting a fixed
 * state fall back to a measurement would reintroduce the frame-late resize
 * everywhere, one state at a time.
 */
export const MEASURED_STATES: readonly OverlayState[] = Object.freeze([
  "steps",
  "deliver",
]);

/** Is this state's height corrected by a measurement? */
export function isMeasured(state: OverlayState): boolean {
  return MEASURED_STATES.includes(state);
}

/**
 * The window size for a state. `contentHeight` is an optional measured
 * fallback for the two content-driven panels; it is ignored for every fixed
 * state, and it can only ever raise the height above that state's floor, up
 * to `OVERLAY_MAX_HEIGHT`.
 *
 * The result is always integral: the Rust side takes a `LogicalSize` and the
 * dock works in physical pixels against an explicit scale factor, so a
 * fractional height would oscillate (audit §5.3).
 */
export function sizeFor(state: OverlayState, contentHeight?: number): OverlaySize {
  const base = OVERLAY_SIZES[state];
  if (!base) {
    // An unknown state must not be a zero-size window. The bar is the safe
    // shape: it is what the window is created as.
    return bar(OVERLAY_COLLAPSED_HEIGHT);
  }
  if (contentHeight === undefined || !Number.isFinite(contentHeight)) {
    return { ...base };
  }
  // The pill is never stacked -- it renders one row of controls and no blocks
  // -- so its height is fixed in both senses and a measurement cannot be
  // evidence about it. Keeping it strictly fixed also keeps the pill<->bar
  // swap a pure width change, which is what makes that transition cheap.
  if (state === "pill" || state === "pill-listening") return { ...base };
  // Otherwise a supplied measurement is honoured for *any* bar state, not only the two in
  // MEASURED_STATES. The card is a flex column and its blocks stack: an
  // engine-down banner sits above a result, a desk caption rides above
  // everything for the session. The table has a target for each block alone
  // and none for their sum, so a stacked card is taller than its dominant
  // block's constant and would clip at the bottom. The caller supplies a
  // height only when it knows the constant may be short (see
  // `overlayStateFor`'s `measured`), and the value can only ever raise the
  // height above the state's floor -- never shrink a fixed state below it.
  const height = Math.max(
    base.height,
    Math.min(OVERLAY_MAX_HEIGHT, Math.ceil(contentHeight))
  );
  return { width: base.width, height };
}

/** True when going from `a` to `b` makes the window smaller on either axis. */
export function isShrink(a: OverlaySize, b: OverlaySize): boolean {
  return b.width < a.width || b.height < a.height;
}

/** The dedupe key `useOverlaySize` remembers: what the window *is*. */
export function sizeKey(size: OverlaySize): string {
  return `${size.width}x${size.height}`;
}

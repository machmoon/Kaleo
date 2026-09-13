"use client";

import * as React from "react";
import * as PopoverPrimitive from "@radix-ui/react-popover";

import { cn } from "@/lib/utils";

function Popover({
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Root>) {
  return <PopoverPrimitive.Root data-slot="popover" {...props} />;
}

function PopoverTrigger({
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Trigger>) {
  return (
    <PopoverPrimitive.Trigger
      data-slot="popover-trigger"
      {...props}
      className={cn(
        props.className,
        "data-[state=open]:bg-primary-foreground data-[state=open]:text-primary data-[state=open]:border-primary/20 data-[state=open]:border-1 transition-all duration-300"
      )}
    />
  );
}

/** Breathing room kept below an open popover so it never sits flush with the
 *  window's bottom edge (the overlay window has no shadow to hide a flush cut). */
const POPOVER_RESERVE_MARGIN = 8;

/**
 * A popover that the overlay window can actually show.
 *
 * The overlay is a native window 58 px tall at rest, and nothing in the page
 * can be seen outside it. A popover portalled to `document.body` — Radix's
 * default — is a sibling of the overlay card, so it is outside every subtree
 * anything measures: no measurement grows the window for it, and the panel is
 * cut off at the window's edge (`position: fixed` escapes the root's
 * `overflow: hidden`, but nothing escapes the window). Both of this app's
 * popovers are `w-96`/`w-80` panels hanging off that 58 px bar, so both were
 * effectively invisible.
 *
 * Two things fix that, and both are needed:
 *
 * 1. **Portal into the measured subtree.** `container` is the zero-sized
 *    `popover-reserve` span rendered right here, which is inside the overlay
 *    card that `useContentHeight` observes (`pages/kaleo/index.tsx`).
 *    Positioning is unaffected — Radix's popper is `position: fixed` and placed
 *    from the anchor's viewport rect, not from its DOM parent — and z-order is
 *    unaffected for the same reason (the popper wrapper carries the content's
 *    own z-index and the card creates no stacking context).
 * 2. **Reserve its height.** Because the popper is `fixed`, being in the
 *    subtree still contributes nothing to `scrollHeight`. The reserve span is
 *    `position: relative` so it is a containing block, and holds one
 *    absolutely-positioned, zero-width strut as tall as the popover reaches
 *    below it. That overflow propagates into the card's `scrollHeight`, which
 *    is exactly the number `useContentHeight` reports, so the height asked for
 *    is the popover's extent and no more, and it goes away when it closes. The
 *    strut is 1 px wide and `pointer-events: none`, so it never widens the bar
 *    or eats a click meant for KiCad underneath.
 *
 * **Half of this is not yet wired, and it is not wired here.** Since the
 * sizing rebuild (`lib/overlay-size.ts`), the window's size comes from a state
 * table and a measurement is consulted only for the states
 * `overlayStateFor` marks `measured` (the step and deliver panels). There is
 * no popover state, so with the bar in any fixed state the reservation above
 * is measured and then ignored. Finishing it is one decision in that lane, not
 * in this file: either a `popover` member in `OVERLAY_SIZES`, or — the smaller
 * change the note at `index.tsx`'s `OverlaySizing` already proposes — let
 * `sizeFor` honour a `contentHeight` whenever one is supplied, and have the
 * page supply one while a popover is open.
 *
 * `avoidCollisions` defaults to **false** here, which is the opposite of
 * Radix's default and deliberate: collision avoidance measures the *viewport*,
 * and this viewport is a 58 px window that is about to be resized to fit. Left
 * on, floating-ui shoves the panel into that sliver, the reserve measures the
 * shoved position, and the window grows by the wrong amount — the measurement
 * feeding back into the thing it measures. Off, the placement is exactly
 * "below the trigger" every time and the reserve is a plain subtraction.
 */
function PopoverContent({
  className,
  align = "center",
  sideOffset = 4,
  avoidCollisions = false,
  ref,
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Content>) {
  const [reserve, setReserve] = React.useState<HTMLSpanElement | null>(null);
  const [content, setContent] = React.useState<HTMLDivElement | null>(null);
  const [reserved, setReserved] = React.useState(0);

  React.useEffect(() => {
    if (!reserve || !content) {
      setReserved(0);
      return;
    }
    let frame = 0;
    const measure = () => {
      frame = 0;
      // How far below the reserve span the popover reaches, in the window's
      // own coordinates. Measured rather than assumed: the panel's height is
      // its content's, and `sideOffset`/`side` are the caller's.
      const needed = Math.ceil(
        content.getBoundingClientRect().bottom -
          reserve.getBoundingClientRect().top +
          POPOVER_RESERVE_MARGIN
      );
      setReserved(Number.isFinite(needed) ? Math.max(0, needed) : 0);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      // One measurement per frame: the resize this can lead to is a native
      // window call, and the overlay's sizing lane pays for every one of them.
      if (frame) return;
      frame = requestAnimationFrame(measure);
    });
    observer.observe(content);
    return () => {
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, [reserve, content]);

  return (
    <>
      <span
        ref={setReserve}
        data-slot="popover-reserve"
        aria-hidden="true"
        style={{ position: "relative", display: "block", width: 0, height: 0 }}
      >
        {reserved > 0 ? (
          <span
            data-slot="popover-strut"
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: 1,
              height: reserved,
              pointerEvents: "none",
            }}
          />
        ) : null}
      </span>
      <PopoverPrimitive.Portal container={reserve ?? undefined}>
        <PopoverPrimitive.Content
          data-slot="popover-content"
          align={align}
          sideOffset={sideOffset}
          avoidCollisions={avoidCollisions}
          id="popover-content"
          ref={(node: HTMLDivElement | null) => {
            setContent(node);
            if (typeof ref === "function") ref(node);
            else if (ref) ref.current = node;
          }}
          className={cn(
            "mt-1 shadow-none bg-popover/90 backdrop-blur-3xl text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 w-72 origin-(--radix-popover-content-transform-origin) rounded-xl border p-4 outline-hidden",
            className
          )}
          {...props}
        />
      </PopoverPrimitive.Portal>
    </>
  );
}

function PopoverAnchor({
  ...props
}: React.ComponentProps<typeof PopoverPrimitive.Anchor>) {
  return <PopoverPrimitive.Anchor data-slot="popover-anchor" {...props} />;
}

export { Popover, PopoverTrigger, PopoverContent, PopoverAnchor };

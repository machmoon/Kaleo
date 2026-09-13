// Pure geometry for the board well. No React, no DOM, no side effects.
//
// COORDINATE FRAMES. The service resolves rotation server-side and sends every
// rectangle in the solver's Y-up frame, min-corner first. SVG is Y-down. The
// flip from one to the other happens EXACTLY ONCE in this app, and it is
// `flipTransform` below — a single group transform the renderer puts on the
// <g> that wraps the board content. It is deliberately not per-rectangle
// arithmetic: arithmetic scattered across a renderer is how a mirrored board
// ends up looking plausible. Nothing downstream may flip again.
//
// THE TEXT TRAP. Anything drawn inside the flipped group is mirrored,
// including glyphs — silkscreen reference labels would render upside down.
// So labels must be drawn OUTSIDE that group, in the un-flipped SVG frame,
// using the positions this module already resolved (`PartGeometry.label`, or
// `toSvgPoint` for anything else). Do not "fix" mirrored text with a second
// local scale(1,-1); that is the same flip twice in different places, which is
// the bug this file exists to prevent.

import type { PlacedPart, Placements, RectMm } from "./types";

/** A rectangle in the form SVG wants: min-corner plus size. */
export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

/** Breathing room around the board edge, in mm, so the outline is not clipped. */
export const DEFAULT_MARGIN_MM = 2;

export interface PadGeometry {
  number: string;
  net: string | null;
  /** Y-up, absolute mm. Draw inside the flipped group. */
  rect: Rect;
}

export interface PartGeometry {
  ref: string;
  part: PlacedPart;
  /** Y-up, absolute mm. Draw inside the flipped group. */
  courtyard: Rect;
  pads: PadGeometry[];
  /** Courtyard centre, Y-up. */
  center: Point;
  /**
   * Where to put the reference label — already in the UN-FLIPPED SVG frame,
   * because text may not live inside the flipped group. See the text trap
   * above.
   */
  label: Point;
}

export interface BoardGeometry {
  /** Board size in mm, as the service reported it (or derived, see below). */
  width: number;
  height: number;
  margin: number;
  /** The board rectangle itself, Y-up — draw it inside the flipped group. */
  board: Rect;
  /** viewBox rect, already in the SVG frame (the flip preserves it). */
  extent: Rect;
  /** `extent` formatted for the `viewBox` attribute. */
  viewBox: string;
  /** The one and only Y flip. Put this on the <g> holding board content. */
  flipTransform: string;
  parts: PartGeometry[];
  byRef: Map<string, PartGeometry>;
}

export function rectFromMm(r: RectMm): Rect {
  const [x, y, w, h] = r;
  // Min-corner-first is the contract, but a zero/negative size would collapse
  // the shape silently, so normalise rather than trusting it blindly.
  return {
    x: w < 0 ? x + w : x,
    y: h < 0 ? y + h : y,
    width: Math.abs(w),
    height: Math.abs(h),
  };
}

export function rectCenter(r: Rect): Point {
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
}

export function partCourtyard(part: PlacedPart): Rect {
  return rectFromMm(part.courtyard_mm);
}

export function padRects(part: PlacedPart): PadGeometry[] {
  return (part.pads ?? []).map((pad) => ({
    number: pad.number,
    net: pad.net ?? null,
    rect: rectFromMm(pad.rect_mm),
  }));
}

/** Courtyard centre in the Y-up frame. */
export function partCenter(part: PlacedPart): Point {
  return rectCenter(partCourtyard(part));
}

/**
 * Board size. `board_mm` is the authority; a missing or non-positive value
 * falls back to the parts' bounding box so a partial response still draws
 * something honest instead of collapsing to a zero-size viewBox.
 */
export function boardSize(placements: Placements): { width: number; height: number } {
  const [w, h] = placements.board_mm ?? [0, 0];
  if (w > 0 && h > 0) return { width: w, height: h };

  let maxX = 0;
  let maxY = 0;
  for (const part of placements.parts ?? []) {
    const c = partCourtyard(part);
    maxX = Math.max(maxX, c.x + c.width);
    maxY = Math.max(maxY, c.y + c.height);
  }
  return { width: Math.max(w > 0 ? w : maxX, 0), height: Math.max(h > 0 ? h : maxY, 0) };
}

/** viewBox rect for a board of this size, with margin on all four sides. */
export function boardExtent(
  placements: Placements,
  margin: number = DEFAULT_MARGIN_MM,
): Rect {
  const { width, height } = boardSize(placements);
  return {
    x: -margin,
    y: -margin,
    width: width + margin * 2,
    height: height + margin * 2,
  };
}

export function viewBoxString(extent: Rect): string {
  return `${extent.x} ${extent.y} ${extent.width} ${extent.height}`;
}

/**
 * The single Y flip, as a group transform: y_svg = height - y_up.
 *
 * `translate(0,H) scale(1,-1)` also means a rect keeps its own `y`/`height`
 * attributes exactly as the service sent them (min-corner-first, Y-up) — the
 * transform turns the bottom edge into the correct SVG top edge on its own.
 */
export function flipTransform(height: number): string {
  return `translate(0,${height}) scale(1,-1)`;
}

/** Map a Y-up point into the un-flipped SVG frame. For text and overlays. */
export function toSvgPoint(p: Point, boardHeight: number): Point {
  return { x: p.x, y: boardHeight - p.y };
}

export function buildBoardGeometry(
  placements: Placements,
  margin: number = DEFAULT_MARGIN_MM,
): BoardGeometry {
  const { width, height } = boardSize(placements);
  const parts: PartGeometry[] = (placements.parts ?? []).map((part) => {
    const courtyard = partCourtyard(part);
    const center = rectCenter(courtyard);
    return {
      ref: part.ref,
      part,
      courtyard,
      pads: padRects(part),
      center,
      label: toSvgPoint(center, height),
    };
  });

  const byRef = new Map<string, PartGeometry>();
  for (const p of parts) {
    // First wins: duplicate refs are an engine-side error, and silently
    // overwriting would make the later one un-highlightable.
    if (!byRef.has(p.ref)) byRef.set(p.ref, p);
  }

  const extent = boardExtent(placements, margin);
  return {
    width,
    height,
    margin,
    board: { x: 0, y: 0, width, height },
    extent,
    viewBox: viewBoxString(extent),
    flipTransform: flipTransform(height),
    parts,
    byRef,
  };
}

export function partByRef(
  geometry: BoardGeometry,
  ref: string,
): PartGeometry | undefined {
  return geometry.byRef.get(ref);
}

export interface Viewport {
  width: number;
  height: number;
}

export interface Fit {
  /** Pixels per mm. */
  scale: number;
  /** Rendered size in pixels, aspect preserved. */
  width: number;
  height: number;
  /** Offset in pixels to centre the rendered box inside the viewport. */
  offsetX: number;
  offsetY: number;
}

/**
 * Fit an extent into a pixel viewport, preserving aspect ratio and centring
 * the result. Equivalent to `preserveAspectRatio="xMidYMid meet"`, exposed as
 * numbers for callers that need to size a wrapper or place HTML overlays.
 */
export function fitToViewport(extent: Rect, viewport: Viewport): Fit {
  const safe = (n: number) => (Number.isFinite(n) && n > 0 ? n : 0);
  const ew = safe(extent.width);
  const eh = safe(extent.height);
  const vw = safe(viewport.width);
  const vh = safe(viewport.height);
  if (ew === 0 || eh === 0 || vw === 0 || vh === 0) {
    return { scale: 0, width: 0, height: 0, offsetX: 0, offsetY: 0 };
  }
  const scale = Math.min(vw / ew, vh / eh);
  const width = ew * scale;
  const height = eh * scale;
  return {
    scale,
    width,
    height,
    offsetX: (vw - width) / 2,
    offsetY: (vh - height) / 2,
  };
}

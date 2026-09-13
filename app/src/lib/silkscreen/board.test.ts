import { describe, expect, it } from "vitest";

import {
  boardExtent,
  boardSize,
  buildBoardGeometry,
  fitToViewport,
  flipTransform,
  partByRef,
  partCenter,
  partCourtyard,
  padRects,
  rectFromMm,
  toSvgPoint,
  viewBoxString,
} from "./board";
import type { Placements } from "./types";

/**
 * The flip is checked against an INDEPENDENT implementation: this parses the
 * transform string the module emits the way a renderer/browser would, and
 * applies it as a 2x3 matrix. A test written in terms of board.ts's own maths
 * would share its blind spot, which is the house rule for this bug class.
 */
function applyTransform(transform: string, p: { x: number; y: number }) {
  // [a c e; b d f]
  let m = [1, 0, 0, 1, 0, 0];
  const mul = (n: number[]) => {
    const [a, b, c, d, e, f] = m;
    const [a2, b2, c2, d2, e2, f2] = n;
    m = [
      a * a2 + c * b2,
      b * a2 + d * b2,
      a * c2 + c * d2,
      b * c2 + d * d2,
      a * e2 + c * f2 + e,
      b * e2 + d * f2 + f,
    ];
  };
  const re = /(translate|scale)\(\s*([-\d.eE]+)\s*[, ]\s*([-\d.eE]+)\s*\)/g;
  let match: RegExpExecArray | null;
  let seen = 0;
  while ((match = re.exec(transform)) !== null) {
    seen += 1;
    const x = Number(match[2]);
    const y = Number(match[3]);
    mul(match[1] === "translate" ? [1, 0, 0, 1, x, y] : [x, 0, 0, y, 0, 0]);
  }
  expect(seen).toBeGreaterThan(0);
  const [a, b, c, d, e, f] = m;
  return { x: a * p.x + c * p.y + e, y: b * p.x + d * p.y + f };
}

const placements: Placements = {
  board_mm: [40, 25],
  frame: "solver-y-up",
  parts: [
    {
      ref: "U1",
      footprint: "SOIC-8",
      value: "LDO",
      layer: "F.Cu",
      rotated: false,
      x_mm: 10,
      y_mm: 5,
      courtyard_mm: [8, 3, 6, 4],
      pads: [
        { number: "1", net: "VIN", rect_mm: [8.2, 3.2, 1, 0.6] },
        { number: "2", net: null, rect_mm: [8.2, 5.2, 1, 0.6] },
      ],
    },
    {
      ref: "C1",
      footprint: "0603",
      value: "1uF",
      layer: "F.Cu",
      rotated: true,
      x_mm: 30,
      y_mm: 20,
      courtyard_mm: [29, 19, 2, 1.5],
      pads: [{ number: "1", net: "GND", rect_mm: [29, 19, 0.8, 1.5] }],
    },
  ],
};

describe("rect conversion", () => {
  it("keeps a min-corner-first rect as-is", () => {
    expect(rectFromMm([1, 2, 3, 4])).toEqual({ x: 1, y: 2, width: 3, height: 4 });
  });

  it("normalises a negative-size rect instead of collapsing it", () => {
    expect(rectFromMm([5, 5, -3, -4])).toEqual({ x: 2, y: 1, width: 3, height: 4 });
  });
});

describe("the single Y flip", () => {
  const height = 25;

  it("maps y_up to height - y_up", () => {
    // Hand-computed, not derived from the module.
    expect(applyTransform(flipTransform(height), { x: 7, y: 0 })).toEqual({ x: 7, y: 25 });
    expect(applyTransform(flipTransform(height), { x: 7, y: 25 })).toEqual({ x: 7, y: 0 });
    expect(applyTransform(flipTransform(height), { x: 7, y: 10 })).toEqual({ x: 7, y: 15 });
  });

  it("leaves x untouched — a mirrored board is the bug this guards", () => {
    const a = applyTransform(flipTransform(height), { x: 0, y: 12 });
    const b = applyTransform(flipTransform(height), { x: 40, y: 12 });
    expect(a.x).toBe(0);
    expect(b.x).toBe(40);
  });

  it("turns a Y-up rect's own y/height into the correct SVG top edge", () => {
    // U1's courtyard is [8, 3, 6, 4] in Y-up: it spans y 3..7 from the bottom.
    // In a 25 mm board that is 18..22 from the top, so the SVG top edge is 18.
    const t = flipTransform(height);
    const bottomLeft = applyTransform(t, { x: 8, y: 3 });
    const topRight = applyTransform(t, { x: 14, y: 7 });
    expect(bottomLeft).toEqual({ x: 8, y: 22 });
    expect(topRight).toEqual({ x: 14, y: 18 });
    // Which is exactly the rect attributes the renderer passes through
    // untouched: y=3, height=4, drawn inside the flipped group.
    const c = partCourtyard(placements.parts[0]);
    expect(c).toEqual({ x: 8, y: 3, width: 6, height: 4 });
    expect(applyTransform(t, { x: c.x, y: c.y + c.height }).y).toBe(18);
  });

  it("is an involution — applying it twice returns the original point", () => {
    const t = flipTransform(height);
    const once = applyTransform(t, { x: 3, y: 9 });
    expect(applyTransform(t, once)).toEqual({ x: 3, y: 9 });
  });

  it("agrees with toSvgPoint", () => {
    const p = { x: 11, y: 4 };
    expect(toSvgPoint(p, height)).toEqual(applyTransform(flipTransform(height), p));
  });
});

describe("board geometry", () => {
  it("reads the board size from board_mm", () => {
    expect(boardSize(placements)).toEqual({ width: 40, height: 25 });
  });

  it("falls back to the parts' bounding box when board_mm is missing", () => {
    const partial = { ...placements, board_mm: [0, 0] as [number, number] };
    // C1's courtyard reaches x 29+2=31, y 19+1.5=20.5.
    expect(boardSize(partial)).toEqual({ width: 31, height: 20.5 });
  });

  it("puts an even margin around the board in the viewBox", () => {
    expect(boardExtent(placements, 2)).toEqual({ x: -2, y: -2, width: 44, height: 29 });
    expect(viewBoxString(boardExtent(placements, 2))).toBe("-2 -2 44 29");
  });

  it("exposes courtyards, pads and centres in the Y-up frame", () => {
    const g = buildBoardGeometry(placements);
    const u1 = partByRef(g, "U1")!;
    expect(u1.courtyard).toEqual({ x: 8, y: 3, width: 6, height: 4 });
    expect(u1.center).toEqual({ x: 11, y: 5 });
    expect(partCenter(placements.parts[0])).toEqual({ x: 11, y: 5 });
    expect(u1.pads.map((p) => p.rect.x)).toEqual([8.2, 8.2]);
    expect(u1.pads[1].net).toBeNull();
    expect(padRects(placements.parts[1])).toHaveLength(1);
  });

  it("resolves label positions in the UN-flipped frame so text is not mirrored", () => {
    const g = buildBoardGeometry(placements);
    const u1 = partByRef(g, "U1")!;
    // Centre is y=5 up from the bottom of a 25 mm board => 20 down from the top.
    expect(u1.label).toEqual({ x: 11, y: 20 });
    const c1 = partByRef(g, "C1")!;
    expect(c1.label).toEqual({ x: 30, y: 25 - 19.75 });
  });

  it("emits a flip transform matching the board height", () => {
    const g = buildBoardGeometry(placements);
    expect(g.flipTransform).toBe("translate(0,25) scale(1,-1)");
    expect(g.board).toEqual({ x: 0, y: 0, width: 40, height: 25 });
  });

  it("looks parts up by ref and misses cleanly", () => {
    const g = buildBoardGeometry(placements);
    expect(partByRef(g, "C1")?.part.value).toBe("1uF");
    expect(partByRef(g, "R99")).toBeUndefined();
  });

  it("survives an empty or part-less placements object", () => {
    const empty: Placements = { board_mm: [0, 0], frame: "solver-y-up", parts: [] };
    const g = buildBoardGeometry(empty);
    expect(g.parts).toEqual([]);
    expect(g.width).toBe(0);
    expect(g.flipTransform).toBe("translate(0,0) scale(1,-1)");
  });
});

describe("fitToViewport", () => {
  it("preserves aspect ratio and centres a wide board", () => {
    // extent 44 x 29 into 440 x 440: scale is limited by width => 10 px/mm.
    const fit = fitToViewport(boardExtent(placements, 2), { width: 440, height: 440 });
    expect(fit.scale).toBeCloseTo(10);
    expect(fit.width).toBeCloseTo(440);
    expect(fit.height).toBeCloseTo(290);
    expect(fit.offsetX).toBeCloseTo(0);
    expect(fit.offsetY).toBeCloseTo(75);
  });

  it("is limited by height when the viewport is wide", () => {
    const fit = fitToViewport({ x: 0, y: 0, width: 40, height: 20 }, { width: 800, height: 100 });
    expect(fit.scale).toBeCloseTo(5);
    expect(fit.width).toBeCloseTo(200);
    expect(fit.offsetX).toBeCloseTo(300);
    expect(fit.offsetY).toBeCloseTo(0);
  });

  it("returns a zero fit rather than NaN for a degenerate input", () => {
    expect(fitToViewport({ x: 0, y: 0, width: 0, height: 0 }, { width: 10, height: 10 })).toEqual({
      scale: 0,
      width: 0,
      height: 0,
      offsetX: 0,
      offsetY: 0,
    });
  });
});

// Adversarial inputs for the board geometry module.
//
// `board.test.ts` proves the flip against an independent matrix evaluation;
// this file feeds the module the responses a buggy or truncated service could
// send — zero and negative board sizes, parts hanging off the outline,
// inverted rectangles, duplicate refs, empty pads — and checks it degrades
// honestly instead of collapsing or lying. Every expected coordinate is
// hand-computed from the input numbers, never derived by calling the module
// under test.

import { describe, expect, it } from "vitest";
import {
  boardExtent,
  boardSize,
  buildBoardGeometry,
  fitToViewport,
  padRects,
  rectFromMm,
  toSvgPoint,
} from "./board";
import type { PlacedPart, Placements, RectMm } from "./types";

function part(
  ref: string,
  courtyard: RectMm,
  pads: { number: string; net?: string | null; rect_mm: RectMm }[] = []
): PlacedPart {
  return {
    ref,
    footprint: `lib:${ref}`,
    value: null,
    layer: "F.Cu",
    rotated: false,
    x_mm: courtyard[0],
    y_mm: courtyard[1],
    courtyard_mm: courtyard,
    pads: pads.map((p) => ({ number: p.number, net: p.net ?? null, rect_mm: p.rect_mm })),
  };
}

function placements(board: [number, number], parts: PlacedPart[]): Placements {
  return { board_mm: board, frame: "solver-y-up", parts };
}

describe("degenerate board sizes", () => {
  it("zero board_mm falls back to the parts' bounding box", () => {
    const p = placements(
      [0, 0],
      [part("U1", [2, 3, 4, 5]), part("C1", [10, 1, 2, 2])]
    );
    // Hand-computed: U1 reaches x=6,y=8; C1 reaches x=12,y=3.
    expect(boardSize(p)).toEqual({ width: 12, height: 8 });
  });

  it("negative board_mm also falls back rather than emitting a negative viewBox", () => {
    const p = placements([-10, -20], [part("U1", [1, 1, 3, 3])]);
    expect(boardSize(p)).toEqual({ width: 4, height: 4 });
  });

  it("one negative axis invalidates the pair — width and height come from the same authority", () => {
    // A half-valid size would mix the service's claim with the derived one.
    const p = placements([50, -1], [part("U1", [0, 0, 4, 2])]);
    const size = boardSize(p);
    expect(size.height).toBe(2);
    // The valid axis is still preferred over the (smaller) derived extent.
    expect(size.width).toBe(50);
  });

  it("zero board and zero parts yields a zero size, and the extent is margin-only", () => {
    const p = placements([0, 0], []);
    expect(boardSize(p)).toEqual({ width: 0, height: 0 });
    expect(boardExtent(p, 2)).toEqual({ x: -2, y: -2, width: 4, height: 4 });
  });

  it("a missing board_mm (truncated response) still derives from parts", () => {
    const p = {
      frame: "solver-y-up",
      parts: [part("U1", [0, 0, 5, 4])],
    } as unknown as Placements;
    expect(boardSize(p)).toEqual({ width: 5, height: 4 });
  });

  it("a missing parts array yields an empty geometry, not a crash", () => {
    const p = { board_mm: [10, 10], frame: "solver-y-up" } as unknown as Placements;
    const g = buildBoardGeometry(p);
    expect(g.parts).toEqual([]);
    expect(g.width).toBe(10);
  });
});

describe("parts outside the outline", () => {
  it("keeps an off-board part drawable and does not stretch the board to cover it", () => {
    const p = placements([10, 10], [part("U1", [12, -3, 4, 4])]);
    const g = buildBoardGeometry(p);
    // The board is what the service said it is…
    expect(g.board).toEqual({ x: 0, y: 0, width: 10, height: 10 });
    // …and the stray part keeps its own (off-board) coordinates.
    expect(g.parts[0].courtyard).toEqual({ x: 12, y: -3, width: 4, height: 4 });
    // Label lands in the SVG frame: centre (14, -1) -> y_svg = 10 - (-1) = 11.
    expect(g.parts[0].label).toEqual({ x: 14, y: 11 });
  });
});

describe("inverted and degenerate rectangles", () => {
  it("normalises a negative width/height to the true min corner", () => {
    // Max-corner-first [5,7] with extents [-2,-3]: the real min corner is (3,4).
    expect(rectFromMm([5, 7, -2, -3])).toEqual({ x: 3, y: 4, width: 2, height: 3 });
  });

  it("a zero-size rectangle survives as a zero-size rect", () => {
    expect(rectFromMm([4, 4, 0, 0])).toEqual({ x: 4, y: 4, width: 0, height: 0 });
  });

  it("an inverted pad still lands where hand arithmetic says", () => {
    const p = placements(
      [20, 10],
      [
        part("U1", [5, 5, 4, 2], [
          { number: "1", net: "GND", rect_mm: [8, 6, -1, -0.5] },
        ]),
      ]
    );
    const g = buildBoardGeometry(p);
    expect(g.parts[0].pads[0].rect).toEqual({ x: 7, y: 5.5, width: 1, height: 0.5 });
  });
});

describe("rotated parts — expectations from independent arithmetic", () => {
  // The service resolves rotation before sending, so `rotated: true` changes
  // NOTHING geometrically here: the module must pass the resolved rectangles
  // through untouched, flip once in the group transform, and place the label
  // in the un-flipped frame. Every number below is worked out by hand from the
  // input rectangle and the board height, without calling board.ts.
  const board: [number, number] = [30, 20];
  const rotated: PlacedPart = {
    ...part(
      "U2",
      // A 6 x 4 courtyard whose min corner sits at (10, 12).
      [10, 12, 6, 4],
      [
        { number: "1", net: "VCC", rect_mm: [11, 13, 1, 2] },
        { number: "2", net: "GND", rect_mm: [14, 13, 1, 2] },
      ]
    ),
    rotated: true,
  };

  it("passes resolved rectangles through untouched", () => {
    const g = buildBoardGeometry(placements(board, [rotated]));
    const u2 = g.byRef.get("U2")!;
    expect(u2.courtyard).toEqual({ x: 10, y: 12, width: 6, height: 4 });
    expect(u2.pads.map((p) => p.rect)).toEqual([
      { x: 11, y: 13, width: 1, height: 2 },
      { x: 14, y: 13, width: 1, height: 2 },
    ]);
  });

  it("centre and label: centre is Y-up, label is flipped exactly once", () => {
    const g = buildBoardGeometry(placements(board, [rotated]));
    const u2 = g.byRef.get("U2")!;
    // Centre by hand: (10 + 6/2, 12 + 4/2) = (13, 14) in Y-up.
    expect(u2.center).toEqual({ x: 13, y: 14 });
    // Label by hand: y_svg = boardHeight - y_up = 20 - 14 = 6.
    expect(u2.label).toEqual({ x: 13, y: 6 });
  });

  it("the flip transform is the board height's, not the extent's", () => {
    const g = buildBoardGeometry(placements(board, [rotated]));
    expect(g.flipTransform).toBe("translate(0,20) scale(1,-1)");
    // Applying it by hand to the pad's min corner (11,13):
    // y' = 20 - 13 = 7; a renderer then draws the rect upward from there.
    expect(toSvgPoint({ x: 11, y: 13 }, 20)).toEqual({ x: 11, y: 7 });
  });
});

describe("duplicate refs and identity", () => {
  it("keeps both parts drawable but the first wins the ref index", () => {
    const first = part("C3", [0, 0, 2, 2]);
    const second = part("C3", [8, 8, 2, 2]);
    const g = buildBoardGeometry(placements([10, 10], [first, second]));
    expect(g.parts).toHaveLength(2);
    expect(g.byRef.size).toBe(1);
    expect(g.byRef.get("C3")?.courtyard).toEqual({ x: 0, y: 0, width: 2, height: 2 });
  });
});

describe("empty and missing pads", () => {
  it("a part with no pads renders with an empty pad list", () => {
    const g = buildBoardGeometry(placements([10, 10], [part("J1", [1, 1, 3, 3])]));
    expect(g.parts[0].pads).toEqual([]);
  });

  it("a part whose pads field is absent entirely does not crash padRects", () => {
    const { pads: _dropped, ...rest } = part("J2", [1, 1, 2, 2]);
    const bare = rest as PlacedPart;
    expect(padRects(bare)).toEqual([]);
    const g = buildBoardGeometry(placements([10, 10], [bare]));
    expect(g.parts[0].pads).toEqual([]);
  });
});

describe("fitToViewport degenerate inputs", () => {
  it("zero or negative extents and viewports collapse to an all-zero fit", () => {
    const zero = { scale: 0, width: 0, height: 0, offsetX: 0, offsetY: 0 };
    expect(fitToViewport({ x: 0, y: 0, width: 0, height: 4 }, { width: 100, height: 100 })).toEqual(zero);
    expect(fitToViewport({ x: 0, y: 0, width: 4, height: -4 }, { width: 100, height: 100 })).toEqual(zero);
    expect(fitToViewport({ x: 0, y: 0, width: 4, height: 4 }, { width: 0, height: 100 })).toEqual(zero);
    expect(fitToViewport({ x: 0, y: 0, width: 4, height: 4 }, { width: 100, height: Number.NaN })).toEqual(zero);
  });

  it("hand-computed fit: 10x5 extent into 100x100 viewport", () => {
    const fit = fitToViewport({ x: 0, y: 0, width: 10, height: 5 }, { width: 100, height: 100 });
    // scale = min(100/10, 100/5) = 10; rendered 100x50; centred vertically.
    expect(fit).toEqual({ scale: 10, width: 100, height: 50, offsetX: 0, offsetY: 25 });
  });
});

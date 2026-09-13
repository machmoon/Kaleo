// @vitest-environment jsdom
//
// The pill <-> bar swap: the one ordering rule that makes it smooth.
//
// The window resize and the content swap are two things on two clocks — an
// `invoke` that only *queues* a native resize, and a React commit that paints
// immediately. Left unordered, an expand paints the 600 px bar against a
// ~132 px viewport (the bar is `w-full`, i.e. `100vw`, and the webview is
// exactly as wide as the window), so the field collapses, the row clips, and
// everything reflows a frame or two later. No easing curve hides a reflow.
//
// `hasRoomFor`/`useShownShape` are the ordering: grow, then swap. This file
// pins that, and the motion.css block below pins the numbers the JS half
// shares with the CSS half — the same guard `overlay-size.test.ts` puts on
// `tauri.conf.json`, for the same reason: two copies of one number drift.
//
// The page module is imported for these, which drags the whole overlay's
// import graph in; the mocks are only there to keep that graph off Tauri.

import { readFileSync } from "node:fs";
import path from "node:path";

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({
    setSize: vi.fn(),
    setPosition: vi.fn(),
    onMoved: vi.fn(async () => () => {}),
    onResized: vi.fn(async () => () => {}),
    scaleFactor: vi.fn(async () => 1),
    outerSize: vi.fn(async () => ({ width: 600, height: 58 })),
  }),
  currentMonitor: vi.fn(async () => null),
  availableMonitors: vi.fn(async () => []),
}));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/plugin-fs", () => ({ readFile: vi.fn(), writeFile: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: vi.fn(async () => null) }));

import {
  OVERLAY_PILL_WIDTH,
  OVERLAY_WIDTH,
  type OverlayState,
} from "@/lib/overlay-size";
import {
  SHAPE_EXIT_MS,
  SHAPE_ROOM_TIMEOUT_MS,
  hasRoomFor,
  needsGhost,
  useShownShape,
} from "./index";

/** Pretend the webview is this many CSS pixels wide, and say so. */
function viewport(width: number) {
  Object.defineProperty(window, "innerWidth", {
    value: width,
    configurable: true,
    writable: true,
  });
}

function resizeTo(width: number) {
  act(() => {
    viewport(width);
    window.dispatchEvent(new Event("resize"));
  });
}

const original = window.innerWidth;
afterEach(() => {
  viewport(original);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("hasRoomFor", () => {
  it("says a bar does not fit a viewport the width of the pill", () => {
    expect(hasRoomFor("bar", OVERLAY_PILL_WIDTH)).toBe(false);
  });

  it("says it fits once the viewport is the window's own width", () => {
    expect(hasRoomFor("bar", OVERLAY_WIDTH)).toBe(true);
  });

  // The Rust side takes a LogicalSize and `innerWidth` is rounded, so an exact
  // `>=` can miss by a pixel on a scaled display — and would then hold every
  // single swap open for the whole deadline.
  it("allows one pixel of slack for a fractional device pixel ratio", () => {
    expect(hasRoomFor("bar", OVERLAY_WIDTH - 1)).toBe(true);
    expect(hasRoomFor("bar", OVERLAY_WIDTH - 2)).toBe(false);
  });

  // Collapsing is not gated, and the reason is a layout fact rather than the
  // shrink delay: the pill's wrapper is `w-fit`, so it lays out at its three
  // controls' intrinsic width and no viewport can reflow it. That exemption
  // has to hold even at widths the pill's own size table would refuse — the
  // listening pill is 280 wide in that table, and holding it back would put a
  // wait on a change that has no content in it at all (`CompactBar` draws the
  // same three controls either way).
  it("never holds up a pill: `w-fit` does not resolve against the viewport", () => {
    expect(hasRoomFor("pill", OVERLAY_WIDTH)).toBe(true);
    expect(hasRoomFor("pill-listening", OVERLAY_WIDTH)).toBe(true);
    expect(hasRoomFor("pill-listening", OVERLAY_PILL_WIDTH)).toBe(true);
  });

  // A measurement that cannot be trusted must not be able to hide the bar.
  it("answers yes rather than blocking when the viewport is unknowable", () => {
    expect(hasRoomFor("bar", Number.NaN)).toBe(true);
    expect(hasRoomFor("bar", 0)).toBe(true);
  });
});

// The outgoing surface is held on screen only where its absence would leave
// bare window behind it. Both shapes are opaque concentric cards, so holding
// one under a *larger* arriving card adds coverage nobody asked for inside the
// smaller one's outline — a denser capsule floating in the middle of the
// prompt field, which is what it looked like on screen before this rule
// existed.
describe("needsGhost", () => {
  it("holds the bar's surface while the pill lands inside it", () => {
    expect(needsGhost("bar", "pill")).toBe(true);
    expect(needsGhost("running", "pill")).toBe(true);
    expect(needsGhost("bar", "pill-listening")).toBe(true);
  });

  it("holds nothing on the way out to the bar: the bar already covers it", () => {
    expect(needsGhost("pill", "bar")).toBe(false);
    expect(needsGhost("pill-listening", "bar")).toBe(false);
    expect(needsGhost("pill", "running")).toBe(false);
  });

  // A block landing mid-run is an arrival, not a substitution; `.kv-settle`
  // covers it on the way in and there is nothing to hold.
  it("ignores everything that is not a pill<->bar crossing", () => {
    expect(needsGhost("bar", "running")).toBe(false);
    expect(needsGhost("running", "result")).toBe(false);
    expect(needsGhost("pill", "pill-listening")).toBe(false);
    expect(needsGhost("bar", "bar")).toBe(false);
  });
});

describe("useShownShape", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("swaps at once when the viewport already has the room", () => {
    viewport(OVERLAY_WIDTH);
    const { result, rerender } = renderHook(
      ({ state }: { state: OverlayState }) => useShownShape(state),
      { initialProps: { state: "pill" as OverlayState } }
    );
    expect(result.current).toBe("pill");
    act(() => rerender({ state: "bar" }));
    expect(result.current).toBe("bar");
  });

  // The whole point: the bar is not drawn into a window that cannot hold it.
  it("holds the pill on screen until the window has grown", () => {
    viewport(OVERLAY_PILL_WIDTH);
    const { result, rerender } = renderHook(
      ({ state }: { state: OverlayState }) => useShownShape(state),
      { initialProps: { state: "pill" as OverlayState } }
    );
    act(() => rerender({ state: "bar" }));
    expect(result.current).toBe("pill");

    // A resize that does not finish the job is not the signal.
    resizeTo(300);
    expect(result.current).toBe("pill");

    resizeTo(OVERLAY_WIDTH);
    expect(result.current).toBe("bar");
  });

  // `invoke("set_window_frame")` can reject, in which case no resize event is
  // ever coming. A bar clipped to the pill's width is bad; a bar that never
  // arrives is worse, so the deadline resolves in that direction — and says so
  // rather than failing silently, which is what the audit's whole "the run felt
  // like nothing happened" bug class looks like from the outside.
  it("gives up loudly and shows the bar anyway if the room never arrives", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    viewport(OVERLAY_PILL_WIDTH);
    const { result, rerender } = renderHook(
      ({ state }: { state: OverlayState }) => useShownShape(state),
      { initialProps: { state: "pill" as OverlayState } }
    );
    act(() => rerender({ state: "bar" }));
    expect(result.current).toBe("pill");

    act(() => {
      vi.advanceTimersByTime(SHAPE_ROOM_TIMEOUT_MS - 1);
    });
    expect(result.current).toBe("pill");

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe("bar");
    expect(warn).toHaveBeenCalled();
  });

  // Collapsing back out of a held expand must not wait on anything: the target
  // is a pill and a pill fits every viewport the overlay has.
  it("lets a collapse through immediately even mid-wait", () => {
    viewport(OVERLAY_PILL_WIDTH);
    const { result, rerender } = renderHook(
      ({ state }: { state: OverlayState }) => useShownShape(state),
      { initialProps: { state: "pill" as OverlayState } }
    );
    act(() => rerender({ state: "bar" }));
    expect(result.current).toBe("pill");
    act(() => rerender({ state: "pill-listening" }));
    expect(result.current).toBe("pill-listening");
  });

  it("stops listening for room once it is unmounted", () => {
    viewport(OVERLAY_PILL_WIDTH);
    const remove = vi.spyOn(window, "removeEventListener");
    const { rerender, unmount } = renderHook(
      ({ state }: { state: OverlayState }) => useShownShape(state),
      { initialProps: { state: "pill" as OverlayState } }
    );
    act(() => rerender({ state: "bar" }));
    unmount();
    expect(remove).toHaveBeenCalledWith("resize", expect.any(Function));
  });
});

// The JS half of the swap and the CSS half share two facts. `SHAPE_EXIT_MS` is
// how long the outgoing surface stays mounted and `--kv-dur-enter` is how long
// it is given to fade; if they drift, the ghost is either cut off mid-fade or
// left on screen at zero opacity holding a stale size. And the pair of eases
// has to be mirrored, which is the arithmetic in motion.css §2 — two
// fast-falling curves leave a hole in an opaque card and the desktop flashes
// through it.
describe("the motion vocabulary this swap is written against", () => {
  const css = readFileSync(path.resolve(__dirname, "../../motion.css"), "utf8");

  it("keeps SHAPE_EXIT_MS in step with --kv-dur-enter", () => {
    const declared = /--kv-dur-enter:\s*(\d+)ms/.exec(css);
    expect(declared).not.toBeNull();
    expect(Number(declared?.[1])).toBe(SHAPE_EXIT_MS);
  });

  it("gives the two halves of the dissolve one clock and mirrored eases", () => {
    const inRule = /\.kv-shape-in\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    const outRule = /\.kv-shape-out\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(inRule).toContain("var(--kv-dur-enter)");
    expect(outRule).toContain("var(--kv-dur-enter)");
    expect(inRule).toContain("var(--kv-ease-opacity)");
    expect(outRule).toContain("var(--kv-ease-opacity-out)");
  });

  // A substitution is two elements laid out in the same place, so an offset
  // makes them cross *through* each other — motion.css §1's own rule, which
  // §2 used to break with a translateX(-4px) left over from the era when
  // `set_size` was anchored at the window's top-left.
  it("moves nothing during the swap: the two shapes share one centred slot", () => {
    const inRule = /\.kv-shape-in\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(inRule).not.toMatch(/translate|scale|--kv-ease-move/);
  });

  // Reduced motion disables the vocabulary rather than shortening it, and a
  // class that is not in both opt-out blocks keeps animating for the one user
  // who explicitly asked it not to.
  it("switches the new class off under both opt-outs", () => {
    expect(css).toMatch(/prefers-reduced-motion[\s\S]*?\.kv-shape-out,/);
    expect(css).toContain('[data-motion="still"] .kv-shape-out');
    expect(css).toContain('.kv-shape-out[data-motion="still"]');
  });
});

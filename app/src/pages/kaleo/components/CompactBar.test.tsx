// @vitest-environment jsdom
//
// The idle pill: three controls in one order, and the one rule it must keep —
// a transcript goes to the draft, never to a submit.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
// The pill's microphone is the one voice control (it is also the "Hey Ada"
// mute), stubbed here so the pill's own rules are what is under test.
vi.mock("./VoiceControl", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./VoiceControl")>()),
  VoiceControl: ({ onTranscript }: { onTranscript: (t: string) => void }) => (
    <button data-testid="voice-start" onClick={() => onTranscript("a 3.3V LDO")}>
      mic
    </button>
  ),
}));
// "Am I talking" is published by the speech layer rather than passed in, so
// the one test that needs it drives this flag rather than a real utterance.
const voice = vi.hoisted(() => ({ speaking: false }));
vi.mock("@/hooks/useIsSpeaking", () => ({ useIsSpeaking: () => voice.speaking }));

import { CompactBar } from "./CompactBar";

const props = {
  baseUrl: "http://127.0.0.1:8081",
  hidden: false,
  onExpand: () => {},
  onTranscript: () => {},
};

afterEach(() => cleanup());

describe("CompactBar", () => {
  // The layout Pat specified, in his order: arrow, mic, handle. This test is
  // the guard on the thing that keeps going wrong — the pill accumulating a
  // fourth control (a status dot, an animated orb, a stop button) until it
  // stops being a pill. There is no text field and no submit here at all.
  it("is exactly three controls — arrow, mic, drag handle — in that order", () => {
    render(<CompactBar {...props} />);

    const row = screen.getByTestId("compact-bar");
    const controls = Array.from(row.querySelectorAll("button"));
    expect(controls).toHaveLength(3);
    expect(controls[0].getAttribute("data-testid")).toBe("overlay-expand");
    expect(controls[1].getAttribute("data-testid")).toBe("voice-start");

    // Nothing that belongs to the full bar leaks into the pill.
    expect(screen.queryByTestId("prompt-input")).toBeNull();
    expect(screen.queryByTestId("prompt-submit")).toBeNull();
    // And no health indicator: engine state is a banner in the card, never a
    // permanent shape at the head of the strip. See PromptBar.tsx's opening
    // comment for where it went and why.
    expect(screen.queryByTestId("engine-status")).toBeNull();
  });

  it("the arrow expands and the mic hands its transcript up, and neither submits", () => {
    const onExpand = vi.fn();
    const onTranscript = vi.fn();
    render(<CompactBar {...props} onExpand={onExpand} onTranscript={onTranscript} />);
    fireEvent.click(screen.getByTestId("overlay-expand"));
    expect(onExpand).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("voice-start"));
    expect(onTranscript).toHaveBeenCalledWith("a 3.3V LDO");
  });

  // The regression this file exists for. A listening sentence used to take
  // the arrow's slot while the microphone was open, which pushed the mic and
  // the handle along the row and back again every time the ear opened — the
  // moving-control complaint in its smallest form. The arrow is now an arrow
  // in every state, and what the microphone is doing, the microphone draws.
  it("keeps the same three controls in the same order while the mic is open", () => {
    const { rerender } = render(<CompactBar {...props} />);
    const order = () =>
      Array.from(screen.getByTestId("compact-bar").querySelectorAll("button")).map((b) =>
        b.getAttribute("data-testid")
      );
    const idle = order();

    rerender(<CompactBar {...props} wake={{ enabled: true, listening: true } as never} />);
    expect(order()).toEqual(idle);
    expect(screen.getByTestId("overlay-expand")).toBeTruthy();
  });

  it("keeps them in the same order while I am talking, too", () => {
    voice.speaking = true;
    try {
      render(<CompactBar {...props} wake={{ enabled: true, listening: false } as never} />);
      const controls = Array.from(
        screen.getByTestId("compact-bar").querySelectorAll("button")
      );
      expect(controls).toHaveLength(3);
      expect(controls[0].getAttribute("data-testid")).toBe("overlay-expand");
      expect(controls[1].getAttribute("data-testid")).toBe("voice-start");
    } finally {
      voice.speaking = false;
    }
  });

  // The motion vocabulary is applied by name (motion.css), never by an inline
  // duration, so what is worth pinning is *which* class the root carries: the
  // shape class that arrives when the bar collapses. A regression here would
  // be someone reaching for a one-off `transition-*` utility instead.
  it("carries the motion vocabulary: shape-in on the pill", () => {
    render(<CompactBar {...props} />);
    expect(screen.getByTestId("compact-bar").classList.contains("kv-shape-in")).toBe(true);
  });

  // Only opacity and transform may be animated: the overlay Card is a
  // backdrop-filter surface over live KiCad pixels, and anything else forces a
  // re-rasterise of the blur every frame. The pill must not carry a width,
  // filter or shadow transition of its own.
  it("animates nothing but opacity and transform", () => {
    render(<CompactBar {...props} />);
    const classes = screen.getByTestId("compact-bar").className;
    expect(classes).not.toMatch(/\btransition-/);
    expect(classes).not.toMatch(/\b(?:blur|shadow|duration|ease)-/);
  });
});

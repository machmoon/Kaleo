// @vitest-environment jsdom
//
// The prompt bar in its two moods. Idle it asks for a board; while a stage
// waits for approval it is a reply to that run, and it says so — the field
// and the button are the only place a user could learn that typing "go"
// approves rather than starting a second paid board.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("./VoiceButton", () => ({
  VoiceButton: () => <button data-testid="voice-start">mic</button>,
}));
vi.mock("@/contexts", () => ({ useSilkscreenRun: () => ({ token: undefined }) }));

import type { EngineHealth } from "@/hooks";
import type { RunRequestDraft } from "@/contexts";
import { PromptBar } from "./PromptBar";
import { speaker } from "@/lib/speech";

const engine = {
  ok: true,
  checking: false,
  lastCheckedAt: 1,
  detail: null,
  recheck: vi.fn(),
} as unknown as EngineHealth;

const request = {
  intent: "",
  datasheets: {},
  time_limit_s: 10,
  review: true,
  ground: false,
} as unknown as RunRequestDraft;

function draw(props: Partial<React.ComponentProps<typeof PromptBar>> = {}) {
  return render(
    <PromptBar
      request={request}
      onRequestChange={() => {}}
      onSubmit={() => {}}
      onCancel={() => {}}
      canStart
      busy={false}
      hidden={false}
      engine={engine}
      baseUrl="http://127.0.0.1:8081"
      {...props}
    />
  );
}

afterEach(() => {
  cleanup();
  // The summary control remembers its mode in localStorage; one test's click
  // must not seed the next test's default.
  localStorage.clear();
});

describe("PromptBar", () => {
  // The control is the ⏎ glyph in every state now, so what it *promises*
  // moved from a word inside the button to its accessible name, and that is
  // what these assert. A word could be read off the screen; a glyph cannot,
  // which is why the name is asserted rather than the icon. See
  // `submitReason`, which is where all of this copy lives.
  it("idle, it asks for a board and the control offers to spend, saying so", () => {
    draw({ request: { ...request, intent: "a 3.3V LDO" } });
    expect(screen.getByTestId("prompt-input").getAttribute("placeholder")).toBe(
      "What do you need built?"
    );
    const submit = screen.getByTestId("prompt-submit");
    expect(submit.getAttribute("data-intent")).toBe("start");
    expect(submit.getAttribute("aria-label")).toContain("costs money");
  });

  it("with an empty field it still says what to do about that", () => {
    draw();
    expect(screen.getByTestId("prompt-submit").getAttribute("aria-label")).toBe(
      "Type what you want on the board first"
    );
  });

  it("while a stage waits, it names the reply that approves it and offers to send", () => {
    draw({
      awaitingApproval: true,
      nextAction: "Route copper",
      request: { ...request, intent: "tighten the ground pour" },
    });
    expect(screen.getByTestId("prompt-input").getAttribute("placeholder")).toBe(
      "Say “go” to route copper, or ask for a change"
    );
    const submit = screen.getByTestId("prompt-submit");
    expect(submit.getAttribute("data-intent")).toBe("reply");
    expect(submit.getAttribute("aria-label")).toBe("Send this to the run in progress");
  });

  it("waiting with nothing left to approve, it still invites a sentence", () => {
    // It used to read "Say “start over” for a new board", which is the app
    // naming the one sentence it accepts. Describing a different board is
    // accepted here now, so the placeholder says that instead.
    draw({ awaitingApproval: true, nextAction: null });
    expect(screen.getByTestId("prompt-input").getAttribute("placeholder")).toBe(
      "Ask for a change, or describe a different board"
    );
  });

  // The interaction this change exists for: a sentence typed while a run is in
  // flight is not refused. The field stays live, the control stays live, and
  // it promises to hold rather than to spend.
  it("mid-run, the field is live and the control promises to hold, not to spend", () => {
    draw({
      busy: true,
      canStart: false,
      request: { ...request, intent: "make it 5V instead" },
    });
    const input = screen.getByTestId("prompt-input") as HTMLInputElement;
    expect(input.disabled).toBe(false);
    const submit = screen.getByTestId("prompt-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    expect(submit.getAttribute("data-intent")).toBe("park");
    expect(submit.getAttribute("aria-label")).toContain("nothing is spent");
    expect(submit.getAttribute("aria-label")).not.toContain("costs money");
    // Cancel is still there, and it no longer claims "nothing more is
    // charged": a model call already in flight is paid for whether or not
    // anyone waits for it, and the placement solve runs its full budget.
    // With no session (this case) all it can do is stop this client waiting.
    const cancelButton = screen.getByTestId("prompt-cancel");
    expect(cancelButton.getAttribute("data-reaches")).toBe("false");
    expect(cancelButton.getAttribute("aria-label")).toContain("does not stop the work");
    expect(cancelButton.getAttribute("aria-label")).not.toContain("nothing more is charged");
  });

  it("with a session, cancel says what closing the run does and does not stop", () => {
    draw({ busy: true, canStart: false, cancelReaches: true });
    const label = screen.getByTestId("prompt-cancel").getAttribute("aria-label") ?? "";
    expect(label).toContain("no further step will run");
    // The half that is easy to leave out and is the whole point.
    expect(label).toContain("finishes and is discarded");
  });

  // Positional consistency across the two bar sizes: the pill's arrow is the
  // last control before the drag handle (CompactBar), so the full bar's must
  // be too. Cancel therefore sits *before* it rather than after.
  it("the submit arrow is the last control in the row, busy or not", () => {
    const { container } = draw({ request: { ...request, intent: "a board" } });
    const row = container.firstElementChild!;
    expect(row.lastElementChild?.querySelector("[data-testid='prompt-submit']")).not.toBeNull();

    cleanup();
    const busyRow = draw({ busy: true, canStart: false }).container.firstElementChild!;
    expect(busyRow.lastElementChild?.querySelector("[data-testid='prompt-submit']")).not.toBeNull();
    // …and the cancel is the one before it, not after.
    const controls = Array.from(busyRow.children);
    const cancelAt = controls.findIndex((el) => el.querySelector("[data-testid='prompt-cancel']"));
    const submitAt = controls.findIndex((el) => el.querySelector("[data-testid='prompt-submit']"));
    expect(cancelAt).toBeGreaterThan(-1);
    expect(cancelAt).toBeLessThan(submitAt);
  });

  it("carries no gear: run options left the 58px row", () => {
    // They did not disappear — `RunOptions` is mounted in the card below
    // (index.tsx, `run-options-disclosure`). What left is the icon.
    const { container } = draw();
    expect(screen.queryByTestId("prompt-options-trigger")).toBeNull();
    expect(container.querySelectorAll("button").length).toBeLessThanOrEqual(3);
  });

  it("an unreachable engine is still the reason a start would fail", () => {
    draw({
      request: { ...request, intent: "a 3.3V LDO" },
      engine: { ...engine, ok: false } as unknown as EngineHealth,
    });
    expect(screen.getByTestId("prompt-submit").getAttribute("aria-label")).toBe(
      "The engine is not answering; a run would fail immediately"
    );
  });

  /** Which child of the row the microphone is, counting from the left. */
  function micSlot(container: HTMLElement): number {
    const row = container.firstElementChild!;
    return Array.from(row.children).findIndex((el) =>
      el.matches("[data-testid='voice-control']") ||
      el.querySelector("[data-testid='voice-control']") !== null
    );
  }

  // The founder's ask: hearing my name must not unfold the strip. The field
  // steps out of the way in place, and the mic stays exactly where it was.
  it("while the microphone is open the field steps aside, and the mic does not move", () => {
    const quiet = draw();
    const quietSlot = micSlot(quiet.container);
    expect(quietSlot).toBeGreaterThan(-1);
    cleanup();

    const listening = draw({ wake: { enabled: true, listening: true } as never });
    expect(screen.queryByTestId("prompt-input")).toBeNull();
    expect(screen.getByTestId("prompt-listening").textContent).toContain(
      "I’m listening for “Hey Ada”."
    );
    // No Stop button: the panel is a status. Both things it used to stop
    // live on the microphone beside it (see VoiceControl.test.tsx).
    expect(screen.queryByTestId("prompt-listening-stop")).toBeNull();
    // The founder's explicit ask: the mic is always in the same place. The
    // listening panel stands in the field's own slot, so the count is equal.
    expect(micSlot(listening.container)).toBe(quietSlot);
  });

  it("an unmuted switch with a closed microphone still shows the field", () => {
    // `arming`: the switch is on, the microphone is not open yet. Standing
    // the listening panel up here is the green-dot lie in a new place.
    draw({ wake: { enabled: true, listening: false } as never });
    expect(screen.getByTestId("prompt-input")).toBeTruthy();
    expect(screen.queryByTestId("prompt-listening")).toBeNull();
  });

  it("what was typed survives the swap, both ways", () => {
    const typed = { ...request, intent: "a 3.3V LDO from 5V" } as RunRequestDraft;
    const { rerender } = render(
      <PromptBar
        request={typed}
        onRequestChange={() => {}}
        onSubmit={() => {}}
        onCancel={() => {}}
        canStart
        busy={false}
        hidden={false}
        engine={engine}
        baseUrl="http://127.0.0.1:8081"
      />
    );
    expect(screen.getByTestId("prompt-input").getAttribute("value")).toBe(
      "a 3.3V LDO from 5V"
    );
    const props = {
      request: typed,
      onRequestChange: () => {},
      onSubmit: () => {},
      onCancel: () => {},
      canStart: true,
      busy: false,
      hidden: false,
      engine,
      baseUrl: "http://127.0.0.1:8081",
    };
    rerender(<PromptBar {...props} wake={{ enabled: true, listening: true } as never} />);
    expect(screen.queryByTestId("prompt-input")).toBeNull();
    // …and back, with the draft intact. The field never owned the text.
    rerender(<PromptBar {...props} wake={{ enabled: false, listening: false } as never} />);
    expect(screen.getByTestId("prompt-input").getAttribute("value")).toBe(
      "a 3.3V LDO from 5V"
    );
  });

  it("while I am talking the field stays away, and the line says talking", () => {
    // The mic is shut here — the duck closed it — so a "listening" sentence
    // would be the microphone lie one state over.
    draw({ wake: { enabled: true, listening: false } as never, isSpeaking: () => true });
    expect(screen.queryByTestId("prompt-input")).toBeNull();
    const panel = screen.getByTestId("prompt-listening");
    expect(panel.getAttribute("data-state")).toBe("speaking");
    expect(panel.textContent).toContain("I’m talking — click the mic to stop me.");
    expect(panel.textContent).not.toContain("listening");
  });

  // The Stop button is gone; the microphone is the one control. While the ear
  // is open, clicking it closes the ear -- the same guarantee, one control
  // fewer, and the assertion is unchanged: setEnabled(false), exactly once.
  it("the microphone mutes the ear rather than cancelling anything the run is doing", () => {
    const setEnabled = vi.fn();
    draw({ wake: { enabled: true, listening: true, setEnabled } as never });
    const mic = screen.getByTestId("voice-start");
    expect(mic.getAttribute("data-stops")).toBe("mic");
    fireEvent.click(mic);
    expect(setEnabled).toHaveBeenCalledExactlyOnceWith(false);
  });

  // The founder's report: once I am talking there is no way to shut me up.
  // The panel said "I'm talking" and the one button beside it muted the
  // *microphone* — an organ that is already shut while I speak (the duck) —
  // so the sentence played on and the click did nothing anyone could hear.
  it("the microphone cuts my voice while I am talking, and leaves the ear alone", () => {
    const silence = vi.spyOn(speaker, "stop").mockImplementation(() => {});
    const setEnabled = vi.fn();
    try {
      draw({
        wake: { enabled: true, listening: false, setEnabled } as never,
        isSpeaking: () => true,
      });
      const mic = screen.getByTestId("voice-start");
      expect(mic.getAttribute("data-stops")).toBe("voice");
      fireEvent.click(mic);
      expect(silence).toHaveBeenCalledOnce();
      // The microphone is the mic button's business, not this one's.
      expect(setEnabled).not.toHaveBeenCalled();
    } finally {
      silence.mockRestore();
    }
  });

  // The four summary-mode tests that lived here drove `RunOptions` through the
  // bar's options popover. That popover is gone from the row, and every one of
  // them already exists against `RunOptions` itself — prose default, memory
  // across a remount, a parent-owned choice changing back out, and freezing
  // while a run is in flight are `RunOptions.test.tsx` lines 42, 51, 69 and
  // 79. Nothing was weakened to make this file pass; the assertions moved
  // house before this change, not because of it.

  it("nothing on the bar calls itself an assistant or a helper", () => {
    const { container } = draw({ awaitingApproval: true, nextAction: "Place parts" });
    expect(container.innerHTML.toLowerCase()).not.toMatch(/assistant|helper/);
  });
});

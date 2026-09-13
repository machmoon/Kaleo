// @vitest-environment jsdom
//
// The Spotlight skin's whole claim is what it does *not* draw. So most of
// this file is absence: at rest there is one field and nothing else, and the
// control that spends money is missing rather than greyed in every state
// where pressing it would be invalid. The rest pins the keyboard, because a
// Spotlight clone that cannot be driven from the keys is a search box.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EngineHealth } from "@/hooks";
import type { CommandItem } from "@/lib/commands";
import { SpotlightSkin } from "./SpotlightSkin";

function health(patch: Partial<EngineHealth> = {}): EngineHealth {
  return {
    baseUrl: "http://127.0.0.1:8081",
    ok: true,
    detail: "",
    checking: false,
    lastCheckedAt: 1,
    recheck: vi.fn(),
    ...patch,
  };
}

function command(patch: Partial<CommandItem> = {}): CommandItem {
  return {
    id: "step:route",
    group: "Next",
    label: "Route copper",
    detail: "in pcbnew",
    cost: "1 call",
    paid: true,
    danger: false,
    arms: true,
    key: "↵",
    phrases: ["route it"],
    ...patch,
  };
}

function draw(props: Partial<React.ComponentProps<typeof SpotlightSkin>> = {}) {
  const merged = {
    value: "",
    onChange: () => {},
    onSubmit: () => {},
    engine: health(),
    baseUrl: "http://127.0.0.1:8081",
    autoFocus: false,
    ...props,
  };
  return render(<SpotlightSkin {...merged} />);
}

afterEach(cleanup);

describe("what the skin shows when there is no reason to show anything", () => {
  it("is one field and nothing else at rest", () => {
    // The brief this skin exists for: "the overlay isnt meant to have so much
    // visual clutter". A toolbar, a row of chips or a status strip here is
    // the thing that was rejected.
    const { container } = draw({ value: "" });
    expect(screen.getByTestId("prompt-input")).toBeTruthy();
    expect(container.querySelectorAll("input")).toHaveLength(1);
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.queryByTestId("spotlight-busy")).toBeNull();
  });

  it("says nothing about an engine that is answering", () => {
    // A permanent green dot beside a search field is decoration: it reports
    // the state nobody needs told. The marker appearing is the news.
    draw({ engine: health({ ok: true, lastCheckedAt: 5 }) });
    expect(screen.queryByTestId("spotlight-engine")).toBeNull();
  });

  it("grows no list for a field with only whitespace in it", () => {
    draw({ value: "   ", results: [command()] });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("grows a list only once something is typed", () => {
    draw({ value: "route", results: [command()] });
    expect(screen.getByRole("listbox")).toBeTruthy();
    expect(screen.getByTestId("spotlight-row-step:route")).toBeTruthy();
  });

  it("keeps the list to the commands that actually match", () => {
    draw({
      value: "route",
      engine: health({ ok: false, lastCheckedAt: 5 }),
      results: [
        command(),
        command({
          id: "reveal",
          label: "Reveal board.kicad_pcb",
          detail: "Finder",
          cost: null,
          paid: false,
          phrases: [],
        }),
      ],
    });
    expect(screen.getByTestId("spotlight-row-step:route")).toBeTruthy();
    expect(screen.queryByTestId("spotlight-row-reveal")).toBeNull();
  });
});

describe("the control that spends money", () => {
  it("offers the run, priced, once a sentence is typed and the engine answers", () => {
    draw({ value: "a 3.3V LDO board" });
    expect(screen.getByTestId("spotlight-row-run").textContent).toContain("Generate a board");
    expect(screen.getByTestId("spotlight-cost-run").textContent).toBe("1 call");
  });

  it("is absent, not greyed, while nothing is typed", () => {
    draw({ value: "" });
    expect(screen.queryByTestId("spotlight-row-run")).toBeNull();
  });

  it("is absent, not greyed, while the engine has never answered", () => {
    // The failure: a run offered against an engine nobody has reached, which
    // fails the instant it is pressed and still looked like an offer.
    draw({ value: "a buck converter", engine: health({ ok: false, lastCheckedAt: null }) });
    expect(screen.queryByTestId("spotlight-row-run")).toBeNull();
  });

  it("is absent, not greyed, while a paid run is already in flight", () => {
    // The failure: pressing Enter twice buys two boards.
    draw({ value: "a buck converter", busy: true });
    expect(screen.queryByTestId("spotlight-row-run")).toBeNull();
  });

  it("is absent while the overlay is hidden by the global shortcut", () => {
    draw({ value: "a buck converter", hidden: true });
    expect(screen.queryByTestId("spotlight-row-run")).toBeNull();
  });

  it("never renders a disabled or greyed control in any state", () => {
    // Rule 6 stated as the property rather than case by case: a disabled row
    // still reads as an offer, so this skin must contain none.
    for (const props of [
      { value: "" },
      { value: "a board", busy: true },
      { value: "a board", engine: health({ ok: false, lastCheckedAt: 9, detail: "refused" }) },
      { value: "route", results: [command()] },
    ]) {
      const { container, unmount } = draw(props);
      expect(container.querySelectorAll("[disabled]")).toHaveLength(0);
      expect(container.querySelectorAll('[aria-disabled="true"]')).toHaveLength(0);
      unmount();
    }
  });

  it("fills exactly one price badge — the row Enter would spend on", () => {
    // Two things that look like the money control in one frame is the state
    // this guards: the user cannot tell which one Enter buys.
    const { container } = draw({ value: "route", results: [command()] });
    const filled = container.querySelectorAll('[data-testid^="spotlight-cost-"].bg-primary');
    expect(filled).toHaveLength(1);
    expect(filled[0].getAttribute("data-testid")).toBe("spotlight-cost-run");
  });

  it("marks a free command as unpaid so it cannot be mistaken for the run", () => {
    draw({
      value: "reveal",
      engine: health({ ok: false, lastCheckedAt: 5 }),
      results: [
        command({ id: "reveal", label: "Reveal board", cost: null, paid: false, arms: false }),
      ],
    });
    const row = screen.getByTestId("spotlight-row-reveal");
    expect(row.getAttribute("data-paid")).toBe("false");
    expect(screen.queryByTestId("spotlight-cost-reveal")).toBeNull();
  });
});

describe("driving it from the keyboard", () => {
  const rows = () => [command(), command({ id: "step:place", label: "Route and place" })];

  it("starts with the first row highlighted", () => {
    draw({ value: "route", results: rows() });
    expect(screen.getByTestId("spotlight-row-run").getAttribute("data-active")).toBe("true");
  });

  it("moves the highlight down and back up with the arrow keys", () => {
    draw({ value: "route", results: rows() });
    const field = screen.getByTestId("prompt-input");
    fireEvent.keyDown(field, { key: "ArrowDown" });
    expect(screen.getByTestId("spotlight-row-step:route").getAttribute("data-active")).toBe("true");
    fireEvent.keyDown(field, { key: "ArrowUp" });
    expect(screen.getByTestId("spotlight-row-run").getAttribute("data-active")).toBe("true");
  });

  it("stops at the ends rather than wrapping round", () => {
    draw({ value: "route", results: rows() });
    const field = screen.getByTestId("prompt-input");
    fireEvent.keyDown(field, { key: "ArrowUp" });
    expect(screen.getByTestId("spotlight-row-run").getAttribute("data-active")).toBe("true");
    for (let i = 0; i < 5; i += 1) fireEvent.keyDown(field, { key: "ArrowDown" });
    expect(screen.getByTestId("spotlight-row-step:place").getAttribute("data-active")).toBe("true");
  });

  it("takes the highlighted command on Enter, and never the run", () => {
    const onSelect = vi.fn();
    const onSubmit = vi.fn();
    draw({ value: "route", results: rows(), onSelect, onSubmit });
    const field = screen.getByTestId("prompt-input");
    fireEvent.keyDown(field, { key: "ArrowDown" });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect.mock.calls[0][0].id).toBe("step:route");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("starts the board on Enter when the run is what is highlighted", () => {
    const onSubmit = vi.fn();
    draw({ value: "a 3.3V LDO board", onSubmit });
    fireEvent.keyDown(screen.getByTestId("prompt-input"), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("does nothing on Enter when there is no row, so an invalid run cannot fire", () => {
    // The absence of the run row has to be the absence of the run, not just
    // of the picture of it.
    const onSubmit = vi.fn();
    const onSelect = vi.fn();
    draw({ value: "a board", busy: true, onSubmit, onSelect });
    fireEvent.keyDown(screen.getByTestId("prompt-input"), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("leaves Shift+Enter alone so a multi-line paste is not a purchase", () => {
    const onSubmit = vi.fn();
    draw({ value: "a 3.3V LDO board", onSubmit });
    fireEvent.keyDown(screen.getByTestId("prompt-input"), { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    draw({ value: "a board", onClose });
    fireEvent.keyDown(screen.getByTestId("prompt-input"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("puts the highlight back on the top row when the query changes", () => {
    // Otherwise Enter takes whatever row now sits at the remembered index —
    // a row the user never looked at, and possibly a paid one.
    const { rerender } = draw({ value: "route", results: rows() });
    fireEvent.keyDown(screen.getByTestId("prompt-input"), { key: "ArrowDown" });
    expect(screen.getByTestId("spotlight-row-step:route").getAttribute("data-active")).toBe("true");
    rerender(
      <SpotlightSkin
        value="route c"
        onChange={() => {}}
        onSubmit={() => {}}
        engine={health()}
        baseUrl="http://127.0.0.1:8081"
        autoFocus={false}
        results={rows()}
      />,
    );
    expect(screen.getByTestId("spotlight-row-run").getAttribute("data-active")).toBe("true");
  });

  it("reports the highlighted row to a screen reader as the field's active option", () => {
    draw({ value: "route", results: rows() });
    const field = screen.getByTestId("prompt-input");
    expect(field.getAttribute("aria-activedescendant")).toBe("spotlight-row-run");
    expect(field.getAttribute("aria-expanded")).toBe("true");
  });
});

describe("the pointer and the keyboard agree", () => {
  it("moves the highlight to the row under the pointer", () => {
    draw({ value: "route", results: [command()] });
    fireEvent.mouseMove(screen.getByTestId("spotlight-row-step:route"));
    expect(screen.getByTestId("spotlight-row-step:route").getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("spotlight-row-run").getAttribute("data-active")).toBe("false");
  });

  it("takes a clicked row", () => {
    const onSelect = vi.fn();
    draw({ value: "route", results: [command()], onSelect });
    fireEvent.click(screen.getByTestId("spotlight-row-step:route"));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });
});

describe("what the engine marker says", () => {
  it("shows an unprobed engine as its own state rather than as failure", () => {
    draw({ engine: health({ ok: false, lastCheckedAt: null }) });
    const mark = screen.getByTestId("spotlight-engine");
    expect(mark.getAttribute("data-online")).toBe("unknown");
    expect(mark.getAttribute("aria-label")).toContain("Checking");
  });

  it("shows an unreachable engine with the reason it gave", () => {
    draw({ engine: health({ ok: false, lastCheckedAt: 7, detail: "connection refused" }) });
    const mark = screen.getByTestId("spotlight-engine");
    expect(mark.getAttribute("data-online")).toBe("false");
    expect(mark.getAttribute("aria-label")).toContain("connection refused");
  });

  it("re-probes when clicked", () => {
    const engine = health({ ok: false, lastCheckedAt: 7 });
    draw({ engine });
    fireEvent.click(screen.getByTestId("spotlight-engine"));
    expect(engine.recheck).toHaveBeenCalledTimes(1);
  });
});

describe("the field itself", () => {
  it("stays a live, focusable field while a run is in flight", () => {
    // `focusPromptIn` looks for `input:not([disabled])`, so disabling the
    // field while busy would break cmd+shift+I exactly when a run is up.
    const { container } = draw({ value: "a board", busy: true });
    expect(container.querySelector("input:not([disabled])")).toBeTruthy();
    expect(screen.getByTestId("spotlight-busy").textContent).toBe("Working…");
  });

  it("does not take focus while the overlay is hidden", () => {
    draw({ value: "", hidden: true, autoFocus: true });
    expect(document.activeElement).not.toBe(screen.getByTestId("prompt-input"));
  });

  it("takes focus when the overlay is up, because the panel opens for typing", () => {
    draw({ value: "", autoFocus: true });
    expect(document.activeElement).toBe(screen.getByTestId("prompt-input"));
  });

  it("reports every keystroke, since the draft lives in the page", () => {
    const onChange = vi.fn();
    draw({ value: "a", onChange });
    fireEvent.change(screen.getByTestId("prompt-input"), { target: { value: "ab" } });
    expect(onChange).toHaveBeenCalledWith("ab");
  });

  it("carries no border, ring or ground of its own — cmdk puts those on the root", () => {
    // The hand-rolled tell `docs/overlay-skins.md` names: PromptBar's
    // `h-9 rounded-md border border-input/50 bg-muted/30` box around the
    // field. The panel owns the radius; the field owns nothing.
    draw({ value: "" });
    const cls = screen.getByTestId("prompt-input").getAttribute("class") ?? "";
    expect(cls).toContain("border-none");
    expect(cls).toContain("outline-none");
    expect(cls).toContain("bg-transparent");
    expect(cls).not.toContain("rounded");
  });
});

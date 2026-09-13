// @vitest-environment jsdom
//
// Pins the one thing that makes the overlay's drag handle a drag handle.
//
// Tauri's `data-tauri-drag-region` does NOT inherit to descendants — the
// injected handler (tauri 2.8.2, src/window/scripts/drag.js, the version
// app/src-tauri/Cargo.lock pins) reads `e.target.getAttribute(...)` on the
// exact mousedown target and nothing else. That is the opposite of Electron's
// `-webkit-app-region: drag`, which does inherit. So the attribute has to sit
// on every element that can become the event target, and a regression here is
// silent: dragging just stops working, with no error anywhere.
//
// The button's own `[&_svg]:pointer-events-none` currently keeps the icon from
// ever being the target, which makes the icon's copy defensive rather than
// load-bearing today — this test pins both so neither half can be dropped on
// the assumption that the other covers it.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DragButton } from "@/components/DragButton";

afterEach(() => cleanup());

describe("DragButton", () => {
  it("puts data-tauri-drag-region on the button and on the icon", () => {
    render(<DragButton />);

    const button = screen.getByRole("button");
    expect(button.hasAttribute("data-tauri-drag-region")).toBe(true);

    const icon = button.querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon!.hasAttribute("data-tauri-drag-region")).toBe(true);
  });

  it("never sets the attribute to the string tauri reads as disabled", () => {
    // `data-tauri-drag-region="false"` is a real opt-out in the handler, and
    // it is a string check, not a truthiness one: React serializes the bare
    // JSX attribute as "true", which the handler accepts. Anything but the
    // literal "false" drags, so this pins the one value that would silently
    // turn the handle off.
    render(<DragButton />);

    const button = screen.getByRole("button");
    const icon = button.querySelector("svg")!;
    expect(button.getAttribute("data-tauri-drag-region")).not.toBe("false");
    expect(icon.getAttribute("data-tauri-drag-region")).not.toBe("false");
  });
});

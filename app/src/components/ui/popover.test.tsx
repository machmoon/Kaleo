// @vitest-environment jsdom
//
// The one property this popover has that Radix's does not: an open panel is
// *inside* the subtree the overlay window measures.
//
// The overlay window is exactly as tall as one observed DOM node
// (`useOverlayHeight`), and at rest that is 58 px. Radix portals to
// `document.body` by default, which is a sibling of that node — so the
// observer never saw the panel, the window was never grown, and a `w-96`
// run-options panel hanging off the bar was clipped away at the window's edge.
// jsdom cannot show that (it has no layout), but it can hold the structural
// half: the panel's DOM parentage, which is what makes it measurable at all.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Popover, PopoverContent, PopoverTrigger } from "./popover";

afterEach(cleanup);

function draw() {
  return render(
    <div data-testid="measured">
      <Popover>
        <PopoverTrigger data-testid="trigger">options</PopoverTrigger>
        <PopoverContent>
          <span data-testid="panel">panel</span>
        </PopoverContent>
      </Popover>
    </div>
  );
}

describe("PopoverContent", () => {
  it("renders the panel inside the measured subtree, not on document.body", () => {
    draw();
    fireEvent.click(screen.getByTestId("trigger"));

    const panel = screen.getByTestId("panel");
    const measured = screen.getByTestId("measured");
    expect(measured.contains(panel)).toBe(true);

    // Specifically inside the reserve span, which is the portal container and
    // the containing block for the strut that gives the panel its height.
    const reserve = measured.querySelector('[data-slot="popover-reserve"]');
    expect(reserve).not.toBeNull();
    expect(reserve!.contains(panel)).toBe(true);
    expect(document.body.contains(measured)).toBe(true);
  });

  it("keeps the reserve span itself out of the layout", () => {
    draw();
    const reserve = screen.getByTestId("measured").querySelector<HTMLElement>(
      '[data-slot="popover-reserve"]'
    )!;
    expect(reserve.style.width).toBe("0px");
    expect(reserve.style.height).toBe("0px");
    // Relative, because the strut is absolutely positioned against it: that is
    // how the panel's height reaches the observed node's scrollHeight.
    expect(reserve.style.position).toBe("relative");
  });

  it("reserves nothing while the popover is closed", () => {
    draw();
    expect(document.querySelector('[data-slot="popover-strut"]')).toBeNull();
  });
});

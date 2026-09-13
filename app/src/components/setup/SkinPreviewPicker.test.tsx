// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import { SKINS } from "@/lib/overlay-skin";
import { SkinPreviewPicker } from "./SkinPreviewPicker";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

const card = (id: string) =>
  screen.getAllByTestId("skin-card").find((el) => el.getAttribute("data-skin") === id) as HTMLElement;

describe("SkinPreviewPicker", () => {
  it("draws one live strip preview per catalogue skin", () => {
    render(<SkinPreviewPicker />);
    for (const skin of SKINS) {
      expect(card(skin.id)).toBeTruthy();
      expect(card(skin.id).querySelector(`[data-testid="strip-preview"][data-skin="${skin.id}"]`)).toBeTruthy();
      expect(card(skin.id).textContent).toContain(skin.summary);
    }
  });

  it("says Not built yet on exactly the unbuilt skins, and never on a built one", () => {
    // The invariant is the badge matching `built`, in BOTH directions. This
    // used to require at least one unbuilt skin, so shipping Spotlight and
    // Orb turned a passing suite red for the one reason that should never
    // fail a test: the feature got finished. It now holds whether the
    // catalogue is all-built, all-unbuilt, or mixed.
    render(<SkinPreviewPicker />);
    for (const skin of SKINS) {
      const badge = card(skin.id).querySelector('[data-testid="skin-unbuilt"]');
      expect(Boolean(badge)).toBe(!skin.built);
      if (badge) expect(badge.textContent).toBe("Not built yet");
    }
  });

  it("lets any skin be picked, built or not", () => {
    // An unbuilt skin still stores: the preference is real and survives to
    // the build that implements it. A picker that refused would silently
    // drop a choice the user made.
    render(<SkinPreviewPicker />);
    for (const skin of SKINS) {
      fireEvent.click(card(skin.id));
      expect(localStorage.getItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN)).toBe(skin.id);
    }
  });

  it("stores the choice where the overlay reads it and reports it", () => {
    const onChange = vi.fn();
    render(<SkinPreviewPicker onChange={onChange} />);
    fireEvent.click(card("terminal"));
    expect(localStorage.getItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN)).toBe("terminal");
    expect(onChange).toHaveBeenCalledWith("terminal");
    expect(card("terminal").getAttribute("aria-checked")).toBe("true");
    expect(card("plain").getAttribute("aria-checked")).toBe("false");
  });

  it("moves with the arrow keys as one radiogroup", () => {
    render(<SkinPreviewPicker />);
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "ArrowRight" });
    expect(localStorage.getItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN)).toBe(SKINS[1].id);
  });
});

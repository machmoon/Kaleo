// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import { SKINS } from "@/lib/overlay-skin";
import { OverlaySkin } from "./OverlaySkin";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

/** One skin's radio, by identity attribute rather than position. */
const card = (id: string) =>
  screen.getAllByTestId("skin-card").find((el) => el.getAttribute("data-skin") === id) as HTMLElement;

/** The shared `Header` calls `useNavigate`, so the pane needs a router. */
const mount = () =>
  render(
    <MemoryRouter>
      <OverlaySkin />
    </MemoryRouter>,
  );

describe("the overlay skin picker", () => {
  it("offers every skin in the catalogue", () => {
    mount();
    for (const skin of SKINS) {
      expect(card(skin.id)).toBeTruthy();
    }
  });

  it("persists the choice where the overlay reads it", () => {
    mount();
    fireEvent.click(card("terminal"));
    expect(localStorage.getItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN)).toBe("terminal");
    expect(card("terminal").getAttribute("aria-checked")).toBe("true");
  });

  it("marks exactly one skin as chosen", () => {
    mount();
    fireEvent.click(card("orb"));
    const checked = SKINS.filter(
      (s) => card(s.id).getAttribute("aria-checked") === "true",
    );
    expect(checked).toHaveLength(1);
    expect(checked[0].id).toBe("orb");
  });

  it("shows the Ada switch only for the skin that has a shell", () => {
    mount();
    expect(screen.queryByTestId("terminal-ada-switch")).toBeNull();
    fireEvent.click(card("terminal"));
    expect(screen.getByTestId("terminal-ada-switch")).toBeTruthy();
  });

  it("defaults Ada routing on, and stores the off state explicitly", () => {
    // Absent means on, so turning it off has to write "0" rather than
    // removing the key — otherwise the switch springs back on next launch.
    mount();
    fireEvent.click(card("terminal"));
    const toggle = screen.getByTestId("terminal-ada-switch");
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(toggle);
    expect(localStorage.getItem(KALEO_STORAGE_KEYS.TERMINAL_ADA)).toBe("0");
  });
});

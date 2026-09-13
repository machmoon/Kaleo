// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ThemeProvider } from "@/contexts/theme.context";
import { STORAGE_KEYS } from "@/config";
import { THEME_RING, ThemeCardPicker } from "./ThemeCardPicker";

beforeEach(() => {
  localStorage.clear();
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
});
afterEach(cleanup);

const mount = () =>
  render(
    <ThemeProvider>
      <ThemeCardPicker />
    </ThemeProvider>,
  );

const card = (choice: string) =>
  screen.getAllByTestId("theme-card").find((el) => el.getAttribute("data-choice") === choice) as HTMLElement;

describe("ThemeCardPicker", () => {
  it("is a radiogroup of three thumbnails with Auto first", () => {
    mount();
    expect(screen.getByRole("radiogroup", { name: "Appearance" })).toBeTruthy();
    expect(screen.getAllByTestId("theme-card").map((el) => el.getAttribute("data-choice"))).toEqual([
      "system",
      "light",
      "dark",
    ]);
    expect(screen.getAllByTestId("theme-thumbnail")).toHaveLength(3);
  });

  it("applies the theme live and stores it under the legacy key", () => {
    mount();
    fireEvent.click(card("dark"));
    expect(localStorage.getItem(STORAGE_KEYS.THEME)).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(card("dark").getAttribute("aria-checked")).toBe("true");
    expect(card("light").getAttribute("aria-checked")).toBe("false");
  });

  it("keeps one tab stop and moves with the arrow keys", () => {
    mount();
    expect(card("system").tabIndex).toBe(0);
    expect(card("light").tabIndex).toBe(-1);
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "ArrowRight" });
    expect(localStorage.getItem(STORAGE_KEYS.THEME)).toBe("light");
    expect(card("light").tabIndex).toBe(0);
    fireEvent.keyDown(screen.getByRole("radiogroup"), { key: "ArrowLeft" });
    expect(localStorage.getItem(STORAGE_KEYS.THEME)).toBe("system");
  });

  it("draws the macOS-blue ring on the selected thumbnail only, leaving --ring alone", () => {
    mount();
    fireEvent.click(card("light"));
    const selected = card("light").querySelector("span") as HTMLElement;
    const other = card("dark").querySelector("span") as HTMLElement;
    expect(selected.style.boxShadow).toContain(THEME_RING);
    expect(other.style.boxShadow).not.toContain(THEME_RING);
    expect(document.documentElement.style.getPropertyValue("--ring")).toBe("");
  });
});

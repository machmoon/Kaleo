// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WelcomeLayout } from "./WelcomeLayout";

afterEach(cleanup);

function mount(over: Partial<React.ComponentProps<typeof WelcomeLayout>> = {}) {
  const handlers = {
    onContinue: vi.fn(),
    onBack: vi.fn(),
    onLater: vi.fn(),
    onExit: vi.fn(),
  };
  render(
    <WelcomeLayout title="Start the engine" index={2} total={6} demo={false} {...handlers} {...over}>
      <input data-testid="field" />
      <p>body</p>
    </WelcomeLayout>,
  );
  return handlers;
}

describe("WelcomeLayout", () => {
  it("announces the step out of six and offers Set Up Later, Back and Continue", () => {
    mount();
    expect(screen.getByTestId("setup-live").textContent).toBe("Step 3 of 6, Start the engine");
    expect(screen.getByTestId("setup-later").textContent).toBe("Set Up Later");
    expect(screen.getByTestId("setup-back")).toBeTruthy();
    expect(screen.getByTestId("setup-continue").textContent).toBe("Continue");
    expect(screen.getByTestId("setup-dots").querySelectorAll('[data-active="true"]')).toHaveLength(1);
    expect(screen.queryByTestId("demo-banner")).toBeNull();
  });

  it("keeps a drag strip at the top and no card border around the content", () => {
    mount();
    const layout = screen.getByTestId("welcome-layout");
    expect(layout.querySelector("[data-tauri-drag-region]")).toBeTruthy();
    expect(layout.className).toContain("bg-background");
    expect(layout.className).not.toContain("border");
  });

  it("Enter continues, Esc is Set Up Later, ⌘← is Back", () => {
    const h = mount();
    fireEvent.keyDown(window, { key: "Enter" });
    expect(h.onContinue).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(h.onLater).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "ArrowLeft", metaKey: true });
    expect(h.onBack).toHaveBeenCalledTimes(1);
  });

  it("Enter inside a text field belongs to the field, and never fires while Continue is disabled", () => {
    const h = mount({ canContinue: false });
    fireEvent.keyDown(screen.getByTestId("field"), { key: "Enter" });
    fireEvent.keyDown(window, { key: "Enter" });
    expect(h.onContinue).not.toHaveBeenCalled();
    expect((screen.getByTestId("setup-continue") as HTMLButtonElement).disabled).toBe(true);
  });

  it("Shift+Esc asks before ending setup, and only the confirm exits", () => {
    const h = mount();
    fireEvent.keyDown(window, { key: "Escape", shiftKey: true });
    expect(screen.getByTestId("setup-exit-confirm")).toBeTruthy();
    expect(h.onExit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("setup-exit-no"));
    expect(screen.queryByTestId("setup-exit-confirm")).toBeNull();
    fireEvent.keyDown(window, { key: "Escape", shiftKey: true });
    fireEvent.click(screen.getByTestId("setup-exit-yes"));
    expect(h.onExit).toHaveBeenCalledTimes(1);
    expect(h.onLater).not.toHaveBeenCalled();
  });

  it("shows the persistent demo banner and hides dots, Back and the footer on request", () => {
    mount({ demo: true, showDots: false, showBack: false, showFooter: false });
    expect(screen.getByTestId("demo-banner").textContent).toMatch(/^Demo mode\./);
    expect(screen.queryByTestId("setup-dots")).toBeNull();
    expect(screen.queryByTestId("setup-footer")).toBeNull();
  });
});

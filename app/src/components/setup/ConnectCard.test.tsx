// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConnectCard } from "./ConnectCard";

afterEach(cleanup);

describe("ConnectCard", () => {
  it("offers the action when unconfigured and disables it when the engine is down or unavailable", () => {
    const onAction = vi.fn();
    const { rerender } = render(
      <ConnectCard id="google" name="Google" description="d" state="unconfigured" actionLabel="Connect" onAction={onAction} />,
    );
    fireEvent.click(screen.getByTestId("connect-action"));
    expect(onAction).toHaveBeenCalledTimes(1);
    rerender(<ConnectCard id="google" name="Google" description="d" state="down" detail="Engine not running, connect later in Settings." actionLabel="Connect" onAction={onAction} />);
    expect((screen.getByTestId("connect-action") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("connect-detail").textContent).toMatch(/Engine not running/);
    rerender(<ConnectCard id="google" name="Google" description="d" state="unavailable" detail="not installed" actionLabel="Connect" onAction={onAction} />);
    expect((screen.getByTestId("connect-action") as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the ready word and the secondary button when ready, never the action", () => {
    const onSecondary = vi.fn();
    render(
      <ConnectCard id="microsoft" name="Microsoft" description="d" state="ready" readyWord="Token issued" secondaryLabel="Disconnect" onSecondary={onSecondary} actionLabel="Connect" onAction={() => undefined} />,
    );
    expect(screen.getByTestId("connect-ready").textContent).toContain("Token issued");
    expect(screen.queryByTestId("connect-action")).toBeNull();
    fireEvent.click(screen.getByTestId("connect-secondary"));
    expect(onSecondary).toHaveBeenCalled();
  });

  it("marks a demo connection and a waiting sign-in", () => {
    render(<ConnectCard id="google" name="Google" description="d" state="connecting" demo />);
    expect(screen.getByTestId("demo-badge").textContent).toBe("Demo");
    expect(screen.getByTestId("connect-waiting")).toBeTruthy();
    expect(screen.getByTestId("connect-card").getAttribute("data-state")).toBe("connecting");
  });
});

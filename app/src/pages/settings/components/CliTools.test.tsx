// @vitest-environment jsdom
//
// The pane is driven through a mocked `@tauri-apps/api/core`, the way the
// other component tests do it, so none of this needs the desktop shell.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { CliToolInfo } from "@/lib/cli";

const invoke = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

import { CliTools } from "./CliTools";

/** The shared `Header` calls `useNavigate`, so the pane needs a router. */
const mount = () =>
  render(
    <MemoryRouter>
      <CliTools />
    </MemoryRouter>,
  );

const TOOLS: CliToolInfo[] = [
  { id: "googleapps", available: true, detail: "python -m googleapps (auth, check, run)" },
  { id: "silkscreen", available: true, detail: "python -m silkscreen — board CLI" },
  { id: "python", available: false, detail: "venv missing; set ADA_PYTHON" },
  { id: "kicad-cli", available: false, detail: "not found" },
];

beforeEach(() => {
  invoke.mockReset();
});
afterEach(cleanup);

describe("the command-line tools settings pane", () => {
  it("lists every tool the Rust allowlist reports, available or not", async () => {
    invoke.mockResolvedValue(TOOLS);
    mount();
    for (const tool of TOOLS) {
      await waitFor(() => expect(screen.getByTestId(`cli-tool-${tool.id}`)).toBeTruthy());
    }
  });

  it("asks the desktop app for the allowlist rather than hardcoding one", async () => {
    invoke.mockResolvedValue(TOOLS);
    mount();
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("list_cli_tools"));
  });

  it("shows each tool's own detail line from Rust, not a rewritten one", async () => {
    invoke.mockResolvedValue(TOOLS);
    mount();
    await waitFor(() =>
      expect(screen.getByTestId("cli-tool-detail-silkscreen").textContent).toBe(
        "python -m silkscreen — board CLI",
      ),
    );
  });

  it("marks an installed tool available and a missing one not found", async () => {
    invoke.mockResolvedValue(TOOLS);
    mount();
    await waitFor(() =>
      expect(screen.getByTestId("cli-tool-googleapps").getAttribute("data-available")).toBe("yes"),
    );
    expect(screen.getByTestId("cli-tool-kicad-cli").getAttribute("data-available")).toBe("no");
    expect(screen.getByTestId("cli-tool-state-kicad-cli").textContent).toBe("Not found");
  });

  it("tells the user how to fix a missing tool, naming the variable cli.rs reads", async () => {
    // The value of the fix line is the exact spelling: a variable the Rust
    // side does not consult would send someone off exporting nothing.
    invoke.mockResolvedValue(TOOLS);
    mount();
    await waitFor(() => expect(screen.getByTestId("cli-tool-fix-python")).toBeTruthy());
    expect(screen.getByTestId("cli-tool-fix-python").textContent).toContain("ADA_PYTHON");
    expect(screen.getByTestId("cli-tool-fix-kicad-cli").textContent).toContain("KICAD_CLI");
  });

  it("points a missing googleapps at the checkout variable, not the interpreter one", async () => {
    invoke.mockResolvedValue([
      { id: "googleapps", available: false, detail: "not found" },
    ] satisfies CliToolInfo[]);
    mount();
    await waitFor(() => expect(screen.getByTestId("cli-tool-fix-googleapps")).toBeTruthy());
    expect(screen.getByTestId("cli-tool-fix-googleapps").textContent).toContain("ADA_REPO_ROOT");
  });

  it("offers no fix line for a tool that is already there", async () => {
    invoke.mockResolvedValue(TOOLS);
    mount();
    await waitFor(() => expect(screen.getByTestId("cli-tool-silkscreen")).toBeTruthy());
    expect(screen.queryByTestId("cli-tool-fix-silkscreen")).toBeNull();
  });

  it("says the allowlist is argv-only and never a shell", async () => {
    invoke.mockResolvedValue(TOOLS);
    mount();
    const posture = screen.getByTestId("cli-tools-posture");
    expect(posture.textContent).toContain("never through a shell");
    expect(posture.textContent).toContain("may only start the programs named above");
    expect(posture.textContent).toContain("refused by name");
  });

  it("keeps the terminal skin's posture separate rather than blurring the two", async () => {
    // The terminal is the user's own shell with their own privileges; saying
    // "Ada only runs an allowlist" without that caveat would misdescribe it.
    invoke.mockResolvedValue(TOOLS);
    mount();
    const posture = screen.getByTestId("cli-tools-posture");
    expect(posture.textContent).toContain("your own shell");
    expect(posture.textContent).toContain("your privileges");
  });

  it("degrades to a named failure when the desktop command is unavailable", async () => {
    // In a browser dev server `invoke` rejects; an empty list would claim
    // this machine has no tools, which is a different and false statement.
    invoke.mockRejectedValue(new Error("window.__TAURI_INTERNALS__ is undefined"));
    mount();
    await waitFor(() => expect(screen.getByTestId("cli-tools-unavailable")).toBeTruthy());
    expect(screen.getByTestId("cli-tools-error").textContent).toContain(
      "window.__TAURI_INTERNALS__ is undefined",
    );
    expect(screen.queryByTestId("cli-tool-python")).toBeNull();
  });

  it("distinguishes a build with no allowlist from an unreachable one", async () => {
    invoke.mockResolvedValue([]);
    mount();
    await waitFor(() => expect(screen.getByTestId("cli-tools-empty")).toBeTruthy());
    expect(screen.queryByTestId("cli-tools-unavailable")).toBeNull();
  });

  it("shows a checking notice before the machine has answered", () => {
    invoke.mockReturnValue(new Promise(() => {}));
    mount();
    expect(screen.getByTestId("cli-tools-loading")).toBeTruthy();
  });
});

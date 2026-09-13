import { useEffect, useState } from "react";
import { Header, Label } from "@/components";
import { listCliTools, type CliToolInfo } from "@/lib/cli";

/**
 * What Hardy may run on this machine, and whether it is actually installed.
 *
 * The ask was "what happened to CLI mode, I don't see that in settings" —
 * and the honest answer is that the allowlist in `src-tauri/src/cli.rs` had
 * never had a face. It shipped as two Tauri commands (`list_cli_tools`,
 * `run_cli`) and nothing on screen, so the only way to learn that `kicad-cli`
 * was missing was to run something that needed it and read the failure.
 *
 * Two things this pane refuses to blur:
 *
 * * **Unavailable has to be actionable.** Rust returns `detail` as free text
 *   ("not found"), which tells the user nothing about what to do. The fix
 *   line below names the install or the exact environment variable, spelled
 *   the way `cli.rs` reads it — a near-miss like `ADA_PYTHON_PATH` sends
 *   someone off exporting a variable nothing consults.
 * * **This is not the terminal skin.** `cli.rs` spawns argv directly, never
 *   through a shell, and only these four ids; `pty.rs` hands the user their
 *   own login shell with their own privileges. Same app, different blast
 *   radius, so the posture is stated on screen rather than assumed.
 */

/** Why a tool is missing and what fixes it. Keyed by the id `cli.rs` returns. */
const FIXES: Record<string, string> = {
  googleapps:
    "Needs the Hardy checkout and its virtualenv. Run ./scripts/install.sh, or point ADA_REPO_ROOT at the checkout if the app is not sitting inside one.",
  silkscreen:
    "Needs the checkout's Python. Run ./scripts/install.sh, or set ADA_PYTHON to an interpreter that has Hardy installed.",
  python:
    "No .venv/bin/python under the checkout. Run ./scripts/install.sh, or set ADA_PYTHON to the interpreter you want Hardy to use.",
  "kicad-cli":
    "Install KiCad, or set KICAD_CLI to the kicad-cli binary if it lives somewhere off PATH.",
};

/** What each tool is for, so the id is not the only label on the row. */
const PURPOSE: Record<string, string> = {
  googleapps: "Workspace auth and checks",
  silkscreen: "Board generation from the CLI",
  python: "The checkout's interpreter, -m module or a .py script",
  "kicad-cli": "ERC, DRC and exports",
};

type Load =
  | { state: "loading" }
  | { state: "ready"; tools: CliToolInfo[] }
  | { state: "unavailable"; reason: string };

export const CliTools = ({ className }: { className?: string }) => {
  const [load, setLoad] = useState<Load>({ state: "loading" });

  useEffect(() => {
    let live = true;
    listCliTools()
      .then((tools) => {
        if (live) setLoad({ state: "ready", tools });
      })
      .catch((error: unknown) => {
        // A browser dev server has no Tauri IPC at all, so `invoke` rejects
        // rather than returning an empty list. Rendering "no tools" there
        // would be a lie about the machine, so the failure is named instead.
        if (live)
          setLoad({
            state: "unavailable",
            reason: error instanceof Error ? error.message : String(error),
          });
      });
    return () => {
      live = false;
    };
  }, []);

  return (
    <div id="cli-tools" className={`space-y-3 ${className ?? ""}`} data-testid="cli-tools-settings">
      <Header
        isMainTitle
        title="Command-line tools"
        description="The fixed list of programs Hardy may run for you, and whether this machine has them."
      />

      {load.state === "loading" ? (
        <p className="text-xs text-muted-foreground" data-testid="cli-tools-loading">
          Checking this machine…
        </p>
      ) : null}

      {load.state === "unavailable" ? (
        <div className="rounded-md border p-3" data-testid="cli-tools-unavailable">
          <p className="text-xs text-muted-foreground">
            Hardy could not ask this machine what it has. Tools run from the desktop app, so
            this list is empty in a browser tab.
          </p>
          <p className="mt-1 text-[11px] text-muted-foreground" data-testid="cli-tools-error">
            {load.reason}
          </p>
        </div>
      ) : null}

      {load.state === "ready" && load.tools.length === 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="cli-tools-empty">
          The desktop app reported no tools at all. That is a build without the CLI
          allowlist compiled in, not a machine missing software.
        </p>
      ) : null}

      {load.state === "ready" && load.tools.length > 0 ? (
        <div className="space-y-2">
          {load.tools.map((tool) => (
            <div
              key={tool.id}
              className="rounded-md border px-3 py-2"
              data-testid={`cli-tool-${tool.id}`}
              data-available={tool.available ? "yes" : "no"}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <Label className="text-sm font-medium">
                    <code>{tool.id}</code>
                  </Label>
                  {PURPOSE[tool.id] ? (
                    <p className="text-xs text-muted-foreground">{PURPOSE[tool.id]}</p>
                  ) : null}
                </div>
                <span
                  className={`shrink-0 text-xs ${tool.available ? "text-muted-foreground" : "text-destructive"}`}
                  data-testid={`cli-tool-state-${tool.id}`}
                >
                  {tool.available ? "Available" : "Not found"}
                </span>
              </div>
              <p
                className="mt-1 truncate text-[11px] text-muted-foreground"
                title={tool.detail}
                data-testid={`cli-tool-detail-${tool.id}`}
              >
                {tool.detail}
              </p>
              {/* An unknown id can only be reported, not fixed — the fix text
                  is written per tool and inventing one would be a guess. */}
              {!tool.available && FIXES[tool.id] ? (
                <p className="mt-1 text-xs" data-testid={`cli-tool-fix-${tool.id}`}>
                  {FIXES[tool.id]}
                </p>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}

      <div className="space-y-1 rounded-md border p-3" data-testid="cli-tools-posture">
        <Label className="text-sm font-medium">What this can and cannot do</Label>
        <p className="text-xs text-muted-foreground">
          Hardy may only start the programs named above, and only as a direct argument list —
          never through a shell, so nothing typed or generated can become a second command.
          Anything not on this list is refused by name.
        </p>
        <p className="text-xs text-muted-foreground">
          The terminal skin is separate and stricter about nothing: it is your own shell,
          your environment, your privileges, the same as Terminal.app. Hardy never types into
          it — a command she proposes is staged as text for you to press Enter on.
        </p>
      </div>
    </div>
  );
};

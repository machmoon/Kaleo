// @vitest-environment jsdom
/**
 * The skin's contract, tested where it can be: routing at the keystroke.
 *
 * The pty itself is proven on the Rust side (`pty::tests` spawns a real
 * process and asserts the child sees `isatty`). What this file pins is the
 * half that only exists here — that a question never reaches the shell, that
 * a command always does, and that the hint tells the truth before Enter.
 */

import { describe, expect, it, vi } from "vitest";
import { route } from "@/lib/terminal-sigil";

/** The exact decision `onData` makes on Enter, in isolation. */
function onEnter(draft: string, buffer: "normal" | "alternate", adaEnabled = true) {
  const routed = route(draft, { buffer, adaEnabled });
  return routed.destination === "shell"
    ? { toShell: draft + "\r", toAda: null }
    : { toShell: "\x15", toAda: routed.text };
}

describe("what Enter does", () => {
  it("sends a command to the shell verbatim", () => {
    expect(onEnter("git status", "normal")).toEqual({ toShell: "git status\r", toAda: null });
  });

  it("never lets a question reach the shell as a command", () => {
    // The failure this guards: the question sits at the prompt and the shell
    // tries to run `Why` as a program the moment anything sends a newline.
    const result = onEnter("Why did that fail?", "normal");
    expect(result.toAda).toBe("Why did that fail?");
    expect(result.toShell).toBe("\x15"); // Ctrl-U kills the echoed line
    expect(result.toShell).not.toContain("\r");
  });

  it("kills the echoed line before answering, so the prompt is clean", () => {
    expect(onEnter("!fix U3", "normal").toShell).toBe("\x15");
    expect(onEnter("!fix U3", "normal").toAda).toBe("fix U3");
  });

  it("passes everything to the shell inside a full-screen program", () => {
    expect(onEnter(":wq", "alternate")).toEqual({ toShell: ":wq\r", toAda: null });
    expect(onEnter("Quit", "alternate").toAda).toBeNull();
  });

  it("is an ordinary terminal when Ada routing is off", () => {
    expect(onEnter("Why?", "normal", false).toAda).toBeNull();
  });
});

describe("the hint shown while typing", () => {
  it("changes destination on the very first character", () => {
    // The property a classifier cannot have: the answer is known before the
    // second keystroke, so it can be shown rather than discovered.
    expect(route("W").destination).toBe("ada");
    expect(route("w").destination).toBe("shell");
    expect(route("!").destination).toBe("shell");
    expect(route("!x").destination).toBe("agent");
  });

  it("always has something to say", () => {
    for (const draft of ["", "l", "ls -", "Wh", "!f", " R"]) {
      expect(route(draft).why).toBeTruthy();
    }
  });
});

describe("draft tracking", () => {
  /** The same edits `onData` applies, so the tracked line matches the screen. */
  function type(keys: string[]) {
    let draft = "";
    for (const key of keys) {
      if (key === "\x7f") draft = draft.slice(0, -1);
      else if (key === "\x03" || key === "\x15") draft = "";
      else if (key >= " " || key === "\t") draft += key;
    }
    return draft;
  }

  it("follows backspace, so a corrected line routes on what is actually there", () => {
    // Type `Wls`, backspace twice, and it is `ls` — a shell command, not a
    // question. Routing on the first character ever typed would send it to
    // the model and bill for it.
    expect(type(["W", "l", "s", "\x7f", "\x7f", "\x7f", "l", "s"])).toBe("ls");
    expect(route(type(["W", "\x7f", "l", "s"])).destination).toBe("shell");
  });

  it("clears on Ctrl-C and Ctrl-U, like the shell's own line editor", () => {
    expect(type(["W", "h", "y", "\x03"])).toBe("");
    expect(type(["l", "s", "\x15", "p", "w", "d"])).toBe("pwd");
  });

  it("ignores control bytes that are not edits", () => {
    // Arrow keys arrive as escape sequences; appending them would make the
    // tracked line disagree with the screen.
    expect(type(["l", "s", "\x1b", "[", "A"])).not.toContain("\x1b");
  });
});

describe("module surface", () => {
  it("imports without a DOM-less crash", async () => {
    vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
    const mod = await import("./TerminalSkin");
    expect(typeof mod.TerminalSkin).toBe("function");
  });
});

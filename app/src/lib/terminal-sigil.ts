/**
 * One input line, two destinations: the shell, or Ada.
 *
 * The ask was a terminal skin that "doubles as Ada and terminal" — not a
 * terminal with a chat pane bolted beside it. Wave and Tabby both put AI in a
 * separate panel; the one piece of prior art that actually shares the input
 * line is Butterfish (MIT), and its answer is the right one: no classifier,
 * no mode switch, no round trip to decide. The *first character the user
 * typed* decides, and because the user typed it, they always know where the
 * line went.
 *
 * Butterfish's rules, adopted:
 *
 * - a line starting with a capital letter goes to the model as chat;
 * - `!` prefixes an agent request — Ada may propose commands;
 * - everything else is a shell command, unchanged.
 *
 * Two rules of our own, both because the Butterfish set has holes that bite
 * in a terminal people actually live in:
 *
 * - **A leading space forces the shell.** `Rscript`, `Xvfb`, `Setup.exe` and
 *   any absolute path a user capitalised are real commands that start with a
 *   capital letter, and silently sending one to a model instead of running it
 *   is the failure mode that would make people turn this off. Shells already
 *   teach " " as the escape hatch via `HISTCONTROL=ignorespace`, so this
 *   borrows a reflex rather than inventing one.
 * - **Nothing routes while the alternate screen is up.** When `vim`, `htop`
 *   or `less` is running, every keystroke belongs to that program — `:wq` is
 *   not a shell command and `Quit` is not a question for Ada. xterm.js
 *   reports this directly as `buffer.active.type`, so it is a fact we read
 *   rather than a guess.
 */

/** Where a submitted line goes. */
export type Destination = "shell" | "ada" | "agent";

export interface Routed {
  destination: Destination;
  /** What to send on — the sigil stripped, the text otherwise untouched. */
  text: string;
  /** Why it went there, for the one-line hint the skin shows under the bar. */
  why: string;
}

export interface RouteContext {
  /**
   * `"alternate"` while a full-screen program owns the terminal. Read from
   * xterm.js's `term.buffer.active.type`.
   */
  buffer?: "normal" | "alternate";
  /** The user turned sigil routing off; the skin is then a plain terminal. */
  adaEnabled?: boolean;
}

const CAPITAL = /^[A-Z]/;

/**
 * Decide where one submitted line goes.
 *
 * Pure and total: every input returns a destination, and `text` is only ever
 * the input with a leading sigil removed. Nothing here talks to a model, a
 * shell, or the DOM, which is what makes the rules testable at all.
 */
export function route(line: string, context: RouteContext = {}): Routed {
  const shell = (why: string): Routed => ({ destination: "shell", text: line, why });

  // A full-screen program owns every keystroke. This check is first because
  // being wrong here means swallowing a `:wq`.
  if (context.buffer === "alternate") {
    return shell("a full-screen program is running");
  }
  if (context.adaEnabled === false) {
    return shell("Hardy routing is off");
  }
  // Whitespace-only, or empty: a bare Enter redraws the prompt.
  if (!line.trim()) return shell("empty line");

  // The escape hatch, checked before any sigil so it always wins.
  if (/^\s/.test(line)) {
    return { destination: "shell", text: line.replace(/^\s+/, ""), why: "leading space forces the shell" };
  }

  if (line.startsWith("!")) {
    const text = line.slice(1).trim();
    if (!text) return shell("a bare ! is history expansion, not a request");
    return { destination: "agent", text, why: "! asks Hardy to work on it" };
  }

  if (CAPITAL.test(line)) {
    return { destination: "ada", text: line, why: "a capital first letter asks Hardy" };
  }

  return shell("runs in your shell");
}

/**
 * The hint the skin shows *while typing*, so the destination is visible
 * before Enter rather than discovered after it.
 *
 * This is the whole reason a sigil beats a classifier: the answer is known
 * from the first character, so it can be shown, so nobody is surprised.
 */
export function preview(line: string, context: RouteContext = {}): Routed {
  return route(line, context);
}

/** The two-line legend the skin prints once, on open. */
export const LEGEND = [
  "Type as usual — it runs in your shell.",
  "Start with a Capital letter to ask Hardy, or ! to have her work on it.",
];

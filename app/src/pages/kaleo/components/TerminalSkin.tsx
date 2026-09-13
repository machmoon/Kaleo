import { useCallback, useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { closePty, decodeBytes, openPty, resizePty, writePty, writeText } from "@/lib/pty";
import { LEGEND, route, type Destination } from "@/lib/terminal-sigil";

/**
 * The overlay's terminal skin: a literal terminal that doubles as Ada.
 *
 * "The terminal skin was a literal terminal so it doubles as ada and
 * terminal" — so this is a real shell on a real pty (`src-tauri/src/pty.rs`),
 * not a chat window styled to look like one. `vim`, `htop`, tab completion,
 * Ctrl-C and the user's own prompt all work, because the process on the other
 * end is talking to a tty.
 *
 * The Ada half shares the same input line rather than sitting in a pane
 * beside it, and the split is decided by the first character the user typed
 * (`@/lib/terminal-sigil`) — capital letter asks Ada, `!` sets her to work,
 * anything else runs. Because the rule is a character and not a classifier,
 * the destination can be shown *while typing*: the strip under the terminal
 * names where Enter will send the line, so nobody is surprised after the
 * fact. That is the property a model-based router cannot have.
 *
 * Interception happens at `onData`, which is the only place that sees
 * keystrokes before they reach the shell. The line being typed is tracked
 * here rather than scraped off the screen, because the screen belongs to the
 * shell — its prompt, its completions, its redraws — and reading it back
 * would be guessing at state we already know exactly.
 */

const DESTINATION_LABEL: Record<Destination, string> = {
  shell: "shell",
  ada: "Hardy",
  agent: "Hardy · working",
};

export interface TerminalSkinProps {
  /** Where the shell starts. Defaults to the shell's own default. */
  cwd?: string;
  /** Sigil routing off makes this an ordinary terminal. */
  adaEnabled?: boolean;
  /**
   * Hand a line to Ada. Returns the final text to print.
   *
   * `write` is passed in rather than the caller buffering, because an Ada
   * turn can start a paid board run: those take minutes, and a terminal that
   * sat silent until the end would read as hung. Each line it is given is
   * printed the moment it arrives.
   */
  onAsk?: (
    text: string,
    mode: "ada" | "agent",
    write: (line: string) => void,
  ) => Promise<string>;
}

export const TerminalSkin = ({ cwd, adaEnabled = true, onAsk }: TerminalSkinProps) => {
  const host = useRef<HTMLDivElement | null>(null);
  const term = useRef<Terminal | null>(null);
  const fit = useRef<FitAddon | null>(null);
  const sessionId = useRef<string>(`term-${Math.random().toString(36).slice(2, 10)}`);
  /** The line the user is typing, tracked rather than scraped off the screen. */
  const draft = useRef<string>("");
  const [hint, setHint] = useState<{ destination: Destination; why: string }>({
    destination: "shell",
    why: "runs in your shell",
  });
  const [error, setError] = useState<string | null>(null);
  const [thinking, setThinking] = useState(false);

  const ask = useCallback(
    async (text: string, mode: "ada" | "agent") => {
      const view = term.current;
      if (!view) return;
      if (!onAsk) {
        view.write("\r\n\x1b[2mAda is not connected in this window.\x1b[0m\r\n");
        await writeText(sessionId.current, "\r");
        return;
      }
      setThinking(true);
      // Echo the question, then leave the shell's prompt alone: the shell
      // never saw this line, so it must not be told to run it.
      view.write(`\r\n\x1b[38;5;110m${mode === "agent" ? "!" : ""}${text}\x1b[0m\r\n`);
      try {
        const answer = await onAsk(text, mode, (line) => {
          // Dim, so streamed progress reads as machinery and Ada's actual
          // answer below it reads as the reply.
          view.write(`\x1b[2m  ${line.replace(/\r?\n/g, " ")}\x1b[0m\r\n`);
        });
        if (answer.trim()) view.write(`${answer.replace(/\n/g, "\r\n")}\r\n`);
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        view.write(`\x1b[31m${detail}\x1b[0m\r\n`);
      } finally {
        setThinking(false);
        // Redraw the shell's prompt by sending it a bare Enter, so the user
        // is back where they were rather than staring at a blank line.
        await writeText(sessionId.current, "\r").catch(() => undefined);
      }
    },
    [onAsk],
  );

  useEffect(() => {
    const node = host.current;
    if (!node) return;

    const view = new Terminal({
      // Matches the overlay's quiet ground rather than xterm's default black,
      // so the skin reads as part of the bar and not a window pasted onto it.
      allowTransparency: true,
      convertEol: false,
      cursorBlink: true,
      fontFamily:
        'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
      fontSize: 12,
      lineHeight: 1.25,
      scrollback: 5000,
      theme: { background: "rgba(0,0,0,0)", foreground: "#d8dee9", cursor: "#88c0d0" },
    });
    const fitAddon = new FitAddon();
    view.loadAddon(fitAddon);
    view.open(node);
    fitAddon.fit();
    term.current = view;
    fit.current = fitAddon;

    let disposed = false;
    const id = sessionId.current;

    void openPty(
      { id, cols: view.cols, rows: view.rows, cwd },
      (event) => {
        if (disposed) return;
        if (event.kind === "output") view.write(decodeBytes(event.b64));
        else view.write("\r\n\x1b[2m[shell exited]\x1b[0m\r\n");
      },
    )
      .then(() => {
        if (disposed) return;
        view.write(`\x1b[2m${LEGEND.join("\r\n")}\x1b[0m\r\n`);
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
      });

    const onData = view.onData((data) => {
      const buffer = view.buffer.active.type;
      // While a full-screen program owns the screen, every byte is its own.
      // Tracking a "line" there would be meaningless and intercepting Enter
      // would swallow a `:wq`.
      if (buffer === "alternate" || !adaEnabled) {
        void writePty(id, new TextEncoder().encode(data)).catch(() => undefined);
        return;
      }

      if (data === "\r") {
        const line = draft.current;
        const routed = route(line, { buffer, adaEnabled });
        draft.current = "";
        setHint({ destination: "shell", why: "runs in your shell" });
        if (routed.destination === "shell") {
          void writePty(id, new TextEncoder().encode(data)).catch(() => undefined);
          return;
        }
        // Ada's line must never reach the shell. Erase what the shell has
        // echoed so far (Ctrl-U kills the line) before answering, or the
        // question sits at the prompt waiting to be run as a command.
        void writePty(id, new TextEncoder().encode("\x15"))
          .then(() => ask(routed.text, routed.destination === "agent" ? "agent" : "ada"))
          .catch(() => undefined);
        return;
      }

      if (data === "\x7f") {
        draft.current = draft.current.slice(0, -1);
      } else if (data === "\x03" || data === "\x15") {
        draft.current = "";
      } else if (data >= " " || data === "\t") {
        draft.current += data;
      }
      setHint(route(draft.current, { buffer, adaEnabled }));
      void writePty(id, new TextEncoder().encode(data)).catch(() => undefined);
    });

    // The resize the terminal-in-Tauri repos all forget. Without it every
    // full-screen program draws into a 24x80 box.
    const onResize = view.onResize(({ cols, rows }) => {
      void resizePty(id, cols, rows).catch(() => undefined);
    });
    const observer = new ResizeObserver(() => {
      try {
        fitAddon.fit();
      } catch {
        // A hidden pane has no size to fit to; the next open re-fits.
      }
    });
    observer.observe(node);

    return () => {
      disposed = true;
      observer.disconnect();
      onData.dispose();
      onResize.dispose();
      void closePty(id).catch(() => undefined);
      view.dispose();
      term.current = null;
      fit.current = null;
    };
    // `cwd` is read once, at open: changing it would mean a different shell.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adaEnabled, ask, cwd]);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="terminal-skin">
      <div ref={host} className="min-h-0 flex-1 overflow-hidden px-2 pt-2" />
      <div
        className="flex items-center justify-between gap-2 px-3 py-1 text-[11px] text-muted-foreground"
        data-testid="terminal-hint"
      >
        <span>
          {thinking ? "Hardy is thinking…" : `↵ → ${DESTINATION_LABEL[hint.destination]}`}
        </span>
        <span className="truncate opacity-70">{error ?? hint.why}</span>
      </div>
    </div>
  );
};

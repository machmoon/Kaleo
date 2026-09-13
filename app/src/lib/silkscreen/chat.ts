/**
 * One orchestrator turn: ask Ada a question, get her answer back.
 *
 * `POST /chat/stream` already existed on the engine and had no client. It is
 * the right endpoint for the terminal skin's Ada half — but it has a property
 * that makes a naive client dangerous, and this module exists to handle it:
 *
 * **The orchestrator can start a board run.** `service/app.py` hands it a
 * `generate` callable and the model decides whether to call it, so a typed
 * sentence can become a paid pipeline. This repo's standing rule is that a
 * paid run is never invisible, so:
 *
 * - every frame is described as it arrives (`describeFrame`), so a run in
 *   progress prints stage by stage instead of the terminal sitting silent;
 * - `ranBoard` is reported on the outcome, so the caller can say a board was
 *   generated rather than letting it look like a chat reply that took a while.
 *
 * Transport mirrors `generateStream` in `client.ts` deliberately — same
 * timeout ceiling, same NDJSON line splitting, same rule that a terminal frame
 * already received survives a connection that then dies.
 */

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { describeFrame } from "./describe";
import { REQUEST_TIMEOUT_MS, SilkscreenError, authHeaders, parseFrame } from "./client";
import type { RunResult, StreamFrame } from "./types";

export interface ChatOutcome {
  /** Ada's reply, as text. */
  assistant: string;
  /** She is asking for more before she can act. */
  needsClarification: boolean;
  /** A board was actually generated this turn — a real, paid run. */
  ranBoard: boolean;
  result: RunResult | null;
  model: string;
}

export interface AskOptions {
  baseUrl: string;
  token?: string;
  signal?: AbortSignal;
  /** Called with one plain sentence per frame, for live output. */
  onLine?: (line: string) => void;
  sessionId?: string;
}

/**
 * Ask, and resolve when the turn ends.
 *
 * Throws `SilkscreenError` rather than returning a falsy answer: a terminal
 * that printed an empty line when the engine was unreachable would read as
 * "Ada had nothing to say", which is the one thing that must not happen.
 */
export async function ask(question: string, options: AskOptions): Promise<ChatOutcome> {
  const text = question.trim();
  if (!text) throw new SilkscreenError("request", "Nothing was asked.");

  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  const onAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onAbort);

  try {
    let response: Response;
    try {
      response = await tauriFetch(`${options.baseUrl}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders(options.token) },
        body: JSON.stringify({
          intent: text,
          ...(options.sessionId ? { session_id: options.sessionId } : {}),
        }),
        signal: controller.signal,
      });
    } catch (error) {
      if (timedOut) throw new SilkscreenError("timeout", "Hardy took too long to answer.");
      throw new SilkscreenError("offline", "Could not reach the engine.", {
        detail: (error as Error)?.message ?? "",
      });
    }

    if (!response.ok || !response.body) {
      throw new SilkscreenError(
        response.status === 404 ? "server" : "request",
        response.status === 404
          ? "This engine build has no /chat/stream route."
          : `The engine refused the question (HTTP ${response.status}).`,
        { status: response.status },
      );
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffered = "";
    let outcome: ChatOutcome | null = null;
    let failure: SilkscreenError | null = null;
    let ranBoard = false;

    const handle = (line: string) => {
      const frame = parseFrame(line);
      if (!frame) return;
      // A board frame means the orchestrator called `generate`. Recorded
      // before anything else, so a run cannot be reported as a plain reply.
      if (typeof frame.event === "string" && frame.event.startsWith("stage.")) {
        ranBoard = true;
      }
      const sentence = describeFrame(frame);
      if (sentence && options.onLine) options.onLine(sentence);
      if (frame.event === "chat.done") outcome = doneOutcome(frame, ranBoard);
      if (frame.event === "chat.error" || frame.event === "run.error") {
        failure = new SilkscreenError(
          "server",
          String(frame.error ?? "Hardy could not finish that."),
          { status: Number(frame.status ?? 500) },
        );
      }
    };

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffered += decoder.decode(value, { stream: true });
        let newline = buffered.indexOf("\n");
        while (newline !== -1) {
          handle(buffered.slice(0, newline));
          buffered = buffered.slice(newline + 1);
          newline = buffered.indexOf("\n");
        }
      }
    } catch (error) {
      if (timedOut) throw new SilkscreenError("timeout", "Hardy took too long to answer.");
      // A terminal frame already arrived, so the turn finished on the engine.
      // The socket dying afterwards does not un-finish it, and reporting a
      // completed turn as failed would throw away a board that was paid for.
      if (outcome === null && failure === null) throw error;
    }
    handle(buffered);

    if (failure) throw failure;
    if (!outcome) {
      throw new SilkscreenError(
        "server",
        "The engine closed the stream before Hardy answered.",
      );
    }
    return outcome;
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

function doneOutcome(frame: StreamFrame, ranBoard: boolean): ChatOutcome {
  const result = (frame.result ?? null) as RunResult | null;
  return {
    assistant: String(frame.assistant ?? "").trim(),
    needsClarification: Boolean(frame.needs_clarification),
    // Either signal counts: a stage frame seen live, or a result in hand.
    ranBoard: ranBoard || result !== null,
    result,
    model: String(frame.model ?? ""),
  };
}

import { useEffect, useRef } from "react";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { authHeaders } from "@/lib/silkscreen/client";

// Ideas sent from Slack, picked up here and started as an ordinary step run.
//
// The Slack bridge (`slackbot/bridge.py`, Socket Mode, on this laptop) files a
// message into the engine's inbox (`service/inbox.py`). This hook is the other
// half: it polls `GET /inbox` while Ada is idle, *accepts* the oldest idea
// (exclusive — a second Ada on another machine gets 409 and moves on), hands
// the sentence to the same `steps.start` the prompt bar uses, and then reports
// the step session id with `start` so the bridge can follow the run through
// the engine's own `GET /steps/<id>`. Accept-then-start is Buildkite's agent
// protocol (`buildkite/agent` `api/jobs.go`: `AcceptJob`, then `StartJob`);
// see service/inbox.py for why each state exists.
//
// The poll itself is `useEngineHealth`'s shape: a timer, an in-flight guard,
// and an unmount guard, and it never throws — no engine, or an engine without
// an inbox route, is an ordinary state.

/** Slack is a person typing; five seconds is instant enough and costs nothing. */
export const INBOX_POLL_INTERVAL_MS = 5_000;

export interface InboxIdea {
  id: string;
  text: string;
  source: string;
  user: string;
}

export interface IdeaInboxOptions {
  baseUrl: string;
  token?: string;
  /** Anything in flight, in either state machine. Nothing is accepted while true. */
  busy: boolean;
  /** `steps.session`: the id a started run reports back. */
  session: string | null;
  /** `steps.status`: an `error` before a new session means the start failed. */
  status: string;
  /** Start the run. Called once per accepted idea, with the sentence. */
  onIdea: (idea: InboxIdea) => void;
  enabled?: boolean;
  intervalMs?: number;
}

async function post(
  baseUrl: string,
  token: string,
  path: string,
  body: Record<string, unknown>,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const response = await tauriFetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify(body),
  });
  let parsed: Record<string, unknown> = {};
  try {
    parsed = (await response.json()) as Record<string, unknown>;
  } catch {
    /* an empty body is still an answer */
  }
  return { status: response.status, body: parsed };
}

/** One name per overlay window, so the engine can tell two desktops apart. */
function newClaimant(): string {
  const c = globalThis.crypto;
  const tail =
    typeof c?.randomUUID === "function"
      ? c.randomUUID().slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `ada-desktop-${tail}`;
}

export function useIdeaInbox({
  baseUrl,
  token = "",
  busy,
  session,
  status,
  onIdea,
  enabled = true,
  intervalMs = INBOX_POLL_INTERVAL_MS,
}: IdeaInboxOptions): void {
  const claimant = useRef(newClaimant());
  const inFlight = useRef(false);
  const mounted = useRef(true);
  // The idea this window accepted and has not yet reported started or failed,
  // with the session that was on screen *before* it, so an old run's id is
  // never reported as the new one.
  const claimed = useRef<{ id: string; before: string | null } | null>(null);
  const live = useRef({ busy, session, onIdea });
  live.current = { busy, session, onIdea };

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const poll = async () => {
      if (inFlight.current || claimed.current || live.current.busy) return;
      inFlight.current = true;
      try {
        const response = await tauriFetch(`${baseUrl}/inbox`, {
          method: "GET",
          headers: authHeaders(token),
        });
        if (!response.ok) return; // an older engine with no inbox: 404, forever
        const { ideas } = (await response.json()) as { ideas?: InboxIdea[] };
        const next = ideas?.[0];
        if (!next || !mounted.current || live.current.busy) return;
        const accepted = await post(baseUrl, token, `/inbox/${encodeURIComponent(next.id)}/accept`, {
          claimant: claimant.current,
        });
        // 409: another Ada took it, or it expired. Either way, not ours.
        if (accepted.status !== 200 || !mounted.current) return;
        claimed.current = { id: next.id, before: live.current.session };
        live.current.onIdea(next);
      } catch {
        /* no engine is an ordinary state; the next tick tries again */
      } finally {
        inFlight.current = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), intervalMs);
    return () => window.clearInterval(timer);
  }, [baseUrl, token, enabled, intervalMs]);

  // Report the outcome of the start once the step machine settles on one.
  useEffect(() => {
    const claim = claimed.current;
    if (!claim) return;
    const report = (path: string, body: Record<string, unknown>) => {
      claimed.current = null;
      void post(baseUrl, token, `/inbox/${encodeURIComponent(claim.id)}/${path}`, {
        claimant: claimant.current,
        ...body,
      }).catch(() => {
        /* the engine fails an unreported accept on its own after five minutes */
      });
    };
    if (session && session !== claim.before) {
      report("start", { session });
    } else if (status === "error") {
      report("fail", { detail: "Hardy could not start the design on the laptop" });
    }
  }, [session, status, baseUrl, token]);
}

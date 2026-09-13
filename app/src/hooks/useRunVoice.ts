// The seam between a finished run and the voice.
//
// The run state machine (`useSilkscreenRunState`) sets `result`, flips
// `status` to "done", and prepends the history entry in one synchronous
// block, so they land in the same render. That history entry's id is the
// only stable per-run identity the state exposes, and it is what keys the
// "speak once" guard — a re-render, a tab away and back, or the same result
// object flowing through again must not restart the digest.
//
// This hook is called from the overlay page, NOT from RunProvider: each
// window has its own provider instance, and a digest read simultaneously by
// two webviews is the talking-over-itself failure the speaker exists to
// prevent.

import { useEffect, useRef } from "react";
import type { SilkscreenRun } from "@/hooks/useSilkscreenRun";
import { announce, speaker, summarizeRun } from "@/lib/speech";

export function useRunVoice(run: SilkscreenRun): void {
  const spokenIdRef = useRef<string | null>(null);

  // A stale digest must never talk over a fresh run: the moment a new run is
  // asked for, whatever is still being said about the last one stops.
  useEffect(() => {
    if (run.status === "starting") speaker.stop();
  }, [run.status]);

  useEffect(() => {
    // Only the live run speaks. Reopening a past run from history is the
    // user re-reading, not the run finishing.
    if (run.status !== "done" || !run.result || run.viewingHistory) return;
    const id = run.history[0]?.id;
    if (!id || spokenIdRef.current === id) return;
    // Mark before announcing: `announce` may decline (muted, or the human is
    // holding the microphone), and unmuting or letting go later must not make
    // an old result suddenly start talking.
    spokenIdRef.current = id;
    // Through `announce`, not `speaker`, so the mute in the voice menu and the
    // microphone's right of way apply to the digest like everything else.
    void announce(summarizeRun(run.result));
  }, [run.status, run.result, run.viewingHistory, run.history]);

  // An unmounting overlay takes its voice with it.
  useEffect(() => () => speaker.stop(), []);
}

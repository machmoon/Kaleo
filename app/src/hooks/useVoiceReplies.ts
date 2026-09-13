// The other half of the conversation.
//
// `useRunVoice` speaks the digest of a finished one-shot run. This speaks the
// moments in the step-by-step flow where the machine has stopped and is
// waiting for a human: a stage landed and wants approving, a stage failed, a
// run refused. Those are turns; everything else in the flow is narration and
// stays silent (see `moments.ts` for the rule).
//
// The "once" guard is a key made of the facts that would change the sentence,
// not a timestamp: a re-render, an elapsed-second tick or the same response
// flowing through a second time must not make it repeat itself. Something the
// engineer has already been told is noise the second time, and noise is what
// gets the whole voice muted.

import { useEffect, useRef } from "react";
import type { StepRun } from "@/hooks/useStepRun";
import { announce, stageFailedLine, stageReadyLine } from "@/lib/speech";

export function useVoiceReplies(steps: StepRun): void {
  const spokenRef = useRef<string | null>(null);

  useEffect(() => {
    // A stage in flight is the one moment with nothing to answer: the strip
    // already shows the seconds, and a voice reading them out is the noise
    // this hook exists to not make.
    if (steps.status === "running" || steps.status === "idle") return;

    if (steps.status === "error") {
      const key = `error:${steps.failedStep ?? "?"}:${steps.error?.message ?? ""}`;
      if (spokenRef.current === key) return;
      spokenRef.current = key;
      void announce(stageFailedLine(steps.failedStep, steps.error?.message));
      return;
    }

    const latest = steps.history[steps.history.length - 1];
    if (!latest) return;
    // History length, not the step name: approving the same stage twice after
    // a reconcile is two landings and deserves two sentences, while one
    // landing re-rendered five times deserves one.
    const key = `${steps.status}:${steps.history.length}:${latest.step}:${steps.available.join(",")}`;
    if (spokenRef.current === key) return;
    spokenRef.current = key;
    void announce(stageReadyLine(latest, steps.available));
  }, [
    steps.status,
    steps.history,
    steps.available,
    steps.failedStep,
    steps.error,
  ]);
}

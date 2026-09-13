import { useState } from "react";
import { Button } from "@/components/ui";
import { StepFrame } from "@/components/setup";
import { skippedSentence, type SetupCardId } from "@/lib/setup/machine";
import { ALL_DEMO_SENTENCE } from "@/lib/setup/service";

export const DONE_TITLE = "You're all set";
export const DONE_SUBTITLE = "The strip appears at the top of your screen. Want a 60-second tour?";

interface DoneStepProps {
  skipped: readonly SetupCardId[];
  /** Every connection made was a demo one: say so, in the sentence the plan fixes. */
  demo: boolean;
  onTour: () => Promise<void>;
  onNotNow: () => Promise<void>;
}

/**
 * Both buttons end the wizard: `setup_finish` reveals the strip either
 * way. Only the first also pushes one caption into it and starts the two
 * Tips cards in the dashboard.
 */
export const DoneStep = ({ skipped, demo, onTour, onNotNow }: DoneStepProps) => {
  const [busy, setBusy] = useState<"tour" | "later" | null>(null);
  const run = (which: "tour" | "later", fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(which);
    void fn().finally(() => setBusy(null));
  };
  const skippedLine = skippedSentence(skipped);
  return (
    <StepFrame stepId="done" title={DONE_TITLE} subtitle={DONE_SUBTITLE}>
      <div className="flex flex-col items-center gap-4">
        <div className="flex items-center gap-2">
          <Button onClick={() => run("tour", onTour)} disabled={busy !== null} data-testid="setup-take-tour">
            {busy === "tour" ? "Starting…" : "Take the tour"}
          </Button>
          <Button variant="outline" onClick={() => run("later", onNotNow)} disabled={busy !== null} data-testid="setup-not-now">
            Not now
          </Button>
        </div>
        {skippedLine ? (
          <p className="text-xs text-muted-foreground" data-testid="setup-skipped-line">
            {skippedLine}
          </p>
        ) : null}
        {demo ? (
          <p className="max-w-[440px] text-xs text-muted-foreground" data-testid="setup-all-demo">
            {ALL_DEMO_SENTENCE}
          </p>
        ) : null}
      </div>
    </StepFrame>
  );
};

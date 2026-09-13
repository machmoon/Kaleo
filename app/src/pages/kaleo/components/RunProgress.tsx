import {
  CheckIcon,
  CircleIcon,
  CircleSlashIcon,
  Loader2,
  MinusIcon,
  XIcon,
} from "lucide-react";
import { Badge, Button } from "@/components";
import type { StageState, StageStatus } from "@/lib/silkscreen/describe";
import { REVIEW_CLEAN_LINE, reviewFailure, reviewSkipped } from "@/lib/silkscreen/steps";
import type { Finding, RunResult } from "@/lib/silkscreen/types";
import type { SilkscreenError } from "@/lib/silkscreen/client";
import { cn } from "@/lib/utils";

function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const STATUS_ICON: Record<StageStatus, typeof CheckIcon> = {
  pending: CircleIcon,
  running: Loader2,
  done: CheckIcon,
  skipped: MinusIcon,
  unreported: CircleSlashIcon,
};

export interface RunProgressProps {
  stages: StageState[];
  /** Wall-clock seconds since submit. A real measurement, not a progress guess. */
  elapsedS: number;
  onCancel: () => void;
}

/**
 * What this button does, and — the half that used to be missing — what it
 * cannot do.
 *
 * It now reaches the engine (`POST /runs/<id>/cancel`), so the first sentence
 * is finally true. The second is `service/runs.py::cancel`'s own reasoning,
 * which the module states plainly: the cancel is a flag the pipeline reads
 * when it next emits an event, so a run inside a solve or a model call keeps
 * going until it next speaks, and that work is billed. The docstring cites
 * Celery's `Control.revoke` for the same behaviour. A button that said only
 * "Cancel" let a reader assume a kill.
 */
export const CANCEL_DETAIL =
  "Ask the engine to stop this run. It stops at its next pipeline event — a " +
  "model call or a solve already under way runs to completion and is still " +
  "billed.";

/**
 * The stage checklist and the clock.
 *
 * The clock is the only thing here that moves on its own. Every row's state
 * comes from a frame the engine sent, so a run that has gone quiet shows a
 * ticking clock over a still list — which is exactly what is happening.
 */
export const RunProgress = ({ stages, elapsedS, onCancel }: RunProgressProps) => {
  const ticked = stages.some(
    (stage) => stage.status === "running" || stage.status === "done"
  );

  return (
    <div className="flex flex-col gap-2" data-testid="run-progress">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
          <span className="text-xs font-medium">Generating</span>
          <span
            className="text-xs tabular-nums text-muted-foreground"
            data-testid="run-elapsed"
          >
            {formatElapsed(elapsedS)}
          </span>
        </div>
        {/* The title lives on a wrapper for the reason PromptBar's submit
            gives: a disabled element suppresses pointer events in some
            webviews, and this sentence is the only place the cost warning is
            written down on this surface. */}
        <span title={CANCEL_DETAIL}>
          <Button
            variant="outline"
            size="sm"
            onClick={onCancel}
            aria-label={CANCEL_DETAIL}
            data-testid="run-cancel"
          >
            <XIcon className="size-3.5" />
            Cancel
          </Button>
        </span>
      </div>

      <ul className="flex flex-col gap-0.5">
        {stages.map((stage) => {
          const Icon = STATUS_ICON[stage.status] ?? CircleIcon;
          const quiet = stage.status === "skipped" || stage.status === "unreported";
          return (
            <li
              key={stage.id}
              className="flex items-center gap-2 text-xs"
              data-testid="run-stage"
              data-stage={stage.id}
              data-status={stage.status}
              title={stage.note ?? stage.summary ?? stage.descriptor.detail}
            >
              <Icon
                className={cn(
                  "size-3.5 shrink-0",
                  stage.status === "running" && "animate-spin",
                  stage.status === "done" && "text-foreground",
                  (stage.status === "pending" || quiet) && "text-muted-foreground/50"
                )}
              />
              <span
                className={cn(
                  stage.status === "pending" && "text-muted-foreground",
                  quiet && "text-muted-foreground/60"
                )}
              >
                {stage.descriptor.label}
              </span>
              {stage.durationS !== null ? (
                <span className="tabular-nums text-[11px] text-muted-foreground/60">
                  {stage.durationS.toFixed(1)}s
                </span>
              ) : null}
              {quiet && stage.note ? (
                <span className="truncate text-[11px] text-muted-foreground/60">
                  {stage.note}
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>

      {!ticked ? (
        // Nothing has arrived yet. The clock is real; the list has not moved,
        // and saying so is better than a bar that fills because time passed.
        <p className="text-[11px] text-muted-foreground">
          Waiting for the engine's first event — the clock is real, the list
          below it has not moved yet.
        </p>
      ) : null}
    </div>
  );
};

// The engine's own vocabulary (agents/review.py). The audit CLI's
// error/warning/info never flows through /generate, but keep it sorted after
// so a future response carrying it still ranks sensibly.
const SEVERITY_ORDER: string[] = [
  "blocker",
  "error",
  "marginal",
  "warning",
  "note",
  "info",
];

function countBySeverity(findings: Finding[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const finding of findings) {
    const key = String(finding.severity ?? "unknown");
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

function severityVariant(severity: string) {
  if (severity === "blocker" || severity === "error") {
    return "destructive" as const;
  }
  if (severity === "marginal" || severity === "warning") {
    return "secondary" as const;
  }
  return "outline" as const;
}

function orderSeverities(counts: Map<string, number>): string[] {
  const known = SEVERITY_ORDER.filter((s) => counts.has(s));
  const rest = [...counts.keys()].filter((s) => !SEVERITY_ORDER.includes(s));
  return [...known, ...rest];
}

/** A measured value, or an explicit statement that the response carried none. */
const Stat = ({
  label,
  value,
  testId,
}: {
  label: string;
  value: string | null;
  testId: string;
}) => (
  <div className="flex flex-col" data-testid={testId}>
    <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
      {label}
    </span>
    <span
      className={cn("text-xs tabular-nums", value === null && "text-muted-foreground/60")}
    >
      {value ?? "not reported"}
    </span>
  </div>
);

export interface RunSummaryProps {
  result: RunResult;
  /** Whether this run asked for the review, so an empty list can be explained. */
  reviewRequested: boolean;
  /** This app's wall clock, used only when the response reports no duration. */
  elapsedS: number;
  onOpenReview: () => void;
  onNewRun: () => void;
}

/**
 * What the run produced, in the response's own numbers.
 *
 * A field the response did not carry reads "not reported" rather than zero: an
 * absent net list and an empty net list are different boards.
 */
export const RunSummary = ({
  result,
  reviewRequested,
  elapsedS,
  onOpenReview,
  onNewRun,
}: RunSummaryProps) => {
  const board = result.placements?.board_mm;
  const partCount = result.parts?.length ?? result.placements?.parts?.length ?? null;
  const netCount = result.nets?.length ?? null;
  const findings = result.findings;
  const counts = findings ? countBySeverity(findings) : null;
  // What became of the critic, in the words every other surface uses. A
  // failed or skipped review leaves an empty finding list, and that list is
  // not a clean board — so it is read before the findings are counted.
  const unreviewed = reviewFailure(result.review) ?? reviewSkipped(result.review);

  return (
    <div className="flex flex-col gap-3" data-testid="run-summary">
      <div className="grid grid-cols-3 gap-x-3 gap-y-2 sm:grid-cols-5">
        <Stat
          label="Board"
          testId="summary-board"
          value={board ? `${board[0].toFixed(1)} × ${board[1].toFixed(1)} mm` : null}
        />
        <Stat
          label="Parts"
          testId="summary-parts"
          value={partCount === null ? null : String(partCount)}
        />
        <Stat
          label="Nets"
          testId="summary-nets"
          value={netCount === null ? null : String(netCount)}
        />
        <Stat
          label="Wirelength"
          testId="summary-wirelength"
          value={
            typeof result.wirelength_mm === "number"
              ? `${result.wirelength_mm.toFixed(1)} mm`
              : null
          }
        />
        <Stat
          label="Took"
          testId="summary-duration"
          value={
            typeof result.duration_s === "number"
              ? `${result.duration_s.toFixed(1)} s`
              : `${elapsedS.toFixed(1)} s (our clock)`
          }
        />
      </div>

      <div className="flex flex-wrap items-center gap-1.5" data-testid="summary-findings">
        {unreviewed ? (
          <span
            className={cn(
              "text-[11px]",
              result.review?.status === "failed" ? "font-medium text-destructive" : "text-muted-foreground"
            )}
            data-testid="summary-review-outcome"
            data-status={result.review?.status}
          >
            {`${unreviewed[0].toUpperCase()}${unreviewed.slice(1)}.`}
          </span>
        ) : findings === undefined ? (
          <span className="text-[11px] text-muted-foreground">
            This response carried no review — nothing was checked, which is not
            the same as nothing being wrong.
          </span>
        ) : findings.length === 0 ? (
          <span className="text-[11px] text-muted-foreground" data-testid="summary-review-outcome" data-status={result.review?.status}>
            {result.review
              ? REVIEW_CLEAN_LINE
              : // Older engine, no block: it never said what became of the
                // critic, so the empty list keeps the reading it always had.
                reviewRequested
                ? "The review ran and reported nothing. Only the checks it runs were run."
                : "Review was off for this run, so no checks ran."}
          </span>
        ) : (
          orderSeverities(counts!).map((severity) => (
            <Badge
              key={severity}
              variant={severityVariant(severity)}
              data-testid="summary-severity"
              data-sev={severity}
            >
              {counts!.get(severity)} {severity}
            </Badge>
          ))
        )}
        {findings && findings.length > 0 ? (
          // Proven and suggested are not the same claim: one is a measurement,
          // the other is a model's opinion. The compact summary at least keeps
          // the split visible; the review pane draws it per finding.
          <span
            className="text-[11px] text-muted-foreground"
            data-testid="summary-origins"
          >
            {findings.filter((f) => f.origin === "proven").length} measured,{" "}
            {findings.filter((f) => f.origin !== "proven").length} unattributed
          </span>
        ) : null}
        {result.blockers?.length ? (
          <Badge variant="destructive" data-testid="summary-blockers">
            {result.blockers.length} blocking
          </Badge>
        ) : null}
      </div>

      <div className="flex items-center gap-2">
        <Button size="sm" onClick={onOpenReview} data-testid="summary-open-review">
          Open the full review
        </Button>
        <Button size="sm" variant="ghost" onClick={onNewRun} data-testid="summary-new-run">
          New board
        </Button>
      </div>
    </div>
  );
};

/**
 * What the user should do about a failure, keyed off `SilkscreenError.kind`.
 *
 * "Nothing is listening on the port" and "the model provider fell over" both
 * arrive as a failed run, and only one of them is fixed by waiting. A status
 * code cannot tell them apart, which is why `kind` exists.
 */
function explain(
  error: SilkscreenError,
  baseUrl: string
): { title: string; body: string; hint?: string } {
  switch (error.kind) {
    case "offline":
      return {
        title: "The engine isn't running.",
        body: `Nothing answered at ${baseUrl}. Hardy talks to the silkscreen engine over HTTP, so the service has to be up before a run can start.`,
        hint: "PORT=8081 python -m service.app",
      };
    case "setup":
      return {
        title: "The engine has no API key.",
        body: "It is running, but GOOGLE_API_KEY is not in its environment, so it cannot call the model. That is a setup step, not an outage.",
        hint: "export GOOGLE_API_KEY=… && PORT=8081 python -m service.app",
      };
    case "auth":
      return {
        title: "The engine refused the token.",
        body: "It answered 401 unauthorized. This engine is running behind a token gate, and the access token on the Engine page is missing or wrong.",
      };
    case "request":
      return {
        title: "The engine refused the request.",
        body: error.message,
      };
    case "upstream":
      return {
        title: "The model provider failed.",
        body: "The engine reached the model and the model did not answer. Nothing is wrong with your prompt.",
      };
    case "timeout":
      return {
        title: "The run ran out of time.",
        body: "The run passed this app\u2019s 300 second ceiling and was cancelled. A smaller board, or a shorter placer budget, comes back sooner.",
      };
    case "cancelled":
      return { title: "Run cancelled.", body: "Nothing was written." };
    default:
      return {
        title: "The engine hit a bug.",
        body: error.message || "The engine failed without saying why.",
      };
  }
}

export interface RunFailureProps {
  error: SilkscreenError;
  baseUrl: string;
  onRetry: () => void;
  onDismiss: () => void;
}

export const RunFailure = ({ error, baseUrl, onRetry, onDismiss }: RunFailureProps) => {
  const { title, body, hint } = explain(error, baseUrl);

  return (
    <div className="flex flex-col gap-2" data-testid="run-failure" data-kind={error.kind}>
      <div className="flex items-start gap-2">
        <XIcon className="mt-0.5 size-3.5 shrink-0 text-destructive" />
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium">{title}</span>
          <p className="text-[11px] text-muted-foreground">{body}</p>
          {hint ? (
            <code className="w-fit rounded-md bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
              {hint}
            </code>
          ) : null}
          {error.errorId ? (
            <span className="text-[11px] text-muted-foreground/70">
              Engine error id {error.errorId}
            </span>
          ) : null}
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" onClick={onRetry} data-testid="failure-retry">
          Try again
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss} data-testid="failure-dismiss">
          Dismiss
        </Button>
      </div>
    </div>
  );
};

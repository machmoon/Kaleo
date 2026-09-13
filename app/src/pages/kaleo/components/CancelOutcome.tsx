import { Button } from "@/components";
import type { Cancellation, MeteringBlock } from "@/hooks/useSilkscreenRun";
import { cn } from "@/lib/utils";

/**
 * What pressing Cancel actually did.
 *
 * Until `service/runs.py` gave a streamed run a name, this app's Cancel was an
 * `AbortController` and nothing else: the socket closed, the pipeline went on
 * solving and went on spending, and the strip said "Run cancelled. The engine
 * may have finished the work it had already started, but nothing came back."
 * That sentence was a guess in both directions — it could not know whether the
 * engine stopped, and it could not know what the attempt cost.
 *
 * Now the engine can be asked, so this card reports rather than guesses. It
 * renders `useSilkscreenRun`'s `cancellation`, which keeps three states apart
 * and never lets them collapse:
 *
 * - `asked` — the POST is out and nothing has answered. Neither stopped nor
 *   still running: not known yet, and it says so.
 * - `settled` — the engine answered and a poll saw the run reach a terminal
 *   state. The only state allowed to say what it cost.
 * - `unknown` — we could not ask, or we asked and the run was still going when
 *   we stopped watching. **It may still be spending**, and that is the whole
 *   reason this state exists as its own word.
 *
 * The three-way split is Argo Workflows' `NodePhase`
 * (`pkg/apis/workflow/v1alpha1/workflow_types.go`), which keeps `Skipped`,
 * `Failed` and `Error` — "an error other than a non-0 exit code", i.e. no
 * verdict at all — as three separate phases rather than folding the last into
 * the second. The tone follows BuildKit's `progressui`
 * (`util/progress/progressui/display.go`, `trace.displayInfo`), where
 * `isCanceled` is tracked as a flag distinct from `hasError` and coloured with
 * `colorCancel` rather than `colorError`: a deliberate stop is not a failure
 * and must not be painted as one.
 */

/**
 * The cost line, in the vocabulary `service/metering.py` actually uses.
 *
 * Four answers, and the fourth is the one that is easy to lose: the engine
 * said nothing at all about cost (an older service, or a frame this window
 * never saw). "Not charged" and "we were not told" are different facts, and
 * a UI that renders the second as the first is inventing a reassurance.
 *
 * Where a count is zero it gets a word rather than a numeral — vitest's
 * `getStateString` (`packages/vitest/src/node/reporters/renderers/utils.ts`,
 * shipped at `vitest/dist/chunks/index.*.js:99`) opens with
 * `if (tasks.length === 0) return c.dim('no tests')` and omits every
 * zero-valued category instead of printing "0 failed". A small surface earns
 * its space by never printing a zero.
 */
export function costLine(metering: MeteringBlock | null): string {
  if (!metering) {
    return "The engine did not say what this cost.";
  }
  if (!metering.enabled) {
    // `off_block()` is a dict with a reason precisely so this can be a
    // sentence rather than an absence. Rendered as the engine wrote it.
    return metering.reason
      ? `Not charged — ${metering.reason}.`
      : "Not charged — metering is off.";
  }
  if (metering.state === "unrecorded") {
    return metering.reason
      ? `Charged, but the ledger did not record it — ${metering.reason}.`
      : "Charged, but the ledger did not record it.";
  }
  const charged = metering.charged_mkcu;
  if (typeof charged !== "number") {
    return "Metered, but this window was not told the amount.";
  }
  if (charged <= 0) {
    return "Billed nothing — the run stopped before it used any credit.";
  }
  const cents = metering.cost_cents;
  const money =
    typeof cents === "number" && cents > 0
      ? ` (${(cents / 100).toFixed(2)} USD)`
      : "";
  // mKCU is the engine's own integer unit, kept as an integer here for the
  // reason `billing/units.py` gives: a float that reaches a balance is a
  // reconciliation bug months later. Shown as engine-minutes beside it
  // because "1 KCU = one minute of attributable engine wall-clock" is the
  // only part of that a hardware engineer has any use for.
  const minutes = (charged / 1000).toFixed(2);
  return `Billed ${charged} mKCU${money} — about ${minutes} min of engine time. Work already under way is billed whether or not anyone waited for it.`;
}

/** The engine's own sentence, or this app's fallback when it sent none. */
function headlineFor(cancellation: Cancellation): string {
  if (cancellation.state === "asked") {
    return "Asking the engine to stop…";
  }
  if (cancellation.state === "unknown") {
    // Never the word "cancelled". We do not know that.
    return "Asked the engine to stop — it has not confirmed.";
  }
  // Settled: the engine's `headline`, rendered verbatim. The service writes
  // "cancelled -- the run stops at its next pipeline event" or
  // "already <state>; nothing was stopped", and improving on either would be
  // this window speaking for the engine.
  return cancellation.headline ?? `The run is ${cancellation.runState ?? "over"}.`;
}

export interface CancelOutcomeProps {
  cancellation: Cancellation | null;
  onDismiss: () => void;
}

export const CancelOutcome = ({ cancellation, onDismiss }: CancelOutcomeProps) => {
  // Nothing was asked for. The old sentence is kept for exactly this case: a
  // run this window stopped listening to without ever naming, which is what
  // happens when the stream died before `run.accepted`.
  if (!cancellation) {
    return (
      <div
        className="kv-settle flex items-center justify-between gap-2 border-t border-input/40 pt-2"
        data-testid="cancel-outcome"
        data-state="unasked"
      >
        <span className="text-[11px] text-muted-foreground">
          Run cancelled here. This window never had a name for it, so the engine
          could not be asked whether it stopped.
        </span>
        <Button size="sm" variant="ghost" onClick={onDismiss} data-testid="cancelled-dismiss">
          Dismiss
        </Button>
      </div>
    );
  }

  const { state } = cancellation;
  // `unknown` is the only state that warns, and it warns because the run may
  // still be spending. `settled` is a deliberate stop, so it stays quiet —
  // BuildKit's colorCancel, not colorError.
  const unresolved = state === "unknown";

  return (
    <div
      className={cn(
        "kv-settle kv-settle-group flex flex-col gap-1 border-t pt-2",
        unresolved ? "border-amber-500/60" : "border-input/40"
      )}
      data-testid="cancel-outcome"
      data-state={state}
      data-run-state={cancellation.runState ?? undefined}
    >
      <div className="flex items-start justify-between gap-2">
        <span
          className={cn(
            "text-[11px] font-medium leading-tight",
            unresolved ? "text-amber-600 dark:text-amber-400" : "text-foreground"
          )}
          data-testid="cancel-headline"
        >
          {headlineFor(cancellation)}
        </span>
        {state === "asked" ? null : (
          <Button
            size="sm"
            variant="ghost"
            onClick={onDismiss}
            data-testid="cancelled-dismiss"
          >
            Dismiss
          </Button>
        )}
      </div>

      {/* Why there is no verdict. Only ever present on `unknown`, and it
          carries the run's name, because the name is what makes the run
          checkable rather than merely lost. */}
      {unresolved ? (
        <p className="text-[10px] text-amber-600 dark:text-amber-400" data-testid="cancel-unresolved">
          {cancellation.detail ? `${cancellation.detail} ` : ""}
          {`It may still be running as ${cancellation.runId}.`}
        </p>
      ) : null}

      {/* What a cancel cannot interrupt — the engine's own sentence, and the
          reason a cancel is not a refund. */}
      {cancellation.notStoppable ? (
        <p className="text-[10px] text-muted-foreground" data-testid="cancel-not-stoppable">
          {cancellation.notStoppable}
        </p>
      ) : null}

      {/* Where it stops, when something was genuinely in flight. Absent when
          the run had already ended, because "aborts at" would then be a
          promise about nothing. */}
      {cancellation.abortsAt ? (
        <p className="text-[10px] text-muted-foreground" data-testid="cancel-aborts-at">
          {`Stops at ${cancellation.abortsAt}.`}
        </p>
      ) : null}

      {/* Cost. Only once the run has actually settled: a figure quoted while
          the pipeline is still spending would be out of date the moment it
          was read, and this surface has one job — not to overstate what it
          knows. */}
      {state === "settled" ? (
        <p className="text-[10px] text-muted-foreground" data-testid="cancel-cost">
          {costLine(cancellation.metering)}
        </p>
      ) : null}
    </div>
  );
};

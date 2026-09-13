import { formatMarginMm } from "@/lib/silkscreen/steps";
import type { MarginAxis as MarginAxisData, MarginTick } from "@/lib/silkscreen/steps";
import { cn } from "@/lib/utils";

/**
 * The kernel's thirteen clauses on one signed axis.
 *
 * The receipt used to be thirteen rows of code identifiers, folded by pass and
 * fail. Both halves of that were wrong. Thirteen rows is a wall in a strip
 * that floats over the board, and folding by verdict throws away the reason
 * the kernel emits *signed* margins: a clause that passes by two hundredths of
 * a millimetre is the interesting one, and a verdict fold deletes it.
 *
 * So every clause is a tick, positioned by its margin, with zero marked. The
 * shape is the point. A case that clears everything by a hair looks nothing
 * like one that clears comfortably, and that difference is readable before any
 * of the names are. Only the clauses that failed or came in under the
 * engine's own tolerance band get a row with words; the rest are a count, and
 * the whole receipt is one button away.
 */
const TONE_TICK: Record<MarginTick["tone"], string> = {
  fail: "h-4 w-[2px] bg-destructive",
  thin: "h-3 w-[2px] bg-warn",
  clear: "h-2 w-px bg-foreground/35",
};

const TONE_TEXT: Record<MarginTick["tone"], string> = {
  fail: "text-destructive",
  thin: "text-warn",
  clear: "text-muted-foreground",
};

const ClauseRow = ({ tick, detail }: { tick: MarginTick; detail: string }) => (
  <li
    className="flex flex-col text-[11px] leading-tight"
    data-testid="case-clause"
    data-clause={tick.name}
    data-tone={tick.tone}
    data-passed={tick.tone !== "fail"}
  >
    <span className="flex items-baseline gap-1.5">
      <span
        className={cn("w-8 shrink-0 font-medium uppercase", TONE_TEXT[tick.tone])}
        data-testid="case-clause-tone"
      >
        {tick.tone === "fail" ? "fail" : "thin"}
      </span>
      <code className="min-w-0 flex-1 truncate">{tick.name}</code>
      <span
        className={cn("shrink-0 tabular-nums", TONE_TEXT[tick.tone])}
        data-testid="case-clause-margin"
      >
        {tick.marginMm === null ? "no margin" : `${formatMarginMm(tick.marginMm)} mm`}
      </span>
    </span>
    {detail ? (
      // A failure says what was measured, on screen. A thin pass keeps its
      // detail for assistive tech and the full receipt: it is a warning, and
      // a warning that costs two lines stops being read.
      <span
        className={cn(
          "pl-[2.375rem] text-muted-foreground",
          tick.tone !== "fail" && "sr-only"
        )}
        data-testid="case-clause-detail"
      >
        {detail}
      </span>
    ) : null}
  </li>
);

export interface MarginAxisProps {
  axis: MarginAxisData;
  /** Clause name to its measured detail, for the rows that get words. */
  details: Record<string, string>;
}

export const MarginAxis = ({ axis, details }: MarginAxisProps) => {
  const named = [...axis.fail, ...axis.thin];
  const band = axis.bandMm;

  return (
    <div className="flex flex-col gap-1" data-testid="case-margin-axis">
      <p className="text-[11px] text-muted-foreground" data-testid="case-axis-summary">
        {axis.ticks.length} clauses measured
        {axis.fail.length ? (
          <span className="text-destructive">
            {" · "}
            {axis.fail.length} fail{axis.fail.length === 1 ? "s" : ""}
          </span>
        ) : null}
        {axis.thin.length && band !== null ? (
          <span className="text-warn">
            {" · "}
            {axis.thin.length} inside the {band.toFixed(2)} mm print tolerance
          </span>
        ) : null}
        {!axis.fail.length && !axis.thin.length ? " · all clear" : null}
      </p>

      {/* The axis itself. Ticks are absolutely placed, so the row costs one
          line of height whatever the clause count is. */}
      <div className="relative h-4 w-full" aria-hidden="true">
        <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-input" />
        {axis.zeroX === null ? null : (
          <div
            className="absolute top-0 h-4 w-px bg-foreground/50"
            style={{ left: `${axis.zeroX * 100}%` }}
            data-testid="case-axis-zero"
          />
        )}
        {axis.ticks.map((tick) =>
          tick.x === null ? null : (
            <div
              key={tick.name}
              className={cn(
                "absolute top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full",
                TONE_TICK[tick.tone]
              )}
              style={{ left: `${tick.x * 100}%` }}
              title={`${tick.name} ${formatMarginMm(tick.marginMm ?? 0)} mm`}
            />
          )
        )}
      </div>

      {/* Every clause is present as data whether or not it has a row of words:
          the receipt is complete, the reading is triaged. */}
      <ul className="sr-only">
        {axis.ticks.map((tick) => (
          <li
            key={tick.name}
            data-testid="case-clause-tick"
            data-clause={tick.name}
            data-tone={tick.tone}
            data-passed={tick.tone !== "fail"}
          >
            <span data-testid="case-tick-margin">
              {tick.marginMm === null ? "no margin" : `${formatMarginMm(tick.marginMm)} mm`}
            </span>
          </li>
        ))}
      </ul>

      <div className="flex justify-between text-[11px] tabular-nums text-muted-foreground/70">
        <span>{formatMarginMm(axis.loMm)}</span>
        <span>{formatMarginMm(axis.hiMm)} mm</span>
      </div>

      {named.length ? (
        <ul className="flex flex-col gap-0.5">
          {named.map((tick) => (
            <ClauseRow key={tick.name} tick={tick} detail={details[tick.name] ?? ""} />
          ))}
        </ul>
      ) : null}

      {axis.clearCount ? (
        <p className="text-[11px] text-muted-foreground" data-testid="case-clear-count">
          {axis.clearCount} clear
          {band === null ? "" : ` by more than ${band.toFixed(2)} mm`}
        </p>
      ) : null}
    </div>
  );
};

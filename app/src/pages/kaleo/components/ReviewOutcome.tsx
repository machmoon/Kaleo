import { Badge } from "@/components";
import {
  REVIEW_CLEAN_LINE,
  findingCitation,
  findingProvenance,
  policyFlag,
  reviewFailure,
  reviewSkipped,
  NO_CITATION,
} from "@/lib/silkscreen/steps";
import type { BadgeTone, ReviewDetails } from "@/lib/silkscreen/steps";
import { cn } from "@/lib/utils";

/**
 * The four tones, in both themes. The strip floats over pcbnew's near-black
 * canvas and over eeschema's near-white sheet, so every tone is defined for
 * both rather than inheriting one and hoping.
 */
export const TONE_CLASS: Record<BadgeTone, string> = {
  bad: "border-destructive/60 text-destructive",
  warn: "border-amber-500/60 text-amber-600 dark:text-amber-400",
  ok: "border-emerald-500/60 text-emerald-700 dark:text-emerald-400",
  muted: "border-input/60 text-muted-foreground",
};

/**
 * What the review step found, with the provenance of every finding.
 *
 * This is the engine's own honesty philosophy reaching the surface. The audit
 * CLI separates a rule that *measured* the board (`Origin.PROVEN`, carrying
 * its measurement in `evidence`) from a model that *proposed* something
 * (`Origin.SUGGESTED`), and refuses to let the second enter as the first. The
 * review step on this wire is the adversarial critic, so unless a finding says
 * otherwise every row here is a proposal — and says `SUGGESTED · critic, not
 * measured` rather than letting a reader assume a check ran.
 *
 * The same rule governs the citation: the quoted datasheet text with its page
 * when the engine quoted one, and the words "No citation" when it did not.
 * A blank line where a citation would be reads as "checked and fine".
 *
 * Rendered in `StepPanel` beside the case, order and BOM receipts. It appears
 * mid-run rather than substituting for something already on screen, so it
 * wears `kv-settle` (motion.css §3) and slides the last 4 px into place
 * instead of popping; `kv-settle-group` staggers its rows for the same reason.
 */
export const ReviewOutcome = ({ details }: { details: ReviewDetails }) => {
  const { findings, blockers, status } = details;
  const measured = findings.filter((f) => findingProvenance(f).measured).length;
  const review = { status, ran: details.ran, detail: details.detail, note: details.note };
  const failed = reviewFailure(review);
  const skipped = reviewSkipped(review);

  // Three outcomes, three cards. A critic that answered nothing (`failed`)
  // or was never asked (`skipped`) left an empty finding list behind, and
  // that list is not evidence of anything: the card wears the failure tone,
  // says so in the engine's words, and grows no rows — never the
  // nothing-to-flag treatment an `ok` review with no findings earns.
  if (failed || skipped) {
    const unreviewed = failed ?? skipped ?? "";
    return (
      <div
        className={cn(
          "kv-settle flex flex-col gap-1 rounded-md border p-2",
          failed ? "border-destructive/60" : "border-amber-500/60"
        )}
        data-testid="review-outcome"
        data-status={status}
      >
        <p
          className={cn("text-[11px] font-medium", failed ? "text-destructive" : "text-amber-600 dark:text-amber-400")}
          data-testid="review-counts"
        >
          {`${unreviewed[0].toUpperCase()}${unreviewed.slice(1)}.`}
        </p>
        {details.note && details.note !== details.detail ? (
          <p className="text-[10px] text-muted-foreground" data-testid="review-note">
            {details.note}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div
      className="kv-settle kv-settle-group flex flex-col gap-1.5 rounded-md border border-input/40 p-2"
      data-testid="review-outcome"
      data-status={status}
    >
      <p className="text-[11px] text-muted-foreground" data-testid="review-counts">
        {findings.length === 0
          ? `${REVIEW_CLEAN_LINE} No rule measured the board.`
          : `${findings.length} finding${findings.length === 1 ? "" : "s"}, ${blockers} blocking · ${measured} measured by a rule`}
      </p>

      {findings.map((finding, index) => {
        const flag = policyFlag(finding.severity);
        const provenance = findingProvenance(finding);
        const citation = findingCitation(finding);
        const refs = finding.refs?.length ? finding.refs : (finding.parts ?? []);
        return (
          <div
            key={finding.id ?? `${finding.severity}-${index}`}
            className="flex flex-col gap-0.5 rounded border border-input/40 p-1.5"
            data-testid="finding"
            data-sev={String(finding.severity)}
            data-origin={provenance.origin}
          >
            <div className="flex items-start gap-1.5">
              <Badge
                variant="outline"
                className={cn("h-4 shrink-0 px-1 text-[9px] tracking-wide", TONE_CLASS[flag.tone])}
                data-testid="finding-flag"
              >
                {flag.label}
              </Badge>
              <span className="min-w-0 flex-1 text-[11px] font-medium leading-tight">
                {finding.title ?? finding.detail ?? String(finding.severity)}
              </span>
              {refs.length ? (
                <span
                  className="shrink-0 font-mono text-[10px] text-muted-foreground"
                  data-testid="finding-refs"
                >
                  {refs.join(" · ")}
                </span>
              ) : null}
            </div>

            {finding.title && finding.detail ? (
              <p className="text-[11px] leading-tight text-muted-foreground" data-testid="finding-detail">
                {finding.detail}
              </p>
            ) : null}

            <p
              className={cn(
                "text-[10px] tracking-wide",
                provenance.measured ? "text-emerald-700 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400"
              )}
              data-testid="finding-origin"
              data-measured={provenance.measured ? "true" : "false"}
            >
              {provenance.label} · {provenance.source}
            </p>

            {provenance.evidence ? (
              <p className="text-[10px] text-muted-foreground" data-testid="finding-evidence">
                {provenance.evidence}
              </p>
            ) : null}

            <p
              className={cn(
                "text-[10px] leading-tight",
                citation ? "text-muted-foreground" : "text-amber-600 dark:text-amber-400"
              )}
              data-testid="finding-citation"
              data-cited={citation ? "true" : "false"}
              data-page={citation?.page ?? undefined}
            >
              {citation ? (
                <>
                  <span className="font-medium">CITED </span>
                  <span className="italic">“{citation.text}”</span>
                  {citation.page ? <span> — p. {citation.page}</span> : null}
                </>
              ) : (
                NO_CITATION
              )}
            </p>

            {finding.suggested_fix ? (
              <p className="text-[10px] leading-tight text-muted-foreground" data-testid="finding-fix">
                <span className="font-medium">FIX </span>
                {finding.suggested_fix}
              </p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
};

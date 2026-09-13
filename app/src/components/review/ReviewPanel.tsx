import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import type { Finding, RunResult } from "@/lib/silkscreen/types";
import { TriangleAlert } from "lucide-react";
import {
  FindingCard,
  SEVERITY_ORDER,
  findingKey,
  findingRefs,
  originBucket,
  severityBucket,
  type OriginBucket,
  type SeverityBucket,
} from "./FindingCard";
import { WhatWasChecked } from "./WhatWasChecked";

export interface ReviewPanelProps {
  /** The structured review. `undefined` means the response carried none. */
  findings?: Finding[];
  /** Engine warnings (placer status and friends), not review findings. */
  warnings?: string[];
  /**
   * The legacy flattened-string surface. `blockers` is the same problems as
   * `findings`, stringified with severity, detail, citation and fix thrown
   * away — so rendering both double-reports every blocker. It is shown only
   * when `findings` is absent, which is the one case where it carries
   * information the panel would otherwise lose.
   */
  blockers?: string[];
  /** Selected finding id, from `findingKey`. The parent owns selection. */
  selectedFindingId?: string | null;
  /** Cross-highlight hook: the refs a finding (or one ref chip) names. */
  onSelectFinding?: (refs: string[], finding: Finding | null) => void;
  /** The request's `review` flag, so a skipped review reads as skipped. */
  reviewRequested?: boolean;
  datasheets?: RunResult["datasheets"];
  grounded?: boolean;
  /** Passed through to `WhatWasChecked`; rendered only if actually present. */
  rulesRun?: string[];
  modelPasses?: string[];
  className?: string;
}

type SeverityFilter = "all" | SeverityBucket;
type OriginFilter = "all" | "proven" | "unproven";

const SEVERITY_LABEL: Record<SeverityBucket, string> = {
  blocker: "Blockers",
  marginal: "Marginal",
  note: "Notes",
  other: "Other",
};

export function ReviewPanel({
  findings,
  warnings,
  blockers,
  selectedFindingId = null,
  onSelectFinding,
  reviewRequested,
  datasheets,
  grounded,
  rulesRun,
  modelPasses,
  className,
}: ReviewPanelProps) {
  const [severityFilter, setSeverityFilter] = useState<SeverityFilter>("all");
  const [originFilter, setOriginFilter] = useState<OriginFilter>("all");

  const rows = useMemo(
    () =>
      (findings ?? []).map((finding, index) => ({
        finding,
        id: findingKey(finding, index),
        sev: severityBucket(finding.severity),
        origin: originBucket(finding.origin),
      })),
    [findings]
  );

  // Every count on this panel is a tally of what actually arrived.
  const counts = useMemo(() => {
    const sev: Record<SeverityBucket, number> = { blocker: 0, marginal: 0, note: 0, other: 0 };
    const origin: Record<OriginBucket, number> = { proven: 0, suggested: 0, unattributed: 0 };
    for (const row of rows) {
      sev[row.sev] += 1;
      origin[row.origin] += 1;
    }
    return { sev, origin };
  }, [rows]);

  const visible = rows.filter((row) => {
    if (severityFilter !== "all" && row.sev !== severityFilter) return false;
    if (originFilter === "proven" && row.origin !== "proven") return false;
    // "Not proven" holds model-suggested and unattributed findings together
    // because neither was measured — but the cards keep them distinct.
    if (originFilter === "unproven" && row.origin === "proven") return false;
    return true;
  });

  const hasStructured = findings !== undefined;
  const legacyBlockers = !hasStructured ? (blockers ?? []) : [];

  return (
    <section data-testid="review-panel" className={cn("flex h-full min-h-0 flex-col gap-3", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <Tabs
          value={severityFilter}
          onValueChange={(value) => setSeverityFilter(value as SeverityFilter)}
        >
          <TabsList>
            <TabsTrigger value="all" data-testid="review-filter-severity" data-value="all">
              All {rows.length}
            </TabsTrigger>
            {SEVERITY_ORDER.filter((sev) => counts.sev[sev] > 0).map((sev) => (
              <TabsTrigger key={sev} value={sev} data-testid="review-filter-severity" data-value={sev}>
                {SEVERITY_LABEL[sev]} {counts.sev[sev]}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        <div className="ml-auto flex items-center gap-1">
          {(
            [
              ["all", `Both ${rows.length}`],
              ["proven", `Proven ${counts.origin.proven}`],
              [
                "unproven",
                `Not proven ${counts.origin.suggested + counts.origin.unattributed}`,
              ],
            ] as [OriginFilter, string][]
          ).map(([value, label]) => (
            <Button
              key={value}
              type="button"
              size="sm"
              variant={originFilter === value ? "secondary" : "ghost"}
              data-testid="review-filter-origin"
              data-value={value}
              aria-pressed={originFilter === value}
              onClick={() => setOriginFilter(value)}
              className="h-7 text-xs"
            >
              {label}
            </Button>
          ))}
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-3 pr-3">
          {warnings && warnings.length > 0 && (
            <ul data-testid="review-warnings" className="flex flex-col gap-1 rounded-xl border border-dashed p-3 text-xs text-muted-foreground">
              {warnings.map((warning, index) => (
                <li key={`${index}-${warning}`} data-testid="review-warning" className="flex gap-2">
                  <TriangleAlert aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
                  <span>{warning}</span>
                </li>
              ))}
            </ul>
          )}

          {legacyBlockers.length > 0 && (
            <div data-testid="review-legacy-blockers" className="rounded-xl border border-dashed p-3">
              <p className="text-xs font-medium">
                {legacyBlockers.length} blocker(s), from the legacy flat list
              </p>
              <p className="mt-1 text-[11px] text-muted-foreground">
                This response carried no structured findings, so these are the flattened strings.
                Severity, evidence, and the parts involved were dropped before they reached us.
              </p>
              <ul className="mt-2 flex flex-col gap-1 text-xs">
                {legacyBlockers.map((line, index) => (
                  <li key={`${index}-${line}`} data-testid="review-legacy-blocker">
                    {line}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {hasStructured && rows.length > 0 && visible.length === 0 && (
            <p data-testid="review-empty-filter" className="rounded-xl border border-dashed p-3 text-xs text-muted-foreground">
              {rows.length} finding(s) exist; none match this filter.
            </p>
          )}

          {visible.map((row) => (
            <FindingCard
              key={row.id}
              finding={row.finding}
              findingId={row.id}
              selected={selectedFindingId === row.id}
              onSelect={
                onSelectFinding
                  ? (refs, finding) => onSelectFinding(refs, finding)
                  : undefined
              }
            />
          ))}

          {rows.length === 0 && !hasStructured && legacyBlockers.length === 0 && (
            <p data-testid="review-no-review" className="text-xs text-muted-foreground">
              This response carried no review at all — neither structured findings nor blockers.
            </p>
          )}

          {/* Always rendered, findings or not: the coverage statement is what
              stops an empty list from reading as a clean board. */}
          <WhatWasChecked
            findingCount={rows.length}
            provenCount={counts.origin.proven}
            suggestedCount={counts.origin.suggested}
            unattributedCount={counts.origin.unattributed}
            warningCount={warnings?.length ?? 0}
            reviewRequested={reviewRequested}
            datasheets={datasheets}
            grounded={grounded}
            rulesRun={rulesRun}
            modelPasses={modelPasses}
          />
        </div>
      </ScrollArea>

      {selectedFindingId && onSelectFinding && (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="outline" className="font-mono">
            {selectedFindingId}
          </Badge>
          <span>
            {findingRefs(
              rows.find((row) => row.id === selectedFindingId)?.finding ?? ({} as Finding)
            ).join(", ") || "no parts named"}
          </span>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-testid="review-clear-selection"
            className="ml-auto h-7 text-xs"
            onClick={() => onSelectFinding([], null)}
          >
            Clear
          </Button>
        </div>
      )}
    </section>
  );
}

export default ReviewPanel;

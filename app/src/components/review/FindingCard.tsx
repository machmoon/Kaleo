import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { Finding } from "@/lib/silkscreen/types";
import { Info, Ruler, ShieldCheck, Sparkles, TriangleAlert, Wrench } from "lucide-react";

/**
 * The service is additive-only and two different producers could feed this
 * surface: the pipeline's review pass sends `{title, detail, parts, citation,
 * suggested_fix}` — the shape `types.ts` now models — while the audit
 * package's separate CLI speaks `{message, evidence, rule, fix, origin}`.
 * Rendering only one of them leaves blank cards against the other producer,
 * so the card reads either and says so here rather than silently dropping
 * half a finding.
 */
type WireFinding = Finding & {
  message?: string;
  fix?: string;
};

export type SeverityBucket = "blocker" | "marginal" | "note" | "other";
export type OriginBucket = "proven" | "suggested" | "unattributed";

/**
 * The engine's own vocabulary is blocker/marginal/note; `types.ts` also allows
 * error/warning/info, and `Severity` is widened with `string` because the
 * service may add a level this build has never heard of. Anything unrecognised
 * buckets as `other` and keeps its own label — we never relabel a severity we
 * do not understand into one we do.
 */
export function severityBucket(severity: string | undefined): SeverityBucket {
  switch (String(severity ?? "").toLowerCase()) {
    case "blocker":
    case "error":
      return "blocker";
    case "marginal":
    case "warning":
      return "marginal";
    case "note":
    case "info":
      return "note";
    default:
      return "other";
  }
}

export const SEVERITY_ORDER: SeverityBucket[] = ["blocker", "marginal", "note", "other"];

/**
 * A finding is `proven` only when it says so. Absent origin is `unattributed`
 * — never `proven`. Today's `/generate` review findings carry no origin at all
 * and come from a model, so defaulting the field to "proven" would present an
 * opinion as a measurement, which is the one thing this panel may never do.
 */
export function originBucket(origin: string | undefined): OriginBucket {
  const value = String(origin ?? "").toLowerCase();
  if (value === "proven") return "proven";
  if (value === "suggested") return "suggested";
  return "unattributed";
}

/** Stable identity for selection. Findings may arrive without an id. */
export function findingKey(finding: Finding, index: number): string {
  return finding.id && finding.id.length > 0 ? finding.id : `finding-${index}`;
}

/** Every part label a finding names, board refs first, spec names as fallback. */
export function findingRefs(finding: Finding): string[] {
  const wire = finding as WireFinding;
  const refs = (finding.refs ?? []).filter(Boolean);
  if (refs.length > 0) return refs;
  return (wire.parts ?? []).filter(Boolean);
}

function headline(finding: Finding): string {
  const wire = finding as WireFinding;
  return (wire.message || finding.title || "").trim();
}

function detailOf(finding: Finding): string {
  const wire = finding as WireFinding;
  // `message` and `title` are the same slot from two producers; only show
  // `detail` as a second paragraph when it is not already the headline.
  const head = headline(finding);
  const detail = (wire.detail ?? "").trim();
  return detail && detail !== head ? detail : "";
}

function fixOf(finding: Finding): string {
  const wire = finding as WireFinding;
  return (wire.fix || finding.suggested_fix || "").trim();
}

function evidenceOf(finding: Finding): string {
  const wire = finding as WireFinding;
  // A citation is a datasheet pointer, not a measurement, so it is rendered as
  // evidence text but never upgrades a finding's origin.
  return (finding.evidence || wire.citation || "").trim();
}

const SEVERITY_STYLE: Record<SeverityBucket, { badge: string; bar: string; icon: typeof Info }> = {
  blocker: {
    badge: "border-destructive/40 bg-destructive/10 text-destructive",
    bar: "before:bg-destructive",
    icon: TriangleAlert,
  },
  marginal: {
    badge: "border-chart-4/50 bg-chart-4/10 text-chart-4",
    bar: "before:bg-chart-4",
    icon: TriangleAlert,
  },
  note: {
    badge: "border-border bg-muted text-muted-foreground",
    bar: "before:bg-border",
    icon: Info,
  },
  other: {
    badge: "border-border bg-muted text-muted-foreground",
    bar: "before:bg-border",
    icon: Info,
  },
};

export interface FindingCardProps {
  finding: Finding;
  /** Stable id, from `findingKey`. The parent owns selection. */
  findingId: string;
  selected?: boolean;
  /** Cross-highlight callback. Given the refs the finding names. */
  onSelect?: (refs: string[], finding: Finding) => void;
  className?: string;
}

export function FindingCard({
  finding,
  findingId,
  selected = false,
  onSelect,
  className,
}: FindingCardProps) {
  const sev = severityBucket(finding.severity);
  const origin = originBucket(finding.origin);
  const proven = origin === "proven";
  const refs = findingRefs(finding);
  const detail = detailOf(finding);
  const evidence = evidenceOf(finding);
  const fix = fixOf(finding);
  const rule = (finding.rule ?? "").trim();
  const style = SEVERITY_STYLE[sev];
  const SeverityIcon = style.icon;
  // Selecting a finding that names no part would highlight nothing, so the
  // card only becomes a control once there is something to point at.
  const selectable = Boolean(onSelect) && refs.length > 0;

  return (
    // An intrinsic element rather than <Card>: the house rule puts data-testid
    // on intrinsic elements only, and the row needs identity attributes.
    <article
      data-testid="finding-card"
      data-sev={sev}
      data-severity-raw={String(finding.severity ?? "")}
      data-origin={origin}
      data-finding-id={findingId}
      data-refs={refs.join(" ")}
      data-selected={selected ? "true" : "false"}
      className={cn(
        "relative overflow-hidden rounded-xl border bg-card text-card-foreground p-4 pl-5",
        "before:absolute before:inset-y-0 before:left-0 before:w-1 before:content-['']",
        style.bar,
        // The origin distinction must survive without colour: a measured
        // finding sits in a solid frame, an unmeasured one in a dashed one.
        proven ? "border-solid" : "border-dashed",
        selected && "ring-2 ring-ring bg-accent/40",
        className
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="outline" className={cn("gap-1", style.badge)}>
          <SeverityIcon aria-hidden="true" />
          {String(finding.severity ?? "unlabelled")}
        </Badge>

        {proven ? (
          <Badge variant="outline" className="gap-1 border-primary/40 bg-primary/10 text-foreground">
            <ShieldCheck aria-hidden="true" />
            proven by measurement
          </Badge>
        ) : origin === "suggested" ? (
          <Badge
            variant="outline"
            className="gap-1 border-dashed border-muted-foreground/50 text-muted-foreground"
          >
            <Sparkles aria-hidden="true" />
            suggested by a model
          </Badge>
        ) : (
          <Badge
            variant="outline"
            className="gap-1 border-dashed border-muted-foreground/50 text-muted-foreground"
            title="This finding carried no origin. It is treated as unverified, not as a measurement."
          >
            <Sparkles aria-hidden="true" />
            unattributed
          </Badge>
        )}

        {rule && (
          <span
            data-testid="finding-card-rule"
            className="ml-auto font-mono text-[11px] text-muted-foreground"
          >
            {rule}
          </span>
        )}
      </div>

      {selectable ? (
        <button
          type="button"
          data-testid="finding-card-head"
          aria-pressed={selected}
          onClick={() => onSelect?.(refs, finding)}
          className="mt-2 block w-full text-left text-sm font-medium hover:underline"
        >
          {headline(finding) || "(no message)"}
        </button>
      ) : (
        <p data-testid="finding-card-head" className="mt-2 text-sm font-medium">
          {headline(finding) || "(no message)"}
        </p>
      )}

      {detail && (
        <p data-testid="finding-card-detail" className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
          {detail}
        </p>
      )}

      {refs.length > 0 && (
        <div data-testid="finding-card-refs" className="mt-2.5 flex flex-wrap gap-1.5">
          {refs.map((ref) => (
            <button
              key={ref}
              type="button"
              data-testid="finding-card-ref"
              data-ref={ref}
              disabled={!onSelect}
              onClick={() => onSelect?.([ref], finding)}
              className={cn(
                "rounded-md border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground",
                onSelect
                  ? "cursor-pointer hover:bg-accent hover:text-accent-foreground"
                  : "cursor-default"
              )}
            >
              {ref}
            </button>
          ))}
        </div>
      )}

      {proven && (
        <div
          data-testid="finding-card-evidence"
          className="mt-2.5 rounded-md border bg-muted/50 px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground"
        >
          <Ruler aria-hidden="true" className="mr-1.5 inline size-3 align-[-2px]" />
          {/* A proven finding without its measurement is a contract breach
              upstream; say so rather than letting the badge stand alone. */}
          {evidence || "marked proven but carried no measurement"}
        </div>
      )}

      {!proven && evidence && (
        <p data-testid="finding-card-citation" className="mt-2.5 text-xs text-muted-foreground">
          Cited: {evidence}
        </p>
      )}

      {fix && (
        <div className="mt-3 border-t pt-2.5">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled
            data-testid="finding-card-fix"
            className="h-auto w-full justify-start whitespace-normal px-2 py-1.5 text-left text-xs text-muted-foreground disabled:opacity-100"
          >
            <Wrench aria-hidden="true" />
            {fix}
          </Button>
          {/* Nothing in this app applies a fix. The control is disabled and
              labelled so it cannot read as an action that silently did work. */}
          <p data-testid="finding-card-fix-note" className="mt-1 px-2 text-[11px] text-muted-foreground">
            Suggested fix — shown for you to apply. Nothing here changes the board.
          </p>
        </div>
      )}
    </article>
  );
}

export default FindingCard;

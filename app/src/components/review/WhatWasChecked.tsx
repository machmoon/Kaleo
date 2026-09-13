import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { RunResult } from "@/lib/silkscreen/types";
import { Check, Minus, ScanEye } from "lucide-react";

/**
 * The coverage statement. It exists so an empty finding list cannot read as a
 * clean board: a list of nothing means either "nothing was wrong" or "nothing
 * looked", and only this component can tell the reader which.
 *
 * Everything here is derived from the response. Where the response says
 * nothing — today `/generate` carries no `rules_run` or `model_passes` — this
 * reports the absence rather than filling it in with a plausible list.
 */
export interface WhatWasCheckedProps {
  /** All counts come from the rendered findings, never from a placeholder. */
  findingCount: number;
  provenCount: number;
  suggestedCount: number;
  unattributedCount: number;
  warningCount?: number;
  /** The request's `review` flag, when the caller knows it. */
  reviewRequested?: boolean;
  /** `RunResult["datasheets"]` — what the design was actually informed by. */
  datasheets?: RunResult["datasheets"];
  /** True when the run asked for findings to be grounded in datasheet pages. */
  grounded?: boolean;
  /** Rendered only if a future response actually carries them. */
  rulesRun?: string[];
  modelPasses?: string[];
  className?: string;
}

/** What no pass in this pipeline looks at, stated plainly and always. */
const NOT_CHECKED = [
  "Signal integrity — impedance, length matching, return paths, crosstalk.",
  "EMC and emissions.",
  "Thermal margins, and whether copper is thick enough for the current it carries.",
  "Manufacturability at your fab: its own DRC, stackup, minimum trace and drill.",
  "Part availability, cost, and whether the footprints match the parts you will buy.",
  "Mechanical fit — connectors, mounting holes, enclosure.",
];

function datasheetLabel(entry: unknown): string {
  if (entry && typeof entry === "object") {
    const part = (entry as { part?: unknown }).part;
    if (typeof part === "string" && part) return part;
  }
  return "(unnamed datasheet)";
}

export function WhatWasChecked({
  findingCount,
  provenCount,
  suggestedCount,
  unattributedCount,
  warningCount = 0,
  reviewRequested,
  datasheets,
  grounded,
  rulesRun,
  modelPasses,
  className,
}: WhatWasCheckedProps) {
  const reviewSkipped = reviewRequested === false;
  const sheets = (datasheets ?? []).map(datasheetLabel);

  const ran: string[] = [];
  if (modelPasses && modelPasses.length > 0) {
    for (const pass of modelPasses) ran.push(`Model pass: ${pass}.`);
  } else if (!reviewSkipped) {
    // True of this engine: the review is one adversarial model pass over the
    // netlist plus whatever datasheet facts it was given. Naming it is not an
    // invention — it is the only pass `/generate` runs.
    ran.push(
      "Design review — one adversarial model pass over the netlist and the datasheet facts it was given."
    );
  }
  if (rulesRun && rulesRun.length > 0) {
    ran.push(`Deterministic rules: ${rulesRun.join(", ")}.`);
  }
  if (sheets.length > 0) {
    ran.push(`Read ${sheets.length} datasheet(s): ${sheets.join(", ")}.`);
  }
  if (grounded) {
    ran.push("Findings were checked back against pages retrieved from those datasheets.");
  }

  const notRun: string[] = [];
  if (reviewSkipped) {
    notRun.push(
      "The design review did not run — this board was placed from the netlist alone, and nothing checked it."
    );
  }
  if (!rulesRun || rulesRun.length === 0) {
    // Deliberately explicit: the geometry/clearance checkers live behind the
    // separate `silkscreen-review` pass, and this response carries no
    // `rules_run`, so claiming any of them ran would be a fabrication.
    notRun.push(
      "No deterministic geometry, clearance, or connectivity rule ran on this response — nothing here was measured off the board file."
    );
  }
  if (sheets.length === 0) {
    notRun.push(
      "No datasheet was supplied, so nothing was compared against a manufacturer document."
    );
  }
  for (const item of NOT_CHECKED) notRun.push(item);

  return (
    <Card className={cn("gap-3 py-4", className)}>
      <CardHeader className="px-4">
        <CardTitle className="flex items-center gap-2 text-sm">
          <ScanEye aria-hidden="true" className="size-4" />
          What was checked
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 text-xs">
        <p data-testid="what-was-checked-counts" className="text-muted-foreground">
          {findingCount === 0
            ? "This review raised nothing."
            : `${findingCount} finding(s): ${provenCount} proven by measurement, ${suggestedCount} suggested by a model, ${unattributedCount} unattributed.`}
          {warningCount > 0 && ` ${warningCount} engine warning(s).`}
        </p>

        {ran.length > 0 && (
          <ul data-testid="what-was-checked-ran" className="mt-3 space-y-1.5">
            {ran.map((line) => (
              <li key={line} data-testid="what-was-checked-item" data-kind="ran" className="flex gap-2">
                <Check aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
                <span>{line}</span>
              </li>
            ))}
          </ul>
        )}

        <p className="mt-3 font-medium">Not checked</p>
        <ul data-testid="what-was-checked-not-run" className="mt-1.5 space-y-1.5 text-muted-foreground">
          {notRun.map((line) => (
            <li key={line} data-testid="what-was-checked-item" data-kind="not-run" className="flex gap-2">
              <Minus aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
              <span>{line}</span>
            </li>
          ))}
        </ul>

        <p data-testid="what-was-checked-signoff" className="mt-3 border-t pt-2.5 text-muted-foreground">
          A review with nothing in it is a clean bill only for what was checked. It is not a
          substitute for a human sign-off before you order boards.
        </p>
      </CardContent>
    </Card>
  );
}

export default WhatWasChecked;

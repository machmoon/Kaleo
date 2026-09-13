import { useEffect, useMemo, useRef, useState } from "react";
import { MinusIcon, PlusIcon } from "lucide-react";
import { Button, Input, Label, Switch } from "@/components";
import { MAX_TIME_LIMIT_S, MIN_TIME_LIMIT_S } from "@/lib/silkscreen/client";
import type { RunRequestDraft } from "@/contexts";
import { readSummaryMode, writeSummaryMode } from "@/lib/silkscreen/deliver";
import type { SummaryMode } from "@/lib/silkscreen/types";
import { cn } from "@/lib/utils";

/** The two ways a finished run can report itself, and what each one means. */
const SUMMARY_MODES: { mode: SummaryMode; label: string; note: string }[] = [
  {
    mode: "structured",
    label: "Structured",
    note: "An agenda: what is still open, why each item blocks, how long it needs. Bookable as a spec review from the Send panel.",
  },
  {
    mode: "prose",
    label: "Prose",
    note: "A written summary of the run, the way it reads today.",
  },
];

/**
 * One datasheet row as the form holds it.
 *
 * The draft stores datasheets as `part -> url`, which cannot represent a row
 * mid-type (an empty part key, a renamed part). So the rows live here and only
 * complete pairs are pushed back into the draft; the client drops half-filled
 * rows anyway, and this keeps them visible while they are being typed.
 */
interface DatasheetRow {
  part: string;
  url: string;
}

const BLANK_ROW: DatasheetRow = { part: "", url: "" };

function rowsToRecord(rows: DatasheetRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const part = row.part.trim();
    const url = row.url.trim();
    if (part && url) out[part] = url;
  }
  return out;
}

function recordToRows(record: Record<string, string>): DatasheetRow[] {
  const rows = Object.entries(record).map(([part, url]) => ({ part, url }));
  return rows.length ? rows : [BLANK_ROW];
}

function sameRecord(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => a[k] === b[k]);
}

/**
 * True when at least one row will actually survive serialization — the same
 * part-AND-url rule as `rowsToRecord`. A URL-only row must not arm the
 * switch: it serializes to an empty `datasheets`, and `ground: true` with no
 * datasheets is a guaranteed 400 from the service on a paid submit.
 */
function canGround(rows: DatasheetRow[]): boolean {
  return rows.some(
    (row) => row.part.trim().length > 0 && row.url.trim().length > 0
  );
}

function clampTimeLimit(raw: string): number {
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) return MIN_TIME_LIMIT_S;
  return Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, n));
}

export interface RunOptionsProps {
  request: RunRequestDraft;
  onChange: (patch: Partial<RunRequestDraft>) => void;
  /** Options freeze mid-run: the engine already has the ones it was given. */
  disabled?: boolean;
  /**
   * Review in KiCad, one approved stage at a time. Kept out of the request
   * draft on purpose: it selects which state machine runs the submit, not
   * what the engine is asked for.
   */
  stepMode?: boolean;
  onStepModeChange?: (enabled: boolean) => void;
  /**
   * How a finished run summarises itself. Kept out of the request draft for
   * the same reason `stepMode` is: it selects how the result is *presented*,
   * and the remembered choice belongs to the person, not to one board.
   * Uncontrolled when omitted — the choice is remembered here either way, so
   * the toggle survives closing the popover, a re-mount and a restored run.
   */
  summary?: SummaryMode;
  onSummaryChange?: (mode: SummaryMode) => void;
}

export const RunOptions = ({
  request,
  onChange,
  disabled,
  stepMode = false,
  onStepModeChange,
  summary,
  onSummaryChange,
}: RunOptionsProps) => {
  const [rows, setRows] = useState<DatasheetRow[]>(() =>
    recordToRows(request.datasheets)
  );
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  // Re-seed when the draft's datasheets change from somewhere else — restoring
  // a past run's request, say. Rows this form itself pushed match already.
  useEffect(() => {
    if (!sameRecord(request.datasheets, rowsToRecord(rowsRef.current))) {
      setRows(recordToRows(request.datasheets));
    }
  }, [request.datasheets]);

  const groundable = useMemo(() => canGround(rows), [rows]);

  const [rememberedSummary, setRememberedSummary] = useState<SummaryMode>(readSummaryMode);
  const summaryMode = summary ?? rememberedSummary;
  const changeSummary = (mode: SummaryMode) => {
    setRememberedSummary(mode);
    writeSummaryMode(mode);
    onSummaryChange?.(mode);
  };

  const commitRows = (next: DatasheetRow[]) => {
    setRows(next);
    const datasheets = rowsToRecord(next);
    // Grounding with no source is a claim the run cannot back up, so it comes
    // off the moment the last URL does.
    const ground = request.ground && canGround(next);
    onChange({ datasheets, ground });
  };

  return (
    <div className="flex flex-col gap-4" data-testid="run-options">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <Label htmlFor="kaleo-time-limit" className="text-xs">
            Placer budget
          </Label>
          <span className="text-xs text-muted-foreground tabular-nums">
            {request.time_limit_s}s
          </span>
        </div>
        <Input
          id="kaleo-time-limit"
          type="number"
          inputMode="numeric"
          min={MIN_TIME_LIMIT_S}
          max={MAX_TIME_LIMIT_S}
          value={request.time_limit_s}
          disabled={disabled}
          data-testid="run-options-time-limit"
          onChange={(e) => onChange({ time_limit_s: clampTimeLimit(e.target.value) })}
          onBlur={(e) => onChange({ time_limit_s: clampTimeLimit(e.target.value) })}
        />
        <p className="text-[11px] text-muted-foreground">
          How long CP-SAT may search, {MIN_TIME_LIMIT_S}–{MAX_TIME_LIMIT_S}s. A
          short budget does not fail the run, it settles for a worse layout.
        </p>
      </div>

      {onStepModeChange ? (
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col">
            <Label htmlFor="kaleo-step-mode" className="text-xs">
              Review in KiCad, stage by stage
            </Label>
            <p className="text-[11px] text-muted-foreground">
              Each stage appears in the open KiCad and waits for your approval
              before the next one runs. The case is written as a STEP assembly
              beside the board.
            </p>
          </div>
          <Switch
            id="kaleo-step-mode"
            checked={stepMode}
            disabled={disabled}
            data-testid="run-options-step-mode"
            onCheckedChange={onStepModeChange}
          />
        </div>
      ) : null}

      {/* Two switches below are read by `/generate` only. The step routes
          (`service/steps.py`) accept neither: the critic is its own step
          there, pressed or not, and no step reads `ground`. So while step
          mode is on they are off, and say so — a switch that looks live and
          reaches nothing is the defect, not the note. */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col">
          <Label
            htmlFor="kaleo-review"
            className={cn("text-xs", stepMode && "text-muted-foreground")}
          >
            Review the board
          </Label>
          <p className="text-[11px] text-muted-foreground" data-testid="run-options-review-note">
            {stepMode
              ? "One-shot runs only. Stage by stage, the critic is the Review step on the strip — press it, or stop before it."
              : "Runs the critic pass. Off means nothing was checked, not that the board is clean."}
          </p>
        </div>
        <Switch
          id="kaleo-review"
          checked={request.review}
          disabled={disabled || stepMode}
          data-testid="run-options-review"
          data-inert={stepMode ? "true" : undefined}
          onCheckedChange={(checked) => onChange({ review: checked })}
        />
      </div>

      <div className="flex flex-col gap-1.5" data-testid="run-options-summary">
        <Label className="text-xs">How the run reports back</Label>
        <div className="flex items-center gap-1.5">
          {SUMMARY_MODES.map(({ mode, label }) => (
            <Button
              key={mode}
              type="button"
              variant={summaryMode === mode ? "default" : "outline"}
              size="sm"
              disabled={disabled}
              aria-pressed={summaryMode === mode}
              data-testid="run-options-summary-option"
              data-mode={mode}
              data-selected={String(summaryMode === mode)}
              onClick={() => changeSummary(mode)}
            >
              {label}
            </Button>
          ))}
        </div>
        <p className="text-[11px] text-muted-foreground">
          {SUMMARY_MODES.find((m) => m.mode === summaryMode)?.note}
        </p>
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col">
          <Label
            htmlFor="kaleo-ground"
            className={cn("text-xs", (!groundable || stepMode) && "text-muted-foreground")}
          >
            Ground in the datasheets
          </Label>
          <p className="text-[11px] text-muted-foreground" data-testid="run-options-ground-note">
            {stepMode
              ? "One-shot runs only. The stage-by-stage routes do not accept grounding, so this would reach nothing."
              : groundable
              ? "After the run, check the review\u2019s findings back against these PDFs."
              : "Needs a part and its datasheet URL — grounding has nothing to check against."}
          </p>
        </div>
        <Switch
          id="kaleo-ground"
          checked={request.ground && groundable}
          disabled={disabled || !groundable || stepMode}
          data-testid="run-options-ground"
          data-inert={stepMode ? "true" : undefined}
          onCheckedChange={(checked) => onChange({ ground: checked })}
        />
      </div>

      <div className="flex flex-col gap-2">
        <Label className="text-xs">Datasheets</Label>
        {rows.map((row, index) => (
          <div
            key={index}
            className="flex items-center gap-1.5"
            data-testid="run-options-datasheet-row"
            data-index={index}
          >
            <Input
              placeholder="Part"
              value={row.part}
              disabled={disabled}
              className="w-24 text-xs"
              onChange={(e) =>
                commitRows(
                  rows.map((r, i) => (i === index ? { ...r, part: e.target.value } : r))
                )
              }
            />
            <Input
              placeholder="https://…/datasheet.pdf"
              value={row.url}
              disabled={disabled}
              className="flex-1 text-xs"
              onChange={(e) =>
                commitRows(
                  rows.map((r, i) => (i === index ? { ...r, url: e.target.value } : r))
                )
              }
            />
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              title="Remove this datasheet"
              disabled={disabled || rows.length === 1}
              onClick={() => commitRows(rows.filter((_, i) => i !== index))}
            >
              <MinusIcon className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button
          variant="outline"
          size="sm"
          className="w-fit"
          disabled={disabled}
          data-testid="run-options-add-datasheet"
          onClick={() => setRows([...rows, { ...BLANK_ROW }])}
        >
          <PlusIcon className="size-3.5" />
          Add a datasheet
        </Button>
      </div>
    </div>
  );
};

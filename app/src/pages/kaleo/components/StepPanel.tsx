import { useState } from "react";
import {
  CheckIcon,
  CircleIcon,
  Loader2,
  BoxIcon,
  ExternalLinkIcon,
  FileTextIcon,
  FolderOpenIcon,
  XIcon,
} from "lucide-react";
import { openPath, openUrl, revealItemInDir } from "@tauri-apps/plugin-opener";
import { Badge, Button, Input, Label, Switch } from "@/components";
import { ModelViewer } from "@/components/ModelViewer";
import {
  STEP_DESCRIPTORS,
  STEP_ORDER,
  MAX_ENCLOSURE_STYLE_CHARS,
  caseDetails,
  casePayload,
  distributorLink,
  formatClock,
  headline as headlineOf,
  headlineFix,
  marginAxis,
  NOTHING_SUBMITTED,
  orderDetails,
  placeDetails,
  railRows,
  receiptLine,
  reviewDetails,
  routeDetails,
  sourcingDetails,
  stageModel,
  statusBadge,
} from "@/lib/silkscreen/steps";
import { MarginAxis } from "./MarginAxis";
// The review card, plus its tone table: the BOM's MPN badge shares its tones
// with the review they were written for.
import { ReviewOutcome, TONE_CLASS } from "./ReviewOutcome";
import type {
  ArmedCommand,
  BackgroundState,
  CaseDetails as CaseDetailsData,
  OrderDetails as OrderDetailsData,
  PlaceDetails as PlaceDetailsData,
  RailRow,
  RouteDetails as RouteDetailsData,
  SourcingDetails as SourcingDetailsData,
} from "@/lib/silkscreen/steps";
import type { StepResponse } from "@/lib/silkscreen/types";
import type { StepRun } from "@/hooks/useStepRun";
import type { StepName } from "@/lib/silkscreen/types";
import { cn } from "@/lib/utils";

export interface StepPanelProps {
  run: StepRun;
  /** What the prompt bar could not make sense of, if anything. */
  note?: string | null;
  onDismiss: () => void;
  /**
   * A sentence resolved to a stage, or to starting over, and is waiting for
   * a human to commit it. Nothing typed or spoken spends money on its own:
   * it arms this, and the engineer fires it. `source` is the sentence,
   * echoed back so what the machine understood is visible before it costs
   * anything — or, for a restart, before a paid run is dropped.
   */
  armed?: ArmedCommand | null;
  /**
   * Fires the armed stage. `payload` is what that approval carries beyond
   * the hook's own fields — today only the case step's style and rigorous
   * flag, which live on this panel and would otherwise be lost on the spoken
   * path.
   */
  onConfirm?: (payload?: Record<string, unknown>) => void;
  onDisarm?: () => void;
  /**
   * False when the newest artifact went to KiCad and the engineer has not
   * looked away from the overlay since. The approve control stays live; it
   * just stops looking like the obvious next click.
   */
  reviewed?: boolean;
}

const WHERE_LABEL: Record<string, string> = {
  kicad: "KiCad",
  overlay: "here",
};

const ENGINE_LABEL: Record<string, string> = {
  kernel: "build123d kernel (OCCT)",
};

const SEVERITY_LABEL: Record<string, string> = {
  blocker: "Blocker",
  warning: "Warning",
  note: "Note",
};

const SEVERITY_CLASS: Record<string, string> = {
  blocker: "border-destructive/60 text-destructive",
  warning: "border-amber-500/60 text-amber-600 dark:text-amber-400",
  note: "border-input/60 text-muted-foreground",
};

/**
 * The datasheet badge, by what the engine actually saw when it fetched the
 * URL. Only `verified` gets the open button: the other three are a page
 * that is not a PDF, a fetch that failed, and no URL at all, and a button
 * that opens one of those would be the panel vouching for what it did not
 * check.
 */
const DATASHEET_LABEL: Record<string, string> = {
  verified: "PDF verified",
  not_pdf: "not a PDF",
  unreachable: "unreachable",
  none: "no datasheet",
};

const DATASHEET_CLASS: Record<string, string> = {
  verified: "border-emerald-500/60 text-emerald-700 dark:text-emerald-400",
  not_pdf: "border-amber-500/60 text-amber-600 dark:text-amber-400",
  unreachable: "border-destructive/60 text-destructive",
  none: "border-input/60 text-muted-foreground",
};

/** The hook's state for one background job, or null when it tracks none for that step. */
function backgroundState(run: StepRun, step: StepName): BackgroundState | null {
  return run.background?.find((job) => job.step === step)?.state ?? null;
}

function errorText(caught: unknown): string {
  if (caught instanceof Error) return caught.message;
  return typeof caught === "string" ? caught : String(caught ?? "unknown error");
}

/**
 * The per-stage receipt, in Ramp's grammar: what the stage did, which engine
 * did it, and the seconds it took.
 *
 * Every part of the line is the stage's own — `receiptLine` reads the
 * measurements off the response, `stageModel` names the engine only where it
 * can name one honestly, and the duration is the engine's `duration_s`. There
 * is deliberately no call count: the step envelope carries no `model.call`
 * frame, and a receipt that priced every stage at "1 call" would be stating a
 * number nothing measured.
 */
const StageReceipt = ({ response }: { response: StepResponse }) => {
  const receipt = receiptLine(response);
  const engine = stageModel(response);
  return (
    <p
      className="text-[10px] text-muted-foreground"
      data-testid="stage-receipt"
      data-step={response.step}
    >
      <span className="font-medium text-foreground">{STEP_DESCRIPTORS[response.step].label}</span>
      {receipt ? ` · ${receipt}` : ""}
      {engine ? ` · ${engine.label}` : ""}
      {` · ${formatClock(response.duration_s)}`}
    </p>
  );
};

/**
 * What the router left behind: the count, and — the repo's one routing rule —
 * every net it could not finish, by name, with its reason, so "N of M" can
 * never read as a finished board over a ratsnest. Every number is the
 * engine's own (`routing.routed`, `routing.unrouted`); nothing is inferred.
 */
const RouteOutcome = ({ details }: { details: RouteDetailsData }) => (
  <div className="flex flex-col gap-1 rounded-md border border-input/40 p-2" data-testid="route-outcome">
    <p
      className={cn("text-[11px] leading-tight", details.unrouted.length > 0 && "text-amber-600 dark:text-amber-400")}
      data-testid="route-count"
      data-routed={details.routed}
      data-total={details.total}
      data-completion={details.completion ?? undefined}
    >
      <span className="font-medium">
        {details.routed} of {details.total} net{details.total === 1 ? "" : "s"} routed
      </span>
      <span className="text-muted-foreground">
        {` · ${details.tracks} track${details.tracks === 1 ? "" : "s"}, ${details.vias} via${details.vias === 1 ? "" : "s"}`}
        {details.unrouted.length
          ? ` · ${details.unrouted.length} left as ratsnest`
          : details.total === 0
            ? " · the board had no nets to route"
            : ""}
      </span>
    </p>
    {details.unrouted.length ? (
      <ul className="flex flex-col gap-0.5" data-testid="route-unrouted">
        {details.unrouted.map(({ net, reason }) => (
          <li
            key={net}
            className="text-[11px] leading-tight"
            data-testid="route-unrouted-net"
            data-net={net}
          >
            <span className="font-mono font-medium">{net}</span>
            <span className="text-muted-foreground"> — {reason}</span>
          </li>
        ))}
      </ul>
    ) : null}
    {details.warnings.map((warning) => (
      <p key={warning} className="text-[11px] text-muted-foreground" data-testid="route-warning">
        {warning}
      </p>
    ))}
  </div>
);

/**
 * What the placer left behind: each part by reference and land pattern, and
 * the solver's warnings. The board size and solver status are on the row's
 * receipt already; this is the list that receipt counts.
 */
const PlaceOutcome = ({ details }: { details: PlaceDetailsData }) => (
  <div className="flex flex-col gap-1 rounded-md border border-input/40 p-2" data-testid="place-outcome">
    {details.parts.length ? (
      <p className="text-[11px] leading-tight" data-testid="place-parts">
        <span className="font-medium">
          {details.parts.length} part{details.parts.length === 1 ? "" : "s"} placed
        </span>
        <span className="text-muted-foreground">
          {": "}
          {details.parts.map((part, index) => (
            <span key={part.ref} data-testid="place-part" data-ref={part.ref} title={part.footprint}>
              {index ? ", " : ""}
              <span className="font-mono">{part.ref}</span>
            </span>
          ))}
        </span>
      </p>
    ) : (
      <p className="text-[11px] text-muted-foreground" data-testid="place-parts">
        The placer reported no parts.
      </p>
    )}
    {details.warnings.map((warning) => (
      <p key={warning} className="text-[11px] text-muted-foreground" data-testid="place-warning">
        {warning}
      </p>
    ))}
  </div>
);

/**
 * What the case step left behind: which engine drew it, the kernel's
 * clause-by-clause receipt when one ran, what it could not check, and the
 * files on disk. The buttons hand a path to the OS the same way the order
 * step's do; when no application claims it the opener rejects, and the path
 * is shown instead of a button that did nothing.
 *
 * The clause receipt itself is the margin axis: see MarginAxis for why the
 * thirteen clauses are a shape rather than a list, and why the fold is by
 * nearness to zero rather than by verdict.
 */
const CaseOutcome = ({ details }: { details: CaseDetailsData }) => {
  const [openNote, setOpenNote] = useState<string | null>(null);
  const { kernel } = details;
  const failed = kernel ? kernel.clauses.filter((c) => !c.passed).length : 0;

  const reveal = () => {
    const target = details.reveal;
    if (!target) return;
    revealItemInDir(target).catch((caught) =>
      setOpenNote(`Could not reveal the case files (${errorText(caught)}). They are beside ${target}`)
    );
  };

  const openModel = () => {
    const model = details.step;
    if (!model) return;
    openPath(model).catch((caught) =>
      setOpenNote(
        `No application opened the model (${errorText(caught)}). It is at ${model} — KiCad's 3D viewer, FreeCAD or any STEP viewer reads it.`
      )
    );
  };

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-input/40 p-2" data-testid="case-outcome">
      <p className="text-[11px] leading-tight" data-testid="case-engine" data-engine={details.engine ?? "unknown"}>
        <span className="text-muted-foreground">Engine: </span>
        <span className="font-medium">
          {details.engine ? ENGINE_LABEL[details.engine] ?? details.engine : "not reported"}
        </span>
        {kernel ? (
          <span
            className={cn(failed > 0 ? "text-destructive" : "text-muted-foreground")}
            data-testid="case-kernel-verdict"
            data-passed={kernel.passed && failed === 0}
          >
            {" · "}
            {failed === 0 && kernel.passed
              ? `${kernel.clauses.length} of ${kernel.clauses.length} checks pass`
              : `${failed} of ${kernel.clauses.length} checks failed`}
          </span>
        ) : (
          <span className="text-muted-foreground" data-testid="case-kernel-verdict" data-passed="none">
            {" · no kernel check"}
          </span>
        )}
      </p>

      {details.brief ? (
        <p className="text-[11px] text-muted-foreground" data-testid="case-brief">
          {details.brief}
        </p>
      ) : null}

      {kernel && kernel.clauses.length ? (
        <MarginAxis
          axis={marginAxis(kernel)}
          details={Object.fromEntries(kernel.clauses.map((c) => [c.name, c.detail]))}
        />
      ) : null}

      {kernel?.warnings.map((warning) => (
        <p key={warning} className="text-[11px] text-muted-foreground" data-testid="case-kernel-warning">
          {warning}
        </p>
      ))}

      {details.warnings.map((warning) => (
        <p key={warning} className="text-[11px] text-muted-foreground" data-testid="case-warning">
          {warning}
        </p>
      ))}

      {details.reveal || details.step ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {details.reveal ? (
            <Button size="sm" variant="outline" onClick={reveal} data-testid="case-reveal">
              <FolderOpenIcon className="size-3.5" />
              Reveal case files
            </Button>
          ) : null}
          {details.step ? (
            <Button size="sm" variant="outline" onClick={openModel} data-testid="case-open-model">
              <BoxIcon className="size-3.5" />
              Open 3D model
            </Button>
          ) : null}
        </div>
      ) : null}

      {openNote ? (
        <p className="break-all text-[11px] text-muted-foreground" data-testid="case-open-note">
          {openNote}
        </p>
      ) : null}
    </div>
  );
};

/**
 * What the order step left behind: the verdict's reasons, the package on
 * disk, and the 3D model when KiCad could export one.
 *
 * The buttons hand a path to the OS. When no application claims it the
 * opener rejects; that is caught and the path is shown instead, because a
 * button that does nothing is the exact complaint this panel answers.
 */
const OrderOutcome = ({
  details,
  closing = true,
  onShow3d,
}: {
  details: OrderDetailsData;
  /**
   * Open KiCad's own 3D viewer on this run's board. Absent on an engine that
   * predates the route, and then the button is not offered at all rather than
   * offered and dead.
   */
  onShow3d?: () => Promise<{ opened: boolean; detail: string | null }>;
  /**
   * False once the run's own receipt carries the not-submitted sentence, so
   * the strip says it once. It is never dropped — it moves.
   */
  closing?: boolean;
}) => {
  const [openNote, setOpenNote] = useState<string | null>(null);
  // Collapsed by default: the viewer is 160 px of a 600 px overlay and reads
  // the file only once shown, so an order with no interest in the model
  // costs nothing.
  const [showModel, setShowModel] = useState(false);

  const reveal = () => {
    if (!details.zip) return;
    const zip = details.zip;
    revealItemInDir(zip).catch((caught) =>
      setOpenNote(`Could not reveal the package (${errorText(caught)}). It is at ${zip}`)
    );
  };

  // "Show me the board in 3D" means KiCad's own 3D viewer, on the routed
  // board KiCad itself reads. The in-app WebGL preview below is a convenience
  // for a glance without leaving the strip, not the answer to that question.
  const [showing3d, setShowing3d] = useState(false);
  const show3d = () => {
    if (!onShow3d) return;
    setShowing3d(true);
    setOpenNote(null);
    onShow3d()
      .then((verdict) => {
        if (!verdict.opened) {
          setOpenNote(
            verdict.detail
              ? `KiCad did not open the 3D viewer: ${verdict.detail}`
              : "KiCad did not open the 3D viewer."
          );
        }
      })
      .catch((caught) => setOpenNote(`Could not reach KiCad (${errorText(caught)}).`))
      .finally(() => setShowing3d(false));
  };

  const openModel = () => {
    if (!details.model) return;
    const model = details.model;
    openPath(model).catch((caught) =>
      setOpenNote(
        `No application opened the model (${errorText(caught)}). It is at ${model} — any glTF viewer reads it.`
      )
    );
  };

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-input/40 p-2" data-testid="order-outcome">
      {details.issues.length ? (
        <ul className="flex flex-col gap-1" data-testid="order-issues">
          {details.issues.map((issue, index) => {
            const severity = String(issue.severity);
            return (
              <li
                key={`${issue.code ?? severity}-${index}`}
                className="flex items-start gap-1.5 text-[11px] leading-tight"
                data-testid="order-issue"
                data-sev={severity}
              >
                <Badge
                  variant="outline"
                  className={cn("h-4 shrink-0 px-1 text-[9px]", SEVERITY_CLASS[severity])}
                >
                  {SEVERITY_LABEL[severity] ?? severity}
                </Badge>
                <span className="min-w-0 flex-1">
                  <span className="font-medium">{issue.title ?? issue.code ?? severity}</span>
                  {issue.detail ? (
                    <span className="text-muted-foreground"> — {issue.detail}</span>
                  ) : null}
                  {issue.parts?.length ? (
                    <span className="text-muted-foreground"> ({issue.parts.join(", ")})</span>
                  ) : null}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-[11px] text-muted-foreground">The order gate raised no issues.</p>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        {details.zip ? (
          <Button size="sm" variant="outline" onClick={reveal} data-testid="order-reveal">
            <FolderOpenIcon className="size-3.5" />
            Reveal order files
          </Button>
        ) : null}
        {onShow3d ? (
          <Button
            size="sm"
            variant="outline"
            onClick={show3d}
            disabled={showing3d}
            data-testid="order-show-3d"
          >
            <BoxIcon className="size-3.5" />
            {showing3d ? "Opening KiCad…" : "Show 3D board"}
          </Button>
        ) : null}
        {details.model ? (
          <Button
            size="sm"
            variant={showModel ? "secondary" : "ghost"}
            onClick={() => setShowModel((shown) => !shown)}
            aria-pressed={showModel}
            data-testid="order-show-model"
          >
            <BoxIcon className="size-3.5" />
            {showModel ? "Hide preview" : "Preview here"}
          </Button>
        ) : null}
        {details.model ? (
          <Button size="sm" variant="ghost" onClick={openModel} data-testid="order-open-model">
            <ExternalLinkIcon className="size-3.5" />
            Open 3D model
          </Button>
        ) : null}
      </div>

      {details.model && showModel ? <ModelViewer path={details.model} className="h-40" /> : null}

      {details.warnings.map((warning) => (
        <p key={warning} className="text-[11px] text-muted-foreground" data-testid="order-warning">
          {warning}
        </p>
      ))}

      {openNote ? (
        <p className="break-all text-[11px] text-muted-foreground" data-testid="order-open-note">
          {openNote}
        </p>
      ) : null}

      {closing ? (
        <p className="text-[11px] text-muted-foreground" data-testid="order-not-submitted">
          {NOTHING_SUBMITTED}
        </p>
      ) : null}
    </div>
  );
};

/**
 * The BOM the sourcing step produced, under the order outcome or on its own.
 *
 * Every MPN is a proposal — nothing here talked to a distributor — so the
 * table says "proposed" and the header counts what was and was not
 * verified. A datasheet opens in the browser only when the engine saw a PDF
 * at that URL; the BOM file reveals in the file manager like the order zip.
 */
const SourcingOutcome = ({ details }: { details: SourcingDetailsData }) => {
  const [openNote, setOpenNote] = useState<string | null>(null);

  const reveal = () => {
    if (!details.bom) return;
    const bom = details.bom;
    revealItemInDir(bom).catch((caught) =>
      setOpenNote(`Could not reveal the BOM (${errorText(caught)}). It is at ${bom}`)
    );
  };

  const openDatasheet = (url: string) => {
    openUrl(url).catch((caught) =>
      setOpenNote(`No browser opened the datasheet (${errorText(caught)}). It is at ${url}`)
    );
  };

  const openDistributor = (url: string) => {
    openUrl(url).catch((caught) =>
      setOpenNote(`No browser opened the listing (${errorText(caught)}). It is at ${url}`)
    );
  };

  const total = details.parts.length;
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-input/40 p-2" data-testid="sourcing-outcome">
      <p className="text-[11px] text-muted-foreground" data-testid="sourcing-counts">
        {total} line item{total === 1 ? "" : "s"} · {details.named - details.confirmed} proposed ·{" "}
        {details.confirmed} confirmed by a distributor · {details.verified} of {total} datasheet
        {total === 1 ? "" : "s"} are PDFs. A proposal is the model's word; check it before ordering.
      </p>

      {total ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] leading-tight" data-testid="bom-table">
            <thead className="text-left text-[10px] text-muted-foreground">
              <tr>
                <th className="pr-2 font-medium">Ref</th>
                <th className="pr-2 font-medium">Value</th>
                <th className="pr-2 font-medium">Package</th>
                <th className="pr-2 font-medium">MPN</th>
                <th className="font-medium">Datasheet</th>
              </tr>
            </thead>
            <tbody>
              {details.parts.map((part) => {
                const status = String(part.datasheet_status);
                const verified = status === "verified" && !!part.datasheet_url;
                return (
                  <tr
                    key={part.ref}
                    className="align-top"
                    data-testid="bom-row"
                    data-ref={part.ref}
                    data-mpn-status={part.mpn_status}
                    data-datasheet-status={status}
                  >
                    <td className="pr-2 font-medium">{part.ref}</td>
                    <td className="pr-2">{part.value}</td>
                    <td className="max-w-[9rem] truncate pr-2 text-muted-foreground" title={part.package}>
                      {part.package}
                    </td>
                    <td className="pr-2">
                      <span className="flex flex-col gap-0.5">
                        <span className="flex flex-wrap items-center gap-1">
                          {part.mpn ? (
                            <span title={part.manufacturer ?? undefined}>{part.mpn}</span>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                          <Badge
                            variant="outline"
                            className={cn(
                              "h-4 px-1 text-[9px]",
                              TONE_CLASS[statusBadge("mpn", part.mpn_status).tone],
                              // A proposal is a real line item whose proof is
                              // missing, so its outline is broken, not solid.
                              part.mpn_status === "proposed" && "border-dashed"
                            )}
                            data-testid="mpn-badge"
                            data-ref={part.ref}
                            data-status={String(part.mpn_status)}
                          >
                            {statusBadge("mpn", part.mpn_status).label}
                            {part.mpn_status === "verified" && part.distributor
                              ? ` · ${part.distributor}`
                              : ""}
                          </Badge>
                          {distributorLink(part) ? (
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-4 px-1 text-[10px]"
                              onClick={() => openDistributor(distributorLink(part) as string)}
                              data-testid="distributor-open"
                              data-ref={part.ref}
                            >
                              <ExternalLinkIcon className="size-2.5" />
                              Open listing
                            </Button>
                          ) : null}
                        </span>
                        {part.mpn_status !== "verified" && part.verify_error ? (
                          <span
                            className="text-[10px] text-amber-600 dark:text-amber-400"
                            data-testid="mpn-verify-error"
                            data-ref={part.ref}
                          >
                            {part.verify_error}
                          </span>
                        ) : null}
                      </span>
                    </td>
                    <td>
                      <span className="flex flex-wrap items-center gap-1">
                        <Badge
                          variant="outline"
                          className={cn("h-4 px-1 text-[9px]", DATASHEET_CLASS[status])}
                          title={part.datasheet_url ?? undefined}
                          data-testid="datasheet-badge"
                          data-ref={part.ref}
                        >
                          {DATASHEET_LABEL[status] ?? status}
                        </Badge>
                        {part.model3d ? (
                          <Badge
                            variant="outline"
                            className="h-4 px-1 text-[9px]"
                            title={part.model3d}
                            data-testid="model3d-badge"
                            data-ref={part.ref}
                          >
                            <BoxIcon className="mr-0.5 size-2.5" />
                            3D
                          </Badge>
                        ) : null}
                        {verified ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-4 px-1 text-[10px]"
                            onClick={() => openDatasheet(part.datasheet_url as string)}
                            data-testid="datasheet-open"
                            data-ref={part.ref}
                          >
                            <ExternalLinkIcon className="size-2.5" />
                            Open datasheet
                          </Button>
                        ) : null}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-[11px] text-muted-foreground">The board has no parts to source.</p>
      )}

      {details.bom ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Button size="sm" variant="outline" onClick={reveal} data-testid="bom-reveal">
            <FileTextIcon className="size-3.5" />
            Reveal BOM
          </Button>
        </div>
      ) : null}

      {details.warnings.map((warning) => (
        <p key={warning} className="text-[11px] text-muted-foreground" data-testid="sourcing-warning">
          {warning}
        </p>
      ))}

      {openNote ? (
        <p className="break-all text-[11px] text-muted-foreground" data-testid="sourcing-open-note">
          {openNote}
        </p>
      ) : null}
    </div>
  );
};

/**
 * The six stages as one 2px rail.
 *
 * The checklist below says what each stage produced; this says where in the
 * board's life you are, in the space a line of text would take. Position is
 * the one thing the checklist reads poorly at a glance, because six rows of
 * similar-looking text have no shape.
 */
const StepRail = ({ rows }: { rows: RailRow[] }) => (
  <div className="flex items-center gap-1" data-testid="step-rail" aria-hidden="true">
    {rows.map((row) => (
      <span
        key={row.id}
        data-step={row.id}
        data-status={row.status}
        className={cn(
          "h-1 flex-1 rounded-full",
          row.status === "done" && "bg-foreground/70",
          row.status === "running" && "animate-pulse bg-foreground/70",
          row.status === "available" && "bg-foreground/25",
          row.status === "preparing" && "animate-pulse bg-foreground/40",
          row.status === "pending" && "bg-input",
          // The one thing the rail gained that is honesty, not decoration.
          row.rail === "failed" && "bg-destructive/70"
        )}
      />
    ))}
  </div>
);

/**
 * The control strip for an approval-gated run.
 *
 * The engineer looks at KiCad; this shows what just landed there and offers
 * the next stage as a button. Every button is one paid request the engine
 * will not take until it is pressed — the whole point of the mode.
 *
 * The order of the panel is the order of the questions being asked: where am
 * I (headline and rail), what has happened (checklist), what happens next
 * (the approve buttons). The receipts for a finished case, order or BOM come
 * after all three, because thirteen clauses or a table of parts between the
 * checklist and the buttons pushed the one control that matters off the
 * bottom of the strip.
 */
/** What one approval costs, stated on the control that spends it. */
const PRICE = "1 call";

export const StepPanel = ({
  run,
  note,
  onDismiss,
  armed = null,
  onConfirm,
  onDisarm,
  reviewed = true,
}: StepPanelProps) => {
  const rows = railRows({
    history: run.history,
    running: run.running,
    available: run.available,
    status: run.status,
    failedStep: run.failedStep,
    background: run.background,
  });
  const latest = run.history[run.history.length - 1];
  const caseOutcome = caseDetails(latest);
  const order = orderDetails(latest);
  const sourcing = sourcingDetails(latest);
  const review = reviewDetails(latest);
  const route = routeDetails(latest);
  const place = placeDetails(latest);

  // The case step's two inputs (`service/steps.py::_case`): a style, and the
  // rigorous flag. Either one makes the engine design afresh — one more model
  // call — instead of collecting the design it started at `place`. Panel
  // state, not hook state: they are what the *next* press of `case` carries,
  // and the hook has no business remembering a half-typed style.
  const [caseStyle, setCaseStyle] = useState("");
  const [caseRigorous, setCaseRigorous] = useState(false);
  const caseOffered = run.available.includes("case");
  const caseAfresh = caseStyle.trim().length > 0 || caseRigorous;
  const payloadFor = (step: StepName): Record<string, unknown> | undefined => {
    if (step !== "case") return undefined;
    const payload = casePayload(caseStyle, caseRigorous);
    return Object.keys(payload).length ? payload : undefined;
  };

  const doneCount = rows.filter((row) => row.status === "done").length;
  const nextStep = run.available[0] ?? null;
  const position = run.running
    ? STEP_ORDER.indexOf(run.running) + 1
    : nextStep
      ? STEP_ORDER.indexOf(nextStep) + 1
      : doneCount;

  // Where the newest artifact went. A stage the engine never showed cannot be
  // nagged about, which is why this is `shown_in_kicad` and not "there is a
  // file somewhere".
  const shownWhere = latest?.shown_in_kicad
    ? WHERE_LABEL[STEP_DESCRIPTORS[latest.step].where]
    : null;
  const unread = !reviewed && shownWhere !== null && nextStep !== null;

  // One sentence, in the first person, answering "what is going on" before
  // anything else on the panel — including the two different truths about a
  // stage that never reached KiCad. `headlineFix` is the engine's own reason
  // when it sent one, and silence when it did not.
  const headlineInput = {
    status: run.status,
    running: run.running,
    latest,
    elapsedS: run.elapsedS,
    error: run.error,
    armed,
    reviewed: !unread,
    available: run.available,
  };
  const headline = headlineOf(headlineInput);
  const notShown = headlineFix(headlineInput);

  // The stage line under the receipts: what the newest stage did, which
  // engine did it, how long it took. There is no run-level receipt — that one
  // went with the redesign. The review card came back (2026-09-06): the
  // redesign dropped it when the review step was a count of findings, and it
  // now carries each finding's provenance, its citation and the spec-review
  // agenda, which are decisions the engineer makes rather than a number.
  const stageReceipt: StepResponse | undefined =
    latest && (review || caseOutcome || order || sourcing || route || place) ? latest : undefined;

  return (
    <div className="flex flex-col gap-2 border-t border-input/40 pt-2" data-testid="step-panel">
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "text-xs font-medium",
            run.status === "error" && "text-destructive"
          )}
          data-testid="step-headline"
        >
          {headline}
        </span>
        <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
          {run.status === "running" ? `${run.elapsedS.toFixed(0)} s · ` : ""}
          {position} of {STEP_ORDER.length}
        </span>
      </div>

      {notShown ? (
        // The engine's own reason, which carries the fix. The rail repeats it
        // on the row it belongs to; this is the one under the headline.
        <p className="text-[11px] leading-tight text-muted-foreground" data-testid="step-headline-fix">
          {notShown}
        </p>
      ) : null}

      <StepRail rows={rows} />

      <ul className="flex flex-col gap-1">
        {rows.map((row) => {
          const Icon =
            row.status === "done" ? CheckIcon : row.status === "running" ? Loader2 : CircleIcon;
          const quiet = row.status === "pending";
          // The job's own state, not the row's: a background failure is
          // reported on the row nobody pressed, in red, and must never be
          // mistaken for the stage having simply not run yet.
          const jobState = backgroundState(run, row.id);
          return (
            <li
              key={row.id}
              data-testid="step-row"
              data-step={row.id}
              data-status={row.status}
              className={cn("flex flex-col gap-0.5", quiet && "opacity-60")}
            >
              <div className="flex items-center gap-1.5">
                <Icon
                  className={cn(
                    "size-3 shrink-0",
                    row.status === "running" && "animate-spin"
                  )}
                />
                <span className="shrink-0 text-[11px] leading-tight">{row.label}</span>
                {/* `receiptLine`'s own words: the terse measured clause that
                    sits right of the label on a finished row -- counts,
                    millimetres, verdicts, and nothing else. */}
                {row.receipt ? (
                  <span className="min-w-0 flex-1 truncate text-[10.5px] leading-tight text-muted-foreground">
                    {row.receipt}
                  </span>
                ) : (
                  <span className="min-w-0 flex-1" />
                )}
                {row.whereLabel ? (
                  // The chip does not vouch for what did not happen: a stage
                  // the bridge could not show reads "on disk", not "in KiCad".
                  <span
                    data-testid="where-chip"
                    data-shown={row.shown ? "true" : "false"}
                    className="shrink-0 rounded-full border border-input/60 px-1.5 text-[10px] text-muted-foreground"
                  >
                    {row.whereLabel}
                  </span>
                ) : null}
              </div>
              {row.preparing ? (
                <span className="pl-[18px] text-[10.5px] leading-tight text-muted-foreground">
                  {row.preparing}
                </span>
              ) : null}
              {row.backgroundNote ? (
                <p
                  data-testid="background-note"
                  data-step={row.id}
                  data-state={jobState ?? undefined}
                  className={cn(
                    "pl-[18px] text-[10.5px] leading-tight",
                    jobState === "failed" ? "text-destructive" : "text-muted-foreground"
                  )}
                >
                  {row.backgroundNote}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>

      {run.status === "error" && run.error ? (
        <div className="flex flex-col gap-1">
          {/* The headline is the engine's own sentence; it is repeated here
              only when something armed has taken the headline over. */}
          {armed ? (
            <p className="text-[11px] font-medium text-destructive">{run.error.message}</p>
          ) : null}
          <p className="text-[11px] text-muted-foreground">
            Trying again is another engine call.
          </p>
          {run.error.detail ? (
            <p className="text-[11px] text-muted-foreground">{run.error.detail}</p>
          ) : null}
        </div>
      ) : null}

      {note ? (
        <p className="text-[11px] text-muted-foreground" data-testid="step-note">
          {note}
        </p>
      ) : null}

      {run.status === "running" ? (
        <div className="flex items-center justify-end">
          <Button size="sm" variant="outline" onClick={run.cancel} data-testid="step-cancel">
            <XIcon className="size-3.5" />
            Cancel
          </Button>
        </div>
      ) : null}

      {caseOffered && (run.status === "waiting" || run.status === "error") ? (
        // The case step's inputs, shown only while it can be pressed. With
        // neither set, pressing collects the design the engine already
        // started; either one asks for a fresh design instead, and the
        // button below says which it will do.
        <div className="flex flex-col gap-1 rounded-md border border-input/40 p-2" data-testid="case-options">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="kaleo-case-rigorous" className="text-[11px]">
              Design the case afresh (rigorous)
            </Label>
            <Switch
              id="kaleo-case-rigorous"
              checked={caseRigorous}
              onCheckedChange={setCaseRigorous}
              data-testid="case-rigorous"
            />
          </div>
          <Input
            className="h-7 text-[11px]"
            placeholder="Case style, optional — e.g. snap lid, vented, wall-mount tabs"
            value={caseStyle}
            maxLength={MAX_ENCLOSURE_STYLE_CHARS}
            onChange={(event) => setCaseStyle(event.target.value)}
            aria-label="Case style"
            data-testid="case-style"
          />
          <p className="text-[10px] text-muted-foreground" data-testid="case-options-note">
            {caseAfresh
              ? "Pressing Case designs afresh with these — one more model call, instead of the design started at placement."
              : "Pressing Case collects the design the engine started at placement. A style or the rigorous switch designs afresh instead."}
          </p>
        </div>
      ) : null}

      {armed && (run.status === "waiting" || run.status === "error" || run.status === "done") ? (
        // A sentence selected this stage; a human commits it. The echo is the
        // point: what the machine understood, before it costs anything.
        <div className="flex flex-col gap-1" data-testid="step-armed" data-step={armed.step}>
          <p className="text-[11px] text-muted-foreground">
            heard <span className="italic">“{armed.source}”</span>
          </p>
          <div className="flex flex-wrap items-center gap-1.5">
            {armed.step === "restart" ? (
              // Spends nothing, but drops a run that already cost money: the
              // engine keeps its steps, this strip forgets them.
              <Button
                size="sm"
                variant="destructive"
                onClick={() => onConfirm?.()}
                data-testid="step-confirm"
                data-step="restart"
                title="This run's steps stay on the engine, not here."
              >
                Start over
              </Button>
            ) : (
              <Button
                size="sm"
                onClick={() => onConfirm?.(payloadFor(armed.step))}
                autoFocus
                data-testid="step-confirm"
                data-step={armed.step}
                title={`Runs in ${WHERE_LABEL[STEP_DESCRIPTORS[armed.step].where]}. Not refundable.`}
              >
                {STEP_DESCRIPTORS[armed.step].action} · {PRICE}
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={onDisarm} data-testid="step-disarm">
              Not that
            </Button>
          </div>
        </div>
      ) : run.status === "waiting" || run.status === "error" ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {run.available.map((step: StepName) => (
            <Button
              key={step}
              size="sm"
              // The primary weight is a claim that this is the obvious next
              // click. It is not, while the artifact is still unread.
              variant={step === run.available[0] && !unread ? "default" : "outline"}
              // Only the case step carries a payload of the panel's own;
              // every other approval is the bare step, so the hook's own
              // fields (`summaryPayload`) are all it sends.
              onClick={() => {
                const payload = payloadFor(step);
                if (payload) run.approve(step, payload);
                else run.approve(step);
              }}
              data-testid="step-approve"
              data-step={step}
              data-unread={step === run.available[0] && unread ? "true" : undefined}
              data-afresh={step === "case" && caseAfresh ? "true" : undefined}
              title={`Runs in ${WHERE_LABEL[STEP_DESCRIPTORS[step].where]}. One engine call, not reversible.`}
            >
              {step === "case" && caseAfresh ? "Design the case afresh" : STEP_DESCRIPTORS[step].action}
              {step === run.available[0] ? ` · ${PRICE}` : ""}
            </Button>
          ))}
          <Button size="sm" variant="ghost" onClick={onDismiss} data-testid="step-dismiss">
            {run.available.length ? "Stop here" : "Done"}
          </Button>
        </div>
      ) : null}

      {run.status === "done" ? (
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] text-muted-foreground">
            {latest?.files.board ? "The routed board is on disk." : "Nothing further to run."}
          </span>
          <Button size="sm" variant="ghost" onClick={onDismiss} data-testid="step-dismiss">
            New run
          </Button>
        </div>
      ) : null}

      {review || caseOutcome || order || sourcing || route || place ? (
        // The receipts scroll inside their own box: the window stops growing
        // at OVERLAY_MAX_HEIGHT, and a BOM below that edge was simply gone,
        // with nothing to say so. The buttons above stay put.
        //
        // Findings come first, because a blocking finding is a decision to
        // make and a table of parts is not; the final receipt comes last,
        // because it is the last frame of the run.
        <div className="max-h-[14rem] overflow-y-auto" data-testid="step-scroll">
          <div className="flex flex-col gap-2">
            {/* These are bounded (the box scrolls at 14rem) and they are the
                run's own output, so they stay until the dashboard can show
                them: `useStepRun.stepRunResult` copies neither `sourcing`
                nor `enclosure` into RunResult, so moving them today would
                delete the BOM and the case receipt rather than relocate
                them. Findings first -- a blocker is a decision to make and a
                table of parts is not. */}
            {stageReceipt ? <StageReceipt response={stageReceipt} /> : null}
            {place ? <PlaceOutcome details={place} /> : null}
            {route ? <RouteOutcome details={route} /> : null}
            {review ? <ReviewOutcome details={review} /> : null}
            {caseOutcome ? <CaseOutcome details={caseOutcome} /> : null}
            {order ? <OrderOutcome details={order} onShow3d={run.show3d} /> : null}
            {sourcing ? <SourcingOutcome details={sourcing} /> : null}
          </div>
        </div>
      ) : null}

    </div>
  );
};

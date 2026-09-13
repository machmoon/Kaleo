// The approval-gated run, as data the overlay can render.
//
// The engineer reviews each stage in KiCad; the overlay is the control strip
// that says what just happened and what may happen next. Everything here is
// pure — it reads a `StepResponse` and produces labels — so the panel that
// draws it has nothing to test but layout.

import type {
  BackgroundJobName,
  BackgroundOutcome,
  BackgroundOutcomes,
  EnclosureBlock,
  Finding,
  OrderIssue,
  ReviewBlock,
  SourcingEntry,
  StepName,
  StepResponse,
  StepStatusResponse,
} from "./types";

/** Every step the engine can take, in pipeline order. */
export const STEP_ORDER: readonly StepName[] = Object.freeze([
  "propose",
  "place",
  "route",
  "review",
  "sourcing",
  "order",
  "case",
]);

export interface StepDescriptor {
  id: StepName;
  /** The checklist row. */
  label: string;
  /** The approve button that triggers it — imperative, what the engineer is authorising. */
  action: string;
  /** Where the engineer looks for it. */
  where: "kicad" | "overlay";
}

export const STEP_DESCRIPTORS: Record<StepName, StepDescriptor> = {
  propose: { id: "propose", label: "Schematic", action: "Propose circuit", where: "kicad" },
  place: { id: "place", label: "Placement", action: "Place parts", where: "kicad" },
  route: { id: "route", label: "Routing", action: "Route copper", where: "kicad" },
  review: { id: "review", label: "Review", action: "Review the design", where: "overlay" },
  sourcing: { id: "sourcing", label: "Sourcing", action: "Source the parts", where: "overlay" },
  order: { id: "order", label: "Order", action: "Prepare fab order", where: "overlay" },
  case: { id: "case", label: "Case", action: "Design the case", where: "overlay" },
};

/** Where a step's result is looked at, as the panel names it. */
export const WHERE_LABEL: Record<StepDescriptor["where"], string> = {
  kicad: "KiCad",
  overlay: "here",
};

// ---------------------------------------------------------------- badges

/**
 * The four tones every status in the panel is drawn in. Four is the whole
 * palette on purpose: an order blocker, a review blocker and an unreachable
 * datasheet all read as the same red, so the engineer learns one code.
 */
export type BadgeTone = "ok" | "warn" | "bad" | "muted";

export interface BadgeSpec {
  label: string;
  tone: BadgeTone;
}

/** The vocabularies the panel renders: the order gate's, the critic's, and the two sourcing ones. */
export type BadgeKind = "issue" | "finding" | "datasheet" | "mpn";

const BADGES: Record<BadgeKind, Record<string, BadgeSpec>> = {
  issue: {
    blocker: { label: "Blocker", tone: "bad" },
    warning: { label: "Warning", tone: "warn" },
    note: { label: "Note", tone: "muted" },
  },
  finding: {
    blocker: { label: "Blocker", tone: "bad" },
    error: { label: "Error", tone: "bad" },
    marginal: { label: "Marginal", tone: "warn" },
    warning: { label: "Warning", tone: "warn" },
    note: { label: "Note", tone: "muted" },
    info: { label: "Info", tone: "muted" },
  },
  // Only `verified` earns an open button: the other three are a page that
  // is not a PDF, a fetch that failed, and no URL at all.
  datasheet: {
    verified: { label: "PDF verified", tone: "ok" },
    not_pdf: { label: "not a PDF", tone: "warn" },
    unreachable: { label: "unreachable", tone: "bad" },
    none: { label: "no datasheet", tone: "muted" },
  },
  // `verified` is a distributor confirming the part number exists; anything
  // less is the model's word, and the badge says so.
  mpn: {
    verified: { label: "Verified", tone: "ok" },
    proposed: { label: "Proposed", tone: "muted" },
    none: { label: "No MPN", tone: "muted" },
  },
};

/**
 * The badge for a status word. An unknown word renders as itself, muted:
 * the engine's vocabulary is additive, and a status this build has never
 * seen must show rather than crash or vanish.
 */
export function statusBadge(kind: BadgeKind, value: string | null | undefined): BadgeSpec {
  const word = String(value ?? "");
  return BADGES[kind][word] ?? { label: word || "unknown", tone: "muted" };
}

function isWebUrl(url: string | null | undefined): url is string {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

/**
 * The distributor page the panel may link to, or null. Only a `verified`
 * MPN gets one — a link under a proposed part number is the panel vouching
 * for what nothing checked — and only to an http(s) URL, since the opener
 * would hand anything else to the OS.
 */
export function distributorLink(entry: Pick<SourcingEntry, "mpn_status" | "distributor_url">): string | null {
  if (entry.mpn_status !== "verified") return null;
  return isWebUrl(entry.distributor_url) ? entry.distributor_url : null;
}

/** The datasheet URL the panel may open, or null: the probe saw a PDF there, and it is a web URL. */
export function datasheetLink(entry: Pick<SourcingEntry, "datasheet_status" | "datasheet_url">): string | null {
  if (entry.datasheet_status !== "verified") return null;
  return isWebUrl(entry.datasheet_url) ? entry.datasheet_url : null;
}

/**
 * `preparing` is a step the engine started on its own — the case and the
 * parts lookup, both begun from the placed board — that may still be
 * approved (pressing it collects the result, or waits for it).
 */
export type StepStatus = "pending" | "running" | "done" | "available" | "preparing";

/**
 * What the row says while the engine is on a step nobody pressed for, so a
 * spinner without a button behind it is not a mystery. Only the steps the
 * engine actually starts by itself have a line.
 */
const PREPARING_LINE: Partial<Record<StepName, string>> = {
  sourcing: "Looking up parts in the background…",
  case: "Designing in the background…",
};

export interface StepRow {
  id: StepName;
  label: string;
  status: StepStatus;
  /** One line about what the step produced, once it has. */
  summary: string | null;
  /** What the engine is doing on its own; set only while `status` is `preparing`. */
  preparing: string | null;
  /** True when the engine reported it pushed this stage into KiCad. */
  shown: boolean;
  /**
   * What became of a job the engine started on its own, once it is no longer
   * running and the step has not been pressed: see `BackgroundJob`. Null
   * while the job runs, once the step is done, and for steps that never ran
   * in the background.
   */
  backgroundNote: string | null;
}

// ---------------------------------------------------------------- background

/**
 * Where a job the engine started on its own stands — `case` and `sourcing`,
 * from the moment `place` lands.
 *
 * The engine's `background` field is a list of what is *still running*, and
 * `background_outcome` says how each job that has left it ended: `finished`
 * when it reports `ok`, `failed` with its `detail` when it does not. An older
 * engine sends no outcome at all — a job's fate then travels only on the
 * envelope of the step that collects it (the case on `case`, the BOM on
 * `sourcing` or on `order`, whichever is pressed first), so `failed` is
 * claimed only when such a warning has actually arrived and everything else
 * that has left the list is `settled`, the honest word for "finished, outcome
 * not yet reported".
 */
export type BackgroundState = "running" | "settled" | "finished" | "failed";

export interface BackgroundJob {
  step: StepName;
  state: BackgroundState;
  /** The engine's own sentence naming the failure; null unless `failed`. */
  warning: string | null;
}

/** The engine's fallback sentence for an outcome that failed without saying why. */
const FAILED_WITHOUT_DETAIL: Record<BackgroundJobName, string> = {
  case: "The case designed in the background failed; the engine gave no reason.",
  sourcing: "The parts lookup in the background failed; the engine gave no reason.",
};

function isBackgroundJobName(step: StepName): step is BackgroundJobName {
  return step === "case" || step === "sourcing";
}

/**
 * The outcomes seen so far, the newer envelope's word winning per job. A job
 * appears in an outcome only once, so this is a merge rather than a replace:
 * a `GET /steps/<id>` that lists the case's outcome must not forget the BOM's.
 */
export function mergeBackgroundOutcomes(
  known: BackgroundOutcomes,
  latest: BackgroundOutcomes | undefined
): BackgroundOutcomes {
  if (!latest) return known;
  const out: BackgroundOutcomes = { ...known };
  for (const job of ["case", "sourcing"] as const) {
    const outcome = latest[job];
    if (outcome && typeof outcome.ok === "boolean") {
      out[job] = { ok: outcome.ok, detail: typeof outcome.detail === "string" ? outcome.detail : null };
    }
  }
  return out;
}

function jobFromOutcome(step: BackgroundJobName, outcome: BackgroundOutcome): BackgroundJob {
  if (outcome.ok) return { step, state: "finished", warning: null };
  const detail = outcome.detail?.trim();
  return { step, state: "failed", warning: detail || FAILED_WITHOUT_DETAIL[step] };
}

/**
 * The warning `service/steps.py` writes when a background job raised, keyed
 * by the step it belongs to. `_collect_sourcing` puts its sentence on
 * whichever of `sourcing`/`order` collected the BOM; `_case` puts its own on
 * `case`. Matched by the fixed prefix, so a rewording of the reason after the
 * colon still matches and a rewording of the prefix is a test failure here,
 * not a silent miss. The case has two sentences: `_case_failure` when the
 * job raised, and `NO_CASE_WARNING` ("enclosure generation failed; …") when
 * it finished with nothing — `_case_outcome` reports both as `ok: false`.
 */
const BACKGROUND_FAILURE: Partial<Record<StepName, RegExp>> = {
  sourcing: /^parts were not sourced: the lookup in the background failed/,
  case: /^(the case designed in the background failed|enclosure generation failed)/,
};

/** The engine's warning that `step`'s background job failed, if one has arrived. */
export function backgroundFailure(
  step: StepName,
  history: readonly StepResponse[]
): string | null {
  const pattern = BACKGROUND_FAILURE[step];
  if (!pattern) return null;
  for (const response of history) {
    for (const warning of response.warnings ?? []) {
      if (pattern.test(warning)) return warning;
    }
  }
  return null;
}

/**
 * The background jobs, folded from what has been seen so far.
 *
 * `seen` is every step that has ever appeared in a `background` list this
 * session; `running` is the engine's latest list (from an envelope or from
 * `GET /steps/<id>`). A step in `history` has been collected and is no
 * longer a background job at all — its outcome is on its own row.
 */
export function backgroundJobs(
  seen: Iterable<StepName>,
  running: readonly StepName[],
  history: readonly StepResponse[],
  /**
   * The engine's `background_outcome`, merged over everything seen so far.
   * A job named here is finished or failed on the engine's own word; a job
   * absent from it (an older engine, or one still running) falls back to the
   * list-and-warning reading above.
   */
  outcomes: BackgroundOutcomes = {}
): BackgroundJob[] {
  const done = new Set(history.map((r) => r.step));
  const live = new Set(running);
  const known = new Set(seen);
  for (const job of ["case", "sourcing"] as const) if (outcomes[job]) known.add(job);
  const jobs: BackgroundJob[] = [];
  for (const step of STEP_ORDER) {
    if (!known.has(step) && !live.has(step)) continue;
    if (done.has(step)) continue;
    if (live.has(step)) {
      jobs.push({ step, state: "running", warning: null });
      continue;
    }
    if (isBackgroundJobName(step)) {
      const outcome = outcomes[step];
      if (outcome) {
        jobs.push(jobFromOutcome(step, outcome));
        continue;
      }
    }
    const warning = backgroundFailure(step, history);
    jobs.push(
      warning
        ? { step, state: "failed", warning }
        : { step, state: "settled", warning: null }
    );
  }
  return jobs;
}

/**
 * What a settled job's row says — an older engine's job that left the list
 * with no outcome: the engine has finished, and only pressing tells how.
 */
const SETTLED_LINE: Partial<Record<StepName, string>> = {
  sourcing: "Finished looking up parts in the background — press to collect; the engine reports the outcome only when collected.",
  case: "Finished designing in the background — press to collect; the engine reports the outcome only when collected.",
};

/** What a finished job's row says: the engine reported `ok`, and pressing collects it. */
const FINISHED_LINE: Partial<Record<StepName, string>> = {
  sourcing: "Parts lookup finished in the background — press to collect",
  case: "Case design finished in the background — press to collect",
};

/** The row's sentence for one background job, or null while it runs. */
export function backgroundNote(job: BackgroundJob | undefined): string | null {
  if (!job || job.state === "running") return null;
  if (job.state === "failed") return job.warning;
  if (job.state === "finished") {
    return FINISHED_LINE[job.step] ?? "Finished in the background — press to collect";
  }
  return SETTLED_LINE[job.step] ?? "Finished in the background — press to collect.";
}

// ---------------------------------------------------------------- case payload

/** `enclosure_style` is refused past this many characters (`MAX_ENCLOSURE_STYLE_CHARS`). */
export const MAX_ENCLOSURE_STYLE_CHARS = 500;

/**
 * What the `case` approval carries. Nothing when nothing was asked for — an
 * empty body collects the design the engine started at `place`; a style or
 * the rigorous flag makes it design afresh, in line, for one more model call
 * (`service/steps.py::_case`). A blank style is not sent, since `" "` would
 * be a non-empty string only in the eyes of a caller that never trimmed it.
 */
export function casePayload(style: string, rigorous: boolean): Record<string, unknown> {
  const trimmed = style.trim();
  return {
    ...(trimmed ? { enclosure_style: trimmed } : {}),
    ...(rigorous ? { enclosure_rigorous: true } : {}),
  };
}

function mm(n: number | undefined): string {
  return n === undefined ? "?" : n.toFixed(1);
}

// ---------------------------------------------------------------- resync

export const UNRECEIVED_SUMMARY = "finished while cancelled — result not received";

/**
 * The history entries that stand in for a step the engine finished but this
 * client never heard back from. Tracked by identity rather than by a field,
 * so the wire type stays exactly what the engine sends.
 */
const unreceived = new WeakSet<StepResponse>();

/**
 * A history marker for a step the engine reports under `status.done` but
 * whose response never arrived — the fetch was cancelled, the engine finished
 * anyway. It carries the engine's `next` so the checklist stays truthful
 * about what may run, and nothing else: no summary fields, because none were
 * received.
 */
export function unreceivedStep(status: StepStatusResponse, step: StepName): StepResponse {
  const marker: StepResponse = {
    session: status.session,
    step,
    stage: status.stage,
    intent: status.intent,
    files: { ...status.files },
    next: [...status.next],
    shown_in_kicad: false,
    events: [],
    duration_s: 0,
  };
  unreceived.add(marker);
  return marker;
}

export function isUnreceivedStep(response: StepResponse): boolean {
  return unreceived.has(response);
}

/**
 * Bring `history` into line with what the engine says: every step in
 * `status.done` the client has no response for gets a marker, in pipeline
 * order, and `available` is the engine's own `next`. Pure; the hook decides
 * what to do with the result.
 */
export function reconcileHistory(
  history: readonly StepResponse[],
  status: StepStatusResponse
): { history: StepResponse[]; available: StepName[] } {
  const seen = new Set(history.map((r) => r.step));
  const missing = STEP_ORDER.filter((step) => status.done.includes(step) && !seen.has(step));
  const next = new Set<StepName>(status.next);
  return {
    history: [...history, ...missing.map((step) => unreceivedStep(status, step))],
    available: STEP_ORDER.filter((s) => next.has(s)),
  };
}

/** One sentence about a finished step, from its own response. */
export function summarizeStep(response: StepResponse): string {
  if (isUnreceivedStep(response)) return UNRECEIVED_SUMMARY;
  switch (response.step) {
    case "propose": {
      const parts = typeof response.parts === "number" ? response.parts : 0;
      const repairs = response.repair_rounds ?? 0;
      const tail = repairs ? `, after ${repairs} repair round${repairs === 1 ? "" : "s"}` : "";
      return `Proposed ${parts} parts and ${response.nets ?? 0} nets${tail}.`;
    }
    case "place": {
      const count = Array.isArray(response.parts) ? response.parts.length : 0;
      const [w, h] = response.board_mm ?? [undefined, undefined];
      const status = response.status ? ` (${response.status})` : "";
      return `Placed ${count} parts on a ${mm(w)} × ${mm(h)} mm board${status}.`;
    }
    case "route": {
      const r = response.routing ?? {};
      const routed = r.routed?.length ?? 0;
      const unrouted = Object.keys(r.unrouted ?? {}).length;
      const total = routed + unrouted;
      const open = unrouted ? `, ${unrouted} left as ratsnest` : "";
      return `Routed ${routed} of ${total} nets with ${r.tracks ?? 0} tracks and ${r.vias ?? 0} vias${open}.`;
    }
    case "review": {
      const failed = reviewFailure(response.review);
      if (failed) return `${failed[0].toUpperCase()}${failed.slice(1)}.`;
      const skipped = reviewSkipped(response.review);
      if (skipped) return `${skipped[0].toUpperCase()}${skipped.slice(1)}.`;
      const n = response.findings?.length ?? 0;
      const blockers = response.blockers?.length ?? 0;
      if (n === 0) return REVIEW_CLEAN_LINE;
      return `${n} finding${n === 1 ? "" : "s"}, ${blockers} blocking.`;
    }
    case "order": {
      // `orderable` sits on the block; the counts live on its manifest.
      const order = response.order ?? {};
      const blockers =
        order.manifest?.blocker_count ??
        (typeof order.blocker_count === "number" ? order.blocker_count : 0);
      if (!order.orderable) {
        return `Not orderable: ${blockers} blocker${blockers === 1 ? "" : "s"} to fix first.`;
      }
      const issues = order.issues?.length ?? 0;
      return issues
        ? `Fab order prepared; ${issues} issue${issues === 1 ? "" : "s"} to read first.`
        : "Fab order prepared with no issues.";
    }
    case "sourcing": {
      const details = sourcingDetails(response);
      if (!details) return "Sourcing produced no bill of materials.";
      const total = details.parts.length;
      const { named, confirmed, verified } = details;
      const distributor = confirmed
        ? `, ${confirmed} confirmed by a distributor`
        : "";
      return `Sourced ${named} of ${total} parts${distributor}; ${verified} datasheet${verified === 1 ? "" : "s"} verified.`;
    }
    case "case":
      return summarizeCase(response.enclosure ?? null);
    default:
      return "Done.";
  }
}

// ---------------------------------------------------------------- order

/** Everything the panel shows for a finished order step. */
export interface OrderDetails {
  orderable: boolean;
  /** Every issue the gate raised, blockers first, then warnings, then notes. */
  issues: OrderIssue[];
  /** `<stem>-order.zip` on the engine's machine. */
  zip: string | null;
  manifest: string | null;
  /** The `.glb` a `kicad-cli` export produced, or null when it did not. */
  model: string | null;
  step: string | null;
  /** Why something did not happen — a missing kicad-cli, a failed export. */
  warnings: string[];
}

const ISSUE_RANK: Record<string, number> = { blocker: 0, warning: 1, note: 2 };

/**
 * The order step's response as the panel needs it, or null for any other
 * step. Issues are ordered by severity and otherwise kept in the gate's own
 * order, so "not orderable" is followed by the reasons, worst first.
 */
export function orderDetails(response: StepResponse | undefined): OrderDetails | null {
  if (!response || response.step !== "order") return null;
  const order = response.order ?? {};
  const issues = (order.issues ?? [])
    .map((issue, index) => ({ issue, index }))
    .sort(
      (a, b) =>
        (ISSUE_RANK[a.issue.severity] ?? 3) - (ISSUE_RANK[b.issue.severity] ?? 3) ||
        a.index - b.index
    )
    .map(({ issue }) => issue);
  return {
    orderable: order.orderable === true,
    issues,
    zip: response.files.order ?? null,
    manifest: response.files.order_manifest ?? null,
    model: response.files.model_glb ?? null,
    step: response.files.model_step ?? null,
    warnings: response.warnings ?? [],
  };
}

// ---------------------------------------------------------------- sourcing

/** The BOM as the panel shows it: the rows, the counts, the file. */
export interface SourcingDetails {
  parts: SourcingEntry[];
  /** Datasheets whose URL really served a PDF — the engine's count. */
  verified: number;
  /** The engine's own `proposed` count, kept as sent. */
  proposed: number;
  unresolved: number;
  /** Parts with a part number at all, proposed or verified — counted from the rows. */
  named: number;
  /** Parts whose MPN a distributor confirmed — counted from the rows. */
  confirmed: number;
  /** `<stem>-bom.csv` on the engine's machine, once written. */
  bom: string | null;
  /** What the engine gave up on — a model that never answered validly. */
  warnings: string[];
}

/**
 * The sourcing block as the panel needs it, or null when the response has
 * none. Both the sourcing step and the order step carry it — the order
 * manifest holds the same BOM — so the table follows whichever came last.
 *
 * The datasheet count is the engine's own (it ran the probe); when it sent
 * none it is counted from the rows rather than shown as a quiet zero. The
 * MPN counts are always taken from the rows: `verified` joined the
 * vocabulary after the engine's `proposed` counter was written, and a row
 * is the only thing that cannot be counted two ways.
 */
export function sourcingDetails(response: StepResponse | undefined): SourcingDetails | null {
  if (!response || (response.step !== "sourcing" && response.step !== "order")) return null;
  const block = response.sourcing;
  if (!block) return null;
  const parts = block.parts ?? [];
  const confirmed = parts.filter((p) => p.mpn_status === "verified").length;
  const named = parts.filter((p) => p.mpn_status === "proposed").length + confirmed;
  return {
    parts,
    verified: block.verified ?? parts.filter((p) => p.datasheet_status === "verified").length,
    proposed: block.proposed ?? parts.filter((p) => p.mpn_status === "proposed").length,
    unresolved: block.unresolved ?? parts.length - named,
    named,
    confirmed,
    bom: response.files.bom ?? null,
    warnings: block.warnings ?? [],
  };
}

/**
 * Which row carries the BOM table, so it is drawn once. The sourcing step
 * owns it when it ran; otherwise the order step, which collects the
 * background job and carries the same block; null when neither has one.
 */
export function bomHost(history: readonly StepResponse[]): StepName | null {
  if (history.some((r) => r.step === "sourcing" && !!r.sourcing)) return "sourcing";
  if (history.some((r) => r.step === "order" && !!r.sourcing)) return "order";
  return null;
}

// ---------------------------------------------------------------- other details

/** The placement detail: what was placed, and anything the solver said. */
export interface PlaceDetails {
  parts: { ref: string; footprint: string }[];
  warnings: string[];
}

export function placeDetails(response: StepResponse | undefined): PlaceDetails | null {
  if (!response || response.step !== "place" || isUnreceivedStep(response)) return null;
  const parts = Array.isArray(response.parts) ? response.parts : [];
  return { parts, warnings: response.warnings ?? [] };
}

/** The routing detail: every net left as ratsnest, by name, with the reason. */
export interface RouteDetails {
  routed: number;
  /** Routed plus unrouted: every net the router was asked about. */
  total: number;
  unrouted: { net: string; reason: string }[];
  tracks: number;
  vias: number;
  /**
   * The engine's own routed fraction (`RouteResult.completion`), or null when
   * this engine did not send one. Not recomputed here: a refusal names every
   * net it covers in `unrouted`, so the engine's 0/0 = 100% blind spot
   * (routing.py's `refuse`) cannot occur, and a client-side figure that
   * disagreed with `routed`/`unrouted` would be a bug to report, not smooth over.
   */
  completion: number | null;
  warnings: string[];
}

export function routeDetails(response: StepResponse | undefined): RouteDetails | null {
  if (!response || response.step !== "route" || isUnreceivedStep(response)) return null;
  const r = response.routing ?? {};
  const unrouted = Object.entries(r.unrouted ?? {}).map(([net, reason]) => ({
    net,
    reason: String(reason),
  }));
  const routed = r.routed?.length ?? 0;
  return {
    routed,
    total: routed + unrouted.length,
    unrouted,
    tracks: r.tracks ?? 0,
    vias: r.vias ?? 0,
    completion: typeof r.completion === "number" ? r.completion : null,
    warnings: [...(r.warnings ?? []), ...(response.warnings ?? [])],
  };
}

const FINDING_RANK: Record<string, number> = {
  blocker: 0,
  error: 0,
  marginal: 1,
  warning: 1,
  note: 2,
  info: 2,
};

/**
 * What became of the critic, from the engine's `review` block.
 *
 * Three outcomes stay three words. `ok` is a review that answered — with
 * findings, or with none, which is a real answer. `failed` is a critic that
 * was asked and answered nothing readable, and then an empty finding list
 * says nothing at all about the board. `skipped` is a review nobody asked
 * for. An older engine sends no block; it never distinguished the first two,
 * so its review reads as `ok` — the reading every caller had before.
 */
export type ReviewOutcomeStatus = "ok" | "failed" | "skipped";

export interface ReviewOutcomeState {
  status: ReviewOutcomeStatus;
  ran: boolean;
  detail: string | null;
  note: string;
}

export function reviewOutcome(review: ReviewBlock | undefined | null): ReviewOutcomeState {
  if (!review) return { status: "ok", ran: true, detail: null, note: "" };
  const status: ReviewOutcomeStatus =
    review.status === "failed" ? "failed" : review.status === "skipped" ? "skipped" : "ok";
  const detail = typeof review.detail === "string" && review.detail.trim() ? review.detail.trim() : null;
  return {
    status,
    ran: typeof review.ran === "boolean" ? review.ran : status !== "skipped",
    detail,
    note: typeof review.note === "string" ? review.note : "",
  };
}

/** What a review that answered nothing says, everywhere it is said. */
export const REVIEW_FAILED_TAIL = "nothing is known about this board";

/**
 * The failed sentence, or null when the review did not fail. One function
 * so the step card, the one-shot card, the receipt and the send panel cannot
 * drift into four wordings of the same fact.
 */
export function reviewFailure(review: ReviewBlock | undefined | null): string | null {
  const outcome = reviewOutcome(review);
  if (outcome.status !== "failed") return null;
  const why = outcome.detail ?? outcome.note.trim();
  return `review failed: ${why || "the critic answered nothing readable"} — ${REVIEW_FAILED_TAIL}`;
}

/** The skipped sentence, or null when the review was not skipped. */
export function reviewSkipped(review: ReviewBlock | undefined | null): string | null {
  const outcome = reviewOutcome(review);
  if (outcome.status !== "skipped") return null;
  const why = outcome.detail ?? outcome.note.trim();
  return `review skipped${why ? `: ${why}` : ""} — ${REVIEW_FAILED_TAIL}`;
}

/** What an `ok` review with an empty finding list says. */
export const REVIEW_CLEAN_LINE = "The critic found nothing to flag.";

/**
 * The review detail: the critic's findings, worst first, in its own order
 * otherwise, and what became of the critic (`reviewOutcome`), which decides
 * whether an empty list means "nothing to flag" or "nothing is known".
 */
export interface ReviewDetails extends ReviewOutcomeState {
  findings: Finding[];
  blockers: number;
}

export function reviewDetails(response: StepResponse | undefined): ReviewDetails | null {
  if (!response || response.step !== "review" || isUnreceivedStep(response)) return null;
  const outcome = reviewOutcome(response.review);
  const findings = (response.findings ?? [])
    .map((finding, index) => ({ finding, index }))
    .sort(
      (a, b) =>
        (FINDING_RANK[a.finding.severity] ?? 3) - (FINDING_RANK[b.finding.severity] ?? 3) ||
        a.index - b.index
    )
    .map(({ finding }) => finding);
  return { ...outcome, findings, blockers: response.blockers?.length ?? 0 };
}

// ---------------------------------------------------------------- case

/** One kernel assertion as the panel shows it; `marginMm` null when unsent. */
export interface CaseClause {
  name: string;
  passed: boolean;
  marginMm: number | null;
  detail: string;
}

export interface CaseKernel {
  passed: boolean;
  clauses: CaseClause[];
  warnings: string[];
  /**
   * The engine's thin-margin band in mm, or null when it did not send one.
   * Null means nothing is marked thin: the threshold is a mechanical number
   * and belongs to `enclosure/rules.py`, so a client that has not been told
   * the band says less rather than making one up.
   */
  thinBandMm: number | null;
}

/** Everything the panel shows for a finished case step. */
export interface CaseDetails {
  /** `kernel`, or null when the engine did not say or named an engine this client does not know. */
  engine: string | null;
  /** The kernel receipt, or null when no kernel check ran. */
  kernel: CaseKernel | null;
  /** The offline fit receipt's signed margins, whole or absent. */
  margins: { x: number; y: number; z: number } | null;
  /** Absolute paths on the engine's machine; null when not written. */
  step: string | null;
  scad: string | null;
  baseStl: string | null;
  lidStl: string | null;
  snapshots: string[];
  /** Any written case file — what "reveal" opens the folder of. */
  reveal: string | null;
  brief: string | null;
  /** The stage's own warnings — a defaulted height, a skipped render. */
  warnings: string[];
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function pathOf(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function stringsOf(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((s): s is string => typeof s === "string" && s.trim() !== "");
}

/**
 * The kernel report, or null when none ran. Defensive the way the fit receipt
 * is: a clause without a name is not a clause, and a clause is passed only
 * when the engine said `true` — anything else reads as failed, because the
 * summary must never claim a check that did not run.
 */
function kernelOf(value: unknown): CaseKernel | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const clauses = (Array.isArray(raw.clauses) ? raw.clauses : [])
    .filter((c): c is Record<string, unknown> => !!c && typeof c === "object" && !Array.isArray(c))
    .filter((c) => typeof c.name === "string" && c.name.trim() !== "")
    .map((c) => ({
      name: c.name as string,
      passed: c.passed === true,
      marginMm: finite(c.margin_mm) ? c.margin_mm : null,
      detail: typeof c.detail === "string" ? c.detail : "",
    }));
  return {
    passed: raw.passed === true,
    clauses,
    warnings: stringsOf(raw.warnings),
    thinBandMm: finite(raw.thin_band_mm) && raw.thin_band_mm > 0 ? raw.thin_band_mm : null,
  };
}

function marginsOf(fit: EnclosureBlock["fit"]): CaseDetails["margins"] {
  const m = fit?.margins_mm;
  if (!m || !finite(m.x) || !finite(m.y) || !finite(m.z)) return null;
  return { x: m.x, y: m.y, z: m.z };
}

/** The case block as the panel needs it, or null when the step carried none. */
export function caseDetails(response: StepResponse | undefined): CaseDetails | null {
  if (!response || response.step !== "case" || isUnreceivedStep(response)) return null;
  return readCase(response.enclosure, response.files);
}

/**
 * The enclosure block read defensively: nulls for anything absent, never a
 * throw, never an invented value. `stepFiles` is the step envelope's file
 * map, whose `case` entry names a written case file when the block's own map
 * does not.
 */
export function readCase(
  enc: EnclosureBlock | null | undefined,
  stepFiles: Record<string, string> = {}
): CaseDetails | null {
  if (!enc || typeof enc !== "object" || Array.isArray(enc)) return null;
  const engine = enc.engine === "kernel" ? "kernel" : null;
  const files =
    enc.files && typeof enc.files === "object" && !Array.isArray(enc.files) ? enc.files : {};
  const step = pathOf(files.step);
  // Legacy: an older engine wrote a .scad and named it here. Nothing writes
  // one any more, but a path that exists is still a path worth revealing.
  const scad = pathOf(files.scad) ?? pathOf(stepFiles.case);
  const baseStl = pathOf(files.base_stl);
  const lidStl = pathOf(files.lid_stl);
  return {
    engine,
    kernel: kernelOf(enc.kernel),
    margins: marginsOf(enc.fit),
    step,
    scad,
    baseStl,
    lidStl,
    snapshots: stringsOf(files.snapshots),
    reveal: step ?? baseStl ?? lidStl ?? scad,
    brief: typeof enc.brief === "string" && enc.brief.trim() ? enc.brief : null,
    warnings: stringsOf(enc.warnings),
  };
}

/** A signed millimetre margin, two decimals, a real minus sign: `−0.20`. */
export function formatMarginMm(value: number): string {
  return `${value < 0 ? "−" : "+"}${Math.abs(value).toFixed(2)}`;
}

function fitMarginsText(margins: CaseDetails["margins"]): string {
  if (!margins) return "";
  return (["x", "y", "z"] as const)
    .map((a) => `${a} ${margins[a] >= 0 ? "+" : ""}${margins[a].toFixed(1)}`)
    .join(", ");
}

/**
 * One sentence about the case that only claims what ran. A kernel receipt
 * names its verdict and every failing clause with its margin. There is only
 * one engine now, so anything without a kernel report says out loud that no
 * acceptance check ran; an engine that sent a fit receipt instead gets that,
 * with a collision called a collision.
 */
function summarizeCase(enc: EnclosureBlock | null): string {
  if (!enc) return "Case generation failed; the board stands without one.";
  const details = readCase(enc);
  if (!details) return "Case generation failed; the board stands without one.";
  const { kernel, engine, margins } = details;
  if (kernel) {
    const total = kernel.clauses.length;
    const failed = kernel.clauses.filter((c) => !c.passed);
    if (failed.length) {
      const named = failed
        .map((c) => (c.marginMm === null ? c.name : `${c.name} (${formatMarginMm(c.marginMm)} mm)`))
        .join(", ");
      return `Case built, ${failed.length} check${failed.length === 1 ? "" : "s"} failed: ${named}.`;
    }
    if (!kernel.passed) return "Case built, kernel verdict failed without naming a clause.";
    if (total === 0) return "Case built, kernel ran no checks.";
    // A pass inside the printer's own tolerance is not a quiet pass.
    const thin = marginAxis(details.kernel!).thin;
    if (thin.length) {
      const named = thin
        .map((t) => (t.marginMm === null ? t.name : `${t.name} (${formatMarginMm(t.marginMm)} mm)`))
        .join(", ");
      return `Case verified: ${total} of ${total} checks pass, ${thin.length} inside the printer's tolerance: ${named}.`;
    }
    return `Case verified: ${total} of ${total} checks pass.`;
  }
  const axes = fitMarginsText(margins);
  const collides = !!margins && (margins.x < 0 || margins.y < 0 || margins.z < 0);
  if (engine === "kernel") return "Case built, kernel check did not run.";
  if (!axes) return "Case generated; no acceptance check ran.";
  return collides
    ? `Case collides with the board: margins ${axes} mm.`
    : `Case fits with margins ${axes} mm.`;
}

/**
 * True when a finished step has something worth a disclosure under its row.
 * A schematic (its counts are the summary, the drawing is in KiCad) and an
 * unreceived step have nothing, and get no chevron rather than an empty box.
 * `host` is which row carries the BOM (`bomHost`).
 */
export function hasDetail(response: StepResponse, host: StepName | null): boolean {
  if (isUnreceivedStep(response)) return false;
  switch (response.step) {
    case "propose":
      return false;
    case "place":
      return (placeDetails(response)?.parts.length ?? 0) > 0 || (response.warnings?.length ?? 0) > 0;
    case "route":
    case "review":
    case "order":
    case "case":
      return true;
    case "sourcing":
      return host === "sourcing";
    default:
      return false;
  }
}

// ---------------------------------------------------------------- headline

export interface HeadlineInput {
  status: "idle" | "running" | "waiting" | "done" | "error";
  running: StepName | null;
  latest: StepResponse | undefined;
  elapsedS: number;
  error: { message: string } | null;
  /**
   * A sentence that selected a stage and is waiting to be committed. It wins
   * over everything else: the only question worth asking is the one that is
   * about to spend money.
   */
  armed?: ArmedCommand | null;
  /**
   * False while the newest artifact went to KiCad and the engineer
   * has not looked away from the strip since. Asking "have you looked?" about
   * a stage that was never shown is the one nag this panel must never make.
   */
  reviewed?: boolean;
  /**
   * What may run now. The engine's `next` is on the latest response, but a
   * resync after a cancel or a 409 learns it from `GET /steps/<id>` instead,
   * and that is the list that decides whether anything is left at all.
   */
  available?: readonly StepName[];
}

/** What the strip says it is doing, in the first person, while a step runs. */
const STEP_GERUND: Record<StepName, string> = {
  propose: "drafting the schematic",
  place: "placing the parts",
  route: "routing copper",
  review: "reviewing the design",
  sourcing: "looking up the parts",
  order: "preparing the fab package",
  case: "designing the case",
};

/**
 * The one sentence at the top of the panel: what I am doing, or what I just
 * did and where it is. First person, because a junior engineer who could not
 * do something says so — "I could not show it in KiCad" — rather than leaving
 * a passive sentence with nobody in it.
 *
 * The KiCad boundary is the reason this is one function and not a chain of
 * ternaries in the panel: `shown_in_kicad` false with a `shown_detail` means
 * the bridge was asked and failed, and false *without* one means it was never
 * asked, and those are different sentences. `headlineFix` carries the second
 * line when the engine gave a reason.
 */
export function headline(input: HeadlineInput): string {
  const { status, running, latest, elapsedS, error, armed = null, reviewed = true } = input;
  if (armed && status !== "running" && status !== "idle") {
    return armed.step === "restart"
      ? "Confirm: start over?"
      : `Confirm: ${STEP_DESCRIPTORS[armed.step].action.toLowerCase()}?`;
  }
  switch (status) {
    case "running":
      return running
        ? `I am ${STEP_GERUND[running]}. ${elapsedS.toFixed(0)} s so far.`
        : `I am working. ${elapsedS.toFixed(0)} s so far.`;
    case "error":
      return error?.message ?? "The step failed.";
    case "done":
      return "Every stage has run. Nothing was ordered.";
    case "waiting": {
      if (!latest) return "Waiting.";
      const left = input.available ?? latest.next;
      if (left.length === 0) return "Every stage has run. Nothing was ordered.";
      const { label, where } = STEP_DESCRIPTORS[latest.step];
      if (isUnreceivedStep(latest)) {
        return `${label} finished on the engine; I did not receive the result.`;
      }
      if (where === "overlay") {
        return latest.step === "review"
          ? `${label} is here. ${summarizeStep(latest)}`
          : `${label} is here.`;
      }
      if (latest.shown_in_kicad) {
        return reviewed
          ? `${label} is in ${WHERE_LABEL[where]}.`
          : `${label} is in ${WHERE_LABEL[where]}. Have you looked?`;
      }
      // Two different facts, and they must not share a sentence: the bridge
      // was asked and failed (it sent a reason), or it was never asked.
      return latest.shown_detail?.trim()
        ? `${label} is on disk. I could not show it in ${WHERE_LABEL[where]}.`
        : `${label} is on disk; I did not show it in ${WHERE_LABEL[where]}.`;
    }
    case "idle":
      return "";
  }
}

/**
 * The second line under the headline: the engine's own reason a stage did not
 * reach KiCad, which carries the fix ("KiCad's API server is off: Preferences
 * > Plugins > Enable API server", "no KiCad bridge: create .venv-kicad …").
 *
 * Null whenever the engine sent none — an invented fix for a bridge nobody
 * asked to start would be a guess, and the headline already says which of the
 * two happened.
 */
export function headlineFix(input: HeadlineInput): string | null {
  const { status, latest } = input;
  if (status !== "waiting" && status !== "done") return null;
  if (input.armed) return null;
  if (!latest || isUnreceivedStep(latest)) return null;
  if (STEP_DESCRIPTORS[latest.step].where === "overlay") return null;
  if (latest.shown_in_kicad) return null;
  return latest.shown_detail?.trim() || null;
}

/**
 * The checklist, from what has come back so far.
 *
 * `history` is every response in arrival order; `running` is the step in
 * flight, if any. A step is `available` when the latest response lists it
 * under `next` — the engine decides what may run, not this file — and
 * `preparing` when the latest response lists it under `background`, which
 * wins over `available`: the button still works, the row says why the
 * engine is already busy on it.
 */
export function stepRows(
  history: readonly StepResponse[],
  running: StepName | null,
  /**
   * The hook's own picture of the background jobs, which is fresher than the
   * latest envelope's list: it also folds in `GET /steps/<id>` polls taken
   * while nothing was pressed. Without it the rows fall back to the envelope,
   * which is what a caller with no hook (a test double, an older page) has.
   */
  background?: readonly BackgroundJob[]
): StepRow[] {
  const latest = history[history.length - 1];
  const byStep = new Map<StepName, StepResponse>();
  for (const response of history) byStep.set(response.step, response);
  const next = new Set<StepName>(latest?.next ?? []);
  const jobs =
    background ??
    (latest?.background ?? []).map(
      (step): BackgroundJob => ({ step, state: "running", warning: null })
    );
  const byJob = new Map<StepName, BackgroundJob>(jobs.map((job) => [job.step, job]));
  return STEP_ORDER.map((id) => {
    const done = byStep.get(id);
    const job = byJob.get(id);
    let status: StepStatus = "pending";
    if (done) status = "done";
    else if (running === id) status = "running";
    else if (job?.state === "running") status = "preparing";
    else if (next.has(id)) status = "available";
    return {
      id,
      label: STEP_DESCRIPTORS[id].label,
      status,
      summary: done ? summarizeStep(done) : null,
      preparing: status === "preparing" ? (PREPARING_LINE[id] ?? null) : null,
      shown: done?.shown_in_kicad ?? false,
      backgroundNote: done ? null : backgroundNote(job),
    };
  });
}

/** The steps the engineer may approve now, in pipeline order. */
export function availableSteps(history: readonly StepResponse[]): StepName[] {
  const latest = history[history.length - 1];
  if (!latest) return [];
  const next = new Set<StepName>(latest.next);
  return STEP_ORDER.filter((s) => next.has(s));
}

/** True once nothing more can run. */
export function stepsExhausted(history: readonly StepResponse[]): boolean {
  return history.length > 0 && availableSteps(history).length === 0;
}

// ---------------------------------------------------------------- margin axis

/**
 * How a clause reads once the band is known.
 *
 * `fail` is the kernel's own verdict. `thin` passed, but by less than the
 * printer's process tolerance, which is the case that pass/fail folding used
 * to hide: a clause clearing by two hundredths of a millimetre will fail on
 * the next revision and nobody will be watching. `clear` is everything else.
 */
export type ClauseTone = "fail" | "thin" | "clear";

export interface MarginTick {
  name: string;
  marginMm: number | null;
  tone: ClauseTone;
  /** Position along the axis, 0 at the left end, 1 at the right; null when unmeasured. */
  x: number | null;
}

export interface MarginAxis {
  /** Every clause, in the kernel's own order. Nothing is dropped. */
  ticks: MarginTick[];
  /** Where zero sits on the axis, or null when no clause carried a margin. */
  zeroX: number | null;
  loMm: number;
  hiMm: number;
  fail: MarginTick[];
  thin: MarginTick[];
  clearCount: number;
  /** The band the tones were judged against, or null when the engine sent none. */
  bandMm: number | null;
}

export function clauseTone(clause: CaseClause, bandMm: number | null): ClauseTone {
  if (!clause.passed) return "fail";
  if (bandMm === null || clause.marginMm === null) return "clear";
  return clause.marginMm < bandMm ? "thin" : "clear";
}

/**
 * Every clause on one signed axis.
 *
 * Thirteen rows of code identifiers is a wall in a floating strip, and a
 * verdict on its own throws away the ordering that makes the receipt legible.
 * One axis carries all thirteen in a single line of vertical space and shows
 * the shape: a case clearing everything by a hair looks nothing like one
 * clearing comfortably, and that difference is the most useful fact here.
 *
 * Zero is always inside the domain, so a tick's side of the origin is the
 * verdict and its distance is the evidence. Pure, so the panel that draws it
 * has nothing to test but layout.
 */
export function marginAxis(kernel: CaseKernel): MarginAxis {
  const band = kernel.thinBandMm;
  const measured = kernel.clauses
    .map((c) => c.marginMm)
    .filter((m): m is number => m !== null);
  // Zero belongs on the axis even when every clause sits to one side of it.
  const lo = Math.min(0, ...measured);
  const hi = Math.max(0, ...measured);
  const span = hi - lo;
  const place = (margin: number | null): number | null => {
    if (margin === null) return null;
    if (span <= 0) return 0.5;
    return Math.min(1, Math.max(0, (margin - lo) / span));
  };
  const ticks: MarginTick[] = kernel.clauses.map((clause) => ({
    name: clause.name,
    marginMm: clause.marginMm,
    tone: clauseTone(clause, band),
    x: place(clause.marginMm),
  }));
  return {
    ticks,
    zeroX: measured.length ? place(0) : null,
    loMm: lo,
    hiMm: hi,
    fail: ticks.filter((t) => t.tone === "fail"),
    thin: ticks.filter((t) => t.tone === "thin"),
    clearCount: ticks.filter((t) => t.tone === "clear").length,
    bandMm: band,
  };
}

// ---------------------------------------------------------------- commands

/**
 * What a sentence meant. There is deliberately no `unknown` here any more.
 *
 * `unknown` was a parse failure, and every caller answered it the same way:
 * by reading the menu back. That is the app telling an engineer who just
 * described a board that it does not speak their language, which is the one
 * thing this bar must never say. The vocabulary did not get bigger — the
 * answer did. A sentence that names no stage is a `request`: a thing to
 * build or to change, which the caller parks or arms, and which still costs
 * nothing until a human confirms it.
 *
 * `later` is the other half of the old `unknown`: it names a real stage that
 * is not this stage's turn. That is a question with an answer ("placement
 * comes first"), not a failure to parse.
 *
 * `none` is nothing said at all — an empty transcript, a bare "go" with no
 * stage waiting. Silence is the one input that is genuinely not a request,
 * and inventing a board out of it is how a mis-fired microphone spends money.
 */
export type StepCommand =
  | { kind: "approve"; step: StepName }
  | { kind: "restart" }
  | { kind: "later"; step: StepName }
  | { kind: "request"; text: string }
  | { kind: "none" };

/**
 * A command a sentence selected, waiting for a human to commit it. `source`
 * is the sentence, echoed back so what the machine understood is visible
 * before it does anything. A restart is armed exactly like an approval: it
 * spends nothing, but it drops a run that money was already spent on, and
 * "try again" is one transcript away from a request to look again.
 */
export type ArmedCommand =
  | { step: StepName; source: string }
  | { step: "restart"; source: string };

/** What arming `command` looks like, or null when there is nothing to arm. */
export function armCommand(command: StepCommand, source: string): ArmedCommand | null {
  if (command.kind === "approve") return { step: command.step, source: source.trim() };
  if (command.kind === "restart") return { step: "restart", source: source.trim() };
  // `request`, `later` and `none` are not armed here: what a request arms
  // depends on what the run is doing, and only the page knows that. See
  // `interpret` in pages/kaleo/index.tsx.
  return null;
}

/**
 * Words that name a step. Matched as whole words, lower-cased, so "order"
 * hits and "border" does not; "go" and friends mean "whatever is next".
 */
const STEP_WORDS: Record<StepName, readonly string[]> = {
  propose: ["propose", "schematic", "circuit"],
  place: ["place", "placement", "placed", "layout"],
  route: ["route", "routing", "copper", "traces", "tracks"],
  review: ["review", "critique", "check", "findings"],
  sourcing: ["source", "sourcing", "bom", "parts", "mpn", "datasheets", "datasheet"],
  order: ["order", "fab", "fabricate", "manufacture", "pcbway", "jlc", "jlcpcb"],
  case: ["case", "enclosure", "housing", "box", "cad", "stp"],
};

const GO_WORDS = new Set([
  "go", "next", "continue", "proceed", "approve", "approved", "ok", "okay",
  "yes", "yep", "yeah", "sg", "lgtm", "ship", "do", "it", "sure",
]);

/**
 * Phrases that mean "throw this run away", matched as whole words in
 * sequence: "run it again" restarts, "check it against the datasheet" does
 * not — a substring match on "again" once turned a review request into a
 * restart. A bare "again" is not on the list on purpose: "route it again"
 * names a stage, and the stage wins. Nor is "try again": after a failed
 * stage it means a retry, which is an approval, not a fresh board.
 */
export const RESTART_PHRASES: readonly string[] = Object.freeze([
  "restart", "start over", "start again", "start fresh", "from scratch",
  "new board", "new run", "run it again", "do it again", "over again", "scrap",
]);

function hasPhrase(words: readonly string[], phrase: readonly string[]): boolean {
  outer: for (let i = 0; i + phrase.length <= words.length; i++) {
    for (let j = 0; j < phrase.length; j++) {
      if (words[i + j] !== phrase[j]) continue outer;
    }
    return true;
  }
  return false;
}

/** One accepted command and every phrase that selects it. */
export interface CommandPhrase {
  command: StepCommand;
  /** Whole-word phrases, lower-cased. The first is the canonical spelling. */
  phrases: readonly string[];
}

/**
 * The whole vocabulary this app accepts, as data.
 *
 * There is one list, and both readers use it: `interpretCommand` matches a
 * typed sentence against it, and the command menu is it, rendered. That is
 * the point — a menu built from a second table would sooner or later offer a
 * command the interpreter rejects, and the engineer would be told "not sure
 * what to do with that" by the same app that had just suggested it.
 *
 * `approve` entries come first, in pipeline order, because `interpretCommand`
 * resolves them in that order; the restart entry is checked before all of
 * them (a run that is being thrown away is never an approval of a stage).
 */
export const COMMAND_VOCABULARY: readonly CommandPhrase[] = Object.freeze([
  ...STEP_ORDER.map((step) =>
    Object.freeze({
      command: Object.freeze({ kind: "approve", step } as StepCommand),
      phrases: STEP_WORDS[step],
    })
  ),
  Object.freeze({
    command: Object.freeze({ kind: "restart" } as StepCommand),
    phrases: RESTART_PHRASES,
  }),
]);

/** Every phrase that selects `command`; empty for one the vocabulary has no words for. */
export function phrasesFor(command: StepCommand): readonly string[] {
  for (const entry of COMMAND_VOCABULARY) {
    if (entry.command.kind !== command.kind) continue;
    if (entry.command.kind === "approve" && command.kind === "approve") {
      if (entry.command.step !== command.step) continue;
    }
    return entry.phrases;
  }
  return [];
}

/** True when `words` contain any of `phrases` as a whole-word sequence. */
function said(words: readonly string[], phrases: readonly string[]): boolean {
  return phrases.some((phrase) => hasPhrase(words, phrase.split(" ")));
}

/**
 * What a sentence typed into the prompt bar means while a step run is open.
 *
 * The engineer talks the way they talk: "sg lets order", "route it", "ok
 * next". If the words name an available step, that is an approval of it;
 * bare agreement approves the first available step; asking to start over
 * restarts; naming a step whose turn has not come is `later`.
 *
 * Everything else is a `request` — the sentence, kept whole. This function
 * does not decide what happens to it and it certainly does not spend
 * anything: "a 3.3 V regulator board" typed at a waiting stage is a
 * legitimate thing for a person to say, and the caller's job is to say what
 * it will do with it, not to answer that it was not on a list.
 */
export function interpretCommand(text: string, available: readonly StepName[]): StepCommand {
  const lower = text.toLowerCase();
  const words = lower.split(/[^a-z0-9]+/).filter(Boolean);
  // Silence is the one input that is not a request. It reaches here from an
  // empty transcript, and turning it into a board would let a mis-fired
  // microphone spend money on nothing anybody said.
  if (words.length === 0) return { kind: "none" };
  for (const entry of COMMAND_VOCABULARY) {
    if (entry.command.kind === "restart" && said(words, entry.phrases)) return { kind: "restart" };
  }
  for (const entry of COMMAND_VOCABULARY) {
    if (entry.command.kind !== "approve") continue;
    if (!available.includes(entry.command.step)) continue;
    if (said(words, entry.phrases)) return { kind: "approve", step: entry.command.step };
  }
  // Names a step that is not available yet: not an approval of something else.
  for (const entry of COMMAND_VOCABULARY) {
    if (entry.command.kind !== "approve") continue;
    if (said(words, entry.phrases)) return { kind: "later", step: entry.command.step };
  }
  if (words.every((w) => GO_WORDS.has(w) || w === "lets" || w === "let" || w === "s")) {
    // Bare agreement. With something waiting it approves that; with nothing
    // waiting it agrees to nothing, and "ok" is not a board specification.
    return available.length ? { kind: "approve", step: available[0] } : { kind: "none" };
  }
  return { kind: "request", text: text.trim() };
}

// ---------------------------------------------------------------- rail
//
// The run as a list of rows with a status whose *shape* means something.
// Everything below is pure and derived from what the engine actually sent:
// a row never states a number the engine did not measure, and a stage whose
// engine is not on the wire says so rather than being labelled with a guess.

/**
 * The eight states a rail row can be in. `not shown` and `replayed` are
 * deliberately *not* here: they are modifiers on a row, and neither changes
 * the glyph — a placement that never reached KiCad is still done.
 */
export type RailStatus =
  | "queued"
  | "ready"
  | "running"
  | "background"
  | "review"
  | "done"
  | "failed"
  | "unreceived";

/** The run states the rail is derived from; mirrors `StepRun.status`. */
export type RailRunStatus = "idle" | "running" | "waiting" | "done" | "error";

export interface RailInput {
  history: readonly StepResponse[];
  running: StepName | null;
  /** The engine's own `next` — including what a resync learned. */
  available: readonly StepName[];
  status: RailRunStatus;
  /**
   * The step that was in flight when the run failed. `useStepRun` clears
   * `running` as it fails, so without this no row can be marked failed and
   * the rail shows the error nowhere.
   */
  failedStep?: StepName | null;
  /** The background jobs as the hook tracks them; see `stepRows`. */
  background?: readonly BackgroundJob[];
}

/**
 * What is doing the work on a stage: a model, a solver, a kernel, a gate.
 *
 * `named` is the honesty bit. A solver is named because the pipeline has
 * exactly one of each and the row can say which without guessing; a model is
 * named only when a `model.call` frame off the wire said which model answered.
 * A step run whose model reported no name of its own still gets the unnamed
 * `model` chip, rather than a plausible "Gemini 3.7 Flash" nothing measured.
 */
export interface StageEngine {
  label: string;
  kind:
    | "model"
    | "cp-sat"
    | "shelf-pack"
    | "astar"
    | "kernel"
    | "kicad-cli"
    | "gate"
    | "probe";
  /** True when `label` came off the wire or names the one engine that stage has. */
  named: boolean;
}

/** The model name a `model.call` frame carried, or null: never inferred. */
function modelFromEvents(events: StepResponse["events"] | undefined): string | null {
  for (const frame of events ?? []) {
    if (frame?.event !== "model.call") continue;
    const name = frame.model;
    if (typeof name === "string" && name.trim()) return name.trim();
  }
  return null;
}

/**
 * Which engine ran a finished stage.
 *
 * A stage whose work is a model call is chipped with the model the wire named
 * — a `model.call` frame, used verbatim — and with an unnamed `model` chip
 * when nothing named one.
 *
 * A stage whose work is *deterministic* is chipped with that engine even when
 * a model call rode along: CP-SAT or the shelf-pack fallback (the response's
 * own `status` says which), the A* maze router, the build123d kernel (when
 * the enclosure block names it), `kicad-cli` when it produced a 3D export,
 * the order gate. The case is the one that matters: a model proposes
 * the spec, but the kernel is what drew and measured the case, and chipping
 * that row with a model name would credit the wrong worker for thirteen
 * measured clauses.
 */
export function stageModel(response: StepResponse | undefined): StageEngine | null {
  if (!response || isUnreceivedStep(response)) return null;
  const named = modelFromEvents(response.events);
  switch (response.step) {
    case "propose":
    case "review":
      return named
        ? { label: named, kind: "model", named: true }
        : { label: "model", kind: "model", named: false };
    case "sourcing":
      return named
        ? { label: `${named} + probe`, kind: "probe", named: true }
        : { label: "model + probe", kind: "probe", named: false };
    case "place":
      return response.status === "fallback"
        ? { label: "shelf pack", kind: "shelf-pack", named: true }
        : { label: "CP-SAT", kind: "cp-sat", named: true };
    case "route":
      return { label: "A* router", kind: "astar", named: true };
    case "case": {
      const engine = response.enclosure?.engine;
      if (engine === "kernel") return { label: "build123d", kind: "kernel", named: true };
      return null;
    }
    case "order":
      return response.files.model_glb || response.files.model_step
        ? { label: "kicad-cli", kind: "kicad-cli", named: true }
        : { label: "order gate", kind: "gate", named: true };
    default:
      return null;
  }
}

/**
 * How many model calls a finished stage reported, or null when the wire said
 * nothing. Null is not zero: a deterministic stage reports no `model.call`
 * frame at all, and so does an engine too old to send them, and a rail that
 * printed `0 calls` over a stage that certainly called a model would be
 * inventing a measurement in the honest direction.
 */
export function stageCalls(response: StepResponse | undefined): number | null {
  if (!response || isUnreceivedStep(response)) return null;
  let calls = 0;
  for (const frame of response.events ?? []) {
    if (frame?.event === "model.call") calls += 1;
  }
  return calls > 0 ? calls : null;
}

/**
 * What the engine is doing while a stage runs, in its own terms. Not a
 * promise about how long it takes: the router's budget is node expansions and
 * the placer may not use its seconds, which is why nothing here is a fraction.
 */
export const METHOD_LINE: Record<StepName, string> = {
  propose: "asks the model for a circuit, validates it, repairs in batches",
  place: "CP-SAT over courtyards, wirelength and area, deterministic",
  route: "A* over a 0.25 mm grid, one net at a time",
  review: "an adversarial critic; findings filtered against the spec",
  sourcing: "proposes MPNs, then fetches each datasheet's first bytes",
  order: "gates the package; exports a 3D model when kicad-cli exists",
  case: "proposes a case spec, builds it, measures every kernel clause",
};

/** What a background row says, so a stage nobody pressed is not a mystery. */
const BACKGROUND_LINE: Partial<Record<StepName, string>> = {
  sourcing: "looking up parts — started on its own",
  case: "designing the case — started on its own",
};

/**
 * The measured receipt for a finished row: counts, millimetres, verdicts, and
 * nothing else. `summarizeStep` writes a sentence for the headline; this is
 * the terse right-of-the-label clause the rail carries on every finished row.
 * Null when the step reported nothing measurable.
 */
export function receiptLine(response: StepResponse | undefined): string | null {
  if (!response || isUnreceivedStep(response)) return null;
  switch (response.step) {
    case "propose": {
      const parts = typeof response.parts === "number" ? response.parts : 0;
      const repairs = response.repair_rounds ?? 0;
      const tail = repairs ? `, ${repairs} repair round${repairs === 1 ? "" : "s"}` : "";
      return `${parts} parts, ${response.nets ?? 0} nets${tail}`;
    }
    case "place": {
      const count = Array.isArray(response.parts) ? response.parts.length : 0;
      const [w, h] = response.board_mm ?? [undefined, undefined];
      const status = response.status ? `, ${response.status}` : "";
      return `${count} parts on ${mm(w)} × ${mm(h)} mm${status}`;
    }
    case "route": {
      const r = response.routing ?? {};
      const routed = r.routed?.length ?? 0;
      const open = Object.keys(r.unrouted ?? {}).length;
      const tail = open ? `, ${open} left as ratsnest` : "";
      const tracks = r.tracks ?? 0;
      const vias = r.vias ?? 0;
      return `${routed} of ${routed + open} nets, ${tracks} track${tracks === 1 ? "" : "s"}, ${vias} via${vias === 1 ? "" : "s"}${tail}`;
    }
    case "review": {
      const outcome = reviewOutcome(response.review);
      if (outcome.status === "failed") return "review failed, nothing known";
      if (outcome.status === "skipped") return "review skipped, nothing known";
      const n = response.findings?.length ?? 0;
      if (n === 0) return "no findings";
      const blockers = response.blockers?.length ?? 0;
      return `${n} finding${n === 1 ? "" : "s"}, ${blockers} blocking`;
    }
    case "sourcing": {
      const details = sourcingDetails(response);
      if (!details) return null;
      return `${details.named} of ${details.parts.length} MPNs, ${details.confirmed} confirmed, ${details.verified} datasheet PDF${details.verified === 1 ? "" : "s"}`;
    }
    case "order": {
      const order = response.order ?? {};
      const blockers =
        order.manifest?.blocker_count ??
        (typeof order.blocker_count === "number" ? order.blocker_count : 0);
      if (!order.orderable) {
        return `not orderable, ${blockers} blocker${blockers === 1 ? "" : "s"}`;
      }
      const issues = order.issues?.length ?? 0;
      return issues ? `prepared, ${issues} issue${issues === 1 ? "" : "s"}` : "prepared, no issues";
    }
    case "case": {
      const details = caseDetails(response);
      const kernel = details?.kernel;
      if (!kernel) return details ? "no acceptance check ran" : null;
      const total = kernel.clauses.length;
      const failed = kernel.clauses.filter((c) => !c.passed).length;
      if (failed) return `${total} checks, ${failed} failed`;
      return `${total} of ${total} checks pass`;
    }
    default:
      return null;
  }
}

export interface RailRow extends StepRow {
  rail: RailStatus;
  /** Where the stage's result is looked at. */
  where: StepDescriptor["where"];
  /** `KiCad` / `here`, or null when nothing was shown there. */
  whereLabel: string | null;
  engine: StageEngine | null;
  /** The measured clause, once the stage has one. */
  receipt: string | null;
  /**
   * The engine's own reason a finished stage never reached KiCad,
   * verbatim, or null. A stage the bridge was never asked to show has no
   * reason and therefore no sentence: the row is silent rather than nagging.
   */
  notShown: string | null;
  /** The stage's own seconds; null when nothing came back to measure. */
  durationS: number | null;
  /** Model calls the stage reported; null when it reported none. */
  calls: number | null;
  /** How the engine works on this stage, while it is working on it. */
  method: string | null;
  hasDetail: boolean;
}

function failedRow(input: RailInput): StepName | null {
  if (input.status !== "error") return null;
  return input.failedStep ?? input.running ?? null;
}

/**
 * The rail status of one row, in this precedence: a failure names its own
 * step; a step the engine finished but this client never received says so; the
 * latest finished step with something still to approve is *in review* (its
 * artifact is waiting for eyes, and approving the next stage is the sign-off);
 * everything else follows the checklist status.
 */
export function railStatus(
  row: StepRow,
  response: StepResponse | undefined,
  latest: StepResponse | undefined,
  input: RailInput
): RailStatus {
  if (row.id === failedRow(input)) return "failed";
  if (row.status === "done") {
    if (response && isUnreceivedStep(response)) return "unreceived";
    if (latest?.step === row.id && input.available.length > 0 && input.status !== "done") {
      return "review";
    }
    return "done";
  }
  if (row.status === "running") return "running";
  if (row.status === "preparing") return "background";
  if (row.status === "available") return "ready";
  return "queued";
}

/** The rail: one decorated row per step, in pipeline order. */
export function railRows(input: RailInput): RailRow[] {
  const { history, running } = input;
  const latest = history[history.length - 1];
  const byStep = new Map<StepName, StepResponse>();
  for (const response of history) byStep.set(response.step, response);
  const host = bomHost(history);
  return stepRows(history, running, input.background).map((row) => {
    const response = byStep.get(row.id);
    const rail = railStatus(row, response, latest, input);
    const { where } = STEP_DESCRIPTORS[row.id];
    const detail = response?.shown_detail;
    const notShown =
      response &&
      !isUnreceivedStep(response) &&
      where !== "overlay" &&
      !response.shown_in_kicad &&
      typeof detail === "string" &&
      detail.trim()
        ? detail
        : null;
    return {
      ...row,
      rail,
      where,
      whereLabel: row.shown ? WHERE_LABEL[where] : notShown ? "on disk" : null,
      engine: stageModel(response),
      receipt: receiptLine(response),
      notShown,
      durationS:
        response && !isUnreceivedStep(response) ? response.duration_s : null,
      calls: stageCalls(response),
      method:
        rail === "running"
          ? METHOD_LINE[row.id]
          : rail === "background"
            ? (BACKGROUND_LINE[row.id] ?? METHOD_LINE[row.id])
            : null,
      hasDetail: response ? hasDetail(response, host) : false,
    };
  });
}

/**
 * Seconds as `m:ss`. The clock is the only number on the rail that moves, so
 * it is the only one allowed to be approximate — and it is never negative and
 * never `NaN:NaN`.
 */
export function formatClock(seconds: number): string {
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

// ---------------------------------------------------------------- provenance

/**
 * Where a finding came from, and whether anything measured it.
 *
 * This is the engine's own distinction reaching the surface: the audit CLI
 * separates `Origin.PROVEN` (a rule measured the board and carries the
 * measurement in `evidence`) from `Origin.SUGGESTED` (a model proposed it).
 * The review step on this wire is `agents/review.py`, an adversarial critic,
 * so a finding that arrives without an `origin` is a model's proposal and the
 * chip says so out loud rather than leaving the reader to assume a check ran.
 */
export type FindingOrigin = "measured" | "suggested";

export interface FindingProvenance {
  origin: FindingOrigin;
  /** `MEASURED` / `SUGGESTED` — the word beside the policy flag. */
  label: string;
  /** Who produced it and what did not happen: `critic, not measured`. */
  source: string;
  /** True only for the engine's `proven` origin. */
  measured: boolean;
  /** The rule's own measurement, when a rule sent one. */
  evidence: string | null;
  /** The rule that produced it, when the wire named one. */
  rule: string | null;
}

export function findingProvenance(finding: Finding): FindingProvenance {
  const rule = finding.rule?.trim() || null;
  const evidence = finding.evidence?.trim() || null;
  if (finding.origin === "proven") {
    return {
      origin: "measured",
      label: "MEASURED",
      source: rule ? `rule ${rule}, measured on the board` : "measured on the board",
      measured: true,
      evidence,
      rule,
    };
  }
  const who = finding.origin && finding.origin !== "suggested" ? String(finding.origin) : "critic";
  return {
    origin: "suggested",
    label: "SUGGESTED",
    source: `${who}, not measured`,
    measured: false,
    evidence,
    rule,
  };
}

/** What the panel writes where a finding cites nothing. Never a blank cell. */
export const NO_CITATION = "No citation — nothing was quoted for this.";

export interface FindingCitation {
  /** The quoted text, exactly as the engine sent it. */
  text: string;
  /** The page it names, when the citation names one; never inferred. */
  page: string | null;
}

/**
 * The citation a finding carries, or null when it carries none.
 *
 * The page is *parsed out of the engine's free text* (`citation` is one
 * string: "AMS1117 datasheet p. 9"), so it is shown only when the text
 * actually says a page. A citation with no page is still a citation; a
 * finding with no citation is `NO_CITATION`, in words, because a blank cell
 * reads as "checked and fine".
 */
export function findingCitation(finding: Finding): FindingCitation | null {
  const text = finding.citation?.trim();
  if (!text) return null;
  const page = /\b(?:pages?|pp?)\.?\s*(\d+(?:\s*[–—-]\s*\d+)?)/i.exec(text);
  return { text, page: page ? page[1].replace(/\s+/g, "") : null };
}

/**
 * A severity as a policy flag: `BLOCKER` / `MARGINAL` / `NOTE`. Built on
 * `statusBadge` so the vocabulary lives in exactly one place and a severity
 * this build has never seen still renders as itself.
 */
export function policyFlag(severity: string | null | undefined): BadgeSpec {
  const spec = statusBadge("finding", severity);
  return { label: spec.label.toUpperCase(), tone: spec.tone };
}

// ---------------------------------------------------------------- receipt

/**
 * The last frame of a finished run: three headings and no fourth.
 *
 * **Verified** is only what something measured — nets the router closed,
 * kernel clauses with their margins, datasheet URLs a probe fetched.
 * **Not verified** is everything a reader might mistake for verified: a
 * model's findings, an MPN nobody confirmed, and the two checks this product
 * does not run at all (ERC and DRC). **Not done** ends, always, with the one
 * sentence that keeps the product honest about what it is.
 */
export interface RunReceipt {
  verified: string[];
  notVerified: string[];
  notDone: string[];
}

export const NOTHING_SUBMITTED =
  "Nothing is submitted. Sending the package to a fab is yours to do.";

export const KICAD_CHECKS = "ERC and DRC are yours to run in KiCad";

function latestOf(
  history: readonly StepResponse[],
  step: StepName
): StepResponse | undefined {
  for (let i = history.length - 1; i >= 0; i--) {
    const response = history[i];
    if (response.step === step && !isUnreceivedStep(response)) return response;
  }
  return undefined;
}

export function runReceipt(history: readonly StepResponse[]): RunReceipt {
  const verified: string[] = [];
  const notVerified: string[] = [];
  const notDone: string[] = [];

  const route = latestOf(history, "route");
  if (route) {
    const r = route.routing ?? {};
    const routed = r.routed?.length ?? 0;
    const open = Object.entries(r.unrouted ?? {});
    verified.push(`${routed} of ${routed + open.length} nets routed`);
    if (open.length) {
      notVerified.push(
        `${open.length} net${open.length === 1 ? "" : "s"} left as ratsnest: ${open
          .map(([net]) => net)
          .join(", ")}`
      );
    }
  }

  const kase = latestOf(history, "case");
  const details = caseDetails(kase);
  const kernel = details?.kernel ?? null;
  if (kernel && kernel.clauses.length) {
    const total = kernel.clauses.length;
    const failed = kernel.clauses.filter((c) => !c.passed);
    if (failed.length) {
      notVerified.push(
        `${failed.length} of ${total} kernel clauses fail: ${failed
          .map((c) => (c.marginMm === null ? c.name : `${c.name} ${formatMarginMm(c.marginMm)} mm`))
          .join(", ")}`
      );
      verified.push(`${total - failed.length} of ${total} kernel clauses pass`);
    } else {
      const measured = kernel.clauses.filter((c) => c.marginMm !== null);
      const positive = measured.length === total && measured.every((c) => (c.marginMm ?? 0) >= 0);
      verified.push(
        `${total} of ${total} kernel clauses pass${positive ? ", all margins positive" : ""}`
      );
    }
  } else if (details) {
    notVerified.push("the case was drawn, not measured: no kernel check ran");
  }

  const sourcing = sourcingDetails(latestOf(history, "sourcing") ?? latestOf(history, "order"));
  if (sourcing) {
    const total = sourcing.parts.length;
    const line = `${sourcing.verified} of ${total} datasheet${total === 1 ? "" : "s"} are PDFs`;
    (sourcing.verified > 0 ? verified : notVerified).push(line);
    if (sourcing.confirmed > 0) {
      verified.push(`${sourcing.confirmed} of ${sourcing.named} MPNs confirmed by a distributor`);
    }
    const unconfirmed = sourcing.named - sourcing.confirmed;
    if (unconfirmed > 0) {
      notVerified.push(
        `${unconfirmed} MPN${unconfirmed === 1 ? "" : "s"} proposed, ${sourcing.confirmed} confirmed by a distributor`
      );
    }
  }

  const review = latestOf(history, "review");
  const reviewFailed = review ? (reviewFailure(review.review) ?? reviewSkipped(review.review)) : null;
  if (review && reviewFailed) {
    // A critic that answered nothing measured nothing and suggested nothing:
    // the finding list it left behind is empty and means nothing.
    notVerified.push(reviewFailed);
  } else if (review) {
    const findings = review.findings ?? [];
    if (findings.length === 0) {
      notVerified.push("the critic raised no findings, which is not a measurement");
    } else {
      const proven = findings.filter((f) => findingProvenance(f).measured).length;
      const suggested = findings.length - proven;
      if (proven) verified.push(`${proven} of ${findings.length} findings measured by a rule`);
      if (suggested) {
        notVerified.push(
          `${suggested} finding${suggested === 1 ? "" : "s"} from the critic, suggested, not measured`
        );
      }
    }
  }

  if (history.some((r) => r.files.board || r.files.placed_board)) {
    notVerified.push(KICAD_CHECKS);
  }

  const never = STEP_ORDER.filter((step) => !history.some((r) => r.step === step));
  if (never.length) {
    notDone.push(`Never run: ${never.map((step) => STEP_DESCRIPTORS[step].label).join(", ")}.`);
  }
  const order = orderDetails(latestOf(history, "order"));
  if (order && !order.orderable) {
    const blockers = order.issues.filter((i) => String(i.severity) === "blocker").length;
    notDone.push(
      `No fab package: ${blockers} blocker${blockers === 1 ? "" : "s"} to fix first.`
    );
  }
  notDone.push(NOTHING_SUBMITTED);

  return { verified, notVerified, notDone };
}

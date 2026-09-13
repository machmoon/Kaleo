// The shapes `service/app.py` actually answers with.
//
// These mirror the documented response contract, which is *additive-only*: the
// service may add fields, never change or remove one. So every field beyond
// the handful the run genuinely cannot complete without is optional here, and
// the UI is written to render what arrived rather than to assume a shape.

/** A rectangle as the service sends it: `[x, y, w, h]`, min-corner first, mm. */
export type RectMm = [number, number, number, number];

export interface Pad {
  number: string;
  net: string | null;
  rect_mm: RectMm;
}

export interface PlacedPart {
  ref: string;
  footprint: string;
  value: string | null;
  layer: string;
  rotated: boolean;
  x_mm: number;
  y_mm: number;
  courtyard_mm: RectMm;
  pads: Pad[];
}

export interface Placements {
  /** `[width, height]` in millimetres. */
  board_mm: [number, number];
  /**
   * Always `"solver-y-up"`. The service resolves rotation before it sends
   * this, so the single Y flip in `board.ts` is the only coordinate work left.
   */
  frame: string;
  parts: PlacedPart[];
}

/**
 * `/generate` findings come from the adversarial reviewer, whose vocabulary is
 * `blocker`/`marginal`/`note` (engine `agents/review.py`). The audit CLI's
 * separate rules engine speaks `error`/`warning`/`info` with a proven/suggested
 * origin; that surface never flows through `/generate` today, but the union is
 * kept open so a future response carrying it renders instead of crashing.
 */
export type Severity =
  | "blocker"
  | "marginal"
  | "note"
  | "error"
  | "warning"
  | "info"
  | (string & {});
export type Origin = "proven" | "suggested" | (string & {});

/**
 * One review finding, as `_finding_dict` in `service/app.py` sends it.
 *
 * `parts` keeps the spec's own part names — the vocabulary `title` and
 * `detail` are written in — while `refs` carries the same parts as they are
 * labelled on the board, which is the identity the board well shares.
 * The audit-CLI fields (`origin`, `evidence`, `rule`) stay optional for the
 * reason above; nothing from `/generate` carries them today.
 */
export interface Finding {
  id?: string;
  severity: Severity;
  title?: string;
  detail?: string;
  parts?: string[];
  refs?: string[];
  citation?: string | null;
  suggested_fix?: string | null;
  origin?: Origin;
  evidence?: string | null;
  rule?: string | null;
}

/**
 * One end of a net: a PIN, not a part.
 *
 * The engine's circuit IR requires pin-level terminals (`C1.1`, never `C1`)
 * because "one leg of this capacitor to this pin" has to be expressible, and
 * `_schematic_dict` in `service/app.py` preserves that: `pin` is the logical
 * name the spec was written in (`"AVDD"`, or `"1"` for a passive) and `number`
 * is the physical pin it resolves to. A UI that collapses these to `part_id`
 * throws away the only thing the IR exists for.
 *
 * `ref` is the board reference designator, and is null until refs are
 * assigned; `part_id` is the spec name and is always present.
 */
export interface SchematicPin {
  part_id: string;
  ref?: string | null;
  pin: string;
  number?: string | null;
}

export interface SchematicNet {
  name: string;
  endpoints: SchematicPin[];
}

/**
 * The `schematic` block: electrical truth with no geometry in it, versioned so
 * a later renderer can tell which shape it received.
 */
export interface Schematic {
  version?: number;
  parts?: {
    /** Spec name — the vocabulary endpoints and findings are written in. */
    id: string;
    ref?: string | null;
    /** `"device"`, or a passive type: `"resistor"`, `"capacitor"`, … */
    kind?: string;
    value?: string | null;
    symbol?: string | null;
    pins?: { name: string; number?: string | null }[];
  }[];
  nets?: SchematicNet[];
}

/**
 * What became of the critic, on the review step envelope, on `GET /steps/<id>`
 * and on the one-shot `/generate` response, beside `findings`/`blockers`.
 *
 * `status: "failed"` means the critic was asked and answered nothing readable
 * — and then an empty `findings` list is NOT evidence of a clean board, which
 * is the one reading every renderer of that list has to refuse. `skipped` is
 * a review that was never asked for. `detail` is the engine's own sentence
 * about a failure (null when there is nothing to say); `note` is its
 * one-line gloss on the outcome. Absent from older engines, which never
 * distinguished a critic that said nothing from one that never answered.
 */
export type ReviewStatus = "ok" | "failed" | "skipped" | (string & {});

export interface ReviewBlock {
  status: ReviewStatus;
  ran: boolean;
  detail: string | null;
  note: string;
}

/** The two jobs `service/steps.py` starts on its own the moment `place` lands. */
export type BackgroundJobName = "case" | "sourcing";

/**
 * How a job the engine started on its own ended. Present under
 * `background_outcome` only once the job has finished — a running job is
 * listed under `background` and nowhere else — so the two fields never both
 * name the same job. `detail` is the engine's own sentence about a failure,
 * verbatim, and null when there is nothing to add.
 */
export interface BackgroundOutcome {
  ok: boolean;
  detail: string | null;
}

export type BackgroundOutcomes = Partial<Record<BackgroundJobName, BackgroundOutcome>>;

export interface RunResult {
  /** The emitted board file, as text. Present on every successful run. */
  kicad_pcb?: string;
  /** The intent string the run was asked for, echoed back. */
  intent?: string;
  /** `[width, height]` in millimetres — same numbers as `placements.board_mm`. */
  board_mm?: [number, number];
  /** CP-SAT solver status, lowercase on the wire: optimal / feasible / fallback. */
  status?: string;
  /** How many repair rounds the propose loop needed. */
  repair_rounds?: number;
  /** Flattened blocker strings. The compatibility surface; prefer `findings`. */
  blockers?: string[];
  findings?: Finding[];
  /** What became of the critic; see `ReviewBlock`. Absent from older engines. */
  review?: ReviewBlock;
  warnings?: string[];
  parts?: { ref: string; footprint: string }[];
  nets?: string[];
  datasheets?: unknown[];
  placements?: Placements;
  schematic?: Schematic;
  order?: Record<string, unknown>;
  /**
   * The router's own report, sent on every run: counts, the nets it finished,
   * and — the honesty contract — every net it could not, by name, with the
   * reason. `completion` is the routed fraction (0..1).
   */
  routing?: {
    tracks?: number;
    vias?: number;
    routed?: string[];
    unrouted?: Record<string, string>;
    warnings?: string[];
    completion?: number;
  };
  grounding?: Record<string, unknown>;
  duration_s?: number;
  wirelength_mm?: number | null;
  served_by?: string | null;
  cache?: { hit?: string[]; read?: string[]; unusable?: string[] };
}

/** The error body both POST routes share. */
export interface RunError {
  error: string;
  detail?: string;
  error_id?: string;
  /** Only the `run.error` stream frame carries this; error bodies do not. */
  status?: number;
}

/**
 * One NDJSON frame off `/generate/stream`.
 *
 * The envelope is `{event, t_s, ...}`; everything else depends on the event
 * name, and unknown events are expected — the engine may emit stages this
 * build has never heard of, and dropping them silently is the contract.
 */
export interface StreamFrame {
  event: string;
  t_s?: number;
  stage?: string;
  status?: number;
  result?: RunResult;
  [key: string]: unknown;
}

/**
 * How a finished run is summarised: as the structured spec-review agenda the
 * engine can put on a calendar, or as the prose paragraph it writes today.
 * Prose is the default because it is what every existing run produced.
 */
export type SummaryMode = "structured" | "prose";

export interface GenerateRequest {
  intent: string;
  datasheets?: Record<string, string>;
  time_limit_s?: number;
  review?: boolean;
  ground?: boolean;
  debug?: boolean;
  /**
   * Additive: an engine that has never heard of it answers exactly as before,
   * which is why the default is `prose` rather than "unset means structured".
   */
  summary?: SummaryMode;
}

// ---------------------------------------------------------------- steps

/** The engine's approval-gated steps, in the order the run can take them. */
export type StepName =
  | "propose"
  | "place"
  | "route"
  | "review"
  | "sourcing"
  | "order"
  | "case";

/** `POST /steps` body: a generate request plus the KiCad live-bridge opt-in. */
export interface StepRequest extends GenerateRequest {
  /** Ask the engine to show each stage in the open KiCad. */
  kicad_live?: boolean;
  /** Free-text style for the `case` step, sent with that step. */
  enclosure_style?: string;
  /**
   * Design the case afresh with the rigorous kernel path rather than
   * collecting the one the engine started in the background. Sent with the
   * `case` step; it was always on the wire and only ever passed through
   * untyped, which meant nothing checked its spelling.
   */
  enclosure_rigorous?: boolean;
}

/**
 * Which engine drew the case. There is exactly one: `kernel`, the
 * build123d/OCCT path with its geometric assertions. The union stays open so
 * an older or future engine's name arrives as a string rather than crashing
 * the panel — but anything that is not `kernel` is an engine this client
 * cannot vouch for, and the panel says so rather than naming it.
 */
export type EnclosureEngine = "kernel" | (string & {});

/** The frozen clause names the kernel verifier can report. */
export type KernelClauseName =
  | "valid_topology"
  | "positive_volume"
  | "solid_count"
  | "bbox"
  | "board_clash"
  | "headroom"
  | "underside"
  | "standoff_concentric"
  | "cutout_admits_plug"
  | "lid_mates"
  | "min_wall"
  | "overhang"
  | "vent_keepout"
  | (string & {});

/** One kernel assertion. A negative `margin_mm` is violated by that much. */
export interface KernelClause {
  name: KernelClauseName;
  passed: boolean;
  margin_mm: number;
  detail: string;
}

/** The kernel's receipt: the verdict, every clause, and what it could not check. */
export interface KernelReport {
  passed: boolean;
  clauses: KernelClause[];
  warnings: string[];
  /**
   * The margin below which a clause has cleared by less than the printer's own
   * process tolerance, in mm, from `enclosure/rules.py`. Additive: an older
   * engine omits it, and a client that has no band marks nothing thin rather
   * than inventing a threshold of its own.
   */
  thin_band_mm?: number | null;
}

/** Absolute paths on the engine's machine; null for anything not written. */
export interface EnclosureFiles {
  scad?: string | null;
  step?: string | null;
  base_stl?: string | null;
  lid_stl?: string | null;
  snapshots?: string[];
}

export interface EnclosureBlock {
  scad?: string;
  params?: Record<string, number>;
  fit?: { margins_mm?: Record<string, number> };
  warnings?: string[];
  repair_rounds?: number;
  engine?: EnclosureEngine;
  /** Null when the kernel did not run — never an empty passing report. */
  kernel?: KernelReport | null;
  files?: EnclosureFiles | null;
  /** The design brief the case was built from; steps route only. */
  brief?: string;
}

/**
 * One part as the sourcing step describes it, `SourcingEntry.as_dict` in
 * `engine/silkscreen/sourcing.py`.
 *
 * An MPN starts as a *proposal* from the model. `mpn_status` is `proposed`
 * until a distributor lookup (`agents/distributor.py`, behind an API key)
 * confirms the part number exists, which makes it `verified` and fills
 * `distributor_url` / `distributor_sku`; a miss stays `proposed` with a
 * `note`, and `none` means the model named nothing. The datasheet URL is the
 * other thing the engine checked — it fetched the first bytes — and
 * `datasheet_status` says what it saw: `verified` (a real PDF), `not_pdf`
 * (reachable, but an HTML viewer page or similar), `unreachable`, or `none`
 * when no URL was proposed. `model3d` is the KiCad-library model attached to
 * the footprint, with `model3d_note` saying why there is none.
 */
export interface SourcingEntry {
  ref: string;
  value: string;
  /** `resistor` | `capacitor` | `inductor` | `diode` | `crystal` |
      `switch` | `testpoint` | `device`. Kept in step with
      `engine/silkscreen/sourcing.KINDS`; this side stores whatever the
      server sent rather than coercing it, so a new kind renders as
      itself. */
  kind: string;
  /** The footprint name, as placed. */
  package: string;
  manufacturer?: string | null;
  mpn?: string | null;
  mpn_status: "verified" | "proposed" | "none" | (string & {});
  datasheet_url?: string | null;
  datasheet_status: "verified" | "not_pdf" | "unreachable" | "none" | (string & {});
  model3d?: string | null;
  model3d_note?: string | null;
  note?: string | null;
  /** The distributor's product page; only sent once `mpn_status` is `verified`. */
  distributor_url?: string | null;
  /** The distributor's own stock number for the confirmed part. */
  distributor_sku?: string | null;
  /** Who confirmed the part number — `"Mouser"`. Set only with `verified`. */
  distributor?: string | null;
  /**
   * Why a proposed MPN is not `verified`, in the engine's words: the
   * distributor said no, or no key was configured to ask. Never set on a
   * verified row, and shown rather than hidden — "proposed" with no reason
   * reads as "nobody looked", which is only sometimes what happened.
   */
  verify_error?: string | null;
}

/**
 * The `sourcing` block, on the sourcing step and again on the order step
 * (the order manifest carries the same BOM). The counts are the engine's
 * own; `warnings` names anything it gave up on, such as a model that never
 * produced valid JSON.
 */
export interface SourcingBlock {
  parts?: SourcingEntry[];
  /** Datasheets whose URL really served a PDF. */
  verified?: number;
  /** Parts the model named an MPN for. */
  proposed?: number;
  /** Parts whose MPN a distributor confirmed. */
  confirmed?: number;
  /** Parts with no MPN at all. */
  unresolved?: number;
  warnings?: string[];
}

/**
 * One line of the agenda the engine proposes for a spec review
 * (`engine/silkscreen/specreview.py`). `why` is the reason the item needs
 * humans in a room, `minutes` is the engine's own estimate, and `blocking`
 * separates what stops the board from what merely wants discussing.
 */
export interface AgendaItem {
  topic: string;
  why: string;
  minutes: number;
  blocking: boolean;
  /** Part refs and net names the item is about, as the board labels them. */
  refs?: string[];
}

/**
 * The `spec_review` block: the meeting the engine would like to book, in
 * enough detail that the engineer approves an actual agenda rather than a
 * mystery button. `summary` is capped by the engine at 400 characters — the
 * anti-wall-of-text rule this feature exists for.
 *
 * A review with no blocking item is a legitimate result and means "nothing
 * needs a meeting"; it is not an empty agenda to be filled in here.
 */
export interface SpecReviewBlock {
  title: string;
  summary: string;
  items: AgendaItem[];
  decisions_needed?: string[];
  /** Which stages the agenda was prepared from: `review`, `route`, … */
  prepared_from?: string[];
  /** The engine's own total. Absent from an older engine; never invented. */
  total_minutes?: number;
}

/**
 * One reason the order should be stopped, questioned or noted, as
 * `OrderIssue.as_dict` in `engine/silkscreen/order.py` sends it. The
 * vocabulary is the order gate's own — `blocker` / `warning` / `note` — and a
 * single blocker makes the board un-orderable, which is why the panel lists
 * them rather than the count alone.
 */
export interface OrderIssue {
  code?: string;
  severity: Severity;
  title?: string;
  detail?: string;
  parts?: string[];
}

/**
 * The `order` block: the manifest a human reads, every issue the gate found,
 * the verdict, and the fab files inline. Nothing in it has been submitted
 * anywhere; `manifest.requires_human_approval` says so on the wire.
 */
export interface OrderBlock {
  orderable?: boolean;
  issues?: OrderIssue[];
  manifest?: {
    blocker_count?: number;
    orderable?: boolean;
    requires_human_approval?: boolean;
    [key: string]: unknown;
  };
  files?: { filename: string; content: string }[];
  [key: string]: unknown;
}

/**
 * The keys `StepResponse.files` may carry, each an absolute path on the
 * engine's machine. `bom` (`<stem>-bom.csv`) arrives with the sourcing step,
 * or with the order step when sourcing was never pressed; `order` and
 * `order_manifest` arrive with the order step;
 * `model_glb` / `model_step` only when a `kicad-cli` export produced a
 * non-empty file — otherwise `warnings` names why.
 */
export type StepFileKey =
  | "schematic"
  | "project"
  | "placed_board"
  | "board"
  | "bom"
  | "case"
  | "order"
  | "order_manifest"
  | "model_glb"
  | "model_step";

/**
 * What every step answers: the envelope (session, where the run stands, the
 * files written, what may run next, whether KiCad was told) plus the stage's
 * own fields, which mirror the one-shot `RunResult` where they overlap.
 */
export interface StepResponse {
  session: string;
  step: StepName;
  /** `proposed` | `placed` | `routed` — the run's furthest point. */
  stage: string;
  intent: string;
  /** Absolute paths of the files written so far; see `StepFileKey`. */
  files: Partial<Record<StepFileKey, string>> & Record<string, string>;
  next: StepName[];
  /**
   * Steps the engine is working on without having been pressed — `case` and
   * `sourcing`, from the moment placement lands until each settles. Absent
   * from older engines; the row shows as preparing while listed.
   */
  background?: StepName[];
  /**
   * How each job that has *finished* in the background ended, keyed by job;
   * a running job stays in `background` and is absent here. Absent from
   * older engines, which report an outcome only on the step that collects it.
   */
  background_outcome?: BackgroundOutcomes;
  shown_in_kicad: boolean;
  /**
   * Why the stage did not reach KiCad, in the bridge's own words, when
   * `shown_in_kicad` is false and the session asked for it. Absent from
   * older engines; null when it was shown or nothing was asked.
   */
  shown_detail?: string | null;
  events: StreamFrame[];
  duration_s: number;
  // propose
  parts?: number | { ref: string; footprint: string }[];
  nets?: number;
  repair_rounds?: number;
  schematic?: Schematic;
  // place
  status?: string;
  board_mm?: [number, number];
  placements?: Placements;
  wirelength_mm?: number | null;
  warnings?: string[];
  kicad_pcb?: string;
  // route
  routing?: RunResult["routing"];
  // review
  findings?: Finding[];
  blockers?: string[];
  /**
   * What became of the critic. `status: "failed"` with an empty `findings`
   * list is a review that answered nothing, not a clean board. Absent from
   * older engines.
   */
  review?: ReviewBlock;
  /**
   * The agenda the engine prepared for a spec review, when it prepared one.
   * Null is a real answer — it means the engine has nothing to propose — and
   * absent means this engine does not produce agendas at all.
   */
  spec_review?: SpecReviewBlock | null;
  // sourcing (also on the order step)
  sourcing?: SourcingBlock;
  // order
  order?: OrderBlock;
  // case
  enclosure?: EnclosureBlock | null;
  /**
   * Everything typed at the strip during this run, in the order it was typed.
   *
   * This rides every step envelope and `GET /steps/<id>`, which is why the
   * client keeps **no copy** of it: a note the `case` step consumed changes
   * `status` on the engine, and a client-side mirror would go on saying
   * "pending" about text that has already been used.
   */
  amendments?: Amendment[];
  /** The session is closed to further steps. Also from the envelope, not kept here. */
  cancelled?: boolean;
}

/**
 * One thing typed while the run was going, as the service records it.
 *
 * `status` is the only field that decides whether this text did anything:
 * `"applied"` means a step consumed it, and anything else means it is text
 * we are holding. Nothing in the UI may render a pending note as though the
 * run were changing because of it.
 */
export interface Amendment {
  id: number;
  text: string;
  at: number;
  /** What the run had reached when it arrived — "make it 5 V" before place
   *  and after route are different requests. */
  stage: string;
  /** The step that will consume it, or null for one kept for a restart. */
  step: string | null;
  status: "pending" | "applied" | string;
}

/** What `POST /steps/<id>/amend` answers. Rendered, not paraphrased. */
export interface AmendResponse {
  session: string;
  amendment: Amendment;
  /** How many notes on this session are still unconsumed. */
  pending: number;
  /**
   * The one door inside this run. `at_step` is non-null only for a step that
   * genuinely reads free text — today `case` and nothing else — and the UI
   * may offer that affordance only when it is non-null.
   */
  applies: { at_step: string | null; when: string | null };
  /** The other door: a new run from the original intent plus every note. */
  restart: { available: boolean; intent: string; cost: string; how: string };
  consuming_steps: string[];
  /** The service's sentence about the stages a note cannot reach. */
  not_consumed: string;
  /** The one honest sentence. Rendered verbatim; never reworded. */
  headline: string;
  cancelled?: boolean;
}

/** What `POST /steps/<id>/cancel` answers. Also rendered verbatim. */
export interface CancelResponse {
  session: string;
  cancelled: boolean;
  already_cancelled: boolean;
  stage: string;
  /** Steps that will now never run — the actual saving. */
  refused_steps: string[];
  /** A step was running under the session lock when the cancel landed. */
  in_flight: boolean;
  aborts_at: string;
  /**
   * Background jobs still going. They are **finishing**, not stopped, and
   * their results are discarded — the UI must say that and not show them
   * as halted.
   */
  still_running: string[];
  /** The service's sentence about what a cancel cannot interrupt. */
  not_stoppable: string;
  restart: { available: boolean; intent: string; cost: string; how: string };
  headline: string;
}

/**
 * `GET /steps/<id>`: where the run stands on the engine, independent of what
 * this client has heard back. `done` is every step the engine has finished
 * (a cancelled request does not un-run its step) and `next` what may run now.
 */
export interface StepStatusResponse {
  session: string;
  stage: string;
  intent: string;
  files: Record<string, string>;
  done: StepName[];
  next: StepName[];
  /**
   * The steps the engine is still working on without having been pressed
   * (`service/steps.py::Session.background`). A step drops off this list the
   * moment its job settles — finished *or failed* — and an engine that sends
   * `background_outcome` says which there; an older one says nothing until
   * the step that collects the job answers with its `warnings`. Absent from
   * older engines.
   */
  background?: StepName[];
  /** How each finished background job ended; see `StepResponse.background_outcome`. */
  background_outcome?: BackgroundOutcomes;
  /**
   * What became of the critic, once the review step has run; see
   * `ReviewBlock`. Absent from older engines and before review runs.
   */
  review?: ReviewBlock;
  kicad_live: boolean;
  /** The latest bridge failure, including one that settled after its step answered. */
  shown_detail?: string | null;
  /** See `StepResponse.amendments` — the engine's list, never a client copy. */
  amendments?: Amendment[];
  cancelled?: boolean;
}

// ---------------------------------------------------------------- delivery

/**
 * `GET /deliver/config`: what the engine could send a finished run to, and
 * never a secret. `hints` name the exact fix for each gap (webhook unset,
 * OAuth client unset, Sign in with Google / `python -m googleapps auth`), so
 * the panel can show them verbatim.
 */
export interface DeliverConfig {
  /** False when the engine runs without the googleapps package beside it. */
  available: boolean;
  chat: boolean;
  gmail: boolean;
  calendar: boolean;
  /** OAuth client id+secret are set on the engine (needed before sign-in). */
  oauth_client?: boolean;
  /** A usable token is on disk for Gmail/Calendar. */
  signed_in?: boolean;
  /** `missing` | `valid` | `expired (will refresh on next use)`. */
  token: string;
  token_path?: string;
  hints: string[];
}

/** `POST /steps/<id>/deliver` body. Every field is optional; at least one must be set. */
export interface DeliverRequest {
  chat?: boolean;
  email?: string[];
  schedule?: boolean;
  attendees?: string[];
  /** Book the structured spec review: the agenda becomes the event body. */
  spec_review?: boolean;
  /**
   * When to hold it, RFC 3339. Null asks the engine for its own default slot
   * — which is a decision, so it is sent explicitly rather than omitted.
   */
  when?: string | null;
}

/**
 * The delivery report: one block per destination that was asked for, each
 * succeeding or failing on its own. Calendar carries `skipped_reason` when
 * nothing was booked on purpose — the review found no blockers, or has not
 * run — which is not an error and must not read as one.
 */
export interface DeliverResponse {
  session: string;
  chat?: { ok: boolean; error?: string };
  email?: { ok: boolean; to?: string[]; message_id?: string; error?: string };
  calendar?: {
    ok: boolean;
    blockers?: number;
    html_link?: string;
    meet_uri?: string;
    skipped_reason?: string;
    error?: string;
  };
  /**
   * The spec review, in the same per-destination shape as `calendar`. Its
   * `skipped_reason` carries the two deliberate non-bookings — the review has
   * not run, or it found no blockers — and those are results, not errors.
   */
  spec_review?: {
    ok: boolean;
    html_link?: string;
    meet_uri?: string;
    skipped_reason?: string;
    error?: string;
  };
}

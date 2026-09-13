// The pipeline's stages, as data a checklist can render.
//
// The names here are the engine's own: `agents/stages.py` emits
// `{"event": "stage.start"|"stage.done", "stage": ...}` with exactly these
// strings, and both drivers (the straight-line SDK one and the ADK workflow)
// call the same stage bodies, so the vocabulary does not depend on which one
// the service is running.

/**
 * A stage the UI tracks. Every id but `validate` is a real engine stage name;
 * `validate` is synthetic — see {@link StageDescriptor.event}.
 */
export type StageId =
  | "read"
  | "propose"
  | "validate"
  | "place"
  | "schematic"
  | "route"
  | "review";

export interface StageDescriptor {
  id: StageId;
  /**
   * The `stage` field on the engine's `stage.start` / `stage.done` frames.
   * `null` for `validate`, which the engine never announces as a stage: the
   * repair loop lives inside `propose` and only surfaces as `propose.round`
   * frames, so this row is driven by those instead.
   */
  event: string | null;
  label: string;
  /** One line of what the stage does, for a subtitle or tooltip. */
  detail: string;
  /**
   * Why this stage may never emit anything, or `null` if it always runs.
   * A stage with a reason here must not read as "stuck" when it stays silent.
   */
  skipReason: string | null;
}

/**
 * The pipeline in the order the engine runs it.
 *
 * `schematic` is here for completeness and will not tick on a run driven by
 * this app: `schematic_stage` returns immediately unless `generate_pcb` was
 * given an `output` path, and `service/app.py` passes none — it answers with
 * the board as text and never writes files. {@link planStages} is what the UI
 * should use so that row starts out honestly marked as not-running.
 */
export const PIPELINE_STAGES: readonly StageDescriptor[] = Object.freeze([
  {
    id: "read",
    event: "read",
    label: "Read datasheets",
    detail: "Pull pins and requirements out of the PDFs you supplied.",
    skipReason: "no datasheets were supplied",
  },
  {
    id: "propose",
    event: "propose",
    label: "Propose a circuit",
    detail: "Ask the model for parts, nets and pin-level connections.",
    skipReason: null,
  },
  {
    id: "validate",
    event: null,
    label: "Validate and repair",
    detail: "Check the proposal against the netlist rules and send back every failure at once.",
    skipReason: null,
  },
  {
    id: "place",
    event: "place",
    label: "Place parts",
    detail: "Solve the layout with CP-SAT, minimising wirelength and board area.",
    skipReason: null,
  },
  {
    id: "schematic",
    event: "schematic",
    label: "Draw the schematic",
    detail: "Emit the .kicad_sch sheet and the project that ties it to the board.",
    skipReason: "the engine only draws a sheet when it is writing files to disk",
  },
  {
    id: "route",
    event: "route",
    label: "Route copper",
    detail: "Maze-route the nets on two layers; anything it cannot finish is named, not hidden.",
    skipReason: "routing was turned off for this run",
  },
  {
    id: "review",
    event: "review",
    label: "Review the design",
    detail: "Argue against the finished board and report what a rule cannot measure.",
    skipReason: "review was turned off for this run",
  },
] as const);

/** Lookup by id, without exposing Object.prototype to a hostile key. */
const BY_ID = new Map<string, StageDescriptor>(
  PIPELINE_STAGES.map((stage) => [stage.id, stage])
);

/** Lookup by the engine's `stage` field. `validate` has none, so it is absent. */
const BY_EVENT = new Map<string, StageDescriptor>(
  PIPELINE_STAGES.filter((s) => s.event !== null).map((s) => [s.event as string, s])
);

export function stageById(id: string): StageDescriptor | null {
  return BY_ID.get(id) ?? null;
}

/** The stage a `stage.start` / `stage.done` frame refers to, or null if we have never heard of it. */
export function stageForEvent(name: unknown): StageDescriptor | null {
  return typeof name === "string" ? BY_EVENT.get(name) ?? null : null;
}

/** What the caller asked the engine for, as far as it changes which stages run. */
export interface RunPlan {
  /** True when the request carried at least one datasheet URL. */
  datasheets?: boolean;
  /** The request's `review` flag. The service defaults it to true. */
  review?: boolean;
  /** Whether copper gets laid. The engine defaults to true and the service does not override it. */
  route?: boolean;
  /**
   * Whether the engine was given an output path. Always false over HTTP —
   * only the CLI passes one — which is why the schematic row never ticks here.
   */
  output?: boolean;
}

/**
 * The stages a run with this plan can actually emit, in order.
 *
 * This is a statement about the engine's control flow, not a prediction about
 * progress: a stage left out here provably cannot emit an event, so showing it
 * as pending would be a spinner that never resolves. Stages that *are* listed
 * still start out untouched and tick only from frames actually received.
 */
export function planStages(plan: RunPlan = {}): StageDescriptor[] {
  const { datasheets = false, review = true, route = true, output = false } = plan;
  return PIPELINE_STAGES.filter((stage) => {
    switch (stage.id) {
      case "read":
        return datasheets;
      case "schematic":
        return output;
      case "route":
        return route;
      case "review":
        return review;
      default:
        return true;
    }
  });
}

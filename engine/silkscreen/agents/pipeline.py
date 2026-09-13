"""Prompt to PCB, with the model checked at every step.

    intent -> datasheets -> propose/validate -> CP-SAT place
           -> placement repair -> schematic -> route -> review -> .kicad_pcb
                               +-> enclosure  (worker thread) --+
                               +-> sourcing   (worker thread) --+
                               +-> simulation (worker thread) --+

Three gates stand between model output and the final board. The circuit IR
refuses malformed proposals and hands every error back for repair. The
placement verifier admits only geometry that satisfies the selected profile.
Finally, a semantic reviewer re-reads the datasheets and argues against the
design.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..board import BoardResult, write_board
from ..netlist import CircuitSpec
from ..placement.adapter import GeneratedPlacement
from ..routing import RouteResult
from ..sourcing import SourcingResult, bom_csv, grouped_bom_csv
from ..spice.simulators import Simulator
from .datasheet import PartFacts
from .effort import (
    UNSET,
    EffortProfile,
    EffortReceipt,
    StageModels,
    build_receipt,
    cheap_sibling,
    model_name,
    profile_for,
)
from .model import Document, Model
from .plan import PlanResult
from .propose import ProposalAttempt
from .review import Finding, ReviewReport, Severity
from .simulate import SimulationResult
from .stages import (
    NO_ARTIFACTS,
    EnclosureResult,
    SchematicArtifacts,
    SimulationJob,
    SourcingJob,
    place_stage,
    placement_repair_stage,
    plan_stage,
    propose_stage,
    read_stage,
    route_stage,
    schematic_stage,
    start_enclosure_stage,
    start_review_stage,
    start_simulation_stage,
    start_sourcing_stage,
)

__all__ = ["PipelineResult", "EnclosureResult", "EventingModel", "generate_pcb"]

#: Event strings are capped so a stream stays a progress signal, never a
#: payload. No event may carry board text or datasheet text, and none carries
#: model output unless the caller asked for it.
MAX_EVENT_TEXT = 160
#: The single opt-in exception: a ``model.response`` event carries the model's
#: own answer verbatim, clipped here, for a client debugging what it was told.
MAX_RESPONSE_TEXT = 16_000
#: Debug requests are usually a few kilobytes, but repaired proposals can carry
#: a complete validation batch and datasheet facts. Keep enough to diagnose the
#: call without allowing one pathological prompt to turn the progress stream
#: into an unbounded transport.
MAX_REQUEST_TEXT = 64_000


@dataclass
class PipelineResult:
    intent: str
    spec: CircuitSpec
    board: BoardResult
    facts: list[PartFacts] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: What the critic pass actually did. ``findings`` alone cannot say --
    #: an empty list is produced by a clean board, by a review that was
    #: switched off, and (before this existed) by a critic whose answer could
    #: not be read. Consumers that render a verdict must consult this first:
    #: only ``review.ok`` makes an empty ``findings`` mean "found nothing".
    review: ReviewReport = field(default_factory=ReviewReport)
    attempts: list[ProposalAttempt] = field(default_factory=list)
    board_path: Path | None = None
    #: The routed copper, or None when routing was turned off.
    route: RouteResult | None = None
    #: The other files a run leaves behind, so every stage is inspectable in
    #: KiCad rather than only the last one.
    schematic_path: Path | None = None
    project_path: Path | None = None
    placed_board_path: Path | None = None
    #: Verifier-grounded placement receipt when integrated repair is enabled.
    placement: GeneratedPlacement | None = None
    #: The generated case, or None -- when it was not requested, and also when
    #: it failed: enclosure failure never fails the run (plan decision 5).
    enclosure: EnclosureResult | None = None
    #: The sourced bill of materials, or None when it was not requested. A
    #: sourcing failure never fails the run either: the stage answers with
    #: the deterministic rows and a warning, so this is None only when off.
    sourcing: SourcingResult | None = None
    #: Where ``bom.csv`` was written, or None when nothing was (sourcing
    #: off, no ``output``, or ``emit_stages`` off -- the ``enclosure.step``
    #: rule).
    bom_path: Path | None = None
    #: Where ``bom-grouped.csv`` (the assembler's one-line-per-part sheet)
    #: was written, beside ``bom.csv`` under the same rule, or None.
    grouped_bom_path: Path | None = None
    #: The SPICE verdict, or None when it was not requested. Never None
    #: when it was: every outcome that is not a run -- no simulator, a part
    #: with no model, no usable testbench, a simulator that raised -- is a
    #: status on the result, and a failed clause is a finding on it
    #: (``result.simulation.findings``, kept apart from the critic's
    #: ``findings`` because the two have different provenance).
    simulation: SimulationResult | None = None
    #: The brief the planning stage produced, or None when it did not run.
    #: A thin intent -- "a home security camera system" -- used to become a
    #: netlist with nothing having decided where power enters; this is that
    #: decision, and it is kept on the result so the engineer can see what
    #: the board was actually designed against.
    plan: PlanResult | None = None
    #: Datasheets that were asked for and could not be read, one line each.
    #:
    #: A part whose read fails is dropped from ``facts`` and the run carries
    #: on -- which is right, one unreadable PDF should not lose the board.
    #: But it was reported *only* as a ``read.failed`` event, and ``emit`` is
    #: a no-op whenever ``on_event`` is None, which is every non-streaming
    #: caller including the CLI. So the board was designed and reviewed from
    #: the model's general knowledge, ``review.ok`` was true, and nothing
    #: anywhere said a datasheet had been missed. ``generate_pcb``'s own
    #: docstring says why that matters: omitting a part's facts "does not
    #: merely lose a citation, it designs and reviews the board as though the
    #: part were undocumented". Every other degradation in this layer has a
    #: result-level surface; this is that surface.
    unread_datasheets: list[str] = field(default_factory=list)
    #: Which effort level produced this board, and what that level cost it.
    #:
    #: The default level is ``fast``: a quarter of the solver budget and one
    #: repair round, which is four times faster on a board whose solve fills
    #: the budget. Whether that board is worse is not knowable from one run,
    #: so this reports what the level *gave* the run rather than a verdict on
    #: what came out --
    #: ``result.effort.headline()`` is one sentence naming the level and its
    #: cost, and ``result.effort.degraded`` is the boolean a renderer can
    #: branch on. None only when a caller built a ``PipelineResult`` by hand;
    #: every path through :func:`generate_pcb` fills it in.
    effort: EffortReceipt | None = None

    @property
    def artifacts(self) -> list[Path]:
        """Every file this run wrote, in the order the stages produced them."""
        ordered: list[Path | None] = [
            self.project_path,
            self.schematic_path,
            self.placed_board_path,
        ]
        # The enclosure is designed on a worker thread alongside the
        # schematic and the copper, so its files may land on disk at any
        # point before the headline board (which _finish writes last); they
        # are listed here, before that board, whatever the wall clock. On
        # The STEP is primary and the STLs are derived from it (plan
        # decision 19); the snapshots are advisory.
        if self.enclosure is not None:
            exports = getattr(self.enclosure, "exports", None)
            if exports is not None:
                ordered.extend([exports.step, exports.base_stl, exports.lid_stl])
            ordered.extend(getattr(self.enclosure, "snapshots", ()))
        # The BOM is sourced on a worker thread the same way, and written by
        # _finish just before the board it describes.
        ordered.append(self.bom_path)
        ordered.append(self.grouped_bom_path)
        ordered.append(self.board_path)
        return [p for p in ordered if p is not None]

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    @property
    def repair_rounds(self) -> int:
        """How many times the model had to be corrected before it validated."""
        return max(0, len(self.attempts) - 1)

    def summary(self) -> str:
        w, h = self.board.size_mm
        lines = [
            f"{self.spec.part_count()} parts, {self.spec.net_count()} nets",
            f"board {w:.2f} x {h:.2f} mm  [{self.board.solver_status}]",
        ]
        if self.repair_rounds:
            lines.append(f"{self.repair_rounds} repair round(s) before it validated")
        if self.route is not None:
            lines.append(self.route.summary())
        if not self.review.ok:
            # Never "no findings": nothing was found because nothing was read.
            lines.append(self.review.note())
        else:
            blockers = len(self.blockers)
            lines.append(
                f"{len(self.findings)} finding(s), {blockers} blocker(s)"
                if self.findings
                else "no findings"
            )
        if self.simulation is not None:
            lines.append(self.simulation.note())
        return " · ".join(lines)


class _EventingModel:
    """A :class:`Model` that reports every round-trip it makes.

    Delegates to the wrapped model unchanged. ``stage`` is set by the pipeline
    before each stage, so a call can be attributed to the stage that made it.

    ``read_stage`` (:mod:`silkscreen.agents.stages`) calls the wrapped model
    concurrently, from several threads at once, when a request names more
    than one part to read -- so ``_call_seq`` (a plain read-modify-write) and
    ``self._emit`` (the driver's own callback: a list append, an SSE write)
    both need a lock, or two concurrent reads can hand out the same call id
    or interleave a caller that isn't safe under concurrent invocation. The
    lock guards only bookkeeping and emission, never the wrapped model's own
    ``generate`` call -- serialising that would silently undo the concurrency
    ``read_stage`` was written to get.

    ``stage`` is kept **per thread**: the enclosure and sourcing stages run
    on worker threads beside the schematic and route stages, and a single
    shared slot would let ``enter("route")`` on the main thread relabel the
    case's model calls -- or the thread's ``enter("enclosure")`` relabel the
    main thread's. The driver's own thread -- the first to enter a stage --
    also keeps the default, because ``read_stage``'s concurrent readers never
    enter anything: they run on ``asyncio.to_thread`` workers and must report
    the stage the driver is in, not an empty one. The call counter is shared
    and locked, so ids stay unique either way.
    """

    def __init__(
        self,
        model: Model,
        emit: Callable[[dict[str, Any]], None],
        include_responses: bool = False,
        *,
        call_prefix: str = "worker",
    ):
        self._model = model
        self._emit = emit
        self._call_prefix = call_prefix
        self._default_stage = ""
        self._stage = threading.local()
        self._driver_thread: int | None = None
        self.include_responses = include_responses
        self._call_seq = 0
        self._lock = threading.Lock()

    @property
    def stage(self) -> str:
        """The stage the *calling* thread entered, or the wrapper's default."""
        return getattr(self._stage, "value", self._default_stage)

    @stage.setter
    def stage(self, value: str) -> None:
        self._stage.value = value
        ident = threading.get_ident()
        if self._driver_thread is None:
            self._driver_thread = ident
        if ident == self._driver_thread:
            self._default_stage = value

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        with self._lock:
            self._call_seq += 1
            call_id = f"{self._call_prefix}-{self._call_seq}"
        if self.include_responses:
            document_refs = []
            for document in documents or []:
                document_refs.append(
                    {
                        "url": document.url,
                        "mime_type": document.mime_type,
                        "bytes": len(document.data) if document.data is not None else 0,
                    }
                )
            with self._lock:
                self._emit(
                    {
                        "event": "model.request",
                        "layer": "worker",
                        "call_id": call_id,
                        "stage": self.stage,
                        "system": (system or "")[:MAX_REQUEST_TEXT],
                        "prompt": prompt[:MAX_REQUEST_TEXT],
                        "documents": document_refs,
                        "temperature": temperature,
                        "max_output_tokens": max_output_tokens,
                        "truncated": len(system or "") > MAX_REQUEST_TEXT
                        or len(prompt) > MAX_REQUEST_TEXT,
                    }
                )
        # A failover model keeps an append-only attempt log. Anything appended
        # during this call is a provider that failed on the way to an answer;
        # a model without such a log simply produces no retry events. Under
        # concurrent reads (see the class docstring) two calls can share one
        # log, so "seen" is only a best-effort watermark -- a retry logged by
        # a sibling call in the same instant could in principle be attributed
        # to the wrong call id. No driver in this repo runs a FallbackModel
        # concurrently against more than one part today, so this is recorded
        # rather than solved.
        log = getattr(self._model, "log", None)
        seen = len(log) if isinstance(log, list) else 0
        started = time.monotonic()
        try:
            # Deliberately outside the lock: this is the actual network call,
            # and serialising it would undo the concurrency read_stage exists
            # to get.
            text = self._model.generate(
                prompt,
                documents=documents,
                system=system,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
        except Exception:
            self._emit_retries(log, seen, call_id)
            self._emit_call(started, call_id=call_id, ok=False, chars=0)
            raise
        self._emit_retries(log, seen, call_id)
        self._emit_call(started, call_id=call_id, ok=True, chars=len(text))
        if self.include_responses:
            with self._lock:
                self._emit(
                    {
                        "event": "model.response",
                        "layer": "worker",
                        "call_id": call_id,
                        "stage": self.stage,
                        "provider": getattr(self._model, "last_provider", None),
                        "model": getattr(self._model, "last_model", None)
                        or getattr(self._model, "model", None),
                        "chars": len(text),
                        "truncated": len(text) > MAX_RESPONSE_TEXT,
                        "text": text[:MAX_RESPONSE_TEXT],
                    }
                )
        return text

    def _emit_call(
        self, started: float, *, call_id: str, ok: bool, chars: int
    ) -> None:
        with self._lock:
            self._emit(
                {
                    "event": "model.call",
                    "layer": "worker",
                    "call_id": call_id,
                    "stage": self.stage,
                    "provider": getattr(self._model, "last_provider", None),
                    "model": getattr(self._model, "last_model", None)
                    or getattr(self._model, "model", None),
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "ok": ok,
                    "chars": chars,
                }
            )

    def _emit_retries(self, log: object, seen: int, call_id: str) -> None:
        if not isinstance(log, list):
            return
        with self._lock:
            for attempt in log[seen:]:
                if getattr(attempt, "ok", True):
                    continue
                self._emit(
                    {
                        "event": "model.retry",
                        "layer": "worker",
                        "call_id": call_id,
                        "stage": self.stage,
                        "provider": getattr(attempt, "provider", None),
                        "error": str(getattr(attempt, "error", "") or "")[
                            :MAX_EVENT_TEXT
                        ],
                        "elapsed_s": round(
                            float(getattr(attempt, "elapsed_s", 0.0)), 3
                        ),
                    }
                )


#: The public name for the tap above. ``generate_pcb`` is not the only driver
#: that runs the stage bodies: ``service/steps.py`` runs them one approved step
#: at a time, and a step run that did not wrap its model reported no model name
#: and no call count at all. One wrapper, one event contract, both drivers.
EventingModel = _EventingModel


def _wire_events(
    model: Model,
    on_event: Callable[[dict[str, Any]], None] | None,
    include_responses: bool,
) -> tuple[
    Callable[[dict[str, Any]], None],
    Model,
    Callable[[str], None],
    Callable[[Model | None, str, str], Model | None],
    Callable[[Model | None], Model | None],
]:
    """Build the event plumbing shared by both pipeline drivers.

    With no callback the model is passed through unwrapped and ``emit`` does
    nothing, so an unwatched run pays nothing for the seam.

    ``emit`` is called from two threads once the enclosure stage is in
    flight, so the callback is serialised: a service writing NDJSON frames
    must never see two events interleave inside one line. The callback's
    exception still propagates to whichever thread emitted -- on the worker
    thread it is stored and re-raised at the join.
    """
    t0 = time.monotonic()
    gate = threading.Lock()

    def emit(evt: dict[str, Any]) -> None:
        if on_event is None:
            return
        with gate:
            evt["t_s"] = round(time.monotonic() - t0, 3)
            on_event(evt)

    tap: _EventingModel | None = None
    agent_model: Model = model
    if on_event is not None:
        tap = _EventingModel(model, emit, include_responses)
        agent_model = tap
    observed: dict[int, Model] = {id(model): agent_model}
    # Taps over a *tier* of the same worker model rather than over a separate
    # policy model. They follow ``enter`` like the primary tap does, because
    # one cheap tap serves several stages and a fixed ``_default_stage``
    # would file the BOM's call under the case's name.
    tier_taps: list[_EventingModel] = []

    def enter(stage: str) -> None:
        if tap is not None:
            tap.stage = stage
        for extra in tier_taps:
            extra.stage = stage

    def observe(
        secondary: Model | None,
        stage: str,
        call_prefix: str,
    ) -> Model | None:
        """Give a secondary policy model the same logging contract.

        Placement may use a model other than the circuit worker. Reusing a
        wrapper for the same object keeps call IDs monotonic; a distinct model
        receives a distinct prefix so its IDs cannot collide with worker IDs.
        """
        if secondary is None or on_event is None:
            return secondary
        existing = observed.get(id(secondary))
        if existing is not None:
            return existing
        secondary_tap = _EventingModel(
            secondary,
            emit,
            include_responses,
            call_prefix=call_prefix,
        )
        # The default, not the per-thread slot: the driver may run its stages
        # on a thread other than this one (the ADK runner does, inside an
        # event loop), and the placement stage never calls ``enter``.
        secondary_tap._default_stage = stage
        observed[id(secondary)] = secondary_tap
        return secondary_tap

    def tier(secondary: Model | None) -> Model | None:
        """Give the cheap tier of the worker model the same logging contract.

        Distinct from ``observe`` in the one way that matters: this tap
        *follows* ``enter``, so the three lanes it can serve each report their
        own stage, and its call ids carry their own prefix so they cannot
        collide with the reasoning tier's.
        """
        if secondary is None or on_event is None:
            return secondary
        extra = _EventingModel(
            secondary, emit, include_responses, call_prefix="cheap"
        )
        tier_taps.append(extra)
        return extra

    return emit, agent_model, enter, observe, tier


def _resolve_effort(
    model: Model,
    agent_model: Model,
    tier: Callable[[Model | None], Model | None],
    emit: Callable[[dict[str, Any]], None],
    *,
    effort: str | None,
    time_limit_s: Any,
    max_repairs: Any,
) -> tuple[EffortProfile, StageModels, float | None, int, EffortReceipt]:
    """Settle the level, the per-stage models and the two budgets, once.

    Called by both drivers with the same arguments, which is what keeps the
    SDK and ADK paths reporting the same level and spending the same budgets
    -- the parity ``engine/tests/test_adk.py`` pins.

    An explicitly-passed ``time_limit_s`` or ``max_repairs`` wins over the
    level and is named in ``receipt.overrides``: the level supplies defaults,
    it does not overrule a caller who said something. ``UNSET`` -- the
    default -- means the caller said nothing and the level decides.

    The cheap tier is built from the *original* model, not from the tap, so
    the sibling is a peer of the model the caller handed in rather than a
    wrapper of a wrapper; ``tier`` then gives it its own tap.
    """
    profile = profile_for(effort)
    overrides: list[str] = []
    if time_limit_s is UNSET:
        budget: float | None = profile.time_limit_s
    else:
        budget = time_limit_s
        overrides.append("time_limit_s")
    if max_repairs is UNSET:
        repairs = profile.max_repairs
    else:
        repairs = int(max_repairs)
        overrides.append("max_repairs")

    cheap = cheap_sibling(model) if profile.cheap_stages else None
    models = StageModels(
        profile=profile,
        primary=agent_model,
        cheap=tier(cheap),
        cheap_name=model_name(cheap),
    )
    receipt = build_receipt(
        profile,
        models,
        time_limit_s=budget,
        max_repairs=repairs,
        overrides=tuple(overrides),
    )
    # First frame of the run on purpose: a client that renders a progress rail
    # can label it before a single stage has started, and a stream captured
    # mid-run still carries the level at its head.
    emit({"event": "effort.selected", **receipt.as_dict()})
    return profile, models, budget, repairs, receipt


def _finish(
    *,
    intent: str,
    spec: CircuitSpec,
    board: BoardResult,
    facts: list[PartFacts],
    review: ReviewReport,
    attempts: list[ProposalAttempt],
    output: str | Path | None,
    route: RouteResult | None = None,
    artifacts: SchematicArtifacts = NO_ARTIFACTS,
    placement: GeneratedPlacement | None = None,
    enclosure: EnclosureResult | None = None,
    sourcing: SourcingResult | None = None,
    simulation: SimulationResult | None = None,
    emit_stages: bool = True,
    unread_datasheets: list[str] | None = None,
    plan: PlanResult | None = None,
    effort: EffortReceipt | None = None,
) -> PipelineResult:
    """Write the board if asked, then assemble the result. Emits nothing.

    This runs after the route stage, so the board it writes carries the copper
    the router laid. Writing it earlier would leave the headline artifact --
    the one named after what the caller asked for -- as the only unrouted file
    in the project.

    The sourced BOM is written here too, as ``bom.csv`` beside the project,
    under the ``enclosure.step`` rule: only with ``output`` set and
    ``emit_stages`` on, since ``--board-only`` promises only the routed
    board. The sourcing stage body never touches the filesystem (it runs on
    a worker thread and is collected by more than one owner), so the one
    place both drivers write the file is the one place both drivers end.
    A failed write degrades the way a failed case does -- a warning on the
    receipt, no path, the board still delivered -- rather than failing a
    run whose board was just written a line above.
    """
    path = None
    if output is not None:
        path = write_board(board, output)

    bom_path: Path | None = None
    grouped_bom_path: Path | None = None
    if sourcing is not None and output is not None and emit_stages:
        bom_path = Path(output).with_name("bom.csv")
        try:
            bom_path.parent.mkdir(parents=True, exist_ok=True)
            bom_path.write_text(bom_csv(sourcing), encoding="utf-8")
        except OSError as exc:
            sourcing.warnings.append(f"bom.csv was not written: {exc}")
            bom_path = None
        grouped_bom_path = Path(output).with_name("bom-grouped.csv")
        try:
            grouped_bom_path.parent.mkdir(parents=True, exist_ok=True)
            grouped_bom_path.write_text(grouped_bom_csv(sourcing), encoding="utf-8")
        except OSError as exc:
            sourcing.warnings.append(f"bom-grouped.csv was not written: {exc}")
            grouped_bom_path = None

    return PipelineResult(
        intent=intent,
        spec=spec,
        board=board,
        facts=facts,
        findings=list(review.findings),
        review=review,
        attempts=attempts,
        board_path=path,
        route=route,
        schematic_path=artifacts.schematic_path,
        project_path=artifacts.project_path,
        placed_board_path=artifacts.placed_board_path,
        placement=placement,
        enclosure=enclosure,
        sourcing=sourcing,
        bom_path=bom_path,
        grouped_bom_path=grouped_bom_path,
        simulation=simulation,
        unread_datasheets=list(unread_datasheets or []),
        plan=plan,
        effort=effort,
    )


def _generate_pcb_sdk(
    model: Model,
    intent: str,
    *,
    datasheets: dict[str, str] | None = None,
    preloaded_facts: list[PartFacts] | None = None,
    output: str | Path | None = None,
    max_repairs: Any = UNSET,
    time_limit_s: Any = UNSET,
    review: bool = True,
    route: bool = True,
    emit_stages: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    include_responses: bool = False,
    placement_profile: str | None = None,
    placement_policy: str = "deterministic",
    placement_feedback: dict[str, Any] | None = None,
    placement_model: Model | None = None,
    placement_fallback_model: Model | None = None,
    placement_max_turns: int = 8,
    plan: bool = False,
    enclosure: bool = False,
    enclosure_style: str = "",
    enclosure_rigorous: bool = False,
    sourcing: bool = False,
    sourcing_probe: Callable[[str], str] | None = None,
    simulate: bool = False,
    simulator: Simulator | str | None = None,
    effort: str | None = None,
) -> PipelineResult:
    """Run the stages as a straight line. See :func:`generate_pcb`."""
    emit, agent_model, enter, observe, tier = _wire_events(
        model, on_event, include_responses
    )
    _profile, models, time_limit_s, max_repairs, receipt = _resolve_effort(
        model,
        agent_model,
        tier,
        emit,
        effort=effort,
        time_limit_s=time_limit_s,
        max_repairs=max_repairs,
    )
    placement_model = observe(
        placement_model, "placement_repair", "placement"
    )
    placement_fallback_model = observe(
        placement_fallback_model, "placement_repair", "placement-fallback"
    )

    unread_datasheets: list[str] = []
    facts = read_stage(
        models.for_stage("read"),
        sheets=datasheets,
        preloaded_facts=preloaded_facts,
        emit=emit,
        enter=enter,
        unread=unread_datasheets,
    )
    plan_result = plan_stage(
        models.for_stage("plan"),
        intent=intent,
        plan=plan,
        max_repairs=max_repairs,
        emit=emit,
        enter=enter,
    )
    spec, attempts = propose_stage(
        models.for_stage("propose"),
        intent=intent,
        facts=facts,
        brief=(
            plan_result.plan.brief_text()
            if plan_result is not None and plan_result.plan is not None
            else None
        ),
        max_repairs=max_repairs,
        emit=emit,
        enter=enter,
        propose_on_event=emit if on_event is not None else None,
    )
    # The critic reads the validated spec and the datasheet facts and nothing
    # else -- not the placement, not the schematic, not the copper -- yet it
    # used to run last, so its whole latency was tail the engineer waited
    # through after the board was otherwise finished (and, with sourcing on,
    # behind the sourcing join as well). Started here it runs inside place's
    # CP-SAT budget (the effort level's, 5-45 s), which makes no model call
    # at all, and is joined below before the three background lanes start:
    # exactly one
    # worker-model call is ever in flight, so the order stays one fixed
    # sequence for both drivers and for a ScriptedModel.
    review_job = start_review_stage(
        models.for_stage("review"),
        spec,
        facts=facts,
        review=review,
        # The refutation round is the effort level's, not a separate flag: it
        # is a whole extra model call whose only job is to remove findings,
        # so it belongs on the dial that already says how much work someone
        # asked for. `thorough` is the only level that spends it.
        refute=_profile.refute,
        emit=emit,
        enter=enter,
    )
    try:
        board = place_stage(
            spec, time_limit_s=time_limit_s, emit=emit, enter=enter
        )
        placement = placement_repair_stage(
            board,
            profile=placement_profile,
            policy=placement_policy,
            feedback=placement_feedback,
            model=placement_model,
            fallback_model=placement_fallback_model,
            max_turns=placement_max_turns,
            emit=emit,
            enter=enter,
        )
    finally:
        # A run abandoned in placement must not leave a paid model call
        # running on a thread nobody will ever join -- the same rule the
        # route/enclosure block below applies.
        review_job.wait()
    if placement is not None:
        board = placement.board
    # The critic was started at the spec and has had place's whole CP-SAT
    # budget to answer in. Joined here, before the three background lanes
    # start, so exactly one worker-model call is ever in flight.
    review_report = review_job.result()
    # The case needs only the placed board, so it is designed on a worker
    # thread while the sheet is drawn and the copper laid, from a snapshot
    # routing's in-place mutation cannot reach. It is joined before review:
    # route makes no model calls, so the enclosure and the critic never race
    # for the same model, and a scripted model answers in the frozen order.
    enclosure_job = start_enclosure_stage(
        models.for_stage("enclosure"),
        board,
        enclosure=enclosure,
        enclosure_style=enclosure_style,
        rigorous=enclosure_rigorous,
        output=output,
        emit_stages=emit_stages,
        emit=emit,
        enter=enter,
    )
    # The parts are sourced the same way, from the same snapshot moment: one
    # model call plus the datasheet probes, needing nothing but the placed
    # parts. Off means no thread and no events (the enclosure's own rule,
    # held here because the stage body has no off switch of its own).
    sourcing_job = (
        start_sourcing_stage(
            models.for_stage("sourcing"),
            board,
            emit=emit,
            enter=enter,
            probe=sourcing_probe,
        )
        if sourcing
        else SourcingJob()
    )
    # And the circuit is verified in SPICE the same way: it needs only the
    # validated spec. Off means no thread and no events, the same rule.
    simulation_job = (
        start_simulation_stage(
            models.for_stage("simulation"),
            spec,
            intent=intent,
            simulator=simulator,
            emit=emit,
            enter=enter,
        )
        if simulate
        else SimulationJob()
    )
    try:
        artifacts = schematic_stage(
            spec,
            board,
            output=output,
            emit_stages=emit_stages,
            emit=emit,
            enter=enter,
            title=intent,
        )
        route_result = route_stage(board, route=route, emit=emit, enter=enter)
    finally:
        # A run abandoned mid-route must not leave a paid model call running
        # on a thread nobody will ever join.
        enclosure_job.wait()
        sourcing_job.wait()
        simulation_job.wait()
    # Enclosure, then sourcing, then simulation: a fixed join order, so a
    # scripted model and the ADK driver settle the three lanes identically.
    enclosure_result = enclosure_job.result()
    sourcing_result = sourcing_job.result()
    simulation_result = simulation_job.result()

    return _finish(
        intent=intent,
        spec=spec,
        board=board,
        facts=facts,
        review=review_report,
        attempts=attempts,
        output=output,
        route=route_result,
        artifacts=artifacts,
        placement=placement,
        enclosure=enclosure_result,
        sourcing=sourcing_result,
        simulation=simulation_result,
        emit_stages=emit_stages,
        unread_datasheets=unread_datasheets,
        plan=plan_result,
        effort=receipt,
    )


def generate_pcb(
    model: Model,
    intent: str,
    *,
    datasheets: dict[str, str] | None = None,
    preloaded_facts: list[PartFacts] | None = None,
    output: str | Path | None = None,
    max_repairs: Any = UNSET,
    time_limit_s: Any = UNSET,
    review: bool = True,
    route: bool = True,
    emit_stages: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    include_responses: bool = False,
    placement_profile: str | None = None,
    placement_policy: str = "deterministic",
    placement_feedback: dict[str, Any] | None = None,
    placement_model: Model | None = None,
    placement_fallback_model: Model | None = None,
    placement_max_turns: int = 8,
    plan: bool = False,
    enclosure: bool = False,
    enclosure_style: str = "",
    enclosure_rigorous: bool = False,
    sourcing: bool = False,
    sourcing_probe: Callable[[str], str] | None = None,
    simulate: bool = False,
    simulator: Simulator | str | None = None,
    effort: str | None = None,
    engine: str = "",
) -> PipelineResult:
    """Generate a placed board from a natural-language intent.

    Args:
        model: The model to use for every stage.
        intent: What to build, in plain language.
        datasheets: ``{part_number: pdf_url}`` to read before designing. Parts
            without a datasheet still work, but nothing can be cited about them.
        preloaded_facts: Facts already read for some parts, from a cache. These
            join the freshly-read ones and are used identically. A caller that
            skips a datasheet read because it has the facts already **must**
            pass them here: omitting them does not merely lose a citation, it
            designs and reviews the board as though the part were undocumented.
        output: Where to write the ``.kicad_pcb``. Skipped if omitted.
        max_repairs: How many times the proposal may be sent back for repair.
            Omitted, the ``effort`` level decides (1 / 3 / 5); passing a value
            overrides the level and is named in ``result.effort.overrides``.
            Lowering it relaxes no validation rule -- it only stops asking
            sooner, so a hard request fails rather than becoming a bad board.
        time_limit_s: Placement solver budget, or ``None`` for no solver limit.
            Omitted, the ``effort`` level decides (5 / 20 / 45 s), and this
            is the number that actually moves the wall clock: CP-SAT is 76-85%
            of a run. What a shorter budget costs was measured on the four
            demo prompts -- see :mod:`silkscreen.agents.effort` -- and
            ``result.effort`` reports which budget this board was placed in.
        review: Run the adversarial review pass.
        route: Lay copper after placing. Off leaves a placed board whose pads
            carry nets and whose copper is empty -- which KiCad draws as a
            ratsnest, and which is what every run produced before routing
            existed. Nets the router cannot finish are listed in
            ``result.route.unrouted`` whether this is on or not.
        emit_stages: Alongside the board, write the ``.kicad_sch``, the
            ``.kicad_pro`` that ties the two together, and the pre-routing
            ``.placed.kicad_pcb``, all named after ``output``. Ignored when
            ``output`` is None, since there is nowhere to put them.
        on_event: Called with one flat dict per stage boundary and per model
            round-trip, each carrying ``event`` and ``t_s`` -- seconds since
            this call began. Events carry counts and status only, never board
            text or datasheet text; raw model output appears only in
            ``model.response`` events, only when ``include_responses`` is set,
            truncated to ``MAX_RESPONSE_TEXT``. An exception raised by the
            callback deliberately propagates and abandons the run, which is how
            a service cancels work for a client that has disconnected. The
            event shape mirrors Google ADK's callback and event model, so this
            seam can be driven by an ADK runner without changing its callers.
        placement_profile: Company profile to verifier-gate after CP-SAT, or
            ``None`` to leave the original placement unchanged.
        placement_policy: Deterministic, Gemini, or an enabled experimental policy.
        placement_feedback: Structured request-local company-profile corrections.
        placement_model: Proposal model for a non-deterministic placement policy.
        placement_fallback_model: Recovery model used by the hybrid policy.
        placement_max_turns: Bounded number of placement proposal turns.
        enclosure: Additionally propose, build and verify a B-rep case for
            the placed board. Opt-in; the stage starts on a worker thread as
            soon as placement is done, runs beside the schematic and route
            stages, and is joined before review. Its failure never fails the
            run -- ``result.enclosure`` is ``None`` and a visible
            ``enclosure.failed`` event says why, but the board
            is still delivered (plan decision 5); that includes a missing
            ``cad`` extra, which is refused in words rather than degraded.
            With ``output`` set and ``emit_stages`` on, ``enclosure.step``
            and its two STLs are written beside the project.
        enclosure_style: Natural-language case intent ("rounded corners, USB
            cutout left"), handed to the proposal prompt as a style hint.
            Ignored when ``enclosure`` is off.
        enclosure_rigorous: Run the enclosure proposal loop at full
            strictness. Off (the default) is demo-fast: one repair round,
            only spec-validation failures and a build that raises repaired,
            and a failing kernel clause rides the receipt instead of blocking
            -- the case ships regardless. On restores the strict loop (three
            repair rounds; every failing clause is a repair item and only a
            passing report is accepted).
            Ignored when ``enclosure`` is off.
        sourcing: Additionally source the placed board's parts -- a
            manufacturer, part number and datasheet URL proposed per part,
            every URL probed for a real PDF, a KiCad-library 3D model
            attached where one matches -- into ``result.sourcing``. Opt-in;
            the stage starts on a worker thread as soon as placement is done,
            beside the enclosure, and is joined right after it, before
            review. Part numbers are proposals, never verified (there is no
            distributor API here), and the statuses say so. Its failure
            never fails the run: the result carries the deterministic rows
            with every status ``"none"`` and a warning naming the failure,
            after a visible ``sourcing.failed`` event. With ``output`` set
            and ``emit_stages`` on, ``bom.csv`` is written beside the project.
        sourcing_probe: The datasheet probe seam of
            :func:`~silkscreen.agents.sourcing.propose_sourcing`; ``None``
            (the default) is the real network probe, and tests pass a fake
            so the suite stays offline. Ignored when ``sourcing`` is off.
        simulate: Additionally verify the circuit's behaviour in SPICE:
            the model is asked for a testbench and the specification
            clauses the intent implies, the deck is simulated with ngspice,
            and every clause comes back with pass/fail, the measured value
            and a signed margin in ``result.simulation``. Opt-in; the stage
            starts on a worker thread as soon as placement is done, beside
            the enclosure and the BOM, and is joined after both, before
            review. Nothing about it fails the run: a part with no SPICE
            model (every device is one) is ``status "unsimulatable"``
            naming it, no ngspice on this machine is ``"unavailable"`` with
            the install hint, a model that gave no usable testbench is
            ``"no_testbench"``, a simulator that raised is ``"failed"``
            after a visible ``simulation.failed`` event -- and a failed
            clause is a finding on ``result.simulation.findings``,
            ``blocker`` when the clause was critical, else ``warning``.
        simulator: The simulator seam of
            :func:`~silkscreen.agents.simulate.simulate_circuit`; ``None``
            (the default) finds ngspice on ``PATH``, a name picks one, and
            tests pass a fake so the verdict path runs offline. Ignored
            when ``simulate`` is off.
        effort: The thinking level -- ``"fast"`` (the default),
            ``"balanced"`` or ``"thorough"``, the frozen vocabulary in
            :mod:`silkscreen.agents.effort`. It supplies the solver and
            repair budgets and moves the case, BOM and SPICE lanes onto the
            cheap model tier; it never turns a lane off, and it never
            overrules a budget the caller passed explicitly. An unknown name
            is a ``ValueError``, never a silent fall back to the default: a
            run that answers a ``thorough`` request at ``fast`` while
            reporting ``thorough`` is the one thing this exists to prevent.
            What it did is on ``result.effort``.
        engine: Which driver runs the stages -- ``"sdk"`` for the straight line
            in this module, ``"adk"`` for the Google ADK workflow in
            :mod:`silkscreen.agents.adk`. Both call the same stage bodies and
            emit the same events. Empty means read ``SILKSCREEN_ENGINE`` from
            the environment, falling back to ``"adk"`` -- the default since
            the 2026-08-30 live-run gate passed; an explicit argument always
            wins over the variable, and ``SILKSCREEN_ENGINE=sdk`` is the kill
            switch back to the straight line.

    Raises:
        ProposalError: no valid circuit emerged within the repair budget.
        UnsupportedPackage: a part's pin count has no footprint rule.
        RuntimeError: the engine name is unknown, or ``"adk"`` was asked for
            without the ``adk`` extra installed.
    """
    chosen = engine or os.environ.get("SILKSCREEN_ENGINE", "") or "adk"
    if chosen == "sdk":
        return _generate_pcb_sdk(
            model,
            intent,
            datasheets=datasheets,
            preloaded_facts=preloaded_facts,
            output=output,
            max_repairs=max_repairs,
            time_limit_s=time_limit_s,
            review=review,
            route=route,
            emit_stages=emit_stages,
            on_event=on_event,
            include_responses=include_responses,
            placement_profile=placement_profile,
            placement_policy=placement_policy,
            placement_feedback=placement_feedback,
            placement_model=placement_model,
            placement_fallback_model=placement_fallback_model,
            placement_max_turns=placement_max_turns,
            plan=plan,
            enclosure=enclosure,
            enclosure_style=enclosure_style,
            enclosure_rigorous=enclosure_rigorous,
            sourcing=sourcing,
            sourcing_probe=sourcing_probe,
            simulate=simulate,
            simulator=simulator,
            effort=effort,
        )
    if chosen == "adk":
        # Imported here, never at module scope: a base install has no google.adk,
        # and silkscreen.agents is imported by the service on every request path.
        try:
            from .adk.runner import generate_pcb_adk
        except ImportError as exc:
            raise RuntimeError(
                "the 'adk' engine needs the adk extra: pip install 'silkscreen[adk]'"
            ) from exc
        return generate_pcb_adk(
            model,
            intent,
            datasheets=datasheets,
            preloaded_facts=preloaded_facts,
            output=output,
            max_repairs=max_repairs,
            time_limit_s=time_limit_s,
            review=review,
            route=route,
            emit_stages=emit_stages,
            on_event=on_event,
            include_responses=include_responses,
            placement_profile=placement_profile,
            placement_policy=placement_policy,
            placement_feedback=placement_feedback,
            placement_model=placement_model,
            placement_fallback_model=placement_fallback_model,
            placement_max_turns=placement_max_turns,
            plan=plan,
            enclosure=enclosure,
            enclosure_style=enclosure_style,
            enclosure_rigorous=enclosure_rigorous,
            sourcing=sourcing,
            sourcing_probe=sourcing_probe,
            simulate=simulate,
            simulator=simulator,
            effort=effort,
        )
    # RuntimeError, not ValueError: the service answers a pipeline ValueError as
    # a 400 with the raw message, and a bad engine name is not a client's fault.
    raise RuntimeError(f"unknown engine {chosen!r}: expected 'sdk' or 'adk'")

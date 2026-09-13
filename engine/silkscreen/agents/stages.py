"""The pipeline stages, as standalone bodies.

Each function is one stage of prompt-to-PCB: read, propose, place, placement
repair, schematic, route, enclosure, sourcing, simulation, and review. They hold
the whole of a stage -- its
model calls, its guards, and the exact events it emits -- so that more than one
driver can run the same stages. The straight line in
:mod:`silkscreen.agents.pipeline` and the ADK workflow in
:mod:`silkscreen.agents.adk` both call these, and therefore emit byte-identical
events; the service and the SPA read those event names, so a driver that grew
its own copy of a stage would silently fork the contract.

``emit`` and ``enter`` come from the driver: ``emit`` publishes one flat event
dict, ``enter`` tells the model wrapper which stage is making its calls. Nothing
here imports :mod:`silkscreen.agents.pipeline` -- that import runs the other way.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from ..board import BoardResult, build_board, route_board, write_board
from ..enclosure.board_shape import board_envelope
from ..enclosure.cad import ExportPaths, export_model
from ..enclosure.errors import EnclosureError
from ..enclosure.ir import EnclosureSpec
from ..enclosure.kernel import KernelReport
from ..enclosure.snapshot import render_packet
from ..netlist import CircuitSpec
from ..placement.adapter import GeneratedPlacement, repair_generated_board
from ..placement.agent import TextModel
from ..placement.pcb_repair import evaluate
from ..routing import RouteResult
from ..schematic import build_schematic, write_project, write_schematic
from ..sourcing import SourcingResult, bom_rows
from ..spice.errors import SpiceError
from ..spice.simulators import Simulator
from ..units import NM_PER_MM, to_mm
from .datasheet import PartFacts, read_datasheet
from .enclosure import propose_enclosure
from .model import Model
from .plan import PlanResult, propose_plan
from .propose import ProposalAttempt, propose_circuit
from .review import (
    ReviewError,
    ReviewReport,
    ReviewStatus,
    Severity,
    run_review,
)
from .simulate import SimulationResult, simulate_circuit
from .sourcing import propose_sourcing

__all__ = [
    "plan_stage",
    "read_stage",
    "propose_stage",
    "place_stage",
    "placement_repair_stage",
    "schematic_stage",
    "route_stage",
    "enclosure_stage",
    "start_enclosure_stage",
    "EnclosureJob",
    "sourcing_stage",
    "start_sourcing_stage",
    "SourcingJob",
    "simulate_stage",
    "start_simulation_stage",
    "SimulationJob",
    "SimulationResult",
    "placed_snapshot",
    "review_stage",
    "start_review_stage",
    "ReviewJob",
    "ReviewReport",
    "SchematicArtifacts",
    "NO_ARTIFACTS",
    "EnclosureResult",
    "proposal_fields",
]

Emit = Callable[[dict[str, Any]], None]
Enter = Callable[[str], None]

#: How many datasheet reads may be in flight at once. A request naming a whole
#: BOM (a dozen-plus parts) must not fire a dozen simultaneous Gemini calls --
#: that is both a self-inflicted rate-limit hit and, against the live model,
#: real concurrent spend with no backpressure. 4 is generous headroom over the
#: common case (one to three parts) while bounding the worst case.
MAX_CONCURRENT_READS = 4


def _run_coro_blocking(coro):
    """Run ``coro`` to completion from synchronous code, loop or no loop.

    :func:`read_stage` is a plain synchronous function called from two
    contexts: the SDK driver's straight line (no event loop) and the ADK
    node body in :mod:`silkscreen.agents.adk.workflow` (already running
    inside the loop ``adk/runner.py``'s ``_drive`` started with
    ``asyncio.run``). Calling ``asyncio.run`` directly would work in the
    first case and raise "cannot be called from a running event loop" in the
    second, so this mirrors ``adk/runner.py``'s own ``_drive``/
    ``_loop_is_running`` split rather than inventing a second convention:
    with no loop running, drive the coroutine here; with one already
    running, give it a fresh loop on a thread of its own.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    raised: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            raised.append(exc)

    thread = threading.Thread(target=target, name="silkscreen-read-gather")
    thread.start()
    thread.join()
    if raised:
        raise raised[0]
    return result[0]


def read_stage(
    agent_model: Model,
    *,
    sheets: dict[str, str] | None,
    preloaded_facts: list[PartFacts] | None,
    emit: Emit,
    enter: Enter,
    unread: list[str] | None = None,
) -> list[PartFacts]:
    """Read every datasheet, returning cached and freshly-read facts together.

    ``unread``, when given, collects one line per datasheet that was asked
    for and could not be read. The ``read.failed`` event says the same thing,
    but ``emit`` is a no-op for every caller that passes no ``on_event`` --
    the CLI included -- and a board designed as though a part were
    undocumented must not be silent about it.

    With no datasheets to read the stage does not run at all and emits nothing,
    rather than reporting an empty pass.

    Uncached parts are read **concurrently**, up to :data:`MAX_CONCURRENT_READS`
    at a time, via ``asyncio.gather`` over a thread per read (each read is a
    blocking download plus a blocking model call, neither of which is async
    here, so ``asyncio.to_thread`` is the bridge). One shared ``agent_model``
    is reused across every concurrent read rather than one client per task:
    ``GeminiModel`` wraps a ``genai.Client`` built once at construction with
    its own ``httpx`` connection pool, the same object every other stage call
    already reuses across sequential requests, and google-genai's client
    issues plain HTTP requests with no shared mutable request state -- there
    is nothing here for concurrent callers to corrupt. ``ScriptedModel``
    (the offline stand-in) is likewise safe: its ``by_marker`` lookup is a
    read-only dict probe, and its ``calls``/``responses`` list mutations are
    single, GIL-atomic list operations, so tests that key responses by a
    per-part marker in the prompt (as every multi-part fixture in this repo
    does) see no cross-talk between concurrent reads.

    Failure isolation follows the same convention :func:`review_stage`'s
    caller applies to review findings and :func:`propose_stage`'s repair loop
    applies to validation errors: one bad part must not sink a batch that
    contains good ones. A part whose read raises (network error, non-PDF
    body, a model that returns no usable pinout) is reported via a
    ``read.failed`` event and simply excluded from the returned facts, so the
    parts that did read still reach ``propose``. Only when *every* requested
    part fails -- there is nothing left for the run to build from -- does the
    stage re-raise (the first failure, since a batch-of-one is the common
    case and a single-part request must keep failing exactly as it did before
    this stage learned to run concurrently).
    """
    facts: list[PartFacts] = list(preloaded_facts or ())
    already = {f.part_number.strip().lower() for f in facts}
    sheets = sheets or {}
    if sheets:
        enter("read")
        emit({"event": "stage.start", "stage": "read"})

    emit_lock = threading.Lock()

    def safe_emit(evt: dict[str, Any]) -> None:
        # Concurrent reads call this from worker threads; the driver's own
        # ``emit`` (a list append, an SSE write, ...) is not guaranteed safe
        # under concurrent callers, so every emission from inside a read is
        # serialised here. The sequential ``read.part`` emissions below need
        # no lock -- they all run on this thread, before any read starts.
        with emit_lock:
            emit(evt)

    to_read: list[tuple[str, str]] = []
    for index, (part_number, url) in enumerate(sheets.items(), start=1):
        # A part supplied both ways is read once; the cached copy wins, since
        # re-reading it is the cost the caller was trying to avoid.
        cached = part_number.strip().lower() in already
        emit(
            {
                "event": "read.part",
                "part": part_number,
                "index": index,
                "total": len(sheets),
                "cached": cached,
            }
        )
        if not cached:
            to_read.append((part_number, url))

    def read_one(part_number: str, url: str) -> PartFacts:
        # The download is the slowest thing in the stage and the only one that
        # can stall on a remote host. Reporting the bytes it landed is what
        # separates "still fetching" from "hung" while someone watches a demo.
        def announce(target: str, **kwargs) -> bytes:
            from .grounding import fetch_pdf

            data = fetch_pdf(target, **kwargs)
            safe_emit(
                {
                    "event": "read.fetch",
                    "part": part_number,
                    "bytes": len(data),
                }
            )
            return data

        return read_datasheet(agent_model, part_number, pdf_url=url, fetch=announce)

    if to_read:

        async def gather_reads() -> list[PartFacts | BaseException]:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_READS)

            async def bound(part_number: str, url: str) -> PartFacts:
                async with semaphore:
                    return await asyncio.to_thread(read_one, part_number, url)

            return await asyncio.gather(
                *(bound(part_number, url) for part_number, url in to_read),
                return_exceptions=True,
            )

        # asyncio.gather preserves the order of its awaitables in the result
        # list regardless of which one finished first, so zipping this back
        # against ``to_read`` keeps ``facts`` in a deterministic, request
        # order -- an order-independent *completion* is fine; an
        # order-independent *result* would not be.
        results = _run_coro_blocking(gather_reads())
        failures: list[BaseException] = []
        for (part_number, _url), result in zip(to_read, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(result)
                emit(
                    {
                        "event": "read.failed",
                        "part": part_number,
                        "error": str(result)[:200],
                    }
                )
                if unread is not None:
                    unread.append(
                        f"{part_number}: the datasheet could not be read "
                        f"({str(result)[:160]}); the board was designed and "
                        f"reviewed without its facts"
                    )
                continue
            facts.append(result)

        if failures and len(failures) == len(to_read):
            raise failures[0]

    if sheets:
        emit(
            {
                "event": "stage.done",
                "stage": "read",
                "parts": len(facts),
                "pins": sum(len(f.pins) for f in facts),
                "requirements": sum(len(f.requirements) for f in facts),
            }
        )
    return facts


def plan_stage(
    agent_model: Model,
    *,
    intent: str,
    plan: bool,
    max_repairs: int,
    emit: Emit,
    enter: Enter,
) -> PlanResult | None:
    """Expand a thin intent into a validated brief, or None when off.

    Opt-in, because it is a model call nobody pressed for: `plan=False` skips
    it entirely and emits nothing, the `read_stage` rule for a stage with
    nothing to do.

    It never fails the run. A model that cannot produce a usable plan gives a
    `PlanResult` whose `plan` is None and whose `warnings` say so, and the
    propose stage designs from the bare intent exactly as it did before -- an
    unplanned board is honest, and losing a board because its brief would not
    parse is not a trade worth making.
    """
    if not plan:
        return None
    enter("plan")
    emit({"event": "stage.start", "stage": "plan"})
    try:
        result = propose_plan(
            agent_model,
            intent,
            max_repairs=max_repairs,
            on_event=emit,
        )
    except ValueError as exc:
        # The sourcing_stage rule: a bad answer is a warning, not a dead run.
        detail = f"the plan could not be made: {exc}"
        emit({"event": "plan.failed", "stage": "plan", "detail": detail[:200]})
        emit({"event": "stage.done", "stage": "plan", "planned": False})
        return PlanResult(plan=None, warnings=[detail])
    emit(
        {
            "event": "stage.done",
            "stage": "plan",
            "planned": result.plan is not None,
            "warnings": len(result.warnings),
        }
    )
    return result


def propose_stage(
    agent_model: Model,
    *,
    intent: str,
    facts: list[PartFacts],
    max_repairs: int,
    emit: Emit,
    enter: Enter,
    propose_on_event: Emit | None,
    brief: str | None = None,
) -> tuple[CircuitSpec, list[ProposalAttempt]]:
    """Ask for a circuit and repair it until it validates.

    ``propose_on_event`` is handed to :func:`propose_circuit` unchanged, so the
    repair loop's own ``propose.round`` events reach the same stream -- or none
    at all when the driver has no listener.
    """
    enter("propose")
    emit({"event": "stage.start", "stage": "propose"})
    spec, attempts = propose_circuit(
        agent_model,
        intent,
        facts=facts,
        brief=brief,
        max_repairs=max_repairs,
        on_event=propose_on_event,
    )
    emit(
        {
            "event": "stage.done",
            "stage": "propose",
            "parts": spec.part_count(),
            "nets": spec.net_count(),
            "repair_rounds": max(0, len(attempts) - 1),
        }
    )
    return spec, attempts


def place_stage(
    spec: CircuitSpec,
    *,
    time_limit_s: float | None,
    emit: Emit,
    enter: Enter,
) -> BoardResult:
    """Solve placement for the accepted circuit. The only model-free stage."""
    enter("place")
    emit(
        {
            "event": "stage.start",
            "stage": "place",
            "time_limit_s": (
                None if time_limit_s is None else float(time_limit_s)
            ),
        }
    )
    board = build_board(spec, time_limit_s=time_limit_s)
    width_mm, height_mm = board.size_mm
    emit(
        {
            "event": "stage.done",
            "stage": "place",
            "solver_status": board.solver_status,
            "board_mm": [width_mm, height_mm],
            "wirelength_mm": (
                None if board.wirelength_nm is None else board.wirelength_nm / NM_PER_MM
            ),
            "warnings": len(board.warnings),
        }
    )
    return board


def placement_repair_stage(
    board: BoardResult,
    *,
    profile: str | None,
    policy: str,
    feedback: dict[str, Any] | None,
    model: TextModel | None,
    fallback_model: TextModel | None,
    max_turns: int,
    emit: Emit,
    enter: Enter,
) -> GeneratedPlacement | None:
    """Verifier-gate a generated placement before schematic emission and routing."""
    if not profile:
        return None
    enter("placement_repair")
    emit(
        {
            "event": "stage.start",
            "stage": "placement_repair",
            "profile": profile,
            "policy": policy,
        }
    )
    result = repair_generated_board(
        board,
        profile=profile,
        policy=policy,
        feedback=feedback,
        model=model,
        fallback_model=fallback_model,
        max_turns=max_turns,
    )
    before = result.run.start
    after = result.run.board
    before_score = evaluate(before, result.run.profile)
    after_score = evaluate(after, result.run.profile)
    emit(
        {
            "event": "stage.done",
            "stage": "placement_repair",
            "profile": profile,
            "policy": result.run.policy,
            "requested_policy": result.requested_policy,
            "completed": result.run.completed,
            "applied": result.applied,
            "moves": sum(len(step.accepted) for step in result.run.steps),
            "hard_before": before_score.hard,
            "hard_after": after_score.hard,
            "policy_fallback": result.policy_fallback,
        }
    )
    return result


class SchematicArtifacts(NamedTuple):
    """The files the schematic stage wrote, or three Nones when it did not run."""

    schematic_path: Path | None = None
    project_path: Path | None = None
    placed_board_path: Path | None = None


#: What the schematic stage returns when it does not run, and the default a
#: driver passes when it has nothing to report. A module-level singleton
#: because it is immutable and shared, and because a NamedTuple constructed in
#: an argument default is the mutable-default footgun's shape even when it is
#: not one.
NO_ARTIFACTS = SchematicArtifacts()


def schematic_stage(
    spec: CircuitSpec,
    board: BoardResult,
    *,
    output: str | Path | None,
    emit_stages: bool,
    emit: Emit,
    enter: Enter,
    title: str | None = None,
) -> SchematicArtifacts:
    """Draw the sheet and leave every stage on disk, not just the last one.

    The only stage body that writes files. The rest hand their results back and
    let the driver's tail decide; this one cannot, because the three artifacts
    it produces have no other owner and both drivers must produce them
    identically. With no ``output`` there is nowhere to put them, so the stage
    does not run and emits nothing -- the same convention ``read_stage`` follows
    with no datasheets.

    The schematic goes out before any copper exists, because it is the artifact
    a person reads first and it does not depend on placement succeeding well.
    """
    out_path = Path(output) if output is not None else None
    if out_path is None or not emit_stages:
        return NO_ARTIFACTS

    enter("schematic")
    emit({"event": "stage.start", "stage": "schematic"})
    stem = out_path.stem
    sheet = build_schematic(
        spec,
        footprints={p.ref: f"silkscreen:{p.footprint.name}" for p in board.parts},
    )
    board.warnings.extend(sheet.warnings)
    artifacts = SchematicArtifacts(
        schematic_path=write_schematic(
            sheet,
            out_path.with_name(f"{stem}.kicad_sch"),
            project_name=stem,
            # The title block names the request, and carries the real date:
            # a sheet dated to the emitter's fixed default would be a lie.
            title=title,
            today=datetime.date.today(),
        ),
        project_path=write_project(
            out_path.with_name(f"{stem}.kicad_pro"), project_name=stem
        ),
        placed_board_path=write_board(
            board, out_path.with_name(f"{stem}.placed.kicad_pcb")
        ),
    )
    emit(
        {
            "event": "stage.done",
            "stage": "schematic",
            "symbols": len(sheet.symbols),
            "warnings": len(sheet.warnings),
        }
    )
    return artifacts


def route_stage(
    board: BoardResult,
    *,
    route: bool,
    emit: Emit,
    enter: Enter,
) -> RouteResult | None:
    """Lay copper on the placed board. Skipped -- and silent -- when off.

    Mutates ``board`` in place, so the ``.kicad_pcb`` the driver's tail writes
    carries the tracks. Nets the router could not finish are named in the
    result and in ``board.unrouted_nets``; a caller reporting the board as
    routed without reading them is the failure the router exists to avoid.
    """
    if not route:
        return None
    enter("route")
    emit({"event": "stage.start", "stage": "route"})
    result = route_board(board)
    emit(
        {
            "event": "stage.done",
            "stage": "route",
            "tracks": len(result.tracks),
            "vias": len(result.vias),
            "routed_nets": len(result.routed),
            "unrouted_nets": len(result.unrouted),
            "copper_mm": round(result.routed_length_nm / NM_PER_MM, 3),
        }
    )
    return result


class EnclosureResult(NamedTuple):
    """What the enclosure stage produced (docs/ai-cad-plan.md v3).

    There is one engine -- the build123d/OCCT kernel -- so there is no
    ``engine`` field to read and no ``.scad`` to ship: the v1 OpenSCAD
    emitter, its fit receipt and its rendered flag were removed on
    2026-09-08. ``step_text`` is the STEP (ISO 10303-21) assembly as text,
    which is ASCII and self-contained and therefore rides the one-shot
    ``/generate`` JSON the way the ``.scad`` used to.
    """

    spec: EnclosureSpec
    step_text: str
    repair_rounds: int
    #: The kernel's clause-by-clause receipt -- the only receipt.
    kernel: KernelReport | None = None
    #: The STEP/STL files :func:`~silkscreen.enclosure.cad.export_model`
    #: wrote, or None when nothing durable was written (no directory: the
    #: one-shot route exports into a scratch directory and keeps only the
    #: text).
    exports: ExportPaths | None = None
    #: The advisory snapshot PNGs, in :data:`~silkscreen.enclosure.snapshot.VIEWS`
    #: order; empty when they were not rendered (never a failure).
    snapshots: tuple[Path, ...] = ()
    #: The text the model was shown. Never enters an event or the one-shot
    #: response.
    brief: str = ""


def proposal_fields(proposal: Any) -> tuple[Any, int, Any, Any, str]:
    """``(spec, repair_rounds, model, kernel, brief)`` from a proposal.

    :func:`~silkscreen.agents.enclosure.propose_enclosure` returns an
    ``EnclosureProposal`` NamedTuple; this reads it by name so a caller that
    only wants some of the fields does not depend on the tuple's arity.
    """
    return (
        proposal.spec,
        proposal.repair_rounds,
        getattr(proposal, "model", None),
        getattr(proposal, "kernel", None),
        getattr(proposal, "brief", "") or "",
    )


def enclosure_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    enclosure: bool,
    enclosure_style: str,
    rigorous: bool = False,
    output: str | Path | None,
    emit_stages: bool,
    export_dir: str | Path | None = None,
    stem: str = "enclosure",
    emit: Emit,
    enter: Enter,
) -> EnclosureResult | None:
    """Propose, verify, and emit a case for the placed board. Opt-in.

    No-ops silently when the run did not ask for an enclosure (the
    ``route_stage`` pattern). A case needs only the outline and the part
    envelopes -- :mod:`~silkscreen.enclosure.board_shape` reads ``Edge.Cuts``
    and footprints and never looks at copper -- so both drivers start this
    stage on a worker thread as soon as placement is done, through
    :func:`start_enclosure_stage`, and join it before review so the critic
    still closes the stream. This body is what that thread runs; called
    directly it is the same stage, in line.

    The stage measures the board by writing it to a scratch file and reading
    it back through :func:`~silkscreen.enclosure.board_shape.board_envelope`
    -- the envelope is derived from ``.kicad_pcb`` text, the same artifact the
    caller receives, not from in-memory state the file might not carry.

    ``rigorous`` selects the proposal loop's temperament (default fast: one
    repair round, a failing kernel clause riding the receipt rather than
    blocking; ``True`` restores the strict verify-and-repair loop -- see
    :func:`~silkscreen.agents.enclosure.propose_enclosure`).

    One engine: the build123d/OCCT kernel (docs/ai-cad-plan.md v3). Files go
    to one directory: ``export_dir`` when given (the steps service passes the
    session's directory), else beside ``output`` when it is set and
    ``emit_stages`` is on (the ``schematic_stage`` filesystem rule --
    ``--board-only`` promises only the routed board). ``stem`` names them:
    ``<stem>.step``, ``<stem>-base.stl``, ``<stem>-lid.stl``, plus the
    advisory snapshot PNGs (plan decision 19); a snapshot failure is a
    warning event, never a failed stage (decision 17).

    With **no** directory nothing durable is written and ``exports`` is
    ``None``, but ``step_text`` is still produced: the model is exported into
    a scratch directory and the STEP read back, so the one-shot
    ``/generate`` JSON still carries a complete case. ``--board-only`` is the
    same case: it declines the stage *files*, and by then the model call and
    the kernel build have already been spent, so withholding the text would
    discard a case that has already been paid for rather than save anything.

    Failure never fails the run (plan decision 5): any
    :class:`~silkscreen.enclosure.errors.EnclosureError` -- an exhausted
    repair budget, or a
    :class:`~silkscreen.enclosure.errors.KernelUnavailable` refusal when the
    ``cad`` extra is not installed -- as well as a ``ValueError`` from
    measuring the board and an ``OSError`` from writing files is caught here,
    surfaced as a visible ``enclosure.failed`` event carrying the message,
    and answered with ``None``; the board is still the product. There is no
    lesser case to fall back to and none is invented. Everything else,
    callback exceptions and
    :class:`~silkscreen.agents.model.ModelError` included, propagates as it
    does from every other stage.
    """
    if not enclosure:
        return None
    enter("enclosure")
    emit({"event": "stage.start", "stage": "enclosure"})
    exports: ExportPaths | None = None
    snapshots: tuple[Path, ...] = ()
    directory: Path | None
    if export_dir is not None:
        directory = Path(export_dir)
    elif output is not None and emit_stages:
        directory = Path(output).parent
    else:
        directory = None
    try:
        with tempfile.TemporaryDirectory(prefix="silkscreen-enclosure-") as tmp:
            measured = write_board(board, Path(tmp) / "board.kicad_pcb")
            envelope = board_envelope(measured)
        # The loop's kernel report is the receipt, build warnings included,
        # so the stage never re-verifies what was already verified. Fast mode
        # (the default) never lets a failing clause block; ``rigorous``
        # restores the strict repair loop.
        spec, repair_rounds, model, kernel, brief = proposal_fields(
            propose_enclosure(
                agent_model,
                envelope,
                style_hint=enclosure_style,
                rigorous=rigorous,
                on_event=emit,
            )
        )
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            exports = export_model(model, directory, stem)
            step_text = exports.step.read_text(encoding="utf-8")
            try:
                snapshots = tuple(render_packet(model, directory))
            except Exception as exc:  # noqa: BLE001 - advisory, never the gate
                emit(
                    {
                        "event": "enclosure.warning",
                        "warning": f"snapshots not rendered: {exc}"[:160],
                    }
                )
        else:
            # The one-shot /generate JSON path: nothing durable is wanted,
            # but the response must still carry a complete case. STEP is
            # ASCII and self-contained, so exporting into a scratch
            # directory and reading it back loses nothing.
            with tempfile.TemporaryDirectory(prefix="silkscreen-step-") as tmp:
                scratch = export_model(model, Path(tmp), stem)
                step_text = scratch.step.read_text(encoding="utf-8")
    except (EnclosureError, ValueError, OSError) as exc:
        emit({"event": "enclosure.failed", "error": str(exc)[:160]})
        return None

    emit(
        {
            "event": "stage.done",
            "stage": "enclosure",
            "cutouts": len(spec.cutouts),
            "lid": spec.lid,
            "wall_mm": round(to_mm(spec.wall_nm), 3),
            "repair_rounds": repair_rounds,
            "kernel_passed": None if kernel is None else bool(kernel.passed),
            "kernel_failed": [] if kernel is None else list(kernel.failed),
            "exports": (
                []
                if exports is None
                else [
                    exports.step.name,
                    exports.base_stl.name,
                    exports.lid_stl.name,
                ]
            ),
        }
    )
    return EnclosureResult(
        spec=spec,
        step_text=step_text,
        repair_rounds=repair_rounds,
        kernel=kernel,
        exports=exports,
        snapshots=snapshots,
        brief=brief,
    )


def placed_snapshot(board: BoardResult) -> BoardResult:
    """A copy of ``board`` that :func:`route_stage` cannot mutate underneath.

    Routing rebinds ``tracks``/``vias``/``unrouted_nets``/``routed_nets`` and
    extends ``warnings`` in place; the schematic stage extends ``warnings``
    too. Every field the router or the sheet touches gets its own container
    here, so a thread measuring the placed board reads geometry that holds
    still. ``parts`` is shared on purpose: nothing after placement moves a
    part, and the placement is exactly what the case is built around.
    """
    return dataclasses.replace(
        board,
        warnings=list(board.warnings),
        tracks=list(board.tracks),
        vias=list(board.vias),
        unrouted_nets=dict(board.unrouted_nets),
        routed_nets=list(board.routed_nets),
    )


class EnclosureJob:
    """The enclosure stage in flight on its own thread, or already settled.

    :meth:`result` joins the thread and then either returns what the stage
    returned or re-raises exactly what it raised -- a ``ModelError``, or an
    event callback that hung up -- so a failure in the background carries
    the same meaning at the join as it would have had in line. A job that
    was never started (the run did not ask for a case) settles immediately
    with ``None`` and no events, the ``enclosure_stage`` no-op preserved.
    """

    def __init__(self, thread: threading.Thread | None = None) -> None:
        self._thread = thread
        self._result: EnclosureResult | None = None
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self) -> None:
        """Block until the stage has finished. Raises nothing; idempotent."""
        if self._thread is not None:
            self._thread.join()

    def result(self) -> EnclosureResult | None:
        self.wait()
        if self._error is not None:
            raise self._error
        return self._result

    def _run(self, work: Callable[[], EnclosureResult | None]) -> None:
        try:
            self._result = work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised at the join
            self._error = exc


def start_enclosure_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    enclosure: bool,
    enclosure_style: str,
    rigorous: bool = False,
    output: str | Path | None,
    emit_stages: bool,
    export_dir: str | Path | None = None,
    stem: str = "enclosure",
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
) -> EnclosureJob:
    """Run :func:`enclosure_stage` on a worker thread from a placed snapshot.

    Called right after placement, so the case is being designed while the
    schematic is drawn and the copper laid, and is ready by the time the run
    -- or the engineer, in step mode -- gets to it. The board handed to the
    thread is :func:`placed_snapshot` of ``board``, taken here, before any
    later stage has touched it.

    ``emit`` and ``enter`` are called from the worker thread. The drivers'
    ``emit`` serialises callbacks and their model wrapper keeps the entered
    stage per thread, so the thread's events carry ``stage: "enclosure"``
    whatever the main thread is doing, and the main thread's stage names are
    never overwritten by it. Events therefore interleave with the schematic
    and route stages in wall-clock order; within the enclosure lane the
    order is the frozen one.

    ``export_dir`` and ``stem`` are handed to :func:`enclosure_stage`
    unchanged, so a background case lands its STEP, STLs and preview in the
    same place an in-line one would.

    With ``enclosure`` off nothing is started and the returned job settles
    to ``None`` at once. ``daemon`` marks the thread so an owner that may
    forget the job (a step session evicted mid-design) never keeps the
    process alive for it; a driver that always joins leaves it off.
    """
    if not enclosure:
        return EnclosureJob()
    snapshot = placed_snapshot(board)
    job = EnclosureJob()

    def work() -> EnclosureResult | None:
        return enclosure_stage(
            agent_model,
            snapshot,
            enclosure=True,
            enclosure_style=enclosure_style,
            rigorous=rigorous,
            output=output,
            emit_stages=emit_stages,
            export_dir=export_dir,
            stem=stem,
            emit=emit,
            enter=enter,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-enclosure", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def sourcing_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    emit: Emit,
    enter: Enter,
    probe: Callable[[str], str] | None = None,
) -> SourcingResult:
    """Source the placed board's parts: MPN proposals, probed datasheets, BOM.

    Needs only the placed parts -- ref, value, land pattern -- so like the
    enclosure it is started on a worker thread as soon as placement is done
    (:func:`start_sourcing_stage`) and collected by the sourcing or order
    step. This body is what that thread runs; called directly it is the same
    stage, in line.

    ``probe`` is the datasheet probe seam of
    :func:`~silkscreen.agents.sourcing.propose_sourcing` (default: the real
    network probe); the service pins it offline in tests.

    Failure never fails the run: a ``ValueError`` -- a
    :class:`~silkscreen.sourcing.SourcingValidationError` that escaped the
    repair loop, a probe answering outside the vocabulary -- is caught here,
    surfaced as a visible ``sourcing.failed`` event, and answered with the
    deterministic :func:`~silkscreen.sourcing.bom_rows` (every status
    ``"none"``) carrying one warning that names the failure. The board is
    still the product; an unsourced BOM is still a BOM. Everything else,
    callback exceptions and :class:`~silkscreen.agents.model.ModelError`
    included, propagates as it does from every other stage.
    """
    enter("sourcing")
    emit({"event": "stage.start", "stage": "sourcing"})
    try:
        result = propose_sourcing(
            agent_model, bom_rows(board), probe=probe, on_event=emit
        )
    except ValueError as exc:
        emit({"event": "sourcing.failed", "error": str(exc)[:160]})
        return SourcingResult(
            bom_rows(board),
            warnings=[f"parts were not sourced: {str(exc)[:200]}"],
        )
    emit(
        {
            "event": "stage.done",
            "stage": "sourcing",
            "parts": len(result.parts),
            "verified": result.verified,
            "proposed": result.proposed,
            "unresolved": result.unresolved,
        }
    )
    return result


class SourcingJob:
    """The sourcing stage in flight on its own thread, or already settled.

    :meth:`result` joins the thread and then either returns what the stage
    returned or re-raises exactly what it raised -- a ``ModelError``, or an
    event callback that hung up -- so a failure in the background carries
    the same meaning at the join as it would have had in line (the
    :class:`EnclosureJob` semantics, exactly). A job that was never started
    settles immediately with ``None`` and no events.
    """

    def __init__(self, thread: threading.Thread | None = None) -> None:
        self._thread = thread
        self._result: SourcingResult | None = None
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self) -> None:
        """Block until the stage has finished. Raises nothing; idempotent."""
        if self._thread is not None:
            self._thread.join()

    def result(self) -> SourcingResult | None:
        self.wait()
        if self._error is not None:
            raise self._error
        return self._result

    def _run(self, work: Callable[[], SourcingResult]) -> None:
        try:
            self._result = work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised at the join
            self._error = exc


def start_sourcing_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
    probe: Callable[[str], str] | None = None,
) -> SourcingJob:
    """Run :func:`sourcing_stage` on a worker thread from a placed snapshot.

    Called right after placement, so the parts are being looked up while
    the engineer reviews the board in KiCad, and the BOM is ready by the
    time the run -- or the engineer, in step mode -- gets to the order. The
    board handed to the thread is :func:`placed_snapshot` of ``board``,
    taken here, before any later stage has touched it.

    ``emit`` and ``enter`` are called from the worker thread, under the
    same rules as :func:`start_enclosure_stage`: the drivers' ``emit``
    serialises callbacks and their model wrapper keeps the entered stage
    per thread, so the thread's events carry ``stage: "sourcing"`` whatever
    the main thread is doing. ``daemon`` marks the thread so an owner that
    may forget the job (a step session evicted mid-run) never keeps the
    process alive for it; a driver that always joins leaves it off.
    """
    snapshot = placed_snapshot(board)
    job = SourcingJob()

    def work() -> SourcingResult:
        return sourcing_stage(
            agent_model, snapshot, emit=emit, enter=enter, probe=probe
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-sourcing", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def simulate_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    intent: str = "",
    simulator: Simulator | str | None = None,
    emit: Emit,
    enter: Enter,
) -> SimulationResult:
    """Verify the circuit's behaviour in SPICE against clauses derived from
    the intent. Opt-in; the caller holds the switch (the sourcing rule).

    Needs only the validated spec -- no placement, no copper -- so like the
    enclosure and the BOM it is started on a worker thread as soon as
    placement is done (:func:`start_simulation_stage`) and joined after
    them, before review, so a scripted model answers the three lanes in a
    fixed order. This body is what that thread runs; called directly it is
    the same stage, in line.

    ``simulator`` is the seam of :func:`~silkscreen.agents.simulate.
    simulate_circuit`: ``None`` finds ngspice on this machine, and the suite
    passes a fake so the verdict path runs with no simulator installed.

    Four of the five statuses are decided inside
    :func:`~silkscreen.agents.simulate.simulate_circuit` and reported on
    ``stage.done`` as facts, not failures: ``unsimulatable`` (a part with no
    behaviour, named), ``unavailable`` (no simulator, with the install
    hint), ``no_testbench`` (the model gave nothing usable within the
    budget) and ``ran``. The fifth is this stage's: a
    :class:`~silkscreen.spice.errors.SpiceError` from the simulator -- a
    singular matrix, a timeout, a strict-promoted warning -- or a
    ``ValueError`` that escaped the loop is caught here, surfaced as a
    visible ``simulation.failed`` event, and answered with status
    ``"failed"`` naming it. The board is still the product. Everything
    else, callback exceptions and :class:`~silkscreen.agents.model.
    ModelError` included, propagates as it does from every other stage.

    **A failed clause never fails the run.** It becomes a finding on the
    result (``blocker`` if the model marked the clause critical, else
    ``warning``), kept on ``result.findings`` rather than the critic's list
    because the two have different provenance and the critic's
    :class:`~silkscreen.agents.review.Finding` has no field to say so.
    """
    enter("simulation")
    emit({"event": "stage.start", "stage": "simulation"})
    try:
        result = simulate_circuit(
            agent_model, spec, intent=intent, simulator=simulator, on_event=emit
        )
    except (SpiceError, ValueError) as exc:
        emit({"event": "simulation.failed", "error": str(exc)[:160]})
        result = SimulationResult(
            status="failed",
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            warnings=[f"circuit was not simulated: {str(exc)[:200]}"],
        )
    emit(
        {
            "event": "stage.done",
            "stage": "simulation",
            "status": result.status,
            "passed": result.passed,
            "clauses": len(result.clauses),
            "failed": len(result.failed),
            "blockers": len(result.blockers),
            "parts": list(result.parts),
            "warnings": len(result.warnings),
        }
    )
    return result


class SimulationJob:
    """The simulation stage in flight on its own thread, or already settled.

    :meth:`result` joins the thread and then either returns what the stage
    returned or re-raises exactly what it raised (the :class:`SourcingJob`
    semantics, exactly). A job that was never started settles immediately
    with ``None`` and no events.
    """

    def __init__(self, thread: threading.Thread | None = None) -> None:
        self._thread = thread
        self._result: SimulationResult | None = None
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self) -> None:
        """Block until the stage has finished. Raises nothing; idempotent."""
        if self._thread is not None:
            self._thread.join()

    def result(self) -> SimulationResult | None:
        self.wait()
        if self._error is not None:
            raise self._error
        return self._result

    def _run(self, work: Callable[[], SimulationResult]) -> None:
        try:
            self._result = work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised at the join
            self._error = exc


def start_simulation_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    intent: str = "",
    simulator: Simulator | str | None = None,
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
) -> SimulationJob:
    """Run :func:`simulate_stage` on a worker thread.

    Called right after placement, beside the enclosure and the BOM, so the
    circuit is being verified while the sheet is drawn and the copper laid.
    The spec is immutable from ``propose`` onward, so no snapshot is taken.
    ``emit`` and ``enter`` are called from the worker thread under the same
    rules as :func:`start_enclosure_stage`: the thread's events carry
    ``stage: "simulation"`` whatever the main thread is doing. ``daemon``
    marks the thread so an owner that may forget the job never keeps the
    process alive for it; a driver that always joins leaves it off.
    """
    job = SimulationJob()

    def work() -> SimulationResult:
        return simulate_stage(
            agent_model,
            spec,
            intent=intent,
            simulator=simulator,
            emit=emit,
            enter=enter,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-simulation", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def review_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts],
    review: bool,
    refute: bool = False,
    emit: Emit,
    enter: Enter,
) -> ReviewReport:
    """Argue against the design. Skipped entirely -- and silently -- if off.

    Returns a :class:`~silkscreen.agents.review.ReviewReport` rather than a
    bare list because the three outcomes -- ran and found nothing, ran and
    could not be read, never ran -- are three different facts and a list
    length can only carry one of them. A :class:`~silkscreen.agents.review.
    ReviewError` is caught here and reported: the board is the product and a
    critic that answered gibberish must not destroy it, which is exactly the
    isolation the sourcing stage applies to its own model failure. What it
    must not do is come back looking clean, so the failure rides the report,
    a ``review.failed`` event, and every consumer that renders a verdict.
    """
    if not review:
        return ReviewReport(status=ReviewStatus.SKIPPED)
    enter("review")
    emit({"event": "stage.start", "stage": "review"})
    try:
        outcome = run_review(agent_model, spec, facts=facts, refute=refute)
    except ReviewError as exc:
        detail = str(exc)
        emit({"event": "review.failed", "stage": "review", "detail": detail[:200]})
        emit(
            {
                "event": "stage.done",
                "stage": "review",
                "findings": 0,
                "blockers": 0,
                "status": ReviewStatus.FAILED.value,
            }
        )
        return ReviewReport(status=ReviewStatus.FAILED, detail=detail)
    for line in outcome.dropped:
        # Said out loud: a critic answer the filter had to throw away is not
        # the same as a critic that found nothing there.
        emit({"event": "review.dropped", "stage": "review", "detail": line[:200]})
    for line in outcome.merged:
        # A merge hides a row the critic wrote, exactly as a drop does, so it
        # is announced on the same terms rather than only counted.
        emit({"event": "review.merged", "stage": "review", "detail": line[:200]})
    emit(
        {
            "event": "stage.done",
            "stage": "review",
            "findings": len(outcome.findings),
            "blockers": sum(
                1 for f in outcome.findings if f.severity is Severity.BLOCKER
            ),
            "dropped": len(outcome.dropped),
            "merged": len(outcome.merged),
            "status": ReviewStatus.OK.value,
        }
    )
    return ReviewReport(
        status=ReviewStatus.OK,
        findings=outcome.findings,
        dropped=outcome.dropped,
        merged=outcome.merged,
    )


class ReviewJob:
    """The critic in flight on its own thread, or already settled.

    The same shape as :class:`EnclosureJob`: :meth:`result` joins and either
    returns the report or re-raises exactly what the thread raised, so a
    failure in the background means what it would have meant in line. A job
    that was never started (``review=False``) settles at once with the
    ``SKIPPED`` report :func:`review_stage` would have returned, and emits
    nothing -- the review-off no-op preserved.
    """

    def __init__(self, thread: threading.Thread | None = None) -> None:
        self._thread = thread
        self._result: ReviewReport = ReviewReport(status=ReviewStatus.SKIPPED)
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self) -> None:
        """Block until the stage has finished. Raises nothing; idempotent."""
        if self._thread is not None:
            self._thread.join()

    def result(self) -> ReviewReport:
        self.wait()
        if self._error is not None:
            raise self._error
        return self._result

    def _run(self, work: Callable[[], ReviewReport]) -> None:
        try:
            self._result = work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised at the join
            self._error = exc


def start_review_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts],
    review: bool,
    refute: bool = False,
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
) -> ReviewJob:
    """Run :func:`review_stage` on a worker thread, starting at the spec.

    The critic reads the validated :class:`~silkscreen.netlist.CircuitSpec`
    and the datasheet facts and nothing else -- not the placement, not the
    schematic, not the copper. It was nonetheless the last thing a run did,
    so its whole latency sat on the tail, after the board was otherwise
    finished, and (with sourcing on) behind the sourcing join as well.
    Started here it overlaps :func:`place_stage`, whose CP-SAT budget is the
    effort level's (5 s by default) and which makes no model call at all, so
    the critic costs nothing the engineer waits for.

    **Determinism.** The driver joins this job before it starts the enclosure,
    sourcing and simulation lanes, so exactly one ``agent_model`` call is ever
    in flight while the critic runs: place and placement repair call the
    worker model not at all (placement repair has its own ``model``/
    ``fallback_model`` seam). The model-call order becomes read, plan,
    propose, review, then the three background lanes -- one fixed sequence, so
    a :class:`~silkscreen.agents.model.ScriptedModel` answers a run the same
    way twice and the SDK and ADK drivers agree.

    With ``review`` off nothing is started, no thread exists and no event is
    emitted; the job settles immediately on the ``SKIPPED`` report.
    ``daemon`` marks the thread so an owner that may abandon the job never
    keeps the process alive for it; a driver that always joins leaves it off.
    """
    if not review:
        return ReviewJob()
    job = ReviewJob()

    def work() -> ReviewReport:
        return review_stage(
            agent_model,
            spec,
            facts=facts,
            review=True,
            refute=refute,
            emit=emit,
            enter=enter,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-review", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job

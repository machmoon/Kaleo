"""Adapter between canonical nanometre boards and the placement verifier."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from ..board import DEFAULT_BOARD_MARGIN_NM, BoardResult
from ..units import mm, to_mm
from .agent import PlacementAgent, PlacementPolicyError, PlacementRun, TextModel
from .api import apply_feedback
from .pcb_repair import Board, CompanyProfile, Component, evaluate, get_profile

__all__ = [
    "GeneratedPlacement",
    "apply_verified_board",
    "repair_generated_board",
    "verifier_board",
]


@dataclass(frozen=True)
class GeneratedPlacement:
    """A verifier run and the canonical board produced from its accepted moves."""

    board: BoardResult
    run: PlacementRun
    requested_policy: str
    applied: bool
    policy_fallback: dict[str, str] | None = None
    #: The verifier-gated model attempt retained when deterministic recovery
    #: replaces it.  This is separate from ``run`` so the applied board and its
    #: receipts stay honest while explicit trace consent can still preserve the
    #: failed proposal for post-training.
    attempted_run: PlacementRun | None = None


#: The distance ``board.emit_kicad_pcb`` puts between the packing rectangle
#: and the ``Edge.Cuts`` outline it actually draws, on every side.  A
#: ``BoardResult``'s ``width_nm``/``height_nm`` are the *packing* rectangle,
#: not the board; the board KiCad opens is this much larger in each direction.
#:
#: Getting this wrong is the whole reason the constant is named here.  The
#: verifier used to judge boundary legality against the packing rectangle,
#: which meant a part flush against it was reported as crossing an edge that
#: is 2 mm further out in the file -- a violation with no physical existence,
#: and one the adapter then "fixed" by growing the shipped board.  OpenROAD
#: refuses the same mistake in ``src/dpl/src/CheckPlacement.cpp``: its
#: ``Opendp::overlap`` takes each cell's corner from
#: ``cell->getDbInst()->getLocation()`` (``Place.cpp::initialLocation``)
#: rather than from the grid the placer built, so a placer that reserved the
#: wrong box cannot hand the checker its own arithmetic and be believed.
#: Here the authoritative frame is the one the emitter writes, so that is the
#: frame the verifier is given.
OUTLINE_MARGIN_MM = to_mm(DEFAULT_BOARD_MARGIN_NM)


def verifier_board(board: BoardResult) -> Board:
    """Project a canonical board into the verifier's bounded millimetre frame.

    The frame is the emitted ``Edge.Cuts`` rectangle with its lower-left
    corner at the origin, so every coordinate is the packing coordinate plus
    :data:`OUTLINE_MARGIN_MM`.  :func:`apply_verified_board` subtracts it
    again; the pair is the only place this offset exists, the way
    ``kicad.py`` owns the single Y flip.
    """
    components = []
    for part in board.parts:
        footprint = part.footprint
        components.append(
            Component(
                ref=part.ref,
                x=to_mm(part.x_nm) + OUTLINE_MARGIN_MM,
                y=to_mm(part.y_nm) + OUTLINE_MARGIN_MM,
                width=to_mm(2 * footprint.courtyard_w_nm),
                height=to_mm(2 * footprint.courtyard_h_nm),
                angle=90 if part.rotated else 0,
                kind="power" if part.ref.startswith(("Q", "U")) else "component",
            )
        )
    return Board(
        width=to_mm(board.width_nm) + 2 * OUTLINE_MARGIN_MM,
        height=to_mm(board.height_nm) + 2 * OUTLINE_MARGIN_MM,
        components=tuple(components),
    )


def apply_verified_board(board: BoardResult, verified: Board) -> BoardResult:
    """Write verifier positions back without changing electrical identity."""
    if board.tracks or board.vias:
        raise ValueError("placement repair must run before copper routing")
    by_ref = {component.ref: component for component in verified.components}
    expected = {part.ref for part in board.parts}
    if set(by_ref) != expected:
        raise ValueError("verified placement changed the board's reference set")

    original = verifier_board(board)
    original_by_ref = {
        component.ref: component for component in original.components
    }
    placement_changed = (
        verified.width != original.width
        or verified.height != original.height
        or any(by_ref[ref] != original_by_ref[ref] for ref in expected)
    )

    parts = []
    for part in board.parts:
        component = by_ref[part.ref]
        if component.angle not in (0, 90):
            raise ValueError(
                f"verified placement angle {component.angle} for {part.ref} "
                "cannot be represented by the canonical board"
            )
        parts.append(
            replace(
                part,
                x_nm=mm(component.x - OUTLINE_MARGIN_MM),
                y_nm=mm(component.y - OUTLINE_MARGIN_MM),
                rotated=component.angle == 90,
            )
        )
    return replace(
        board,
        parts=parts,
        width_nm=mm(verified.width - 2 * OUTLINE_MARGIN_MM),
        height_nm=mm(verified.height - 2 * OUTLINE_MARGIN_MM),
        # CP-SAT computed this for its original coordinates.  Once the verifier
        # moves a part or grows the outline, retaining that objective would
        # expose a precise-looking but incorrect metric to API clients.
        wirelength_nm=None if placement_changed else board.wirelength_nm,
    )


def _profile_frame(board: Board, profile: CompanyProfile) -> Board:
    """Grow/translate a tight solver outline only as much as the profile needs.

    CP-SAT owns component packing and may return an outline whose residual edge
    gap is smaller than a company profile. Moving parts alone cannot make a
    component fit inside a board narrower than the component plus both required
    margins, so the verifier's frame must be allowed to grow before repair.
    Existing surplus is preserved; this is the minimum translation and growth
    that makes boundary legality possible.
    """
    if not board.components:
        return board
    rects = [component.rect() for component in board.components]
    min_x = min(rect[0] for rect in rects)
    min_y = min(rect[1] for rect in rects)
    shift_x = max(0.0, profile.edge_margin - min_x)
    shift_y = max(0.0, profile.edge_margin - min_y)
    max_x = max(rect[2] for rect in rects) + shift_x
    max_y = max(rect[3] for rect in rects) + shift_y
    width = max(board.width, max_x + profile.edge_margin)
    height = max(board.height, max_y + profile.edge_margin)
    framed = Board(
        width=width,
        height=height,
        components=tuple(
            replace(component, x=component.x + shift_x, y=component.y + shift_y)
            for component in board.components
        ),
        keepouts=tuple(
            replace(keepout, x=keepout.x + shift_x, y=keepout.y + shift_y)
            for keepout in board.keepouts
        ),
    )
    # A CP-SAT outline can meet its own smaller spacing rule while leaving no
    # free lane for the stricter profile clearance. Give the deterministic
    # repair bounded search room proportional to the board's packing scale.
    if any(
        violation.kind == "clearance"
        for violation in evaluate(framed, profile).violations
    ):
        slack = profile.clearance * math.ceil(math.sqrt(len(framed.components)))
        framed = replace(
            framed,
            width=framed.width + slack,
            height=framed.height + slack,
        )
    return framed


def repair_generated_board(
    board: BoardResult,
    *,
    profile: str | CompanyProfile = "compact-control",
    policy: str = "deterministic",
    feedback: dict[str, Any] | None = None,
    model: TextModel | None = None,
    fallback_model: TextModel | None = None,
    max_turns: int = 8,
) -> GeneratedPlacement:
    """Repair a generated board, falling back to deterministic policy safely."""
    selected = get_profile(profile) if isinstance(profile, str) else profile
    selected = apply_feedback(selected, feedback)
    placement_board = _profile_frame(verifier_board(board), selected)
    fallback: dict[str, str] | None = None
    attempted_run: PlacementRun | None = None
    try:
        run = PlacementAgent(
            model,
            fallback_model=fallback_model,
            max_turns=max_turns,
        ).run(placement_board, selected, policy=policy)
    except PlacementPolicyError:
        run = PlacementAgent().run(placement_board, selected, policy="deterministic")
        fallback = {
            "from": policy,
            "to": "deterministic",
            "reason": "proposal backend failed",
        }

    representable = all(
        component.angle in (0, 90) for component in run.board.components
    )
    if run.completed and not representable and policy != "deterministic":
        attempted_run = run
        run = PlacementAgent().run(placement_board, selected, policy="deterministic")
        fallback = {
            "from": policy,
            "to": "deterministic",
            "reason": "proposal backend returned an unsupported rotation",
        }
    elif not run.completed and policy != "deterministic":
        attempted_run = run
        run = PlacementAgent().run(placement_board, selected, policy="deterministic")
        fallback = {
            "from": policy,
            "to": "deterministic",
            "reason": "proposal backend did not complete repair",
        }

    # A bounded repair may honestly run out of evaluations. Keep the already
    # legal canonical placement rather than writing a verifier-incomplete one.
    applied = run.completed
    updated = apply_verified_board(board, run.board) if applied else board
    return GeneratedPlacement(
        board=updated,
        run=run,
        requested_policy=policy,
        applied=applied,
        policy_fallback=fallback,
        attempted_run=attempted_run,
    )

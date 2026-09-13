"""The placement verifier's frame, checked against the file KiCad opens.

``engine/silkscreen/placement/`` sits between CP-SAT and the emitters, and
everything it decides rests on one projection: :func:`verifier_board` turns a
``BoardResult`` into rectangles in millimetres.  Nothing pinned that
projection, and it was wrong -- it judged boundary legality against the
*packing* rectangle while ``board.emit_kicad_pcb`` draws ``Edge.Cuts`` two
millimetres further out on every side, so every generated board arrived
carrying boundary violations with no physical existence.

Every expectation below is therefore computed by a **second, independent
reader of the emitted ``.kicad_pcb``** -- :mod:`silkscreen.audit.geometry`,
which parses absolute coordinates on its own and calls neither ``board.py``
nor the placement package.  A check written in terms of the code that
produced the geometry shares its blind spots; this is the same discipline
``engine/tests/test_kicad.py`` uses for courtyard overlap and the reason
``audit/geometry.py`` exists at all.

Prior art the design follows, named so it can be checked:

* OpenROAD, ``src/dpl/src/CheckPlacement.cpp``.  ``Opendp::checkPlacement``
  calls ``importDb()`` and ``initGrid()`` to rebuild its own view, and the
  verdict in ``Opendp::overlap`` comes from ``initialLocation()``, i.e.
  ``cell->getDbInst()->getLocation()`` -- the database, not the grid the
  placer built.  A placer that reserved the wrong box does not get to hand
  the checker its own arithmetic.
* KiCad, ``pcbnew/drc/drc_test_provider_courtyard_clearance.cpp``.
  ``testCourtyardClearances()`` reads each footprint's ``F_CrtYd`` polygon
  and reports ``DRCE_OVERLAPPING_FOOTPRINTS`` when the two collide at the
  applicable clearance.  That is the quantity ``min_courtyard_gap_nm`` below
  measures, so a negative gap here is a violation KiCad would also raise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from silkscreen.audit.geometry import AuditBoard, load_audit_board
from silkscreen.board import (
    DEFAULT_BOARD_MARGIN_NM,
    BoardResult,
    build_board,
    write_board,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.placement.adapter import (
    OUTLINE_MARGIN_MM,
    apply_verified_board,
    repair_generated_board,
    verifier_board,
)
from silkscreen.placement.pcb_repair import (
    CompanyProfile,
    evaluate,
    get_profile,
)
from silkscreen.units import mm, to_mm

PROFILE = "compact-control"


def _spec():
    """The TODO.txt LDO demo prompt, as a literal spec so no model is needed."""
    return parse_circuit_spec({
        "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
        "passives": {
            "c_in": {"type": "capacitor", "value": "10uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
            "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2"],
            "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
        },
    })


def _rotatable_spec():
    """Two-terminal parts the solver may turn, so rotation is exercised."""
    return parse_circuit_spec({
        "devices": {
            "NE555D": {
                "pins": {
                    "GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                    "CTRL": "5", "THRES": "6", "DISCH": "7", "VCC": "8",
                },
            }
        },
        "passives": {
            "r1": {"type": "resistor", "value": "68k"},
            "r2": {"type": "resistor", "value": "68k"},
            "c_t": {"type": "capacitor", "value": "10uF"},
            "c_dec": {"type": "capacitor", "value": "100nF"},
        },
        "nets": {
            "+9V": ["NE555D.VCC", "NE555D.RESET", "r1.1", "c_dec.1"],
            "GND": ["NE555D.GND", "c_t.2", "c_dec.2"],
            "DISCH": ["NE555D.DISCH", "r1.2", "r2.1"],
            "THRES": ["NE555D.THRES", "NE555D.TRIG", "r2.2", "c_t.1"],
        },
    })


# ---------------------------------------------------- the independent reader


def _audit(board: BoardResult, tmp_path: Path, name: str) -> AuditBoard:
    path = tmp_path / f"{name}.kicad_pcb"
    write_board(board, path)
    return load_audit_board(path)


def _courtyards(audit: AuditBoard) -> dict[str, tuple[int, int, int, int]]:
    """Every courtyard as ``(x0, y0, x1, y1)`` nanometres, KiCad Y-down."""
    return {
        part.ref: (
            part.courtyard.x0,
            part.courtyard.y0,
            part.courtyard.x1,
            part.courtyard.y1,
        )
        for part in audit.parts
        if part.courtyard is not None
    }


def _min_courtyard_gap_nm(audit: AuditBoard) -> int:
    """The quantity KiCad's courtyard-clearance provider measures."""
    parts = [part for part in audit.parts if part.courtyard is not None]
    return min(
        first.courtyard.gap_to(second.courtyard)
        for index, first in enumerate(parts)
        for second in parts[index + 1 :]
    )


def _min_edge_clearance_nm(audit: AuditBoard) -> int:
    outline = audit.outline
    assert outline is not None, "an emitted board must carry Edge.Cuts"
    return min(
        min(
            part.courtyard.x0 - outline.x0,
            part.courtyard.y0 - outline.y0,
            outline.x1 - part.courtyard.x1,
            outline.y1 - part.courtyard.y1,
        )
        for part in audit.parts
        if part.courtyard is not None
    )


# ------------------------------------------------------------------- the tests


@pytest.mark.parametrize(
    ("spec_factory", "rotatable"),
    [(_spec, frozenset()), (_rotatable_spec, frozenset({"R1", "R2", "C1", "C2"}))],
)
def test_the_projected_rectangles_are_the_ones_in_the_emitted_file(
    tmp_path: Path, spec_factory, rotatable
) -> None:
    """Every verifier rectangle equals the ``F.CrtYd`` box read back off disk.

    Rotation included: the solver may turn a two-terminal part, and the
    projection carries that as ``angle=90`` with unswapped width/height, so a
    swap done in the wrong place would show up here as a transposed box.
    """
    board = build_board(
        spec_factory(),
        time_limit_s=15.0,
        # build_board makes an unknown rotatable ref a hard error, the
        # edge_refs convention, so each case names only its own parts.
        rotatable_refs=set(rotatable),
    )
    projected = verifier_board(board)
    audit = _audit(board, tmp_path, "placed")
    courtyards = _courtyards(audit)
    height_nm = audit.outline.y1 - audit.outline.y0

    assert set(courtyards) == {component.ref for component in projected.components}
    for component in projected.components:
        x0, y0, x1, y1 = courtyards[component.ref]
        # audit is KiCad Y-down with the outline corner at (-margin, -margin);
        # the verifier frame is Y-up with that corner at the origin.
        expected = (
            to_mm(x0 - audit.outline.x0),
            to_mm(audit.outline.y1 - y1),
            to_mm(x1 - audit.outline.x0),
            to_mm(height_nm - (y0 - audit.outline.y0)),
        )
        assert component.rect() == pytest.approx(expected, abs=1e-6)

    assert projected.width == pytest.approx(to_mm(audit.outline.x1 - audit.outline.x0))
    assert projected.height == pytest.approx(to_mm(height_nm))


def test_the_frame_offset_is_the_margin_the_emitter_actually_draws(
    tmp_path: Path,
) -> None:
    """``OUTLINE_MARGIN_MM`` is measured off the file, not asserted from itself."""
    board = build_board(_spec(), time_limit_s=15.0)
    audit = _audit(board, tmp_path, "placed")

    measured_nm = -audit.outline.x0
    assert measured_nm == -audit.outline.y0 == DEFAULT_BOARD_MARGIN_NM
    assert mm(OUTLINE_MARGIN_MM) == measured_nm
    assert audit.outline.x1 - audit.outline.x0 == board.width_nm + 2 * measured_nm


def test_projecting_and_writing_back_leaves_the_board_untouched() -> None:
    """The offset lives in exactly two places and they are each other's inverse.

    ``kicad.py`` owns the single Y flip for the same reason: an offset applied
    on one side and forgotten on the other is a whole board's worth of parts
    two millimetres away from where the solver put them.
    """
    board = build_board(_spec(), time_limit_s=15.0)

    updated = apply_verified_board(board, verifier_board(board))

    assert [(p.ref, p.x_nm, p.y_nm, p.rotated) for p in updated.parts] == [
        (p.ref, p.x_nm, p.y_nm, p.rotated) for p in board.parts
    ]
    assert (updated.width_nm, updated.height_nm) == (board.width_nm, board.height_nm)
    # Nothing moved, so CP-SAT's objective is still true of this board.
    assert updated.wirelength_nm == board.wirelength_nm


def test_a_solved_board_carries_no_boundary_violation_it_does_not_have(
    tmp_path: Path,
) -> None:
    """A part flush against the packing rectangle still clears the real edge.

    This is the regression the frame fix exists for.  Judged against the
    packing rectangle, every board CP-SAT produced reported boundary
    violations; judged against the outline in the file, the profile's edge
    margin is satisfied with room to spare, and the independent reader is
    what says so.
    """
    board = build_board(_spec(), time_limit_s=15.0)
    profile = get_profile(PROFILE)
    audit = _audit(board, tmp_path, "placed")

    assert to_mm(_min_edge_clearance_nm(audit)) >= profile.edge_margin

    boundary = [
        violation
        for violation in evaluate(verifier_board(board), profile).violations
        if violation.kind == "boundary"
    ]
    assert boundary == []


def test_an_applied_repair_is_legal_on_the_board_that_gets_written(
    tmp_path: Path,
) -> None:
    """``hard == 0`` has to survive being measured by something else.

    A verifier that agrees with the placer by construction is decoration, so
    the claim is re-checked in the emitted file's own coordinates against the
    two quantities KiCad checks: courtyard clearance
    (``DRCE_OVERLAPPING_FOOTPRINTS``) and distance to ``Edge.Cuts``.
    """
    board = build_board(_spec(), time_limit_s=15.0)
    profile = get_profile(PROFILE)

    result = repair_generated_board(board, profile=PROFILE)

    assert result.applied is True
    assert evaluate(result.run.board, profile).hard == 0

    audit = _audit(result.board, tmp_path, "repaired")
    assert to_mm(_min_courtyard_gap_nm(audit)) >= profile.clearance
    assert to_mm(_min_edge_clearance_nm(audit)) >= profile.edge_margin


def test_an_incomplete_repair_is_never_written_to_the_board() -> None:
    """A repair that cannot finish is visible and not applied.

    OpenROAD makes the same distinction rather than placing illegally:
    ``Opendp::diamondSearch`` returns an empty ``PixelPt()`` when its
    displacement budget is exhausted, ``diamondMove`` declines to place, and
    the cell is named in ``placement_failures_`` (``src/dpl/src/Place.cpp``)
    before ``detailedPlacement`` reports and stops.  Here the equivalent of
    "named, not placed" is ``applied is False`` with the failing run still
    attached for inspection.

    Reaching it takes a deliberately unsatisfiable profile: two parts pinned
    by ``fixed_refs`` at a clearance no arrangement can deliver, because
    ``_profile_frame`` grows the outline until every *movable* part can be
    made legal.  That is worth stating plainly -- on a board CP-SAT actually
    produced, with a profile that pins nothing, this branch does not fire.
    """
    board = build_board(_spec(), time_limit_s=15.0)
    refs = tuple(part.ref for part in board.parts)[:2]
    pinned = CompanyProfile(
        name="pinned",
        clearance=50.0,
        edge_margin=0.8,
        fixed_refs=refs,
    )

    result = repair_generated_board(board, profile=pinned)

    assert result.run.completed is False
    assert result.applied is False
    assert [(p.ref, p.x_nm, p.y_nm) for p in result.board.parts] == [
        (p.ref, p.x_nm, p.y_nm) for p in board.parts
    ]
    # The failure is reported, not swallowed: the run names what is wrong and
    # which parts are involved, the way CheckPlacement.cpp's per-category
    # ``reportFailures`` names its cells.
    violations = evaluate(result.run.board, pinned).violations
    assert violations
    assert set(refs) <= {ref for violation in violations for ref in violation.refs}

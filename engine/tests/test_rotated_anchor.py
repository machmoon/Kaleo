"""Rotated-footprint geometry.

The bug these cover was recorded and left open for a day: ``emit_kicad_pcb``
placed a rotated footprint's anchor without swapping the courtyard half-extents,
so the anchor came out short by exactly ``(ch-cw, cw-ch)``. Nothing raised. The
file parsed, the courtyard drew, the net list was right -- and every pad on that
part sat somewhere the router did not think it was.

That is why the checks here never ask ``board.py`` where a pad is. They ask
``kiutils``, which reads the written file the way KiCad does, and apply the
rotation independently in :func:`pads_from_file`. A test that called
``board_pads`` to decide where a pad *should* be would have agreed with the bug.
"""

from __future__ import annotations

import math

import pytest
from kiutils.board import Board
from silkscreen.board import (
    BoardResult,
    board_pads,
    build_board,
    emit_kicad_pcb,
    part_anchor,
    placed_half_extents,
    route_board,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.units import mm

#: Written coordinates are millimetre decimals; 1 nm of slack absorbs the
#: emitter's 4-decimal formatting without hiding a real offset.
EPS_MM = 1e-4

# An SOIC-8 is the useful shape here: its courtyard is markedly taller than it
# is wide, so a 90-degree turn moves the anchor a long way and a missing swap
# cannot hide inside rounding.
SPEC = {
    "devices": {"DRIVER": {"pins": {f"P{i}": str(i) for i in range(1, 9)}}},
    "passives": {
        "Cby": {"type": "capacitor", "value": "100nF"},
        "Rs": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VCC": ["DRIVER.P8", "Cby.1"],
        "GND": ["DRIVER.P4", "Cby.2"],
        "OUT": ["DRIVER.P1", "Rs.1"],
        "FB": ["DRIVER.P2", "Rs.2"],
    },
}


def build(rotatable=frozenset()):
    spec = parse_circuit_spec(SPEC)
    return build_board(spec, time_limit_s=5.0, rotatable_refs=set(rotatable))


def rotated_board() -> BoardResult:
    """A board with one part deliberately turned 90 degrees.

    Built by hand rather than by asking the solver to rotate something. The
    solver rotates only when it pays, so a spec that happens not to need it
    produces an all-upright board and every assertion below passes vacuously --
    which is exactly what happened on the first draft of these tests. Hand
    placement makes the rotated case unconditional.

    ``x_nm``/``y_nm`` is the bottom-left of the box the placer *would* have
    reserved, so the swapped extents are baked into the fixture's coordinates
    the same way they come out of ``pack``.
    """
    spec = parse_circuit_spec(SPEC)
    board = build_board(spec, time_limit_s=5.0)

    turned = next(p for p in board.parts if p.ref == "U1")
    fp = turned.footprint
    assert fp.courtyard_w_nm != fp.courtyard_h_nm, (
        "a square courtyard cannot show an extent swap; pick another part"
    )
    turned.rotated = True

    # Re-lay the parts left to right on the swapped extents so nothing
    # overlaps and every courtyard stays inside the board.
    cursor = 0
    tallest = 0
    for part in board.parts:
        half_w, half_h = placed_half_extents(part)
        part.x_nm = cursor
        part.y_nm = 0
        cursor += half_w * 2 + mm(1.0)
        tallest = max(tallest, half_h * 2)
    board.width_nm = cursor - mm(1.0)
    board.height_nm = tallest
    return board


def pads_from_file(text: str) -> dict[tuple[str, str], tuple[float, float, float]]:
    """``{(ref, pad): (x, y, angle)}`` in board millimetres, read back.

    Applies the footprint's own rotation to each pad offset here rather than
    trusting anything in ``silkscreen.board``: KiCad turns a footprint
    counter-clockwise on screen with Y pointing down, so a local point maps
    through a rotation by ``-angle``.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "b.kicad_pcb"
        path.write_text(text, encoding="utf-8")
        brd = Board().from_file(str(path))

    out = {}
    for fp in brd.footprints:
        ref = fp.properties.get("Reference", fp.libraryNickname)
        angle = fp.position.angle or 0
        r = math.radians(angle)
        c, s = math.cos(r), math.sin(r)
        for pad in fp.pads:
            px, py = pad.position.X, pad.position.Y
            ox, oy = px * c + py * s, -px * s + py * c
            out[(ref, pad.number)] = (fp.position.X + ox, fp.position.Y + oy, angle)
    return out


def test_the_fixture_actually_rotates_something():
    """Guard the guard: a fixture that rotates nothing proves nothing.

    The first draft of this file asked the solver to rotate and asserted on
    whatever came back. The solver declined -- rotation is optional and that
    board did not need it -- so every rotation test passed against the broken
    emitter. This asserts the fixture is doing what its name says.
    """
    board = rotated_board()
    turned = [p for p in board.parts if p.rotated]
    assert turned, "the fixture produced no rotated part"
    fp = turned[0].footprint
    assert fp.courtyard_w_nm != fp.courtyard_h_nm, (
        "a square courtyard hides an extent swap"
    )


@pytest.mark.parametrize("rotatable", [frozenset(), frozenset({"U1"})])
def test_written_pads_land_where_the_router_was_told_they_are(rotatable):
    """The invariant the whole bug class violates.

    ``board_pads`` feeds the router; ``emit_kicad_pcb`` feeds the fab. If they
    disagree the run still reports success and the copper misses the copper.
    """
    board = rotated_board() if rotatable else build()
    written = pads_from_file(emit_kicad_pcb(board))

    for pad in board_pads(board):
        # board_pads is Y-up; the file is Y-down. One flip, applied here.
        want_x = pad.x_nm / 1e6
        want_y = (board.height_nm - pad.y_nm) / 1e6
        got_x, got_y, _ = written[(pad.ref, pad.number)]
        assert abs(got_x - want_x) < EPS_MM and abs(got_y - want_y) < EPS_MM, (
            f"{pad.ref} pad {pad.number}: router was told "
            f"({want_x:.4f}, {want_y:.4f}), file says ({got_x:.4f}, {got_y:.4f})"
        )


def test_a_rotated_parts_pads_stay_inside_its_reserved_box():
    """The placer reserved a box. The pads have to be in it.

    This is the check that fails loudly on the old ``(ch-cw, cw-ch)`` anchor:
    the courtyard the solver kept clear and the copper it was keeping clear
    *of* were in different places.
    """
    board = rotated_board()
    rotated = [p for p in board.parts if p.rotated]
    assert rotated, "fixture did not rotate"

    for part in rotated:
        half_w, half_h = placed_half_extents(part)
        lo_x, hi_x = part.x_nm, part.x_nm + half_w * 2
        lo_y, hi_y = part.y_nm, part.y_nm + half_h * 2
        mine: list[tuple[int, int]] = []
        for pad in board_pads(board):
            if pad.ref != part.ref:
                continue
            assert lo_x <= pad.x_nm <= hi_x and lo_y <= pad.y_nm <= hi_y, (
                f"{part.ref} pad {pad.number} at ({pad.x_nm}, {pad.y_nm}) is "
                f"outside its reserved box x[{lo_x},{hi_x}] y[{lo_y},{hi_y}]"
            )
            mine.append((pad.x_nm, pad.y_nm))

        # Inside the box is necessary but weak -- a small offset still fits.
        # The pad field is symmetric about the footprint anchor, so its centre
        # must land on the centre of the reserved box.
        cx = (min(x for x, _ in mine) + max(x for x, _ in mine)) / 2
        cy = (min(y for _, y in mine) + max(y for _, y in mine)) / 2
        assert abs(cx - (lo_x + hi_x) / 2) <= 1, (
            f"{part.ref}'s pads are off-centre in x by {cx - (lo_x + hi_x) / 2}"
        )
        assert abs(cy - (lo_y + hi_y) / 2) <= 1, (
            f"{part.ref}'s pads are off-centre in y by {cy - (lo_y + hi_y) / 2}"
        )


def test_rotation_swaps_the_half_extents_and_nothing_else():
    board = rotated_board()
    for part in board.parts:
        fp = part.footprint
        half_w, half_h = placed_half_extents(part)
        if part.rotated:
            assert (half_w, half_h) == (fp.courtyard_h_nm, fp.courtyard_w_nm)
        else:
            assert (half_w, half_h) == (fp.courtyard_w_nm, fp.courtyard_h_nm)
        # The anchor is the centre of the box the placer reserved.
        ax, ay = part_anchor(part)
        assert ax == part.x_nm + half_w
        assert ay == part.y_nm + half_h


def test_a_rotated_board_routes_instead_of_refusing():
    """Routing used to abort on any rotated part, because of this bug."""
    board = rotated_board()
    assert any(p.rotated for p in board.parts)
    result = route_board(board)
    assert not any("rotated" in w for w in result.warnings), result.warnings
    assert result.tracks, "a rotated board produced no copper at all"
    assert result.routed, "no net came out routed"


def test_a_rotated_board_still_writes_no_overlapping_courtyards():
    """The round-trip property, with rotation in play."""
    board = rotated_board()
    boxes = []
    for part in board.parts:
        half_w, half_h = placed_half_extents(part)
        ax, ay = part_anchor(part)
        boxes.append((ax - half_w, ay - half_h, ax + half_w, ay + half_h))
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            apart = a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
            assert apart, f"courtyards overlap: {a} and {b}"


def test_rotatable_refs_naming_an_unknown_part_is_an_error():
    """Silently ignoring it means the part quietly does not rotate."""
    spec = parse_circuit_spec(SPEC)
    with pytest.raises(ValueError, match="NOSUCH"):
        build_board(spec, time_limit_s=5.0, rotatable_refs={"NOSUCH"})


def test_an_unrotated_board_is_unchanged_by_the_fix():
    """The default path must be byte-identical to before."""
    first = emit_kicad_pcb(build())
    second = emit_kicad_pcb(build())
    assert first == second
    assert " 90)" not in first, "nothing should be rotated without opting in"


def test_pad_extents_swap_when_the_part_turns():
    """A pad on its side is a different rectangle for clearance purposes."""
    board = rotated_board()
    rotated = {p.ref for p in board.parts if p.rotated}
    assert rotated
    by_ref = {}
    for pad in board_pads(board):
        by_ref.setdefault(pad.ref, []).append(pad)
    for ref in rotated:
        part = next(p for p in board.parts if p.ref == ref)
        for pad in by_ref[ref]:
            source = part.footprint.pad_by_number(pad.number)
            assert (pad.w_nm, pad.h_nm) == (source.h_nm, source.w_nm)


def test_the_written_file_records_the_rotation_angle():
    board = rotated_board()
    written = pads_from_file(emit_kicad_pcb(board))
    angles = {ref: angle for (ref, _), (_, _, angle) in written.items()}
    for part in board.parts:
        assert angles[part.ref] == (90 if part.rotated else 0)


def test_the_board_is_not_bigger_than_the_margin_allows():
    """A rotated part must not push copper or courtyard off the outline."""
    board = rotated_board()
    margin = mm(2.0)
    for pad in board_pads(board):
        assert -margin <= pad.x_nm <= board.width_nm + margin
        assert -margin <= pad.y_nm <= board.height_nm + margin

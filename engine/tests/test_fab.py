"""Fabrication output: Gerber, Excellon, BOM and pick-and-place.

The claim this file exists to check is the one in ``fab.py``'s docstring: under
``%FSLAX46Y46*%`` with ``%MOMM*%`` one Gerber coordinate unit *is* one
nanometre, so the engine's integer geometry is written verbatim with no scaling
step. A scale error there is invisible in the file and fatal on the bench, so
the coordinate tests below compute the expected integer independently -- from
``PlacedPart`` and ``Pad`` -- and look for that exact literal in the output.

The second thing being pinned is the frame. ``emit_kicad_pcb`` flips Y because
KiCad's canvas is Y-down; Gerber is Y-up. Both writers describe the same board,
so a change to either that breaks the relation between them is a bug even
though each file on its own still parses.

Solver-derived boards use a short time limit; anything that does not need a
placement is built by hand so the test does not pay for CP-SAT.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal

import pytest
from silkscreen.board import BoardResult, PlacedPart, build_board, emit_kicad_pcb
from silkscreen.fab import (
    FabLayer,
    bom_csv,
    cpl_csv,
    excellon_drill,
    fab_files,
    gerber_copper,
    gerber_mask,
    gerber_outline,
    gerber_paste,
    gerber_silkscreen,
)
from silkscreen.footprints import Footprint, Pad, dual_row_header
from silkscreen.netlist import parse_circuit_spec
from silkscreen.packing import Layer
from silkscreen.routing import Via
from silkscreen.units import NM_PER_MM, mm

#: The outline margin every emitted coordinate is shifted by. Deliberately
#: written out rather than imported from ``fab``: the point of the coordinate
#: tests is to check the module against an independently stated number.
MARGIN_NM = mm(2.0)

#: Soldermask expansion per side that ``gerber_mask`` applies by default --
#: 2 mil, the industry-default opening enlargement.
DEFAULT_MASK_EXPANSION_NM = 51_000

GERBER_FILES = (
    "silkscreen-F_Cu.GTL",
    "silkscreen-B_Cu.GBL",
    "silkscreen-F_Mask.GTS",
    "silkscreen-B_Mask.GBS",
    "silkscreen-F_Paste.GTP",
    "silkscreen-B_Paste.GBP",
    "silkscreen-F_Silkscreen.GTO",
    "silkscreen-B_Silkscreen.GBO",
    "silkscreen-Edge_Cuts.GKO",
)

EXPECTED_FILES = GERBER_FILES + (
    "silkscreen-PTH.DRL",
    "silkscreen-BOM.csv",
    "silkscreen-CPL.csv",
)

#: A coordinate command: an operation code applied at an integer position.
_COORD_RE = re.compile(r"^X(-?\d+)Y(-?\d+)D0([123])\*$", re.M)
#: A rectangular aperture definition, e.g. ``%ADD10R,1.2X2.2*%``.
_RECT_RE = re.compile(r"^%ADD(\d+)R,(-?[\d.]+)X(-?[\d.]+)\*%$", re.M)
#: A round aperture definition, e.g. ``%ADD10C,0.1*%``.
_CIRCLE_RE = re.compile(r"^%ADD(\d+)C,(-?[\d.]+)\*%$", re.M)


def _spec():
    """The same circuit ``test_board.py`` places: a regulator, a driver, four
    passives -- enough shapes that pad offsets, rotation-free geometry and
    grouping all have something to bite on."""
    return parse_circuit_spec({
        "devices": {
            "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
            "DRV8837": {"pins": {"IN1": "1", "IN2": "2", "VM": "3", "GND": "4",
                                 "OUT1": "5", "OUT2": "6", "VCC": "7",
                                 "nSLEEP": "8"}},
        },
        "passives": {
            "c_in": {"type": "capacitor", "value": "22uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
            "c_dec": {"type": "capacitor", "value": "100nF"},
            "r_sleep": {"type": "resistor", "value": "10k"},
        },
        "nets": {
            "VIN": ["AMS1117-3.3.VIN", "c_in.1", "DRV8837.VM"],
            "GND": ["AMS1117-3.3.GND", "DRV8837.GND", "c_in.2", "c_out.2",
                    "c_dec.2"],
            "+3V3": ["AMS1117-3.3.VOUT", "DRV8837.VCC", "c_out.1", "c_dec.1",
                     "r_sleep.1"],
            "SLEEP": ["DRV8837.nSLEEP", "r_sleep.2"],
            "MOT": ["DRV8837.OUT1", "DRV8837.IN1"],
        },
    })


@pytest.fixture(scope="module")
def board():
    return build_board(_spec(), time_limit_s=4.0)


@pytest.fixture(scope="module")
def two_sided():
    return build_board(_spec(), two_sided=True, time_limit_s=4.0)


# ------------------------------------------------------------------- helpers


def _nm(mm_text: str) -> int:
    """A millimetre string from a file -> exact nanometres.

    Via ``Decimal`` rather than ``float`` so ``12.345`` cannot arrive as
    ``12345000.000000002`` and turn an exact comparison into an approximate one.
    """
    return int(Decimal(mm_text) * NM_PER_MM)


def _coords(gerber: str) -> list[tuple[int, int, int]]:
    """Every coordinate command as ``(x_nm, y_nm, opcode)``."""
    return [(int(x), int(y), int(op)) for x, y, op in _COORD_RE.findall(gerber)]


def _flashes(gerber: str) -> list[tuple[int, int]]:
    """Pad flashes (D03) in emission order."""
    return [(x, y) for x, y, op in _coords(gerber) if op == 3]


def _rect_apertures(gerber: str) -> list[tuple[int, int, int]]:
    """Rectangular apertures as ``(code, w_nm, h_nm)`` in definition order."""
    return [(int(c), _nm(w), _nm(h)) for c, w, h in _RECT_RE.findall(gerber)]


def _expected_flashes(board: BoardResult, *, bottom: bool) -> list[tuple[int, int]]:
    """Where every pad on one side should land, derived from the board alone.

    Written out in full rather than by calling ``fab``'s own helper: a check
    expressed in terms of the code under test shares its blind spot.
    """
    out = []
    for part in board.parts:
        if (part.layer is Layer.BOTTOM) != bottom:
            continue
        fp = part.footprint
        for pad in fp.pads:
            out.append((
                part.x_nm + fp.courtyard_w_nm + pad.x_nm + MARGIN_NM,
                part.y_nm + fp.courtyard_h_nm + pad.y_nm + MARGIN_NM,
            ))
    return out


def _kicad_anchors(text: str) -> dict[str, tuple[int, int]]:
    """Ref -> footprint anchor in nanometres, read back out of ``.kicad_pcb``."""
    out: dict[str, tuple[int, int]] = {}
    for block in text.split("  (footprint ")[1:]:
        at = re.search(r"^    \(at (\S+) (\S+)(?: \S+)?\)$", block, re.M)
        ref = re.search(r'\(property "Reference" "([^"]+)"', block)
        assert at and ref, "a footprint block must carry both an anchor and a ref"
        out[ref.group(1)] = (_nm(at.group(1)), _nm(at.group(2)))
    return out


def _cpl_rows(board: BoardResult) -> dict[str, list[str]]:
    """Ref -> parsed CPL row, through Python's own CSV reader."""
    rows = list(csv.reader(io.StringIO(cpl_csv(board))))
    return {row[0]: row for row in rows[1:]}


def _one_part_board(part: PlacedPart) -> BoardResult:
    """A BoardResult around one hand-placed part, with no solver involved."""
    return BoardResult(
        parts=[part],
        nets=[],
        width_nm=20_000_000,
        height_nm=20_000_000,
        solver_status="fallback",
    )


def _asym_footprint() -> Footprint:
    """A footprint whose pads are deliberately not symmetric about the anchor.

    Symmetric pads hide both mirroring and quarter-turn errors: they map onto
    themselves.
    """
    return Footprint(
        name="ASYM-2",
        pads=[
            Pad("1", -900_000, 0, 400_000, 900_000),
            Pad("2", 1_300_000, 500_000, 400_000, 900_000),
        ],
        courtyard_w_nm=2_000_000,
        courtyard_h_nm=1_000_000,
        body_w_nm=1_500_000,
        body_h_nm=500_000,
        description="asymmetric test part",
    )


# --------------------------------------------------------- structure and format


def test_fab_files_returns_twelve_uniquely_named_files(board):
    """A missing file reads as an oversight at the fab and stalls the order."""
    files = fab_files(board)
    assert all(isinstance(f, FabLayer) for f in files)
    names = [f.filename for f in files]
    assert len(files) == 12
    assert names == list(EXPECTED_FILES)
    assert len(set(names)) == 12, "two layers sharing a filename overwrite one another"


def test_every_gerber_opens_with_the_format_spec_and_closes_with_m02(board):
    """The format spec must be the first statement; M02 ends the stream."""
    by_name = {f.filename: f.content for f in fab_files(board)}
    for name in GERBER_FILES:
        lines = by_name[name].splitlines()
        assert lines[0] == "%FSLAX46Y46*%", f"{name} does not declare 4.6 format first"
        assert lines[-1] == "M02*", f"{name} does not terminate"
        assert by_name[name].endswith("\n"), f"{name} lacks a trailing newline"


def test_every_gerber_declares_millimetres_and_names_its_generator(board):
    by_name = {f.filename: f.content for f in fab_files(board)}
    for name in GERBER_FILES:
        content = by_name[name]
        assert "%MOMM*%" in content, f"{name} does not set units to mm"
        assert "G04 Generated by silkscreen*" in content, f"{name} has no generator"
        assert "%TF.GenerationSoftware,silkscreen,silkscreen*%" in content


def test_gerbers_contain_no_creation_timestamp(board):
    """A timestamp is the one attribute that would break byte-identical output."""
    for layer in fab_files(board):
        assert "CreationDate" not in layer.content, layer.filename


def test_excellon_is_well_formed_and_declares_no_tools(board):
    """Every footprint in the fixture circuit is SMD and nothing was routed, so
    the drill file is empty.

    Empty means empty: a tool definition with no hits, or a hit with no tool,
    is how a fab ends up drilling something that is not in the design.
    """
    text = excellon_drill(board)
    lines = text.splitlines()
    assert lines[0] == "M48", "an Excellon file opens with M48"
    assert lines[-1] == "M30", "an Excellon file ends with M30"
    assert "METRIC" in lines
    assert not re.search(r"^T\d+[CFS]", text, re.M), "no tool may be defined"
    assert not re.search(r"^T\d+\s*$", text, re.M), "no tool may be selected"
    assert not re.search(r"^X[-\d]", text, re.M), "an all-SMD design has no drill hits"

    # And it is the same file for any all-SMD board, since it describes no geometry.
    assert excellon_drill(_one_part_board(
        PlacedPart(ref="U1", footprint=_asym_footprint(), value="v")
    )) == text


def _excellon_hits(text: str) -> dict[int, list[tuple[int, int]]]:
    """Tool diameter (nm) -> the holes drilled with it, in file order."""
    tools: dict[str, int] = {}
    for number, diameter in re.findall(r"^T(\d+)C([\d.]+)$", text, re.M):
        tools[number] = _nm(diameter)
    hits: dict[int, list[tuple[int, int]]] = {}
    current: int | None = None
    for line in text.splitlines():
        select = re.fullmatch(r"T(\d+)", line)
        if select:
            current = tools[select.group(1)]
            hits.setdefault(current, [])
            continue
        hit = re.fullmatch(r"X(-?[\d.]+)Y(-?[\d.]+)", line)
        if hit:
            assert current is not None, f"hit {line!r} before any tool was selected"
            hits[current].append((_nm(hit.group(1)), _nm(hit.group(2))))
    return hits


def _header_and_via_board() -> BoardResult:
    """A 2x4 through-hole header and one via, placed by hand."""
    header = dual_row_header(8, row_spacing_mm=2.54)
    assert all(pad.drill_nm > 0 for pad in header.pads)
    part = PlacedPart(
        ref="J1", footprint=header, value="conn", x_nm=mm(3), y_nm=mm(4)
    )
    return BoardResult(
        parts=[part],
        nets=[],
        width_nm=mm(20),
        height_nm=mm(20),
        solver_status="fallback",
        vias=[Via(x_nm=mm(15), y_nm=mm(16), net="N", diameter_nm=mm(0.6),
                  drill_nm=mm(0.3))],
    )


def test_through_hole_pads_are_drilled_where_their_copper_is():
    """Regression: the drill program used to iterate vias only, so a board with
    a pin header shipped copper rings and no holes. Every hole here is computed
    from ``PlacedPart`` and ``Pad`` without going through ``fab``."""
    board = _header_and_via_board()
    part = board.parts[0]
    fp = part.footprint
    expected_pads = [
        (
            part.x_nm + fp.courtyard_w_nm + pad.x_nm + MARGIN_NM,
            part.y_nm + fp.courtyard_h_nm + pad.y_nm + MARGIN_NM,
        )
        for pad in fp.pads
    ]
    assert len(expected_pads) == 8
    assert len(set(expected_pads)) == 8, "a 2x4 header has eight distinct holes"

    hits = _excellon_hits(excellon_drill(board))
    assert set(hits) == {mm(1.0), mm(0.3)}, "one tool per distinct diameter"
    assert hits[mm(1.0)] == expected_pads
    assert hits[mm(0.3)] == [(mm(15) + MARGIN_NM, mm(16) + MARGIN_NM)]

    # Each hole sits at the centre of a flash on BOTH copper layers: a plated
    # hole with no ring on the back is the same bug in the other direction.
    for side in (False, True):
        flashes = set(_flashes(gerber_copper(board, bottom=side)))
        for hole in expected_pads:
            assert hole in flashes, f"hole {hole} has no copper on side {side}"
    # And no paste: a stencilled hole is a reflow defect.
    assert _flashes(gerber_paste(board)) == []
    assert _flashes(gerber_paste(board, bottom=True)) == []
    # Mask opens over the ring on both faces.
    assert len(_flashes(gerber_mask(board))) == 8
    assert len(_flashes(gerber_mask(board, bottom=True))) == 8


def test_rotated_header_holes_turn_with_their_pads():
    board = _header_and_via_board()
    turned = BoardResult(
        parts=[PlacedPart(ref="J1", footprint=board.parts[0].footprint,
                          value="conn", x_nm=mm(3), y_nm=mm(4), rotated=True)],
        nets=[], width_nm=mm(20), height_nm=mm(20), solver_status="fallback",
    )
    hits = _excellon_hits(excellon_drill(turned))
    assert set(hits[mm(1.0)]) == set(_flashes(gerber_copper(turned)))


def test_drill_file_is_named_pth_because_every_hole_is_plated():
    """A header pad carries copper on every layer and a via exists to join
    two, so the program is plated-through by definition. The KiCad/Excellon
    naming convention is ``-PTH.drl`` for it; ``-NPTH`` would tell a CAM
    operator to leave every via unplated."""
    names = [f.filename for f in fab_files(_header_and_via_board())]
    drills = [n for n in names if n.upper().endswith(".DRL")]
    assert drills == ["silkscreen-PTH.DRL"]
    assert not any("NPTH" in n for n in names)
    by_name = {f.filename: f.content for f in fab_files(_header_and_via_board())}
    assert len(re.findall(r"^X", by_name["silkscreen-PTH.DRL"], re.M)) == 9


# --------------------------------------------------------------- coordinates


def test_no_gerber_coordinate_is_negative(board, two_sided):
    """Some fabs reject a file containing a negative coordinate outright.

    The margin shift is what buys this, so it is checked on the geometry as
    written rather than on the shift arithmetic.
    """
    for source in (board, two_sided):
        by_name = {f.filename: f.content for f in fab_files(source)}
        for name in GERBER_FILES:
            body = [ln for ln in by_name[name].splitlines() if ln.startswith("X")]
            for line in body:
                match = _COORD_RE.match(line)
                assert match, f"{name}: unparseable coordinate {line!r}"
                x, y = int(match.group(1)), int(match.group(2))
                assert x >= 0 and y >= 0, f"{name}: negative coordinate {line!r}"


def test_outline_is_the_board_plus_two_millimetres_of_margin_on_every_side(board):
    """The profile is the same rectangle emit_kicad_pcb draws on Edge.Cuts,
    slid so its bottom-left corner sits exactly on the origin."""
    text = gerber_outline(board)
    width = board.width_nm + 2 * MARGIN_NM
    height = board.height_nm + 2 * MARGIN_NM

    points = {(x, y) for x, y, _ in _coords(text)}
    assert points == {(0, 0), (width, 0), (width, height), (0, height)}

    # Four closed segments, stroked with a single round pen.
    ops = [op for _, _, op in _coords(text)]
    assert ops == [2, 1] * 4, "the profile should be four move/draw pairs"
    circles = _CIRCLE_RE.findall(text)
    assert len(circles) == 1 and _nm(circles[0][1]) == mm(0.1)
    assert not _rect_apertures(text), "a profile is stroked, never flashed"


def test_one_gerber_unit_is_one_nanometre(board):
    """The central claim: 4.6 format in mm means integer nm are written verbatim.

    The expected integer is computed here from ``PlacedPart`` and ``Pad``
    without going through ``fab``, then looked for as a literal in the file. A
    micron- or millimetre-scaled writer produces a file that still parses, still
    has the right shape, and is 1000x wrong.
    """
    part = next(p for p in board.parts if p.ref == "U2")  # SOIC-8, asymmetric pads
    fp = part.footprint
    pad = fp.pad_by_number("1")
    assert pad is not None

    expected_x = part.x_nm + fp.courtyard_w_nm + pad.x_nm + MARGIN_NM
    expected_y = part.y_nm + fp.courtyard_h_nm + pad.y_nm + MARGIN_NM
    copper = gerber_copper(board)
    assert f"X{expected_x}Y{expected_y}D03*" in copper, (
        f"U2 pad 1 should flash at the literal integer "
        f"({expected_x}, {expected_y}) nm"
    )
    # Millions, not thousands: this is nanometres, not microns.
    assert expected_x > NM_PER_MM and expected_y > NM_PER_MM

    # And not just that one pad: every flash, in emission order.
    assert _flashes(copper) == _expected_flashes(board, bottom=False)

    # Aperture sizes are the pad sizes, in mm text.
    sizes = {(w, h) for _, w, h in _rect_apertures(copper)}
    assert sizes == {
        (pad.w_nm, pad.h_nm) for p in board.parts for pad in p.footprint.pads
    }


def test_gerber_is_y_up_where_kicad_is_y_down(board):
    """Two writers, one board, opposite handedness.

    ``emit_kicad_pcb`` emits ``board.height_nm - y`` and no margin shift; the
    Gerbers emit ``y`` shifted by the margin. Pinning the relation means a
    future edit to either writer cannot silently diverge from the other.
    """
    kicad = _kicad_anchors(emit_kicad_pcb(board))
    cpl = _cpl_rows(board)
    assert set(kicad) == set(cpl) == {p.ref for p in board.parts}

    for part in board.parts:
        kicad_x, kicad_y = kicad[part.ref]
        gerber_x = _nm(cpl[part.ref][1])
        gerber_y = _nm(cpl[part.ref][2])
        assert gerber_x - MARGIN_NM == kicad_x, f"{part.ref} X frame disagrees"
        assert gerber_y - MARGIN_NM == board.height_nm - kicad_y, (
            f"{part.ref}: gerber Y {gerber_y} and kicad Y {kicad_y} do not "
            f"describe the same point on a {board.height_nm} nm board"
        )

    # The flip is real, not a no-op: at least one part sits off-centre in Y, so
    # a writer that forgot to flip would fail the assertion above.
    assert any(
        _nm(cpl[p.ref][2]) - MARGIN_NM != kicad[p.ref][1] for p in board.parts
    ), "a board symmetric in Y would make this test vacuous"


# ------------------------------------------------------------ layers and sides


def test_single_sided_board_puts_every_pad_on_top_copper(board):
    """Nothing is placed on the underside, but the file for it still exists."""
    assert all(p.layer is not Layer.BOTTOM for p in board.parts)
    total_pads = sum(len(p.footprint.pads) for p in board.parts)

    top = gerber_copper(board)
    bottom = gerber_copper(board, bottom=True)
    assert len(_flashes(top)) == total_pads
    assert _flashes(bottom) == [], "no part is on the bottom, so no copper is"

    # Empty, but still a Gerber a CAM step will accept.
    assert bottom.splitlines()[0] == "%FSLAX46Y46*%"
    assert bottom.splitlines()[-1] == "M02*"
    assert "%TF.FileFunction,Copper,L2,Bot*%" in bottom
    assert not _rect_apertures(bottom), "an empty layer defines no apertures"


def test_two_sided_board_splits_pads_across_both_copper_layers(two_sided):
    top = _flashes(gerber_copper(two_sided))
    bottom = _flashes(gerber_copper(two_sided, bottom=True))
    total_pads = sum(len(p.footprint.pads) for p in two_sided.parts)

    assert top, "the top layer should still carry the ICs"
    assert bottom, "two_sided=True should move some passives underneath"
    assert len(top) + len(bottom) == total_pads, "a pad went missing or was doubled"
    assert top == _expected_flashes(two_sided, bottom=False)
    assert bottom == _expected_flashes(two_sided, bottom=True)


def test_bottom_copper_is_emitted_unmirrored(two_sided):
    """PINNED BEHAVIOUR, not a judgement about what is correct.

    ``fab.py`` applies the same anchor-plus-offset arithmetic to both sides, so
    an asymmetric footprint lands at identical coordinates whether it is on top
    or on the bottom -- the bottom layer is written as seen from the top, with
    no X mirror. Measured here on two copies of one asymmetric footprint at the
    same position, one per side. Whether B.Cu ought to be mirrored is a
    question for whoever owns this module.
    """
    fp = _asym_footprint()
    board = BoardResult(
        parts=[
            PlacedPart(ref="C1", footprint=fp, value="v",
                       x_nm=1_000_000, y_nm=2_000_000, layer=Layer.TOP),
            PlacedPart(ref="C2", footprint=fp, value="v",
                       x_nm=1_000_000, y_nm=2_000_000, layer=Layer.BOTTOM),
        ],
        nets=[], width_nm=20_000_000, height_nm=20_000_000, solver_status="fallback",
    )
    top = _flashes(gerber_copper(board))
    bottom = _flashes(gerber_copper(board, bottom=True))
    assert top == bottom, "bottom copper is currently not mirrored relative to top"

    anchor_x = 1_000_000 + fp.courtyard_w_nm + MARGIN_NM
    assert bottom == [
        (anchor_x + pad.x_nm, 2_000_000 + fp.courtyard_h_nm + pad.y_nm + MARGIN_NM)
        for pad in fp.pads
    ]
    # Spelled out: pad 1 sits left of the anchor on BOTH sides.
    assert bottom[0][0] < anchor_x < bottom[1][0]


def test_a_part_the_solver_never_sided_stays_on_top():
    """``Layer.EITHER`` means no side was chosen; dropping it would lose copper
    from both files, where nobody would notice."""
    part = PlacedPart(ref="R1", footprint=_asym_footprint(), value="v",
                      layer=Layer.EITHER)
    board = _one_part_board(part)
    assert len(_flashes(gerber_copper(board))) == 2
    assert _flashes(gerber_copper(board, bottom=True)) == []
    assert _cpl_rows(board)["R1"][3] == "Top"


def test_mask_openings_are_expanded_and_paste_is_one_to_one(board):
    """Mask grows per side; paste is 1:1 because reduction is a stencil-house
    decision, and guessing one silently changes every solder joint."""
    copper = _rect_apertures(gerber_copper(board))
    paste = _rect_apertures(gerber_paste(board))
    assert paste == copper, "paste apertures must equal the copper pads exactly"

    for expansion in (DEFAULT_MASK_EXPANSION_NM, 100_000):
        mask = _rect_apertures(gerber_mask(board, expansion_nm=expansion))
        assert len(mask) == len(copper)
        for (m_code, m_w, m_h), (c_code, c_w, c_h) in zip(mask, copper, strict=True):
            assert m_code == c_code, "aperture numbering should stay in step"
            assert m_w - c_w == 2 * expansion, f"{m_w} vs {c_w} at {expansion} nm"
            assert m_h - c_h == 2 * expansion

    # The default really is 2 mil per side, i.e. 0.102 mm on the diameter.
    default = _rect_apertures(gerber_mask(board))
    assert default[0][1] - copper[0][1] == 102_000

    # Growing the opening does not move it.
    assert _flashes(gerber_mask(board)) == _flashes(gerber_copper(board))


def test_footprint_without_a_body_gets_copper_but_no_silkscreen():
    """A zero-size rectangle is four zero-length strokes: four dots of ink."""
    fp = Footprint(
        name="NO-BODY",
        pads=[Pad("1", 0, 0, 500_000, 500_000)],
        courtyard_w_nm=500_000,
        courtyard_h_nm=500_000,
        body_w_nm=0,
        body_h_nm=0,
    )
    board = _one_part_board(PlacedPart(ref="X1", footprint=fp, value="v"))

    silk = gerber_silkscreen(board)
    assert _coords(silk) == [], "a bodyless part must contribute no strokes"
    assert not _CIRCLE_RE.findall(silk), "and no pen to stroke them with"
    assert silk.splitlines()[0] == "%FSLAX46Y46*%"
    assert silk.splitlines()[-1] == "M02*"

    assert len(_flashes(gerber_copper(board))) == 1, "its copper is still needed"


def test_silkscreen_traces_the_body_outline_with_a_pen(board):
    """The legend is stroked, not flashed, and every stroke lies on a body edge."""
    silk = gerber_silkscreen(board)
    assert _coords(silk), "the fixture circuit should have a legend to draw"
    assert not _rect_apertures(silk)
    circles = _CIRCLE_RE.findall(silk)
    assert {_nm(c[1]) for c in circles} == {mm(0.12)}

    # Each stroke endpoint sits on some part's body rectangle: the outline is
    # clipped around pads, never redrawn somewhere else.
    edges = set()
    for part in board.parts:
        fp = part.footprint
        if not (fp.body_w_nm and fp.body_h_nm):
            continue
        ax = part.x_nm + fp.courtyard_w_nm + MARGIN_NM
        ay = part.y_nm + fp.courtyard_h_nm + MARGIN_NM
        edges.add((ax - fp.body_w_nm, ax + fp.body_w_nm,
                   ay - fp.body_h_nm, ay + fp.body_h_nm))
    for x, y, _ in _coords(silk):
        assert any(
            (x in (x0, x1) and y0 <= y <= y1)
            or (y in (y0, y1) and x0 <= x <= x1)
            for x0, x1, y0, y1 in edges
        ), f"stroke endpoint ({x}, {y}) is on no body outline"


def test_silkscreen_strokes_stay_clear_of_every_pad(board):
    """Regression: the legend used to stroke the raw body rectangle, putting
    0.06 mm of ink (half the pen) on every pad the body edge touches -- on a
    chip passive the pads sit under the body ends, on an LQFP the pad row
    starts exactly at the body edge. Independent math: widen each stroke by
    the pen half-width and measure axis-aligned separation from each pad flash.
    """
    pen_half = mm(0.12) // 2
    clearance = mm(0.2)

    pads = []
    for part in board.parts:
        ax = part.x_nm + part.footprint.courtyard_w_nm + MARGIN_NM
        ay = part.y_nm + part.footprint.courtyard_h_nm + MARGIN_NM
        for pad in part.footprint.pads:
            pads.append((ax + pad.x_nm - pad.w_nm // 2,
                         ax + pad.x_nm + pad.w_nm // 2,
                         ay + pad.y_nm - pad.h_nm // 2,
                         ay + pad.y_nm + pad.h_nm // 2))

    coords = _coords(gerber_silkscreen(board))
    strokes = [
        (min(mx, x) - pen_half, max(mx, x) + pen_half,
         min(my, y) - pen_half, max(my, y) + pen_half)
        for (mx, my, m_op), (x, y, op) in zip(coords, coords[1:], strict=False)
        if m_op == 2 and op == 1  # a D02 move followed by its D01 draw
    ]
    assert strokes, "the fixture circuit should have a legend to check"

    for sx0, sx1, sy0, sy1 in strokes:
        for px0, px1, py0, py1 in pads:
            gap_x = max(px0 - sx1, sx0 - px1)
            gap_y = max(py0 - sy1, sy0 - py1)
            assert max(gap_x, gap_y) >= clearance, (
                f"silk stroke ({sx0}, {sy0})..({sx1}, {sy1}) comes within "
                f"{max(gap_x, gap_y)} nm of pad ({px0}, {py0})..({px1}, {py1})"
            )


# ------------------------------------------------------------------- rotation


def test_rotated_pad_flashes_at_the_quarter_turned_offset():
    """Gerber has no concept of a part, so the rotation has to be baked into
    every coordinate: a counter-clockwise quarter turn maps (x, y) -> (-y, x).

    Unreachable through ``build_board`` today (``allow_rotation`` is never set),
    so the placement is built by hand.
    """
    fp = Footprint(
        name="ROT-1",
        pads=[Pad("1", 300_000, 700_000, 400_000, 900_000)],
        courtyard_w_nm=1_000_000,
        courtyard_h_nm=2_000_000,
        body_w_nm=500_000,
        body_h_nm=1_500_000,
    )
    flat = _one_part_board(PlacedPart(ref="U1", footprint=fp, value="v"))
    turned = _one_part_board(
        PlacedPart(ref="U1", footprint=fp, value="v", rotated=True)
    )
    anchor = (fp.courtyard_w_nm + MARGIN_NM, fp.courtyard_h_nm + MARGIN_NM)

    assert _flashes(gerber_copper(flat)) == [
        (anchor[0] + 300_000, anchor[1] + 700_000)
    ]
    assert _flashes(gerber_copper(turned)) == [
        (anchor[0] - 700_000, anchor[1] + 300_000)
    ], "a rotated pad should land at anchor + (-py, px)"


def test_rotated_pad_aperture_swaps_width_and_height():
    fp = Footprint(
        name="ROT-2",
        pads=[Pad("1", 300_000, 700_000, 400_000, 900_000)],
        courtyard_w_nm=1_000_000,
        courtyard_h_nm=2_000_000,
    )
    flat = gerber_copper(_one_part_board(PlacedPart(ref="U1", footprint=fp, value="v")))
    turned = gerber_copper(
        _one_part_board(PlacedPart(ref="U1", footprint=fp, value="v", rotated=True))
    )
    assert _rect_apertures(flat) == [(10, 400_000, 900_000)]
    assert _rect_apertures(turned) == [(10, 900_000, 400_000)]

    # The mask opening turns with the pad and is still grown on both axes.
    assert _rect_apertures(
        gerber_mask(
            _one_part_board(PlacedPart(ref="U1", footprint=fp, value="v",
                                       rotated=True)),
            expansion_nm=50_000,
        )
    ) == [(10, 1_000_000, 500_000)]


def test_rotated_silkscreen_body_swaps_its_extents():
    fp = Footprint(
        name="ROT-3",
        pads=[Pad("1", 0, 0, 100_000, 100_000)],
        courtyard_w_nm=2_000_000,
        courtyard_h_nm=2_000_000,
        body_w_nm=500_000,
        body_h_nm=1_500_000,
    )
    turned = gerber_silkscreen(
        _one_part_board(PlacedPart(ref="U1", footprint=fp, value="v", rotated=True))
    )
    anchor = fp.courtyard_w_nm + MARGIN_NM
    xs = {x for x, _, _ in _coords(turned)}
    ys = {y for _, y, _ in _coords(turned)}
    assert xs == {anchor - 1_500_000, anchor + 1_500_000}
    assert ys == {anchor - 500_000, anchor + 500_000}


# ------------------------------------------------------------------ bom / cpl


def test_bom_header_is_exactly_what_assembly_houses_read(board):
    assert bom_csv(board).splitlines()[0] == "Comment,Designator,Footprint,Quantity"


def test_bom_groups_identical_parts_into_one_row(board):
    rows = list(csv.reader(io.StringIO(bom_csv(board))))[1:]
    by_comment = {row[0]: row for row in rows}
    assert len(rows) == len(by_comment), "a (value, footprint) pair should appear once"

    # C1 and C2 are both 22uF in the same package, so they share a row.
    assert by_comment["22uF"][1] == "C1;C2"
    assert by_comment["22uF"][3] == "2"
    assert by_comment["100nF"][1] == "C3"
    assert by_comment["100nF"][3] == "1"

    # Every part is accounted for exactly once, and quantities add up.
    designators = [d for row in rows for d in row[1].split(";")]
    assert sorted(designators) == sorted(p.ref for p in board.parts)
    assert sum(int(row[3]) for row in rows) == len(board.parts)


def test_bom_rows_and_designators_are_deterministically_ordered():
    """C2 before C10, which plain string ordering does not give."""
    fp = Footprint(
        name="C_0603",
        pads=[Pad("1", -775_000, 0, 900_000, 950_000)],
        courtyard_w_nm=1_475_000,
        courtyard_h_nm=725_000,
    )
    refs = ["C10", "C2", "C1"]
    board = BoardResult(
        parts=[PlacedPart(ref=r, footprint=fp, value="100nF") for r in refs],
        nets=[], width_nm=10_000_000, height_nm=10_000_000, solver_status="fallback",
    )
    row = list(csv.reader(io.StringIO(bom_csv(board))))[1]
    assert row[1] == "C1;C2;C10"
    assert bom_csv(board) == bom_csv(board)


def test_bom_quotes_values_containing_commas_and_quotes():
    """A value like ``1,0 "ohm"`` must survive as one field, not become three."""
    value = '1,0 "ohm" ±5%'
    footprint_name = 'R, "odd"'
    fp = Footprint(
        name=footprint_name,
        pads=[Pad("1", 0, 0, 100_000, 100_000)],
        courtyard_w_nm=100_000,
        courtyard_h_nm=100_000,
    )
    board = BoardResult(
        parts=[PlacedPart(ref="R1", footprint=fp, value=value)],
        nets=[], width_nm=1_000_000, height_nm=1_000_000, solver_status="fallback",
    )
    text = bom_csv(board)
    assert '"1,0 ""ohm"" ±5%"' in text, "quotes must be doubled, field wrapped"

    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["Comment", "Designator", "Footprint", "Quantity"]
    assert parsed[1] == [value, "R1", footprint_name, "1"]


def test_cpl_header_is_exactly_what_assembly_houses_read(board):
    assert cpl_csv(board).splitlines()[0] == "Designator,Mid X,Mid Y,Layer,Rotation"


def test_cpl_has_one_row_per_part_in_a_stable_order(board):
    rows = list(csv.reader(io.StringIO(cpl_csv(board))))[1:]
    assert len(rows) == len(board.parts)
    assert [r[0] for r in rows] == ["C1", "C2", "C3", "R1", "U1", "U2"]


def test_cpl_coordinates_agree_with_the_copper_layer(board):
    """The CPL carries the same margin-shifted, Y-up anchor the Gerbers use, so
    the assembler's origin lines up with the copper without anyone having to
    know the outline margin exists."""
    part = next(p for p in board.parts if p.ref == "U2")
    row = _cpl_rows(board)[part.ref]
    anchor = (_nm(row[1]), _nm(row[2]))

    flashes = set(_flashes(gerber_copper(board)))
    for pad in part.footprint.pads:
        assert (anchor[0] + pad.x_nm, anchor[1] + pad.y_nm) in flashes, (
            f"U2 pad {pad.number} is not where the CPL says the part is"
        )
    # Millimetres at four decimals, always.
    assert re.fullmatch(r"-?\d+\.\d{4}", row[1]) and re.fullmatch(
        r"-?\d+\.\d{4}", row[2]
    )


def test_cpl_layer_and_rotation_columns(two_sided):
    rows = _cpl_rows(two_sided)
    assert {row[3] for row in rows.values()} == {"Top", "Bottom"}
    for part in two_sided.parts:
        expected = "Bottom" if part.layer is Layer.BOTTOM else "Top"
        assert rows[part.ref][3] == expected, part.ref
        assert rows[part.ref][4] == "0", "build_board never rotates today"

    turned = _one_part_board(
        PlacedPart(ref="U1", footprint=_asym_footprint(), value="v", rotated=True)
    )
    assert _cpl_rows(turned)["U1"][4] == "90"


# ---------------------------------------------------------------- determinism


def test_fab_files_are_byte_identical_when_regenerated(board):
    """No timestamps, no UUIDs, no locale: an unchanged design diffs cleanly."""
    first = fab_files(board)
    second = fab_files(board)
    assert [f.filename for f in first] == [f.filename for f in second]
    for a, b in zip(first, second, strict=True):
        assert a.content == b.content, f"{a.filename} is not reproducible"


def _board_with_values(values):
    """A board carrying one single-pad part per supplied value."""
    parts = []
    for index, value in enumerate(values):
        fp = Footprint(
            name="C_0603",
            pads=[Pad(number="1", x_nm=0, y_nm=0, w_nm=mm(1), h_nm=mm(1), net="N")],
            courtyard_w_nm=mm(1),
            courtyard_h_nm=mm(1),
        )
        parts.append(
            PlacedPart(
                ref=f"C{index}", footprint=fp, value=value, x_nm=mm(2), y_nm=mm(2)
            )
        )
    return BoardResult(
        parts=parts,
        nets=["N"],
        width_nm=mm(20),
        height_nm=mm(20),
        solver_status="optimal",
    )

def test_bom_neutralises_spreadsheet_formulas():
    """A model-supplied value must not execute when the BOM is opened.

    Values here are whatever the model read off a datasheet, and the file's
    destination is a spreadsheet, where a leading '=', '+', '-' or '@' is a
    formula rather than text.
    """
    hostile = ["=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1)"]
    board = _board_with_values(hostile + ["10uF"])
    rows = list(csv.reader(io.StringIO(bom_csv(board))))[1:]
    assert rows, "expected one row per distinct value"
    for row in rows:
        assert row[0][:1] not in "=+-@", (
            f"{row[0]!r} would be evaluated as a formula by a spreadsheet"
        )
    assert any(row[0] == "10uF" for row in rows), (
        "a benign value must survive unchanged"
    )

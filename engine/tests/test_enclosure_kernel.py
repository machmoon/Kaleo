"""Tests for :mod:`silkscreen.enclosure.kernel`, the B-rep acceptance gate.

Gated on the ``cad`` extra the way ``test_spice.py`` gates on the
ngspice binary (``needs_build123d``), with one ungated test pinning that the
module imports without the kernel.

The models under test are built **by hand** with build123d in
:func:`hand_model` -- a shelled rounded box with an open top, a plate lid with
a ring lip, two insert bosses at the board's mounting holes, a keep-out slab
with parts on both sides, and a USB-C plug prism through the left wall -- so
the verifier is exercised against geometry whose numbers this file states,
not against ``cad.build_enclosure``'s (which gets its own integration tests
at the bottom). Every expected margin is computed inline from those numbers,
never by calling a ``kernel.py`` helper: a check written in terms of the code
under test would share its blind spot.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from silkscreen.enclosure import kernel, rules, snapshot
from silkscreen.enclosure.board_shape import (
    BoardEnvelope,
    Layer,
    MountingHole,
    PartExtent,
    board_envelope,
)
from silkscreen.enclosure.cad import EnclosureModel, build_enclosure, kernel_available
from silkscreen.enclosure.errors import KernelFitError
from silkscreen.enclosure.ir import Cutout, EnclosureSpec
from silkscreen.enclosure.kernel import (
    CLAUSES,
    Clause,
    KernelReport,
    verify_model,
)
from silkscreen.units import mm

needs_build123d = pytest.mark.skipif(
    not kernel_available(), reason="build123d not installed (pip install -e '.[cad]')"
)

# ------------------------------------------------------------- the hand model
#
# All numbers in mm; the model frame is the assembled frame of the contract:
# X as KiCad, Y flipped (front = KiCad max-Y), Z up, origin at the base's
# outer bottom-left-front corner.

OW, OD, OH = 40.0, 30.0, 19.0          # base outer size and height
WALL, R, CLR = 2.0, 3.0, 3.0           # wall, corner radius, board-to-wall clearance
LT, LIPW, LIPD, SLACK = 2.0, 1.2, 3.0, 0.2   # lid plate, lip ring, slack per side
SO_H, SO_OD, BORE, BORE_D = 4.0, 7.0, 4.0, 4.8  # standoff height/OD, insert bore
BX0, BY0, BW, BD, BT = 100.0, 50.0, 30.0, 20.0, 1.6  # KiCad outline + thickness
HOLES_K = {"H1": (104.0, 54.0), "H2": (126.0, 66.0)}
J1_H = 3.0                             # the USB-C receptacle's body height
# ref, KiCad x0, y0, x1, y1, height, side
PARTS_K = (
    ("U1", 112.0, 56.0, 120.0, 62.0, 5.0, "top"),
    ("J1", 100.0, 58.0, 108.0, 66.0, J1_H, "top"),
    ("C1", 108.0, 62.0, 112.0, 66.0, 2.0, "bottom"),
)
Z_BOARD = WALL + SO_H                  # slab bottom
Z_TOP = Z_BOARD + BT                   # board top surface
PLUG_W, PLUG_H = 13.0, 7.0             # USB-C plug envelope


def kicad_to_model(xk: float, yk: float) -> tuple[float, float]:
    """The frame map, written out by hand (the oracle for every position)."""
    return WALL + CLR + (xk - BX0), WALL + CLR + (BY0 + BD - yk)


HOLES_MODEL = {ref: kicad_to_model(*xy) for ref, xy in HOLES_K.items()}
J1_CY = (kicad_to_model(0, 58.0)[1] + kicad_to_model(0, 66.0)[1]) / 2
PLUG_Y0, PLUG_Y1 = J1_CY - PLUG_W / 2, J1_CY + PLUG_W / 2
# The opening is centred on the receptacle's axis, not started at the board
# top: a mating plug's axis is the receptacle's axis, so a 7 mm overmold on a
# 3 mm top-mount part spans board_top - 2.0 .. board_top + 5.0. Starting it at
# the board top left the wall in the lower 2 mm of the overmold's way.
PLUG_AXIS_Z = Z_TOP + J1_H / 2
PLUG_Z0, PLUG_Z1 = PLUG_AXIS_Z - PLUG_H / 2, PLUG_AXIS_Z + PLUG_H / 2


def _b3d():
    import build123d

    return build123d


def box(x0, y0, z0, x1, y1, z1):
    b = _b3d()
    return b.Box(
        x1 - x0, y1 - y0, z1 - z0, align=(b.Align.MIN, b.Align.MIN, b.Align.MIN)
    ).moved(b.Location((x0, y0, z0)))


def build_base(*, cut_opening: bool = True, thin_wall: bool = False, extra=None):
    """Shelled rounded box, open top, two insert bosses, USB-C opening."""
    b = _b3d()
    mn = (b.Align.MIN, b.Align.MIN, b.Align.MIN)
    outer = b.fillet(b.Box(OW, OD, OH, align=mn).edges().filter_by(b.Axis.Z), R)
    base = b.offset(outer, amount=-WALL, openings=outer.faces().sort_by(b.Axis.Z)[-1])
    for hx, hy in HOLES_MODEL.values():
        boss = b.Cylinder(
            SO_OD / 2, SO_H, align=(b.Align.CENTER, b.Align.CENTER, b.Align.MIN)
        ).moved(b.Location((hx, hy, WALL)))
        bore = b.Cylinder(
            BORE / 2, BORE_D, align=(b.Align.CENTER, b.Align.CENTER, b.Align.MAX)
        ).moved(b.Location((hx, hy, WALL + SO_H)))
        base = base.fuse(boss).cut(bore)
    if cut_opening:
        base = base.cut(box(-1, PLUG_Y0, PLUG_Z0, WALL + 1, PLUG_Y1, PLUG_Z1))
    if thin_wall:
        # Shave the right wall from the cavity side: 2.0 -> 1.1 mm over a patch.
        base = base.cut(box(OW - WALL - 0.5, 8, 5, OW - WALL + 0.9, 22, 14))
    if extra is not None:
        base = base.fuse(extra)
    return base.clean()


def build_lid(*, slots=()):
    """Plate with a ring lip, in the printed orientation (plate on the bed,
    lip pointing +Z); ``slots`` are (x0, y0, x1, y1) through-cuts."""
    b = _b3d()
    mn = (b.Align.MIN, b.Align.MIN, b.Align.MIN)
    plate = b.fillet(b.Box(OW, OD, LT, align=mn).edges().filter_by(b.Axis.Z), R)
    lo_w, lo_d = OW - 2 * WALL - 2 * SLACK, OD - 2 * WALL - 2 * SLACK
    x0 = WALL + SLACK
    lip = b.Box(lo_w, lo_d, LIPD, align=mn).moved(b.Location((x0, x0, LT)))
    lip = b.fillet(lip.edges().filter_by(b.Axis.Z), R - WALL - SLACK)
    lip = lip.cut(
        b.Box(lo_w - 2 * LIPW, lo_d - 2 * LIPW, LIPD, align=mn).moved(
            b.Location((x0 + LIPW, x0 + LIPW, LT))
        )
    )
    lid = plate.fuse(lip)
    for sx0, sy0, sx1, sy1 in slots:
        lid = lid.cut(box(sx0, sy0, -1, sx1, sy1, LT + 1))
    return lid.clean()


def lid_location(lift: float = 0.0):
    """The Location that flips the printed lid onto the rim: a half-turn
    about X, so printed (x, y, z) lands at (x, OD - y, OH + LT + lift - z)."""
    return _b3d().Location((0, OD, OH + LT + lift), (180, 0, 0))


def grown_parts(*, grow_bottom: float = 0.0, grow_top: float = 0.0):
    """``PARTS_K`` with U1 taller by ``grow_top`` and C1 taller by
    ``grow_bottom``.

    The keep-out solid and the ``BoardEnvelope`` must describe the same
    board -- the contract builds the keep-out from the envelope -- so a test
    that makes a part taller feeds this one list to both. Growing J1 too
    would move the receptacle axis and, with it, where the plug wants its
    opening, which is a different test than this one.
    """
    out = []
    for ref, kx0, ky0, kx1, ky1, h, side in PARTS_K:
        if ref == "U1":
            h += grow_top
        elif side == "bottom":
            h += grow_bottom
        out.append((ref, kx0, ky0, kx1, ky1, h, side))
    return tuple(out)


def build_board(parts=PARTS_K):
    """Keep-out: substrate slab plus one prism per part on each side."""
    x0, y0 = kicad_to_model(BX0, BY0 + BD)
    slab = box(x0, y0, Z_BOARD, x0 + BW, y0 + BD, Z_TOP)
    prisms = []
    for _ref, kx0, ky0, kx1, ky1, h, side in parts:
        mx0, my1 = kicad_to_model(kx0, ky0)
        mx1, my0 = kicad_to_model(kx1, ky1)
        if side == "top":
            prisms.append(box(mx0, my0, Z_TOP, mx1, my1, Z_TOP + h))
        else:
            prisms.append(box(mx0, my0, Z_BOARD - h, mx1, my1, Z_BOARD))
    return slab.fuse(*prisms).clean()


def build_plug():
    """USB-C plug prism from inside the cavity out through the left wall."""
    return box(-1.0, PLUG_Y0, PLUG_Z0, WALL + CLR + 1.0, PLUG_Y1, PLUG_Z1)


def rib(d: float, *, x0: float = 8.0, x1: float = 35.0):
    """A rib inside the front wall whose underside slopes ``d`` mm sideways
    over 3 mm of height: overhang angle from vertical = atan(d / 3)."""
    b = _b3d()
    pts = [b.Vector(x0, WALL, 10), b.Vector(x0, WALL + d, 13), b.Vector(x0, WALL, 13)]
    face = b.Face(b.Wire.make_polygon(pts, close=True))
    return b.extrude(face, amount=x1 - x0, dir=(1, 0, 0))


def hand_envelope(part_rows=PARTS_K) -> BoardEnvelope:
    parts = []
    for ref, kx0, ky0, kx1, ky1, h, side in part_rows:
        parts.append(
            PartExtent(
                ref, mm(kx0), mm(ky0), mm(kx1), mm(ky1), mm(h), False,
                side=Layer.TOP if side == "top" else Layer.BOTTOM,
                connector="USB_C" if ref == "J1" else None,
            )
        )
    holes = tuple(
        MountingHole(ref, mm(x), mm(y), mm(3.2)) for ref, (x, y) in HOLES_K.items()
    )
    x0, y0, x1, y1 = mm(BX0), mm(BY0), mm(BX0 + BW), mm(BY0 + BD)
    tops = [row[5] for row in part_rows if row[6] == "top"]
    bottoms = [row[5] for row in part_rows if row[6] != "top"]
    return BoardEnvelope(
        outline_nm=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        x_min_nm=x0, y_min_nm=y0, x_max_nm=x1, y_max_nm=y1,
        thickness_nm=mm(BT), parts=tuple(parts),
        max_height_nm=mm(max(tops, default=0.0)),
        max_height_bottom_nm=mm(max(bottoms, default=0.0)),
        mounting_holes=holes,
    )


def hand_spec(**overrides) -> EnclosureSpec:
    kwargs = dict(
        wall_nm=mm(WALL), clearance_nm=mm(CLR), lid="lip", corner_radius_nm=mm(R),
        cutouts=(Cutout(id="usb", ref="J1", face="left", margin_nm=mm(0.5)),),
        standoffs=True, vents=False, label=None,
        mount="holes", insert="M3", material="PLA",
    )
    kwargs.update(overrides)
    return EnclosureSpec(**kwargs)


def hand_model(
    *, base=None, lid=None, board=None, lid_assembled=None, plugs=None,
    standoffs=None, outer_z: float = OH + LT, no_lid: bool = False,
) -> EnclosureModel:
    """The correct model unless a piece is overridden."""
    if no_lid:
        lid, lid_assembled = None, None
    else:
        lid = build_lid() if lid is None else lid
        lid_assembled = lid_location() if lid_assembled is None else lid_assembled
    return EnclosureModel(
        base=build_base() if base is None else base,
        lid=lid,
        board=build_board() if board is None else board,
        lid_assembled=lid_assembled,
        plugs=(("usb", build_plug()),) if plugs is None else plugs,
        standoffs=(
            tuple((ref, mm(x), mm(y)) for ref, (x, y) in HOLES_MODEL.items())
            if standoffs is None else standoffs
        ),
        outer_nm=(mm(OW), mm(OD), mm(outer_z)),
        params_mm={"wall": WALL},
    )


def _clause(report: KernelReport, name: str):
    return next(c for c in report.clauses if c.name == name)


# ------------------------------------------------------------------- ungated

def test_modules_import_without_the_kernel():
    """kernel.py and snapshot.py import on a machine without build123d; only
    calling them needs the extra (``KernelUnavailable`` names it)."""
    assert tuple(kernel.CLAUSES) == CLAUSES
    assert snapshot.VIEWS == ("iso", "iso_opposite", "top", "front", "section")
    assert callable(verify_model) and callable(snapshot.render_packet)


def test_thin_reports_only_nominal_passes_in_clause_order():
    """A pass inside one slide-fit allowance is a warning, not a result.

    The band is stated by ``rules`` (0.2 mm); the expectations here are
    computed from that number rather than from ``thin()``'s own comparison,
    and a *failing* clause is never thin -- it is already in ``failed``.
    Only a clause whose margin is a *length* can be nominal: ``overhang``
    reports degrees and ``min_wall`` reports its own 50 um tolerance, so
    neither is comparable to a millimetre band whatever its number, and a
    clause that had nothing to measure is absent, not nominal.
    """
    band = mm(0.2)
    assert band == rules.THIN_MARGIN_NM
    report = KernelReport(
        clauses=(
            # Comfortably clear: 1 mm of air is ten times the band.
            Clause("board_clash", True, mm(1.0), "clears by 1.000 mm"),
            # Just inside the band: half a slide fit.
            Clause("headroom", True, mm(0.1), "clears by 0.100 mm"),
            # Nothing measured is not a nominal pass.
            Clause("underside", True, 0, "nothing to check: no bottom-side parts"),
            # Violated by twice the band -- failing, so never thin.
            Clause("min_wall", False, -mm(0.4), "0.400 mm too thin"),
            # Exactly one band is the printer's tolerance, not less than it.
            Clause("lid_mates", True, band, "clears by 0.200 mm"),
            # Zero degrees off the limit is not zero millimetres of fit.
            Clause("overhang", True, 0, "exactly at the limit"),
        )
    )
    assert report.thin() == ["headroom"]
    assert report.failed == ["min_wall"]
    # A wider band pulls in the length clauses it covers, still in clause
    # order -- and still never the angle clause or the empty one.
    assert report.thin(band_nm=mm(1.5)) == ["board_clash", "headroom", "lid_mates"]
    # A band of zero can never report anything: a pass is a margin >= 0.
    assert report.thin(band_nm=0) == []


# ------------------------------------------------------------- correct model

@needs_build123d
def test_correct_model_passes_every_clause_with_nonnegative_margin():
    report = verify_model(hand_model(), hand_spec(), hand_envelope())
    assert [c.name for c in report.clauses] == list(CLAUSES)
    assert report.passed, report.text()
    assert all(c.margin_nm >= 0 for c in report.clauses), report.text()
    # The measured numbers are the ones the geometry states.
    headroom = _clause(report, "headroom")
    assert headroom.margin_nm == mm((OH + LT - LT) - (Z_TOP + 5.0) - 3.0)  # 3.4 mm
    underside = _clause(report, "underside")
    assert underside.margin_nm == mm(SO_H - 2.0)
    lid_mates = _clause(report, "lid_mates")
    assert lid_mates.margin_nm == mm(SLACK)  # gap 0.2 each side, allowed [0, 0.4]
    assert "0.200" in lid_mates.detail
    report.raise_for_failures()  # no-op on a passing report


@needs_build123d
def test_report_text_has_one_line_per_clause_with_mm_numbers():
    report = verify_model(hand_model(), hand_spec(), hand_envelope())
    lines = [line for line in report.text().splitlines() if not line.startswith("WARN")]
    assert len(lines) == len(CLAUSES)
    for line, name in zip(lines, CLAUSES, strict=True):
        assert line.startswith(f"PASS {name} margin=")
        assert " mm" in line


# ------------------------------------------------------------ single failures

@needs_build123d
def test_lid_seated_too_low_fails_lid_mates_by_the_rim_overlap():
    """Lowering the lid 0.2 mm sinks the plate into the rim: the intersection
    is the rim's ring area times 0.2 mm, reported as a cube side."""
    lift = -0.2
    model = hand_model(lid_assembled=lid_location(lift), outer_z=OH + LT + lift)
    report = verify_model(model, hand_spec(), hand_envelope())

    def rounded_rect_area(a, b, r):
        return a * b - (4 - math.pi) * r * r

    ring = rounded_rect_area(OW, OD, R) - rounded_rect_area(
        OW - 2 * WALL, OD - 2 * WALL, R - WALL
    )
    expected = -mm((ring * 0.2) ** (1 / 3))
    assert report.failed == ["lid_mates"], report.text()
    assert abs(_clause(report, "lid_mates").margin_nm - expected) <= 1_000  # 1 um
    assert _clause(report, "lid_mates").margin_nm < 0


@needs_build123d
def test_bottom_part_through_the_floor_fails_board_clash_by_the_cube_side():
    """C1 grown 2.5 mm downward pokes 0.5 mm into the floor: 4 x 4 x 0.5 =
    8 mm³, cube side 2.0 mm."""
    parts = grown_parts(grow_bottom=2.5)
    model = hand_model(board=build_board(parts))
    report = verify_model(model, hand_spec(), hand_envelope(parts))
    clash = _clause(report, "board_clash")
    assert not clash.passed
    assert abs(clash.margin_nm - (-mm(2.0))) <= 1_000
    # The same growth eats the standoff gap: 4.0 mm gap vs 2.0 + 2.5 needed.
    under = _clause(report, "underside")
    assert not under.passed and abs(under.margin_nm - (-mm(0.5))) <= 1_000
    assert set(report.failed) == {"board_clash", "underside"}


@needs_build123d
def test_taller_part_fails_headroom_by_the_missing_air():
    """U1 grown so the lid underside is 2.6 mm above it: 0.4 mm short of 3.0."""
    grow = (OH - Z_TOP - 5.0) - 2.6
    parts = grown_parts(grow_top=grow)
    model = hand_model(board=build_board(parts))
    report = verify_model(model, hand_spec(), hand_envelope(parts))
    head = _clause(report, "headroom")
    assert not head.passed
    assert abs(head.margin_nm - (-mm(0.4))) <= 1_000
    assert report.failed == ["headroom"], report.text()


@needs_build123d
def test_uncut_wall_fails_cutout_admits_plug_by_the_blocked_volume():
    """The wall never opened: the prism's slice through the wall is inside the
    case -- 13 x 7 x 2 = 182 mm³, cube side 5.667 mm."""
    model = hand_model(base=build_base(cut_opening=False))
    report = verify_model(model, hand_spec(), hand_envelope())
    plug = _clause(report, "cutout_admits_plug")
    assert not plug.passed
    assert abs(plug.margin_nm - (-mm((PLUG_W * PLUG_H * WALL) ** (1 / 3)))) <= 1_000
    assert "usb" in plug.detail
    assert report.failed == ["cutout_admits_plug"]


@needs_build123d
def test_thinned_wall_fails_min_wall_by_the_shortfall():
    """One patch of the right wall is 1.1 mm: 0.85 mm under the 1.95 mm floor."""
    model = hand_model(base=build_base(thin_wall=True))
    report = verify_model(model, hand_spec(), hand_envelope())
    wall = _clause(report, "min_wall")
    assert not wall.passed
    assert abs(wall.margin_nm - mm(1.1 - (WALL - 0.05))) <= 1_000  # -0.85 mm
    assert "1.100 mm" in wall.detail
    assert "min_wall" in report.failed


@needs_build123d
def test_overhang_passes_at_45_degrees_and_fails_at_60():
    """A rib whose underside slopes 45 degrees from vertical is printable;
    one at 60 degrees is not, and the margin is the 15 degrees of excess."""
    ok = verify_model(
        hand_model(base=build_base(extra=rib(3.0))), hand_spec(), hand_envelope()
    )
    assert _clause(ok, "overhang").passed
    assert abs(_clause(ok, "overhang").margin_nm) <= 1_000  # exactly at the limit

    bad = verify_model(
        hand_model(base=build_base(extra=rib(3.0 * math.tan(math.radians(60))))),
        hand_spec(), hand_envelope(),
    )
    over = _clause(bad, "overhang")
    assert not over.passed
    assert abs(over.margin_nm - (-15 * 1_000_000)) <= 1_000
    assert "60.0 deg" in over.detail and "degrees" in over.detail


@needs_build123d
def test_vent_slot_over_a_boss_fails_keepout_by_its_distance():
    """A lid slot straight over boss H1: the slot overlaps the boss axis, so
    its clearance to the boss edge is -3.5 mm, 5.5 mm short of the 2 mm rule.
    The same slot moved 7 mm away clears it by 0.75 mm."""
    hx, hy = HOLES_MODEL["H1"]
    ly = OD - hy  # the lid is stored flipped about X
    near = build_lid(slots=((hx - 5, ly - 0.75, hx + 5, ly + 0.75),))
    report = verify_model(
        hand_model(lid=near), hand_spec(vents=True), hand_envelope()
    )
    vent = _clause(report, "vent_keepout")
    assert not vent.passed
    assert abs(vent.margin_nm - (-mm(SO_OD / 2 + 2.0))) <= 1_000
    assert report.failed == ["vent_keepout"], report.text()

    far = build_lid(slots=((hx - 5, ly + 6.25, hx + 5, ly + 7.75),))
    report = verify_model(hand_model(lid=far), hand_spec(vents=True), hand_envelope())
    vent = _clause(report, "vent_keepout")
    assert vent.passed
    assert abs(vent.margin_nm - mm(6.25 - SO_OD / 2 - 2.0)) <= 1_000


@needs_build123d
def test_off_axis_standoff_fails_concentric_by_the_offset():
    hx, hy = HOLES_MODEL["H1"]
    standoffs = (("H1", mm(hx + 0.3), mm(hy)), ("H2", *map(mm, HOLES_MODEL["H2"])))
    report = verify_model(
        hand_model(standoffs=standoffs), hand_spec(), hand_envelope()
    )
    con = _clause(report, "standoff_concentric")
    assert not con.passed
    assert abs(con.margin_nm - mm(0.05 - 0.3)) <= 1_000
    assert "H1" in con.detail


@needs_build123d
def test_wrong_outer_nm_fails_bbox():
    model = hand_model(outer_z=OH + LT + 1.0)
    report = verify_model(model, hand_spec(), hand_envelope())
    bb = _clause(report, "bbox")
    assert not bb.passed
    assert abs(bb.margin_nm - (1_000 - mm(1.0))) <= 1_000
    assert report.failed == ["bbox"]


# ----------------------------------------------------- honesty and reporting

@needs_build123d
def test_lid_none_reports_lid_clauses_as_nothing_to_check():
    report = verify_model(
        hand_model(no_lid=True, outer_z=OH), hand_spec(lid="none"), hand_envelope()
    )
    assert report.passed, report.text()
    for name in ("headroom", "lid_mates"):
        clause = _clause(report, name)
        assert clause.margin_nm == 0
        assert clause.detail.startswith("nothing to check")
    assert "no lid" in _clause(report, "solid_count").detail


@needs_build123d
def test_no_cutouts_and_corner_standoffs_are_nothing_to_check():
    model = hand_model(plugs=(), standoffs=(("front_left", mm(5), mm(5)),))
    report = verify_model(model, hand_spec(cutouts=()), hand_envelope())
    assert _clause(report, "cutout_admits_plug").detail.startswith("nothing to check")
    con = _clause(report, "standoff_concentric")
    assert con.passed and con.margin_nm == 0
    assert con.detail.startswith("nothing to check") and "corner" in con.detail


@needs_build123d
def test_kernel_exception_is_a_failed_clause_not_a_pass():
    """A model whose lid is not a shape at all: every lid clause fails with
    the exception named, and the base-only clauses still evaluate."""
    model = hand_model(lid=object())
    report = verify_model(model, hand_spec(), hand_envelope())
    assert not report.passed
    top = _clause(report, "valid_topology")
    assert not top.passed and "could not be evaluated" in top.detail
    assert top.margin_nm == kernel.UNEVALUATED_NM < 0
    assert _clause(report, "underside").passed  # base-only, unaffected


@needs_build123d
def test_kernel_fit_error_names_every_failing_clause():
    parts = grown_parts(grow_bottom=2.5)
    model = hand_model(
        base=build_base(cut_opening=False, thin_wall=True),
        board=build_board(parts),
    )
    report = verify_model(model, hand_spec(), hand_envelope(parts))
    expected = {"board_clash", "underside", "cutout_admits_plug", "min_wall"}
    assert set(report.failed) == expected, report.text()
    with pytest.raises(KernelFitError) as info:
        report.raise_for_failures()
    err = info.value
    assert set(err.failed) == expected
    assert set(err.margins_nm) == set(CLAUSES)
    for name in expected:
        assert name in str(err)
        assert err.margins_nm[name] < 0


# ------------------------------------------------- integration with cad.py

_STROKE = "(stroke (width 0.05) (type solid))"


def _real_board(path: Path) -> BoardEnvelope:
    """The ``test_enclosure_cad.py`` board: 50 x 30 outline, USB-C J1 at the
    left edge, U1 mid-board, four 3.2 mm mounting holes."""
    def part(ref, lib, cx, cy, half):
        return f"""  (footprint "{lib}" (layer "F.Cu") (at {cx} {cy})
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start {-half} {-half}) (end {half} {half}) {_STROKE} (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )"""

    def hole(ref, x, y):
        lib = "MountingHole:MountingHole_3.2mm_M3"
        return f"""  (footprint "{lib}" (layer "F.Cu") (at {x} {y})
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_circle (center 0 0) (end 3.2 0) {_STROKE} (layer "F.CrtYd"))
    (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2)
      (drill 3.2) (layers "*.Cu" "*.Mask"))
  )"""

    body = [
        part("J1", "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12", 4.5, 15.0, 4.5),
        part("U1", "Package_QFP:LQFP-48_7x7mm_P0.5mm", 25.0, 15.0, 4.5),
        hole("H1", 4.0, 4.0), hole("H2", 46.0, 4.0),
        hole("H3", 4.0, 26.0), hole("H4", 46.0, 26.0),
    ]
    edges = [
        ((0, 0), (50, 0)), ((50, 0), (50, 30)), ((50, 30), (0, 30)), ((0, 30), (0, 0))
    ]
    text = (
        '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        '  (generator_version "8.0")\n  (general (thickness 1.6))\n'
        '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (44 "Edge.Cuts" user)\n'
        '    (47 "F.CrtYd" user "F.Courtyard"))\n  (net 0 "")\n'
        + "\n".join(body) + "\n"
        + "\n".join(
            f"  (gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) {_STROKE} "
            '(layer "Edge.Cuts"))'
            for a, b in edges
        )
        + "\n)\n"
    )
    path.write_text(text, encoding="utf-8")
    return board_envelope(path)


def _real_spec(**overrides) -> EnclosureSpec:
    kwargs = dict(
        wall_nm=mm(2.0), clearance_nm=mm(1.0), lid="lip", corner_radius_nm=mm(3.0),
        cutouts=(Cutout(id="usb", ref="J1", face="left", margin_nm=mm(0.5)),),
        standoffs=True, vents=False, label=None, mount="holes", insert="M3",
        material="PLA",
    )
    kwargs.update(overrides)
    return EnclosureSpec(**kwargs)


@needs_build123d
def test_build_enclosure_lip_lid_with_holes_and_usb_cutout_passes(tmp_path: Path):
    envelope = _real_board(tmp_path / "holes.kicad_pcb")
    spec = _real_spec()
    report = verify_model(build_enclosure(spec, envelope), spec, envelope)
    assert report.passed, report.text()
    assert all(c.margin_nm >= 0 for c in report.clauses), report.text()
    assert "H1" in _clause(report, "standoff_concentric").detail
    assert "usb: admitted" in _clause(report, "cutout_admits_plug").detail


@needs_build123d
def test_build_enclosure_lid_none_passes(tmp_path: Path):
    envelope = _real_board(tmp_path / "holes.kicad_pcb")
    spec = _real_spec(lid="none", cutouts=())
    model = build_enclosure(spec, envelope)
    assert model.lid is None and model.lid_assembled is None
    report = verify_model(model, spec, envelope)
    assert report.passed, report.text()
    assert all(c.margin_nm >= 0 for c in report.clauses), report.text()
    assert _clause(report, "lid_mates").detail.startswith("nothing to check")


@needs_build123d
def test_overhang_scores_a_chamfered_round_corner_by_its_surface_not_its_facets():
    """A 45-degree chamfer swept round a rounded corner is a cone. Its chord
    facets lean further than the cone (a live 1 mm-radius case read 47.9
    degrees and failed); the surface itself is exactly at the limit."""
    b = _b3d()
    mn = (b.Align.MIN, b.Align.MIN, b.Align.MIN)
    block = b.fillet(b.Box(6, 6, 3, align=mn).edges().filter_by(b.Axis.Z), 1.0)
    block = b.chamfer(block.edges().group_by(b.Axis.Z)[0], 0.5)
    block = block.moved(b.Location((WALL + 4, WALL + 4, WALL)))
    report = verify_model(
        hand_model(base=build_base(extra=block)), hand_spec(), hand_envelope()
    )
    over = _clause(report, "overhang")
    assert over.passed, over.detail

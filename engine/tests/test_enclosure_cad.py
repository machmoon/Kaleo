"""Tests for :mod:`silkscreen.enclosure.cad`, the build123d emitter.

Gated on the ``cad`` extra exactly the way ``test_spice.py`` gates on
the ngspice binary: ``needs_build123d`` skips every kernel test when
build123d is absent, and one ungated test pins that the module still imports
and reports the kernel honestly.

Oracle discipline: expected positions are computed inline from the raw
board literals below (hole coordinates, outline corners, wall, clearance)
with the frame map written out by hand -- never by calling ``cad.py``'s own
helpers. Geometry claims are measured on the B-rep with volumes, which is
what the kernel verifier will do too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from silkscreen.enclosure.board_shape import board_envelope
from silkscreen.enclosure.cad import (
    build_enclosure,
    export_model,
    kernel_available,
)
from silkscreen.enclosure.errors import CutoutError
from silkscreen.enclosure.ir import Cutout, EnclosureSpec

needs_build123d = pytest.mark.skipif(
    not kernel_available(), reason="build123d not installed (pip install -e '.[cad]')"
)


def _nm(value_mm: float) -> int:
    """Raw mm literal -> nm, independent of silkscreen.units."""
    return int(round(value_mm * 1_000_000))


# ---------------------------------------------------------------- fixtures

# Board: outline (0,0)..(50,30) KiCad mm, 1.6 mm substrate. J1 is a USB-C
# receptacle hugging the left edge; U1 sits mid-board; H1..H4 are 3.2 mm
# NPTH mounting holes 4 mm in from each corner.
BOARD = dict(x0=0.0, y0=0.0, x1=50.0, y1=30.0, thickness=1.6)
HOLES = {"H1": (4.0, 4.0), "H2": (46.0, 4.0), "H3": (4.0, 26.0), "H4": (46.0, 26.0)}
HOLE_DRILL = 3.2
J1 = dict(cx=4.5, cy=15.0, half=4.5)
U1 = dict(cx=25.0, cy=15.0, half=4.5)
WALL, CLEARANCE, MARGIN = 2.0, 1.0, 0.5
# make_spec() defaults to a lip lid, which widens the board-to-wall gap to
# slack + ring + sliding fit (0.2 + 1.2 + 0.2) when the spec asks for less.
LIP_CLEARANCE = max(CLEARANCE, 0.2 + 1.2 + 0.2)

_STROKE = "(stroke (width 0.05) (type solid))"


def _hole(ref: str, x: float, y: float) -> str:
    lib = "MountingHole:MountingHole_3.2mm_M3"
    return f"""  (footprint "{lib}" (layer "F.Cu") (at {x} {y})
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_circle (center 0 0) (end {HOLE_DRILL} 0) {_STROKE} (layer "F.CrtYd"))
    (pad "" np_thru_hole circle (at 0 0) (size {HOLE_DRILL} {HOLE_DRILL})
      (drill {HOLE_DRILL}) (layers "*.Cu" "*.Mask"))
  )"""


def _part(ref: str, lib: str, cx: float, cy: float, half: float) -> str:
    return f"""  (footprint "{lib}" (layer "F.Cu") (at {cx} {cy})
    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start {-half} {-half}) (end {half} {half}) {_STROKE} (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )"""


def _board_text(*, holes: bool) -> str:
    x0, y0, x1, y1 = BOARD["x0"], BOARD["y0"], BOARD["x1"], BOARD["y1"]
    body = [
        _part("J1", "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
              J1["cx"], J1["cy"], J1["half"]),
        _part("U1", "Package_QFP:LQFP-48_7x7mm_P0.5mm", U1["cx"], U1["cy"], U1["half"]),
    ]
    if holes:
        body += [_hole(ref, x, y) for ref, (x, y) in HOLES.items()]
    return f"""(kicad_pcb (version 20240108) (generator "pcbnew")
  (generator_version "8.0")
  (general (thickness {BOARD["thickness"]}))
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (44 "Edge.Cuts" user)
    (47 "F.CrtYd" user "F.Courtyard")
  )
  (net 0 "")
{chr(10).join(body)}
  (gr_line (start {x0} {y0}) (end {x1} {y0}) {_STROKE} (layer "Edge.Cuts"))
  (gr_line (start {x1} {y0}) (end {x1} {y1}) {_STROKE} (layer "Edge.Cuts"))
  (gr_line (start {x1} {y1}) (end {x0} {y1}) {_STROKE} (layer "Edge.Cuts"))
  (gr_line (start {x0} {y1}) (end {x0} {y0}) {_STROKE} (layer "Edge.Cuts"))
)
"""


@pytest.fixture()
def envelope(tmp_path: Path):
    path = tmp_path / "holes.kicad_pcb"
    path.write_text(_board_text(holes=True), encoding="utf-8")
    return board_envelope(path)


@pytest.fixture()
def envelope_no_holes(tmp_path: Path):
    path = tmp_path / "plain.kicad_pcb"
    path.write_text(_board_text(holes=False), encoding="utf-8")
    return board_envelope(path)


def make_spec(**overrides) -> EnclosureSpec:
    kwargs = dict(
        wall_nm=_nm(WALL),
        clearance_nm=_nm(CLEARANCE),
        lid="lip",
        corner_radius_nm=_nm(3.0),
        cutouts=(Cutout(id="usb", ref="J1", face="left", margin_nm=_nm(MARGIN)),),
        standoffs=True,
        vents=False,
        label=None,
        mount="holes",
        insert="M3",
        material="PLA",
    )
    kwargs.update(overrides)
    return EnclosureSpec(**kwargs)


def _assembled_lid(model):
    return model.lid.moved(model.lid_assembled)


# ----------------------------------------------------------------- ungated


def test_module_imports_and_reports_the_kernel_honestly():
    assert isinstance(kernel_available(), bool)


# --------------------------------------------------------------- solids


@needs_build123d
def test_base_and_lid_are_single_valid_solids(envelope):
    model = build_enclosure(make_spec(), envelope)
    for name, part in (("base", model.base), ("lid", model.lid)):
        assert part.is_valid, name
        assert len(part.solids()) == 1, name
        assert part.volume > 0, name
    assert model.board.is_valid and model.board.volume > 0


@needs_build123d
def test_outer_bbox_matches_outer_nm_within_a_micron(envelope):
    model = build_enclosure(make_spec(), envelope)
    both = model.base + _assembled_lid(model)
    bb = both.bounding_box()
    ox, oy, oz = (v / 1e6 for v in model.outer_nm)
    assert abs(bb.min.X) <= 1e-3 and abs(bb.min.Y) <= 1e-3 and abs(bb.min.Z) <= 1e-3
    assert abs(bb.max.X - ox) <= 1e-3
    assert abs(bb.max.Y - oy) <= 1e-3
    assert abs(bb.max.Z - oz) <= 1e-3
    # Inline: outer = board + 2*clearance + 2*wall on each axis. A lip lid
    # needs slack + ring + a sliding fit between board edge and wall, so the
    # 1.0 mm the spec asked for is widened to 0.2 + 1.2 + 0.2 = 1.6 mm.
    lip_clearance = LIP_CLEARANCE
    assert ox == pytest.approx(BOARD["x1"] - BOARD["x0"] + 2 * lip_clearance + 2 * WALL)
    assert oy == pytest.approx(BOARD["y1"] - BOARD["y0"] + 2 * lip_clearance + 2 * WALL)
    assert any("clearance widened" in w for w in model.warnings)
    # A screw lid has no ring, so the spec's clearance is used as given.
    screw = build_enclosure(make_spec(lid="screw"), envelope)
    sx = screw.outer_nm[0] / 1e6
    # The screw bosses' strip on each X side is the boss diameter: the M3
    # bore (4.0, NopSCADlib) plus 1.6 mm of wall each side, never under the
    # 7 mm YAPP/k2f datum.
    strip = max(7.0, 4.0 + 2 * 1.6)
    expected = BOARD["x1"] - BOARD["x0"] + 2 * CLEARANCE + 2 * WALL + 2 * strip
    assert sx == pytest.approx(expected)


@needs_build123d
def test_base_never_enters_the_board_keepout(envelope):
    model = build_enclosure(make_spec(), envelope)
    assert (model.base & model.board).volume == pytest.approx(0.0, abs=1e-6)
    assert (_assembled_lid(model) & model.board).volume == pytest.approx(0.0, abs=1e-6)


@needs_build123d
def test_lid_and_base_do_not_interpenetrate_when_assembled(envelope):
    model = build_enclosure(make_spec(), envelope)
    assert (model.base & _assembled_lid(model)).volume == pytest.approx(0.0, abs=1e-6)


@needs_build123d
def test_usb_c_opening_admits_the_plug(envelope):
    model = build_enclosure(make_spec(), envelope)
    (cid, prism) = model.plugs[0]
    assert cid == "usb"
    assert prism.volume > 0
    # The plug prism minus the base is the whole prism: nothing in the way.
    assert (prism - model.base).volume == pytest.approx(prism.volume, rel=1e-6)
    # And it really does cross the left wall (x < 0 to inside the cavity).
    bb = prism.bounding_box()
    assert bb.min.X < 0.0 < WALL < bb.max.X


@needs_build123d
def test_mount_holes_puts_a_boss_concentric_with_each_hole(envelope):
    from build123d import Align, Cylinder, Pos

    model = build_enclosure(make_spec(mount="holes"), envelope)
    # Inline frame math from the raw literals: assembled x = wall + clearance
    # + (hx - x0); assembled y = wall + clearance + (y1 - hy) (the Y flip).
    expected = {
        ref: (WALL + LIP_CLEARANCE + (hx - BOARD["x0"]),
              WALL + LIP_CLEARANCE + (BOARD["y1"] - hy))
        for ref, (hx, hy) in HOLES.items()
    }
    recorded = {ref: (cx / 1e6, cy / 1e6) for ref, cx, cy in model.standoffs}
    assert set(recorded) == set(expected)
    for ref, (ex, ey) in expected.items():
        assert recorded[ref] == pytest.approx((ex, ey), abs=1e-6)
        # Probe the B-rep itself: a ring of 0.8 mm test pins 2.9 mm out from
        # the expected centre (inside a 7 mm boss, outside the 4.0 mm M3
        # bore), at mid-standoff height, must be entirely inside the base --
        # only a boss around that centre makes that so.
        z = WALL + 2.0
        for dx, dy in ((2.9, 0), (-2.9, 0), (0, 2.9), (0, -2.9)):
            pin = Pos(ex + dx, ey + dy, z) * Cylinder(
                0.4, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN)
            )
            left = (pin - model.base).volume
            assert left == pytest.approx(0.0, abs=1e-6), (ref, dx, dy, left)
        # And the insert bore is open at the centre.
        bore = Pos(ex, ey, WALL + 3.0) * Cylinder(
            0.5, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN)
        )
        assert (bore & model.base).volume == pytest.approx(0.0, abs=1e-6), ref


# NopSCADlib ``vitamins/inserts.scad``, transcribed here rather than imported
# from ``rules.py``: (printed hole, standoff-length insert, insert OD). The
# hole column is already the as-printed size, so a bore wider than it has
# nothing for the knurl to bite. ``F1BM2`` is 4.0 mm long and is its own
# short row; the others take the ``CNCK`` short lengths.
NOPSCAD_INSERTS = {
    "M2": (3.2, 4.0, 3.6),
    "M2.5": (4.0, 4.0, 4.6),
    "M3": (4.0, 3.0, 4.6),
    "M4": (5.6, 4.0, 6.3),
}
BORE_EXTRA, MIN_FLOOR, BOSS_WALL = 1.0, 1.2, 1.6  # rules.py, restated by hand


@needs_build123d
@pytest.mark.parametrize("insert", sorted(NOPSCAD_INSERTS))
def test_every_insert_gets_its_own_bore_standoff_and_boss(envelope, insert):
    """The bore is the insert's printed hole, the standoff is deep enough to
    swallow the whole insert over a floor, and the boss keeps a printable
    wall around the bore.

    The failure this pins is silent: a 0.3 mm-oversize bore, or a bore
    shortened to fit a 4 mm standoff, leaves the insert proud and the board
    resting on brass -- geometry no clause can see, because the insert is
    hardware, not modelled.
    """
    bore, length, od = NOPSCAD_INSERTS[insert]
    p = build_enclosure(make_spec(mount="holes", insert=insert), envelope).params_mm
    assert p["bore_d"] == pytest.approx(bore)
    assert p["insert_length"] == pytest.approx(length)
    assert p["bore_depth"] == pytest.approx(length + BORE_EXTRA)
    assert p["standoff_h"] >= p["bore_depth"] + MIN_FLOOR
    assert p["standoff_d"] >= bore + 2 * BOSS_WALL
    # And the boss is wider than the insert it swallows, M4 included.
    assert p["standoff_d"] > od


@needs_build123d
def test_self_tapping_screws_get_an_undersized_bore(envelope):
    """No insert: an M3 self-tapper cuts its own thread in a hole 0.3 mm
    under the 3.0 mm nominal, to the same depth as the short M3 insert."""
    p = build_enclosure(make_spec(mount="holes", insert="self_tap"), envelope).params_mm
    assert p["bore_d"] == pytest.approx(3.0 - 0.3)
    assert p["bore_depth"] == pytest.approx(NOPSCAD_INSERTS["M3"][1] + BORE_EXTRA)


@needs_build123d
def test_mount_holes_without_holes_falls_back_to_corners(envelope_no_holes):
    model = build_enclosure(make_spec(mount="holes"), envelope_no_holes)
    names = [name for name, _, _ in model.standoffs]
    assert sorted(names) == ["back_left", "back_right", "front_left", "front_right"]
    assert any("no mounting holes" in w and "corner" in w for w in model.warnings)
    assert (model.base & model.board).volume == pytest.approx(0.0, abs=1e-6)


@needs_build123d
def test_mount_pins_clears_the_drilled_board(envelope):
    model = build_enclosure(make_spec(mount="pins"), envelope)
    assert (model.base & model.board).volume == pytest.approx(0.0, abs=1e-6)
    assert "pin_d" in model.params_mm


@needs_build123d
def test_mount_none_seats_the_board_on_the_floor(envelope):
    model = build_enclosure(make_spec(mount="none", standoffs=False), envelope)
    assert model.standoffs == ()
    assert model.params_mm["standoff_h"] == 0.0
    assert model.params_mm["board_bottom"] == pytest.approx(WALL)


# ------------------------------------------------------------- lid styles


@needs_build123d
@pytest.mark.parametrize("lid", ["lip", "screw", "snap"])
def test_every_lid_style_builds_and_mates(envelope, lid):
    model = build_enclosure(make_spec(lid=lid, label="KAL"), envelope)
    assert model.lid is not None and model.lid_assembled is not None
    assert model.lid.is_valid and len(model.lid.solids()) == 1
    # Printed orientation: outer face on the bed, everything else above it.
    bb = model.lid.bounding_box()
    # The label is a deboss, so nothing stands proud of the bed face.
    assert model.params_mm["label_emboss"] == 0.0
    assert model.params_mm["label_depth"] > 0
    assert abs(bb.min.Z) <= 1e-3
    assert bb.max.Z > WALL - 1e-3
    assert (model.base & _assembled_lid(model)).volume == pytest.approx(0.0, abs=1e-6)


@needs_build123d
def test_lid_none_is_absent_and_the_case_is_the_base(envelope):
    model = build_enclosure(make_spec(lid="none", cutouts=()), envelope)
    assert model.lid is None
    assert model.lid_assembled is None
    assert model.params_mm["lid_z"] == 0.0
    bb = model.base.bounding_box()
    assert abs(bb.max.Z - model.outer_nm[2] / 1e6) <= 1e-3


@needs_build123d
def test_lip_lid_ring_descends_into_the_cavity(envelope):
    model = build_enclosure(make_spec(lid="lip"), envelope)
    lid = _assembled_lid(model)
    base_z = model.params_mm["base_z"]
    low = lid.bounding_box().min.Z
    assert abs(low - (base_z - model.params_mm["lip_depth"])) <= 1e-3
    assert model.params_mm["lip_depth"] > 0


@needs_build123d
def test_vents_and_top_window_pierce_the_lid(envelope):
    plain = build_enclosure(make_spec(cutouts=()), envelope)
    vented = build_enclosure(make_spec(cutouts=(), vents=True), envelope)
    windowed = build_enclosure(
        make_spec(
            cutouts=(Cutout(id="win", ref="U1", face="top", margin_nm=_nm(MARGIN)),)
        ),
        envelope,
    )
    assert vented.params_mm["vent_count"] > 0
    assert vented.lid.volume < plain.lid.volume
    assert windowed.lid.volume < plain.lid.volume
    # The window is the courtyard plus margin on both axes (inline literal).
    want = 2 * U1["half"] + 2 * MARGIN
    assert windowed.params_mm["cutout_win_w"] == pytest.approx(want)
    assert windowed.params_mm["cutout_win_h"] == pytest.approx(want)


# ---------------------------------------------------------------- cutouts


@needs_build123d
def test_cutout_on_absent_ref_is_a_hard_error(envelope):
    spec = make_spec(cutouts=(Cutout(id="ghost", ref="J9", face="left", margin_nm=0),))
    with pytest.raises(CutoutError, match="J9"):
        build_enclosure(spec, envelope)


@needs_build123d
def test_cutout_on_a_face_the_part_is_nowhere_near_is_a_hard_error(envelope):
    # U1 is mid-board, 20 mm from the left edge.
    spec = make_spec(cutouts=(Cutout(id="mid", ref="U1", face="left", margin_nm=0),))
    with pytest.raises(CutoutError, match="left"):
        build_enclosure(spec, envelope)


@needs_build123d
def test_cutout_on_unknown_face_is_a_hard_error(envelope):
    spec = make_spec(cutouts=(Cutout(id="odd", ref="J1", face="bottom", margin_nm=0),))
    with pytest.raises(CutoutError, match="bottom"):
        build_enclosure(spec, envelope)


# ----------------------------------------------------------------- export


@needs_build123d
def test_export_writes_three_files_and_step_reimports_two_solids(envelope, tmp_path):
    from build123d import import_step

    model = build_enclosure(make_spec(), envelope)
    out = tmp_path / "out"
    paths = export_model(model, out, "enclosure")
    assert paths.step.name == "enclosure.step"
    assert paths.base_stl.name == "enclosure-base.stl"
    assert paths.lid_stl.name == "enclosure-lid.stl"
    for p in (paths.step, paths.base_stl, paths.lid_stl):
        assert p.exists() and p.stat().st_size > 84, p
    # Three files and no fourth: the OpenSCAD preview wrapper is gone
    # (docs/ai-cad-plan.md v3), so nothing here writes a .scad.
    assert sorted(p.name for p in out.iterdir()) == [
        "enclosure-base.stl", "enclosure-lid.stl", "enclosure.step",
    ]
    imported = import_step(paths.step)
    assert len(imported.solids()) == 2
    assert sorted(c.label for c in imported.children) == ["base", "lid"]


@needs_build123d
def test_the_step_is_self_contained_ascii(envelope, tmp_path):
    """STEP is what the one-shot /generate route ships inline, so it has to
    be text that stands on its own -- an ISO 10303-21 exchange file, not a
    wrapper importing companion meshes the response does not carry."""
    model = build_enclosure(make_spec(label="KAL"), envelope)
    paths = export_model(model, tmp_path / "out", "case")
    text = paths.step.read_text(encoding="utf-8")
    assert text.startswith("ISO-10303-21;")
    assert "END-ISO-10303-21;" in text
    assert "DATA;" in text
    assert ".stl" not in text


@needs_build123d
def test_params_receipt_is_all_floats(envelope):
    model = build_enclosure(make_spec(lid="screw", vents=True, label="KAL"), envelope)
    assert model.params_mm
    for name, value in model.params_mm.items():
        assert type(value) is float, name
        assert value == round(value, 3), name


def _corner_connector_board(tmp_path: Path):
    """No mounting holes and a JST-PH housing in the front-left corner, the
    shape of a generated board whose connector sits where a screw boss goes."""
    text = _board_text(holes=False).replace(
        "  (gr_line",
        _part("J2", "Connector_JST:JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical",
              4.5, 25.5, 4.0) + "\n  (gr_line",
        1,
    )
    path = tmp_path / "corner.kicad_pcb"
    path.write_text(text, encoding="utf-8")
    return board_envelope(path)


@needs_build123d
def test_screw_lid_drops_the_boss_standing_in_a_corner_plug_path(tmp_path):
    """Found on a live run: the front-left screw boss filled the gap between the
    wall and a JST housing, so the case built and the plug could not go in
    (cutout_admits_plug: 113 mm^3 blocked). The boss is skipped by name."""
    from silkscreen.enclosure.kernel import verify_model

    env = _corner_connector_board(tmp_path)
    spec = make_spec(
        lid="screw", mount="corners",
        cutouts=(Cutout(id="jst", ref="J2", face="left", margin_nm=_nm(MARGIN)),),
    )
    model = build_enclosure(spec, env)
    report = verify_model(model, spec, env)
    plug = next(c for c in report.clauses if c.name == "cutout_admits_plug")
    assert plug.passed, plug.detail
    assert any(
        "no screw boss at screw_front_left" in w and "'jst'" in w
        for w in model.warnings
    ), model.warnings
    assert not any("screw_back_right" in w for w in model.warnings)

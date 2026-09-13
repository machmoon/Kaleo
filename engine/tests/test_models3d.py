"""``models3d.py``: the library 3D model each generated land pattern claims.

Two kinds of test. The offline ones pin the table's honesty: every mapped
package answers a model, every unmapped one answers a reason, and the emitter
writes exactly one model line per part that has one. The gated one runs only
where KiCad's model library is installed and checks that every path the table
can produce names a file that exists there -- a renamed library file would
otherwise leave a dangling path that KiCad reports as a missing model and the
GLB export silently draws without.
"""

import glob
import os
import re
from pathlib import Path

import pytest
from silkscreen.board import BoardResult, PlacedPart, emit_kicad_pcb
from silkscreen.footprints import (
    chip_passive,
    dual_row_header,
    for_passive,
    lqfp,
    soic,
    sot23,
    sot223,
    switch,
)
from silkscreen.models3d import MODELS_VAR, Model3D, model_for, why_unmatched
from silkscreen.units import mm

#: Where KiCad installs its model library, per platform (the same
#: found-never-assumed convention as ``service/kicad_cli.py``).
_LIBRARY_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels",
    "/usr/share/kicad/3dmodels",
    "C:/Program Files/KiCad/*/share/kicad/3dmodels",
)


def _installed_library() -> Path | None:
    configured = os.environ.get("KICAD_3DMODELS_DIR", "").strip()
    if configured:
        return Path(configured) if Path(configured).is_dir() else None
    for pattern in _LIBRARY_CANDIDATES:
        for hit in sorted(glob.glob(pattern)):
            if Path(hit).is_dir():
                return Path(hit)
    return None


def _every_model() -> list[Model3D]:
    """Every distinct model the table can answer, over the packages the
    emitter can draw."""
    found: dict[str, Model3D] = {}
    for size in ("0402", "0603", "0805", "1206", "1210"):
        for ref in ("R1", "C1", "L1", "D1"):
            model = model_for(f"C_{size}", ref)
            if ref == "D1" and size in ("0402", "1210"):
                assert model is None  # no such diode in the library
                continue
            assert model is not None
            found[model.path] = model
    for name in (
        "SOT-23",
        "SOT-223-3_TabPin2",
        "SOIC-8",
        "SOIC-14",
        "SOIC-16",
        "LQFP-32",
        "LQFP-48",
        "LQFP-64",
        "LQFP-100",
        "LQFP-144",
        "USB_C_Receptacle_Power",
        "SW_SPST_TL3305A",
    ):
        model = model_for(name, "U1")
        assert model is not None, name
        found[model.path] = model
    return list(found.values())


def test_chip_model_follows_the_reference_prefix_not_the_pattern():
    # One rectangle, four parts: the ref is the only thing that tells them apart.
    assert model_for("C_0603", "R3").name == "R_0603_1608Metric"
    assert model_for("C_0603", "C3").name == "C_0603_1608Metric"
    assert model_for("C_0805", "L1").name == "L_0805_2012Metric"
    assert model_for("C_0603", "D2").name == "D_0603_1608Metric"
    assert model_for("C_0603", "R3").library == "Resistor_SMD"


def test_every_path_uses_the_legacy_alias_every_kicad_resolves():
    for model in _every_model():
        assert model.path.startswith(f"${{{MODELS_VAR}}}/")
        assert model.path.endswith(".step")
        assert re.fullmatch(
            r"\$\{KISYS3DMOD\}/[\w.-]+\.3dshapes/[\w.-]+\.step", model.path
        )


def test_generated_footprint_names_are_the_ones_the_table_keys_on():
    # The table is keyed on Footprint.name; if a generator renames its
    # pattern the model silently vanishes, which is what this pins.
    assert model_for(chip_passive("0603").name, "R1") is not None
    # for_passive renames the chip to the type's initial (R_0603, Y_1210).
    assert model_for(for_passive("resistor", "10k").name, "R1").name.startswith("R_")
    assert model_for(for_passive("inductor", "10uH").name, "L1").name.startswith("L_")
    assert model_for(for_passive("crystal", "8MHz").name, "Y1") is None
    assert model_for(sot23().name, "U1") is not None
    assert model_for(sot223().name, "U1") is not None
    assert model_for(soic(8).name, "U1") is not None
    assert model_for(lqfp(48).name, "U1") is not None
    # The dual-row header is the one generated pattern whose name cannot pin
    # a library body: see test_a_dual_row_header_claims_no_model below.
    header = dual_row_header(30, row_spacing_mm=15.24, body_w_mm=17.78, body_h_mm=43.18)
    assert model_for(header.name, "U1") is None


@pytest.mark.parametrize(
    "name, ref, fragment",
    [
        ("Y_1210", "Y1", "crystal"),
        ("LQFP-44", "U1", "0.8 mm-pitch"),
        ("SOIC-20", "U1", "wide body"),
        ("SOIC-4", "U1", "no SOIC-4"),
        ("C_0603", "Q1", "'Q'"),
        ("C_0402", "D1", "Diode_SMD library has no 0402"),
        ("Unknown-99", "U1", "Unknown-99"),
    ],
)
def test_unmatched_packages_answer_none_with_a_reason(name, ref, fragment):
    assert model_for(name, ref) is None
    assert fragment in why_unmatched(name, ref)


def test_emitter_writes_one_model_line_per_part_that_has_one():
    parts = [
        PlacedPart("R1", chip_passive("0603"), "10k", mm(2), mm(2)),
        PlacedPart("Y1", chip_passive("1210"), "8MHz", mm(6), mm(2)),
        PlacedPart("U1", sot223(), "AMS1117", mm(10), mm(5)),
    ]
    board = BoardResult(parts, [], mm(20), mm(15), "OPTIMAL")
    text = emit_kicad_pcb(board)
    lines = [line for line in text.splitlines() if line.strip().startswith("(model ")]
    assert len(lines) == 2
    assert "Resistor_SMD.3dshapes/R_0603_1608Metric.step" in lines[0]
    assert "Package_TO_SOT_SMD.3dshapes/SOT-223.step" in lines[1]
    # The model line sits inside its footprint, after the descr it belongs to.
    assert text.index('(descr "') < text.index("(model ")
    assert text.index("(model ") < text.index('(property "Reference" "R1"')
    # A chip resistor's frames agree, so its offset is zero -- written as
    # floats now that `Model3D.offset_mm` is a real field rather than a
    # hard-coded literal in the emitter.
    assert "(offset (xyz 0.0 0.0 0.0))" in lines[0]


@pytest.mark.skipif(
    _installed_library() is None,
    reason="KiCad's 3D model library is not installed (KICAD_3DMODELS_DIR)",
)
def test_every_mapped_model_exists_in_the_installed_library():
    library = _installed_library()
    missing = []
    for model in _every_model():
        relative = model.path.replace(f"${{{MODELS_VAR}}}/", "", 1)
        if not (library / relative).is_file():
            missing.append(relative)
    assert not missing, f"models the table names but the library lacks: {missing}"


# ------------------------------------------------------- model orientation

#: Where KiCad installs its footprint library, beside the models.
_FOOTPRINT_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "/usr/share/kicad/footprints",
    "C:/Program Files/KiCad/*/share/kicad/footprints",
)


def _installed_footprints() -> Path | None:
    configured = os.environ.get("KICAD_FOOTPRINT_DIR", "").strip()
    if configured:
        return Path(configured) if Path(configured).is_dir() else None
    for pattern in _FOOTPRINT_CANDIDATES:
        for hit in sorted(glob.glob(pattern)):
            if Path(hit).is_dir():
                return Path(hit)
    return None


def _one_part_board(fp, ref: str) -> str:
    part = PlacedPart(ref, fp, x_nm=mm(5), y_nm=mm(5))
    return emit_kicad_pcb(
        BoardResult(parts=[part], nets=[], width_nm=mm(20), height_nm=mm(20),
                    solver_status="manual")
    )


def test_a_diode_model_is_turned_onto_its_cathode_and_nothing_else_is():
    """KiCad's ``D_*`` models put the band on pad 1; this pipeline's diode has
    the anode on pin 1, so the model turns 180 to land the band on pad 2.
    Symmetric passives and the fixed packages share the library's frame and
    stay at 0 -- a turn there would be the very lie this guards against."""
    assert model_for("D_0603", "D1").rotate_deg == 180
    assert model_for("R_0603", "R1").rotate_deg == 0
    assert model_for("SOIC-8", "U1").rotate_deg == 0
    assert model_for("SOT-223-3_TabPin2", "U1").rotate_deg == 0
    diode = _one_part_board(for_passive("diode", "LED"), "D1")
    assert "(rotate (xyz 0 0 180))" in diode
    assert "(rotate (xyz 0 0 0))" not in diode
    ic = _one_part_board(soic(8), "U1")
    assert "(rotate (xyz 0 0 0))" in ic


#: Generated pattern, the pad on it the library's pad 1 corresponds to, and
#: the reference whose prefix picks the model. The diode is the one whose
#: correspondence is not identity: library pad 1 is the cathode, ours is 2.
_ORIENTATION_CASES = [
    (soic(8), "1", "U1"),
    (soic(16), "1", "U1"),
    (sot23(), "1", "Q1"),
    (sot223(), "1", "U1"),
    (lqfp(48), "1", "U1"),
    (lqfp(144, body_mm=20.0), "1", "U1"),
    (for_passive("diode", "LED"), "2", "D1"),
    # The tactile switch: our pad 1 is the library's pad 1, no rotation. This
    # is the check that catches a mirrored land pattern on the one connector-
    # family part that has a model to be mirrored against.
    (switch("SW_SPST_TL3305A"), "1", "SW1"),
]


@pytest.mark.skipif(
    _installed_footprints() is None,
    reason="KiCad's footprint library is not installed (KICAD_FOOTPRINT_DIR)",
)
@pytest.mark.parametrize("fp,our_pad,ref", _ORIENTATION_CASES, ids=lambda c: str(c))
def test_the_models_pad_1_lands_on_our_pad_1_after_its_rotation(fp, our_pad, ref):
    """The library model is drawn for the library footprint of the same name.
    Turn that footprint's pad 1 by the rotation we write and it must sit in
    the same quadrant as the pad we claim it is -- otherwise the 3D view (and
    the order step's GLB) shows pin 1, or the cathode band, on the wrong pad.
    Before the fix every IC pattern was the library's mirror image."""
    from kiutils.footprint import Footprint as LibFootprint

    model = model_for(fp.name, ref)
    assert model is not None
    library = _installed_footprints()
    path = library / f"{model.library}.pretty" / f"{model.name}.kicad_mod"
    assert path.is_file(), path
    lib = LibFootprint.from_file(str(path))
    lib_pad = next(p for p in lib.pads if p.number == "1")
    x, y = lib_pad.position.X, lib_pad.position.Y
    assert model.rotate_deg in (0, 180)
    if model.rotate_deg == 180:
        x, y = -x, -y
    ours = fp.pad_by_number(our_pad)

    def sign(v: float) -> int:
        return 0 if abs(v) < 1e-6 else (1 if v > 0 else -1)

    assert (sign(x), sign(y)) == (sign(ours.x_nm), sign(ours.y_nm)), (
        f"{fp.name}: library pad 1 turned by {model.rotate_deg} sits at "
        f"({x}, {y}) mm, our pad {our_pad} at "
        f"({ours.x_nm / 1e6}, {ours.y_nm / 1e6}) mm"
    )




# --------------------------------------------------- connectors and power entry

#: Every connector / power-entry package the connector contract fixes, with
#: the ``enclosure.rules.connector_class`` it must resolve to. These three
#: tables -- the model map here, ``PLUG_ENVELOPES`` and ``HEIGHTS_NM`` -- are
#: keyed on the same footprint names and are only useful if they agree, which
#: is what the tests below pin. Before this row existed ``connector_class``
#: answered None for every board this engine could emit, so the case cut no
#: opening for a power connector it had no way to recognise.
_CONNECTOR_PACKAGES = [
    ("Barrel_Jack_5.5x2.1mm", "Barrel_Jack"),
    ("TerminalBlock_2P_5.08mm", "TerminalBlock"),
    ("JST_PH_2P", "JST_PH"),
    ("JST_PH_3P", "JST_PH"),
    ("JST_PH_4P", "JST_PH"),
    ("PinHeader_1x02_P2.54mm", "PinHeader"),
    ("PinHeader_1x03_P2.54mm", "PinHeader"),
    ("PinHeader_1x04_P2.54mm", "PinHeader"),
    ("PinHeader_1x05_P2.54mm", "PinHeader"),
    ("PinHeader_1x06_P2.54mm", "PinHeader"),
    ("PinHeader_1x08_P2.54mm", "PinHeader"),
    ("PinHeader_1x10_P2.54mm", "PinHeader"),
    ("USB_C_Receptacle_Power", "USB_C"),
    # A button is a connector class in the sense that matters here: something
    # has to reach it through the wall. PLUG_ENVELOPES sizes the "SW_"
    # opening from the courtyard rather than from a plug cross-section, which
    # is right -- what enters is a finger or a cap, not a mating half.
    ("SW_SPST_TL3305A", "SW_"),
]

#: Battery packages. They are deliberately *not* connector classes: nothing
#: mates with a coin cell through the wall, so a cutout would be a hole in the
#: case for no reason. They still need a height, since the lid closes over one.
_BATTERY_PACKAGES = ["BatteryHolder_CR2032", "BatteryHolder_AAA_1x"]

#: Test points are deliberately in neither list. Nothing mates with one
#: through the wall (a probe touches it with the case open), so it is not a
#: connector class; and its height is a real, deliberate **zero** -- a bare
#: pad has no body -- so it cannot ride the "must be greater than zero"
#: assertion the parts above share. Both facts are checked below instead.
_TESTPOINT_PACKAGE = "TestPoint_Pad_1.5x1.5mm"


@pytest.mark.parametrize("name, expected", _CONNECTOR_PACKAGES)
def test_every_connector_package_resolves_to_its_enclosure_class(name, expected):
    from silkscreen.enclosure.rules import connector_class

    assert connector_class(name) == expected


@pytest.mark.parametrize("name", _BATTERY_PACKAGES)
def test_a_battery_holder_is_not_a_connector_class(name):
    from silkscreen.enclosure.rules import connector_class

    assert connector_class(name) is None


@pytest.mark.parametrize(
    "name", [n for n, _ in _CONNECTOR_PACKAGES] + _BATTERY_PACKAGES
)
def test_every_connector_package_has_a_measured_height(name):
    """A part with no height entry takes DEFAULT_HEIGHT_NM (3 mm). For a
    header that is 5.5 mm of crushed plastic, so every package the emitter can
    draw must answer a real number, never the default."""
    from silkscreen.enclosure.heights import height_for

    height_nm, was_default = height_for(name)
    assert not was_default, f"{name} fell through to DEFAULT_HEIGHT_NM"
    assert height_nm > 0


def test_a_test_pad_needs_no_opening_and_stands_no_height():
    """Zero here is a measurement, not a missing entry, which is why
    ``height_for`` is asked for its fallback flag as well as its number: a
    bare pad has nothing above the board, and cutting a hole in the case for
    something a probe only touches with the lid off would be a hole for no
    reason."""
    from silkscreen.enclosure.heights import height_for
    from silkscreen.enclosure.rules import connector_class

    assert connector_class(_TESTPOINT_PACKAGE) is None
    height_nm, was_default = height_for(_TESTPOINT_PACKAGE)
    assert (height_nm, was_default) == (0, False)


def test_the_tactile_switch_stands_the_actuator_the_pipeline_actually_draws():
    """The cross-lane one: ``footprints`` draws the TL3305**A** and
    ``enclosure.heights`` sizes the cavity.

    KiCad's own models measure the TL3305 actuator at 3.8 mm (A), 5.0 (B) and
    7.0 (C), and the A is the only one this pipeline has a land pattern for.
    The height table said 3.5 mm, which is under the part -- a lid built to it
    rests on the button and holds it closed. This is the one direction that
    table refuses to err in, so the number is pinned to the model's own Z.

    The assertion is on ``height_for`` rather than the table key because the
    lookup takes the *longest* matching key: a shorter ``"TL3305"`` row would
    lose to ``"SW_SPST"`` and change nothing.
    """
    from silkscreen.enclosure.heights import height_for

    name = switch("SW_SPST_TL3305A").name
    height_nm, was_default = height_for(name)
    assert not was_default
    assert height_nm == mm(3.8)


def test_a_screw_terminal_opening_is_its_own_face_not_an_invented_plug():
    """A screw terminal has no mating plug -- bare wire goes in and a
    screwdriver has to reach the screws -- so both envelope axes are the zero
    that means "the part's own extent plus the loose-fit margin"."""
    from silkscreen.enclosure.rules import PLUG_ENVELOPES, plug_envelope

    entry = PLUG_ENVELOPES["TerminalBlock"]
    assert (entry.width_nm, entry.height_nm) == (0, 0)
    opening = plug_envelope(
        "TerminalBlock", courtyard_width_nm=mm(12), part_height_nm=mm(13.8)
    )
    assert opening.width_nm > mm(12)
    assert opening.height_nm > mm(13.8)
    assert not opening.round


def test_the_usb_c_receptacle_claims_the_part_it_was_drawn_from():
    """The one connector with a model: ``footprints._usb_c_power`` draws the
    GCT USB4125 6-way, and that library footprint is body-anchored, so its
    model needs no offset the emitter cannot write."""
    model = model_for("USB_C_Receptacle_Power", "J1")
    assert model is not None
    assert model.library == "Connector_USB"
    assert model.name == "USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal"
    assert model.rotate_deg == 0


@pytest.mark.parametrize("name, ref, fragment", [
    # No model of the part the pattern was drawn from.
    ("Barrel_Jack_5.5x2.1mm", "J1", "no PJ-102AH model"),
    ("BatteryHolder_CR2032", "BT1", "no 3002 model"),
    # The library has the model; the two frames do not share an origin.
    ("TerminalBlock_2P_5.08mm", "J1", "(2.54, -0.3) mm offset"),
    ("BatteryHolder_AAA_1x", "BT1", "(22.35, 0) mm offset"),
    # The headers and the JST sizes used to be here for the same reason -- a
    # frame the emitter could not express. `Model3D.offset_mm` expresses it
    # now, so they claim their models and are covered by
    # `test_a_connectors_model_offset_is_the_measured_frame_difference`.
])
def test_the_connectors_with_no_honest_counterpart_say_so(name, ref, fragment):
    assert model_for(name, ref) is None
    assert fragment in why_unmatched(name, ref)


@pytest.mark.parametrize(
    "name, drawn",
    [
        ("PinHeader_1x07_P2.54mm", "1x02"),
        ("PinHeader_1x40_P2.54mm", "1x10"),
        ("JST_PH_6P", "2P"),
    ],
)
def test_an_undrawn_header_size_says_that_and_not_a_frame_mismatch(name, drawn):
    """The join between the connector lane and the offset lane.

    These branches used to answer "KiCad has the model, but the emitter writes
    a zero offset so the body would sit N mm from the pads". Both halves of
    that are now false: ``Model3D.offset_mm`` carries the translation and
    ``board.emit_kicad_pcb`` writes it, which is exactly why every size
    ``footprints.py`` *does* draw now claims its model. Reaching this branch
    therefore means the size is not drawn here at all, and that is the reason
    a BOM row must carry -- a stale reason sends someone to fix a frame that
    is already fixed.
    """
    assert model_for(name, "J1") is None
    reason = why_unmatched(name, "J1")
    assert "not drawn here" in reason or "is drawn here" in reason
    assert drawn in reason
    assert "offset" not in reason


def test_a_dual_row_header_claims_no_model():
    """This entry used to claim ``PinHeader_2xNN_P2.54mm_Vertical``, which is
    wrong on both axes it certifies: KiCad's part is a 2.54 mm-wide double row
    numbered alternately across the rows, while ``dual_row_header`` straddles a
    module at 15.24 mm and numbers down one column then back up the other. The
    name carries the pitch and never the row spacing, so no name in the family
    can pin a body -- and a module drawn as a 2.54 mm header is exactly the
    picture-of-a-different-part this module refuses to produce."""
    for pins in (4, 30, 40):
        name = dual_row_header(pins).name
        assert model_for(name, "U1") is None, name
        reason = why_unmatched(name, "U1")
        assert "row spacing" in reason
        assert f"PinHeader_2x{pins // 2:02d}_P2.54mm_Vertical" in reason


# ------------------------------------------------- one origin, two libraries

#: Every generated pattern the table maps, and the reference whose prefix
#: picks its model. The gated test below is the one that would have caught
#: the first draft of the connector row, where six of eight entries named the
#: right part in the wrong frame.
def _mapped_patterns():
    from silkscreen.footprints import connector

    cases = [
        (chip_passive("0603"), "R1"),
        (for_passive("diode", "LED"), "D1"),
        (soic(8), "U1"),
        (soic(16), "U1"),
        (sot23(), "Q1"),
        (sot223(), "U1"),
        (lqfp(48), "U1"),
        (lqfp(144, body_mm=20.0), "U1"),
        (connector("USB_C_Receptacle_Power"), "J1"),
    ]
    return [(fp, ref) for fp, ref in cases if model_for(fp.name, ref) is not None]


@pytest.mark.skipif(
    _installed_footprints() is None,
    reason="KiCad's footprint library is not installed (KICAD_FOOTPRINT_DIR)",
)
@pytest.mark.parametrize(
    "fp, ref", _mapped_patterns(), ids=lambda c: getattr(c, "name", str(c))
)
def test_a_mapped_model_declares_the_offset_its_frames_differ_by(fp, ref):
    """A model authored about the *library* origin has to be told where ours
    is, or the body sits beside the pads instead of on them -- and nothing
    raises, because DRC does not read models and the GLB draws the part
    happily in the wrong place.

    This test used to assert the two frames *shared* an origin, which was the
    right invariant only while ``emit_kicad_pcb`` hard-coded
    ``(offset (xyz 0 0 0))``. ``Model3D.offset_mm`` now carries the
    difference, so the rule is no longer "they must agree" but "whatever they
    differ by, the model must declare it".

    The invariant is the library footprint's own ``F.Fab`` body outline: it
    must be centred on the library origin, because this pipeline's patterns are
    package-centred (footprints.py's Frame note) and are built by recentring the
    library part onto that outline. When the two agree the model lands on the
    pads; when they do not, the gap between them *is* the displacement. Pads
    cannot be compared directly for this -- our lands differ from the library's
    in size, and the SOT-223 numbers its tab pin 2 where KiCad numbers it 4 --
    but the body outline is the same rectangle in both.

    KiCad anchors its through-hole connectors on pin 1, so their F.Fab centre
    is not their origin: 11.43 mm out on a 1x10 header, (1.0, 2.45) on a JST PH.
    That is what this catches, and the reason those packages answer None until
    ``Model3D`` can carry an offset the emitter writes."""
    from kiutils.footprint import Footprint as LibFootprint

    model = model_for(fp.name, ref)
    assert model is not None
    library = _installed_footprints()
    path = library / f"{model.library}.pretty" / f"{model.name}.kicad_mod"
    assert path.is_file(), path
    lib = LibFootprint.from_file(str(path))

    points: list[tuple[float, float]] = []
    for item in lib.graphicItems:
        if getattr(item, "layer", None) != "F.Fab":
            continue
        for attr in ("start", "end", "center", "position"):
            point = getattr(item, attr, None)
            if point is not None:
                points.append((point.X, point.Y))
        for point in getattr(item, "coordinates", None) or []:
            points.append((point.X, point.Y))
    assert points, f"{model.name} has no F.Fab outline to measure"
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    # How far the library part is anchored from its own body centre, which is
    # where our pattern's origin sits. Y negated because `(offset (xyz ...))`
    # is Y-up and the footprint file is Y-down -- settled by rendering a 1x10
    # header both ways: +11.43 lands the plastic on the pads, -11.43 slides
    # it 22.86 mm off the part.
    assert model.offset_mm == pytest.approx((-cx, cy, 0.0), abs=2e-3), (
        f"{fp.name} -> {model.name}: the library part is anchored "
        f"({cx:.3f}, {cy:.3f}) mm off its own body, so the model needs an "
        f"offset of ({-cx:.3f}, {cy:.3f}, 0.0); it declares {model.offset_mm}"
    )


# --------------------------------------------------------------- model offset


def _library_pads(path):
    """``{pad number: (x_mm, y_mm)}`` read straight out of a ``.kicad_mod``."""
    import re

    text = path.read_text()
    pads = {}
    for match in re.finditer(r'\(pad\s+"([^"]+)"', text):
        segment = text[match.start() : match.start() + 400]
        at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)", segment)
        if at:
            pads[match.group(1)] = (float(at.group(1)), float(at.group(2)))
    return pads


_OFFSET_PAIRS = {
    "PinHeader_1x02_P2.54mm": (
        "Connector_PinHeader_2.54mm.pretty",
        "PinHeader_1x02_P2.54mm_Vertical.kicad_mod",
    ),
    "PinHeader_1x10_P2.54mm": (
        "Connector_PinHeader_2.54mm.pretty",
        "PinHeader_1x10_P2.54mm_Vertical.kicad_mod",
    ),
    "JST_PH_2P": (
        "Connector_JST.pretty",
        "JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal.kicad_mod",
    ),
    "JST_PH_4P": (
        "Connector_JST.pretty",
        "JST_PH_S4B-PH-K_1x04_P2.00mm_Horizontal.kicad_mod",
    ),
    "USB_C_Receptacle_Power": (
        "Connector_USB.pretty",
        "USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal.kicad_mod",
    ),
}


@pytest.mark.skipif(
    _installed_footprints() is None,
    reason="KiCad's footprint library is not installed (KICAD_FOOTPRINT_DIR)",
)
def test_a_connectors_model_offset_is_the_measured_frame_difference():
    """The offset must be derived from the two frames, not from a table.

    KiCad anchors a through-hole connector on **pin 1**; these land patterns
    are recentred on the body. The model is authored about the library
    origin, so writing ``(offset (xyz 0 0 0))`` -- which the emitter did for
    every part -- put the plastic beside the pads. On a 1x10 header that is
    11.43 mm, and nothing raises: DRC does not read 3D models and the GLB
    renders it happily in the wrong place.

    The expected value here is computed from the library file, never from
    ``models3d``'s own numbers, so a hand-edited offset cannot pass.
    """
    from silkscreen.footprints import connector
    from silkscreen.models3d import model_for

    for package, (pretty, mod) in _OFFSET_PAIRS.items():
        path = _installed_footprints() / pretty / mod
        if not path.is_file():
            continue
        library = _library_pads(path)
        ours = {p.number: (p.x_nm / 1e6, p.y_nm / 1e6) for p in connector(package).pads}
        shared = sorted(set(library) & set(ours))
        assert shared, f"{package}: no pad numbers in common with {mod}"

        deltas = {
            (
                round(ours[n][0] - library[n][0], 4),
                round(ours[n][1] - library[n][1], 4),
            )
            for n in shared
        }
        assert len(deltas) == 1, (
            f"{package}: the two frames differ by more than a translation "
            f"({deltas}); the pattern is not this library part"
        )
        tx, ty = deltas.pop()

        model = model_for(package, "J1")
        assert model is not None, f"{package} claims no model"
        # Y is negated: `(offset (xyz ...))` is Y-up, the footprint file is
        # Y-down. Verified by rendering both signs on a 1x10 header --
        # +11.43 lands the body on the pads, -11.43 slides it 22.86 mm off.
        assert model.offset_mm == pytest.approx((tx, -ty, 0.0), abs=1e-4), (
            f"{package}: offset {model.offset_mm} does not match the measured "
            f"frame difference ({tx}, {-ty}, 0.0)"
        )


def test_the_emitted_board_carries_each_models_offset():
    """A computed offset that never reaches the file fixes nothing."""
    from silkscreen.board import build_board, emit_kicad_pcb
    from silkscreen.netlist import parse_circuit_spec

    spec = parse_circuit_spec(
        """{
        "devices": {"hdr": {"kind": "connector",
                            "package": "PinHeader_1x04_P2.54mm",
                            "pins": {"A": "1", "B": "2", "C": "3", "D": "4"}}},
        "passives": {"r1": {"type": "resistor", "value": "1k"}},
        "nets": {"N1": ["hdr.A", "r1.1"], "N2": ["hdr.D", "r1.2"]}
        }"""
    )
    text = emit_kicad_pcb(build_board(spec, time_limit_s=5.0))
    assert "(offset (xyz 0.0 3.81 0.0))" in text
    # And a part whose frames DO agree still writes zeros.
    assert "(offset (xyz 0.0 0.0 0.0))" in text

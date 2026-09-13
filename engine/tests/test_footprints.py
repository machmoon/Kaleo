"""Land-pattern geometry checks for engine/silkscreen/footprints.py.

KiCad's default netclass clearance is 0.2mm. A land pattern that puts
adjacent same-side pads closer than that trips DRC on every board carrying
the part (this is exactly what happened with the SOT-223-3 pattern -- see
CLAUDE.md's "Known issues", formerly item 11). These tests compute the gap
between adjacent pad edges directly from each Pad's own x/y/w/h fields,
independently of any helper in footprints.py that might already assume the
pads are correctly spaced.
"""

from __future__ import annotations

import glob
import math
import os
from pathlib import Path

import pytest
from silkscreen.footprints import (
    BATTERY_PACKAGES,
    CONNECTOR_PACKAGES,
    SILK_PAD_CLEARANCE_NM,
    SILK_STROKE_NM,
    SWITCH_PACKAGES,
    TESTPOINT_PACKAGES,
    Footprint,
    UnsupportedPackage,
    battery_holder,
    cathode_mark,
    chip_passive,
    connector,
    dual_row_header,
    for_passive,
    lqfp,
    pin1_mark,
    probe_point,
    silk_segments,
    soic,
    sot23,
    sot223,
    switch,
)
from silkscreen.units import to_mm

#: KiCad's default netclass copper-to-copper clearance, in millimetres.
DEFAULT_CLEARANCE_MM = 0.2


def _same_side_pairs(fp: Footprint) -> list[tuple]:
    """Group pads by x position (i.e. "side" of the footprint) and pair up
    each side's pads by adjacency along y, sorted by y descending."""
    by_x: dict[int, list] = {}
    for pad in fp.pads:
        by_x.setdefault(pad.x_nm, []).append(pad)
    pairs = []
    for pads_on_side in by_x.values():
        if len(pads_on_side) < 2:
            continue
        ordered = sorted(pads_on_side, key=lambda p: p.y_nm, reverse=True)
        for a, b in zip(ordered, ordered[1:], strict=False):
            pairs.append((a, b))
    return pairs


def _edge_gap_mm(a, b) -> float:
    """Gap between two pads' facing edges along y, computed from raw
    center/height fields rather than any pre-existing spacing helper."""
    center_gap_nm = abs(a.y_nm - b.y_nm)
    half_extents_nm = a.h_nm / 2 + b.h_nm / 2
    return to_mm(int(center_gap_nm - half_extents_nm))


def test_sot223_adjacent_pad_gap_meets_default_clearance():
    fp = sot223()
    pairs = _same_side_pairs(fp)
    # The three side-by-side pads (1, 2, 3) share an x column and give two
    # adjacent pairs; make sure we actually found them.
    assert len(pairs) == 2
    for a, b in pairs:
        gap_mm = _edge_gap_mm(a, b)
        assert gap_mm >= DEFAULT_CLEARANCE_MM, (
            f"pads {a.number!r}/{b.number!r} only {gap_mm:.3f}mm apart, "
            f"below the {DEFAULT_CLEARANCE_MM}mm default netclass clearance"
        )


def _tab(fp: Footprint):
    """The tab is the one pad on the other side of the body from the leads."""
    return max(fp.pads, key=lambda p: p.x_nm)


def test_sot223_pads_do_not_overlap_tab():
    fp = sot223()
    tab = _tab(fp)
    others = [p for p in fp.pads if p is not tab]
    assert tab.w_nm > max(p.w_nm for p in others)
    for pad in others:
        # Different x columns (side leads vs. tab), so the clearance that
        # matters is simply that their footprints don't overlap in x.
        gap_x_nm = abs(pad.x_nm - tab.x_nm) - (pad.w_nm / 2 + tab.w_nm / 2)
        assert to_mm(int(gap_x_nm)) >= DEFAULT_CLEARANCE_MM


def test_sot223_body_and_courtyard_still_sane():
    fp = sot223()
    assert fp.courtyard_w_nm >= fp.body_w_nm
    assert fp.courtyard_h_nm >= fp.body_h_nm
    # Every pad must fit within the courtyard half-extents.
    for pad in fp.pads:
        assert abs(pad.x_nm) + pad.w_nm / 2 <= fp.courtyard_w_nm
        assert abs(pad.y_nm) + pad.h_nm / 2 <= fp.courtyard_h_nm


# ------------------------------------------------- orientation on the legend


def _cross(o, a, b) -> int:
    """z of (a - o) x (b - o), in the footprint's own Y-down frame."""
    return (a.x_nm - o.x_nm) * (b.y_nm - o.y_nm) - (a.y_nm - o.y_nm) * (b.x_nm - o.x_nm)


@pytest.mark.parametrize(
    "fp",
    [soic(8), soic(14), sot23(), sot223(), lqfp(48), dual_row_header(30)],
    ids=lambda fp: fp.name,
)
def test_pin_1_is_top_left_and_the_count_runs_anticlockwise_on_screen(fp):
    """Chirality, computed from raw pad coordinates, no library involved.

    KiCad's footprint frame is Y-down, so "pin 1 top-left" is ``x < 0,
    y < 0`` and a real dual-row or quad package counts anticlockwise on
    screen: the turn from pin 2 to the last pin, seen from pin 1, has a
    *negative* cross product in that frame. The old generators had it
    positive -- the mirror image, which no rotation of a real part can be
    soldered to: on a SOT-223 it swaps ground and input.
    """
    first = fp.pads[0]
    assert first.number == "1"
    assert first.x_nm < 0 and first.y_nm < 0, (first.x_nm, first.y_nm)
    second = fp.pads[1]
    last = [p for p in fp.pads if p.x_nm > 0][-1]
    assert _cross(first, second, last) < 0, fp.name
    # Within the pin-1 column the numbers increase downwards.
    column = sorted((p for p in fp.pads if p.x_nm == first.x_nm), key=lambda p: p.y_nm)
    assert [p.number for p in column] == [str(i + 1) for i in range(len(column))]
    # And the whole count sweeps one way round the centre: anticlockwise on a
    # Y-down screen is a strictly *decreasing* bearing from pin 1, every side
    # included (a bottom row running the wrong way would pass the checks
    # above). A tab repeats a lead's number instead of continuing the count
    # -- on SOT-223-3_TabPin2 the tab *is* pin 2 -- so the sweep visits each
    # pin once, at its first land. Dropping every pad whose number repeats
    # would drop the real pin 2 along with the tab and leave the sequence
    # reading 1, 3.
    seen: set[str] = set()
    counted, repeats = [], []
    for pad in fp.pads:
        (counted if pad.number not in seen else repeats).append(pad)
        seen.add(pad.number)
    # Whatever the sweep set aside has to be a tab -- a second land across the
    # body from the leads -- and never a lead the pad order happened to put
    # second, which would hide a misnumbered column behind a passing sweep.
    for pad in repeats:
        assert pad.x_nm > 0 > first.x_nm, f"{fp.name}: pad {pad.number} repeats"
    start = math.atan2(first.y_nm, first.x_nm)
    bearings = [
        (math.atan2(p.y_nm, p.x_nm) - start) % math.tau or math.tau for p in counted
    ]
    assert [p.number for p in counted] == [str(i + 1) for i in range(len(counted))]
    assert all(a > b for a, b in zip(bearings, bearings[1:], strict=False)), fp.name


def test_the_sot223_tab_is_pin_2_and_carries_pin_2s_net():
    """Every pad number must exist as a schematic pin, or KiCad's parity check
    reports an orphan pad; the tab is pin 2 electrically (AMS1117: tab = Vout)."""
    fp = sot223(nets={"1": "GND", "2": "VOUT", "3": "VIN"})
    assert {p.number for p in fp.pads} == {"1", "2", "3"}
    tab = _tab(fp)
    assert tab.number == "2" and tab.net == "VOUT"
    assert sum(1 for p in fp.pads if p.number == "2") == 2


def _gap_to_pad_nm(x: int, y: int, pad) -> float:
    dx = max(abs(x - pad.x_nm) - pad.w_nm / 2, 0)
    dy = max(abs(y - pad.y_nm) - pad.h_nm / 2, 0)
    return (dx * dx + dy * dy) ** 0.5


@pytest.mark.parametrize(
    "fp",
    # The tactile switch is here with the ICs and not with the headers: it is
    # a surface-mount part with no square pad to carry the mark, so the dot is
    # the only thing on the printed board that says which pad is 1.
    [soic(8), sot23(), sot223(), lqfp(32), switch("SW_SPST_TL3305A")],
    ids=lambda fp: fp.name,
)
def test_every_smd_multi_pad_part_gets_a_pin_1_dot_clear_of_copper(fp):
    mark = pin1_mark(fp)
    assert mark is not None, fp.name
    x, y, r = mark
    pad1 = fp.pad_by_number("1")
    assert y == pad1.y_nm
    assert abs(x) > abs(pad1.x_nm) + pad1.w_nm // 2, "the dot is not outboard of pad 1"
    ink = r + SILK_STROKE_NM // 2
    for pad in fp.pads:
        assert _gap_to_pad_nm(x, y, pad) - ink >= SILK_PAD_CLEARANCE_NM - 1, (
            f"{fp.name}: pin-1 dot touches pad {pad.number}"
        )


def test_a_chip_passive_and_a_header_get_no_pin_1_dot():
    assert pin1_mark(chip_passive("0603")) is None
    assert pin1_mark(dual_row_header(30)) is None  # its square pad is the mark


def test_a_diode_gets_a_cathode_bar_beside_pad_2_and_a_resistor_does_not():
    diode = for_passive("diode", "LED")
    assert diode.polarised
    bar = cathode_mark(diode)
    assert bar is not None
    x0, y0, x1, y1 = bar
    cathode = diode.pad_by_number("2")
    assert x0 == x1 and x0 > cathode.x_nm + cathode.w_nm // 2
    assert y1 - y0 == cathode.h_nm
    for pad in diode.pads:
        assert _gap_to_pad_nm(x0, (y0 + y1) // 2, pad) - SILK_STROKE_NM // 2 >= (
            SILK_PAD_CLEARANCE_NM - 1
        )
    assert not for_passive("resistor", "1k").polarised
    assert cathode_mark(for_passive("resistor", "1k")) is None


# ---------------------------------------------------------------------------
# Connectors and battery holders
#
# The bug these guard against is not a crash: it is a board that emits, passes
# DRC and passes parity while carrying a "power connector" no plug fits. So the
# checks below are about correspondence with a real part, not self-consistency.
# Everything is computed from raw Pad fields, and the last test compares the
# generated pad centres with the KiCad library footprint each was measured
# from -- the only check that can actually catch an invented dimension.
# ---------------------------------------------------------------------------

#: package -> the KiCad library footprint it was measured from, relative to the
#: installed footprint directory. A package with no entry here has no library
#: counterpart and is exempt from the comparison test; there are currently none.
_LIBRARY_COUNTERPART = {
    "Barrel_Jack_5.5x2.1mm":
        "Connector_BarrelJack.pretty/BarrelJack_CUI_PJ-102AH_Horizontal.kicad_mod",
    "TerminalBlock_2P_5.08mm":
        "TerminalBlock_Phoenix.pretty/"
        "TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal.kicad_mod",
    "JST_PH_2P":
        "Connector_JST.pretty/JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal.kicad_mod",
    "JST_PH_3P":
        "Connector_JST.pretty/JST_PH_S3B-PH-K_1x03_P2.00mm_Horizontal.kicad_mod",
    "JST_PH_4P":
        "Connector_JST.pretty/JST_PH_S4B-PH-K_1x04_P2.00mm_Horizontal.kicad_mod",
    "USB_C_Receptacle_Power":
        "Connector_USB.pretty/"
        "USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal.kicad_mod",
    "BatteryHolder_CR2032":
        "Battery.pretty/BatteryHolder_Keystone_3002_1x2032.kicad_mod",
    "BatteryHolder_AAA_1x":
        "Battery.pretty/BatteryHolder_Keystone_2466_1xAAA.kicad_mod",
    "SW_SPST_TL3305A":
        "Button_Switch_SMD.pretty/SW_SPST_TL3305A.kicad_mod",
    "TestPoint_Pad_1.5x1.5mm":
        "TestPoint.pretty/TestPoint_Pad_1.5x1.5mm.kicad_mod",
}
_LIBRARY_COUNTERPART.update({
    f"PinHeader_1x{n:02d}_P2.54mm":
        f"Connector_PinHeader_2.54mm.pretty/PinHeader_1x{n:02d}_P2.54mm_Vertical.kicad_mod"
    for n in (2, 3, 4, 5, 6, 8, 10)
})

#: Where KiCad installs its footprint library, per platform. Same
#: found-never-assumed convention as ``test_models3d.py``.
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


#: package table -> the entry point that draws from it. One mapping rather
#: than a chain of ``if package in ...``, so a table added without an entry
#: point (or the reverse) is a KeyError here and not a silent exemption from
#: every test below.
_TABLES = (
    (CONNECTOR_PACKAGES, connector),
    (BATTERY_PACKAGES, battery_holder),
    (SWITCH_PACKAGES, switch),
    (TESTPOINT_PACKAGES, probe_point),
)


def _build(package: str) -> Footprint:
    """The one place a test turns a package name into a footprint, so a name
    that is advertised in only one of the tables fails loudly."""
    for table, build in _TABLES:
        if package in table:
            return build(package)
    raise AssertionError(f"{package} is in no package table")


_ALL_PACKAGES = [name for table, _ in _TABLES for name in sorted(table)]


def test_every_advertised_package_builds_and_is_named_after_itself():
    """``Footprint.name`` *is* the package name: ``enclosure.rules
    .connector_class`` matches on it by substring, so a footprint named
    anything else gets no opening cut in the case."""
    # Every pair of tables is a separate namespace; one name in two of them
    # would make ``_build`` answer whichever table it reached first.
    assert len(_ALL_PACKAGES) == len(set(_ALL_PACKAGES))
    for package in _ALL_PACKAGES:
        fp = _build(package)
        assert fp.name == package
        assert fp.description


@pytest.mark.parametrize("package", _ALL_PACKAGES)
def test_pad_numbers_match_the_advertised_pin_count(package):
    """Counted as *distinct pad numbers*, not pads: a CR2032 holder has three
    pads and two pins, because both spring clips are the one + terminal --
    the same reason the SOT-223 tab shares pin 2's number. A pad number with
    no schematic pin behind it is the orphan KiCad's parity check reports."""
    expected = {k: v for table, _ in _TABLES for k, v in table.items()}[package]
    fp = _build(package)
    assert len({pad.number for pad in fp.pads}) == expected


def test_nets_reach_the_pads_they_name():
    fp = connector("JST_PH_3P", {"1": "VCC", "3": "GND"})
    assert fp.pad_by_number("1").net == "VCC"
    assert fp.pad_by_number("2").net == ""
    assert fp.pad_by_number("3").net == "GND"
    # Both CR2032 clips are pin 1, so both must carry pin 1's net; a net that
    # reached only the first clip would leave half the holder unconnected.
    holder = battery_holder("BatteryHolder_CR2032", {"1": "VBAT", "2": "GND"})
    assert [p.net for p in holder.pads if p.number == "1"] == ["VBAT", "VBAT"]


def _rect_gap_mm(a, b) -> float:
    """Gap between two pads' nearest edges, from raw centre/size fields only.

    Both axes at once, unlike ``_edge_gap_mm`` above: these packages put pads
    in a row along x as often as along y, and a check that only looked at y
    would call a JST's 0.8 mm side-by-side gap infinite.
    """
    dx = abs(a.x_nm - b.x_nm) - (a.w_nm + b.w_nm) / 2
    dy = abs(a.y_nm - b.y_nm) - (a.h_nm + b.h_nm) / 2
    if dx >= 0 and dy >= 0:
        return to_mm(int(math.hypot(dx, dy)))
    return to_mm(int(max(dx, dy)))


@pytest.mark.parametrize("package", _ALL_PACKAGES)
def test_pads_of_different_nets_clear_the_default_netclass(package):
    fp = _build(package)
    for i, a in enumerate(fp.pads):
        for b in fp.pads[i + 1:]:
            if a.number == b.number:
                continue  # one net, one terminal: they may be as close as they like
            gap = _rect_gap_mm(a, b)
            assert gap >= DEFAULT_CLEARANCE_MM, (
                f"{package}: pads {a.number} and {b.number} are {gap}mm apart"
            )


#: The packages whose pads carry ordinary 1..n numbering. USB-C is the
#: exception on purpose: a real 6-way power-only receptacle's pads are called
#: A5/B5/A9/B9/A12/B12, and renumbering them 1..6 would invent names the part
#: does not have.
_NUMBERED_PACKAGES = [p for p in _ALL_PACKAGES if p != "USB_C_Receptacle_Power"]


@pytest.mark.parametrize("package", _NUMBERED_PACKAGES)
def test_pin_1_is_the_top_left_pad(package):
    """The frame is Y-down (see footprints.py's "Frame" note), so the pad
    nearest the top of the screen, ties broken leftwards, is pin 1. Every
    multi-pin generator in this module was once mirrored because it had been
    written Y-up; this is that check applied to the new packages."""
    fp = _build(package)
    first = min(fp.pads, key=lambda p: (p.y_nm, p.x_nm))
    assert first.number == "1", (
        f"{package}: the top-left pad is {first.number}, not 1 -- mirrored?"
    )


@pytest.mark.parametrize("package", _ALL_PACKAGES)
def test_the_courtyard_encloses_every_pad(package):
    """The placer only ever sees the courtyard, so a pad outside it is a part
    the solver is free to overlap with its neighbour."""
    fp = _build(package)
    assert fp.courtyard_w_nm > 0 and fp.courtyard_h_nm > 0
    for pad in fp.pads:
        assert abs(pad.x_nm) + pad.w_nm // 2 <= fp.courtyard_w_nm, pad.number
        assert abs(pad.y_nm) + pad.h_nm // 2 <= fp.courtyard_h_nm, pad.number


def _segment_gap_to_pad_nm(seg, pad) -> float:
    """Distance from an axis-aligned silk segment to a pad rectangle."""
    x0, y0, x1, y1 = seg
    lo_x, hi_x = min(x0, x1), max(x0, x1)
    lo_y, hi_y = min(y0, y1), max(y0, y1)
    dx = max(lo_x - (pad.x_nm + pad.w_nm / 2), (pad.x_nm - pad.w_nm / 2) - hi_x, 0)
    dy = max(lo_y - (pad.y_nm + pad.h_nm / 2), (pad.y_nm - pad.h_nm / 2) - hi_y, 0)
    return math.hypot(dx, dy)


@pytest.mark.parametrize("package", _ALL_PACKAGES)
def test_silkscreen_never_lands_on_copper(package):
    """Ink on a pad resists solder and most fabs clip it silently, so the
    shipped board stops matching the approved artwork."""
    fp = _build(package)
    for seg in silk_segments(fp):
        for pad in fp.pads:
            gap = _segment_gap_to_pad_nm(seg, pad) - SILK_STROKE_NM / 2
            assert gap >= SILK_PAD_CLEARANCE_NM - 1, (
                f"{package}: silk {seg} touches pad {pad.number}"
            )


def test_every_through_hole_pad_has_an_annulus():
    """A pad no wider than its own drill is an annular ring of zero: the hole
    removes the whole land and the joint has nothing to wet."""
    for package in _ALL_PACKAGES:
        for pad in _build(package).pads:
            if pad.is_tht:
                assert pad.drill_nm < min(pad.w_nm, pad.h_nm), (package, pad.number)


def test_the_usb_c_pads_are_the_real_parts_names():
    """A 6-way power-only USB-C receptacle breaks out CC1/CC2, VBUS and GND
    and nothing else -- there is no A1, no A4 and no D+/D- on the part, so
    there is none here either."""
    fp = connector("USB_C_Receptacle_Power")
    assert [p.number for p in fp.pads] == ["B12", "B9", "A5", "B5", "A9", "A12"]
    assert all(not p.is_tht for p in fp.pads), "the 6 signal pads are SMD"


def test_the_tactile_switch_has_two_pads_per_pole_and_wires_both():
    """The TL3305's four pads are two poles: the pair on each row is one
    terminal inside the part. Numbering the second pair 3/4 would give the
    board two pads no symbol pin exists for -- the orphan KiCad's parity check
    reports -- and netting only the first pad of each pole would leave half
    the switch unconnected, the CR2032-clip bug in a second part."""
    fp = switch("SW_SPST_TL3305A", {"1": "BTN", "2": "GND"})
    assert len(fp.pads) == 4
    assert [p.net for p in fp.pads if p.number == "1"] == ["BTN", "BTN"]
    assert [p.net for p in fp.pads if p.number == "2"] == ["GND", "GND"]
    # Each pole's two pads face each other across the body, so the part works
    # in either rotation -- computed from the raw fields, not asserted from
    # the table the generator was written against.
    for number in ("1", "2"):
        xs = sorted(p.x_nm for p in fp.pads if p.number == number)
        assert xs[0] == -xs[1], f"pole {number} is not symmetric about x=0"


def test_the_courtyards_match_the_kicad_footprints_they_came_from():
    """A courtyard *tighter* than the library's is the direction this file
    must not err in: the placer sees nothing else, so a tight courtyard lets
    two parts sit closer than the real bodies allow. Checked against the
    F.CrtYd half-extents read out of the library file by hand and recorded
    here, rather than against ``fit_courtyard``'s own arithmetic."""
    library_half_extents_mm = {
        # Button_Switch_SMD.pretty/SW_SPST_TL3305A: F.CrtYd -4.65..4.65 x,
        # -2.5..2.5 y.
        "SW_SPST_TL3305A": (4.65, 2.5),
        # TestPoint.pretty/TestPoint_Pad_1.5x1.5mm: F.CrtYd -1.25..1.25 both.
        "TestPoint_Pad_1.5x1.5mm": (1.25, 1.25),
    }
    for package, (want_w, want_h) in library_half_extents_mm.items():
        fp = _build(package)
        assert to_mm(fp.courtyard_w_nm) == pytest.approx(want_w, abs=0.001), package
        assert to_mm(fp.courtyard_h_nm) == pytest.approx(want_h, abs=0.001), package


def test_the_test_point_is_one_pad_with_no_orientation():
    """The reason a test point is cheap to add honestly: there is no winding
    to mirror and no pin 1 to point at, so the whole bug class the rest of
    this file guards cannot arise. It is also not a Keystone post -- those
    have a hole and a body, and this is the flat pad a probe touches."""
    fp = probe_point("TestPoint_Pad_1.5x1.5mm")
    assert [p.number for p in fp.pads] == ["1"]
    assert not fp.pads[0].is_tht
    assert (fp.pads[0].x_nm, fp.pads[0].y_nm) == (0, 0)
    assert pin1_mark(fp) is None


def test_an_unknown_package_refuses_rather_than_guessing():
    with pytest.raises(UnsupportedPackage):
        connector("Barrel_Jack_2.5mm")
    with pytest.raises(UnsupportedPackage):
        battery_holder("BatteryHolder_AA_1x")
    # The two tables are separate namespaces, and neither entry point falls
    # through to the other: a battery asked for as a connector is a caller bug.
    with pytest.raises(UnsupportedPackage):
        connector("BatteryHolder_CR2032")
    with pytest.raises(UnsupportedPackage):
        battery_holder("JST_PH_2P")
    # The switch and test-point tables are two more separate namespaces, and
    # neither falls through to the connectors: the four entry points exist so
    # a caller that asks for the wrong kind of part is told, not served.
    with pytest.raises(UnsupportedPackage):
        switch("SW_SPST_TL3305B")   # the tall variant; only the A is drawn
    with pytest.raises(UnsupportedPackage):
        switch("PinHeader_1x02_P2.54mm")
    with pytest.raises(UnsupportedPackage):
        probe_point("SW_SPST_TL3305A")
    with pytest.raises(UnsupportedPackage):
        connector("SW_SPST_TL3305A")
    assert issubclass(UnsupportedPackage, ValueError)


def test_every_package_names_the_library_footprint_it_came_from():
    """A package with no counterpart would be exempt from the comparison test
    below, which is exactly how an invented land pattern would slip through.
    If one is ever added deliberately, delete it from here *and* say so in the
    generator's docstring."""
    assert set(_LIBRARY_COUNTERPART) == set(_ALL_PACKAGES)


def _library_pads(path: Path) -> list[tuple[str, float, float, float, float]]:
    from kiutils.footprint import Footprint as LibFootprint

    lib = LibFootprint.from_file(str(path))
    return [
        (pad.number, pad.position.X, pad.position.Y, pad.size.X, pad.size.Y)
        for pad in lib.pads
    ]


@pytest.mark.skipif(
    _installed_footprints() is None,
    reason="KiCad's footprint library is not installed (KICAD_FOOTPRINT_DIR)",
)
@pytest.mark.parametrize("package", _ALL_PACKAGES)
def test_the_generated_pattern_matches_the_kicad_footprint_it_was_measured_from(
    package,
):
    """The check the rest of this file cannot make.

    Every other test here is self-consistent -- it would pass just as happily
    on a land pattern invented out of nothing, which is precisely the board
    that emits cleanly and cannot be plugged into. This one reads the library
    ``.kicad_mod`` each generator's docstring names and asserts the pads are
    the same pads.

    Positions are compared with each set's own centroid removed, because
    KiCad anchors most through-hole connectors on pad 1 while this module
    anchors on the package centre. That difference is a translation and
    nothing else: shape, spacing and numbering all survive it, and any real
    disagreement (a wrong pitch, a mirrored row, a pad in the wrong place)
    does not.

    Library pads this module does not draw are excluded by number, currently
    only the USB-C shield tabs -- see ``_usb_c_power``'s docstring for why
    they are absent and what that costs.
    """
    library = _installed_footprints()
    ours = [
        (p.number, to_mm(p.x_nm), to_mm(p.y_nm), to_mm(p.w_nm), to_mm(p.h_nm))
        for p in _build(package).pads
    ]
    drawn = {n for n, *_ in ours}
    theirs = [row for row in _library_pads(library / _LIBRARY_COUNTERPART[package])
              if row[0] in drawn]
    assert len(ours) == len(theirs), f"{package}: pad count differs from the library"

    def centred(rows):
        cx = sum(r[1] for r in rows) / len(rows)
        cy = sum(r[2] for r in rows) / len(rows)
        return sorted((r[0], round(r[1] - cx, 4), round(r[2] - cy, 4), r[3], r[4])
                      for r in rows)

    for mine, real in zip(centred(ours), centred(theirs), strict=True):
        assert mine[0] == real[0], f"{package}: pad numbering differs"
        for got, want, what in zip(mine[1:], real[1:], "xywh", strict=True):
            assert abs(got - want) <= 0.001, (
                f"{package} pad {mine[0]}: {what} is {got}mm, the library says {want}mm"
            )


@pytest.mark.skipif(
    _installed_footprints() is None,
    reason="KiCad's footprint library is not installed (KICAD_FOOTPRINT_DIR)",
)
@pytest.mark.parametrize("package", _ALL_PACKAGES)
def test_the_generated_drills_match_the_kicad_footprint(package):
    """Kept apart from the position check because a drill has no frame to be
    compared in: it either is the library's hole or it is a hole the part's
    pin does not fit. Oval drills are skipped -- ``Pad`` carries one diameter,
    and the only oval holes in these library parts are the USB-C shield tabs
    this module deliberately does not draw."""
    from kiutils.footprint import Footprint as LibFootprint

    library = _installed_footprints()
    lib = LibFootprint.from_file(str(library / _LIBRARY_COUNTERPART[package]))
    drawn = {p.number for p in _build(package).pads}
    expected = {
        pad.number: pad.drill.diameter
        for pad in lib.pads
        if pad.number in drawn and pad.drill and not pad.drill.oval
    }
    for pad in _build(package).pads:
        want = expected.get(pad.number)
        if want is None:
            assert not pad.is_tht, f"{package} pad {pad.number}: SMD in the library"
            continue
        assert abs(to_mm(pad.drill_nm) - want) <= 0.001, (
            f"{package} pad {pad.number}: drill {to_mm(pad.drill_nm)}mm, "
            f"the library says {want}mm"
        )

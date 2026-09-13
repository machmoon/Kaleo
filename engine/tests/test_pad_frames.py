"""The pad offsets CP-SAT optimises must describe the board that gets written.

This is the silent-geometry bug class from the other side. `test_kicad.py`
checks that nothing *overlaps*; nothing checked that the netlist the placer
minimises is the netlist the emitter draws. It was not: the Y flip in
`build_board`'s terminal offsets had the wrong sign, so every package was
mirrored vertically before the solver saw it. The geometry stayed consistent
-- so no test failed -- while the placement was optimised against a mirror
image of the circuit.

As in `test_kicad.py`, the expected values are computed with independent
arithmetic that never calls the code under test.
"""

from __future__ import annotations

from silkscreen.board import _Reserve as Reserve  # the solver's per-side reservation
from silkscreen.board import board_pads, build_board, part_anchor, solver_pad_offset
from silkscreen.footprints import lqfp, soic, sot223

SPEC_PARTS = ("soic", "sot223", "lqfp")


def _fp(name):
    return {"soic": soic(8), "sot223": sot223(), "lqfp": lqfp(32)}[name]


def test_a_pad_above_the_package_centre_is_high_in_the_solver_box():
    """The frames disagree about Y, so the conversion must subtract.

    `pad.y_nm` is Y-**down** from the package centre; the solver measures Y
    **up** from the reserved box's bottom-left. A pad drawn above the centre
    (`y_nm < 0`) is therefore near the *top* of the box: its offset must
    exceed the half-height. Under the old `+` it was below it.
    """
    reserve = Reserve(side=0, bottom=0, top=0)
    for name in SPEC_PARTS:
        fp = _fp(name)
        half_h = fp.courtyard_h_nm
        for pad in fp.pads:
            _ox, oy = solver_pad_offset(fp, pad, reserve)
            # Independent arithmetic, stated in the frame's own terms.
            assert oy == half_h - pad.y_nm, f"{name} pad {pad.number}"
            if pad.y_nm < 0:
                assert oy > half_h, f"{name} pad {pad.number} should sit high"
            elif pad.y_nm > 0:
                assert oy < half_h, f"{name} pad {pad.number} should sit low"


def test_the_offset_preserves_the_packages_own_pad_order():
    """Pin 1 of a SOIC is above pin 5; the solver must be told so.

    This is the assertion that actually fails on a mirror, stated without
    reference to any half-extent: whichever pad is higher on the package must
    be the pad with the greater Y-up offset.
    """
    reserve = Reserve(side=0, bottom=0, top=0)
    fp = soic(8)
    top = fp.pad_by_number("1")
    bottom = fp.pad_by_number("5")
    assert top.y_nm < bottom.y_nm  # Y-down: pin 1 is drawn above pin 5
    _, oy_top = solver_pad_offset(fp, top, reserve)
    _, oy_bottom = solver_pad_offset(fp, bottom, reserve)
    assert oy_top > oy_bottom


def test_the_reservation_shifts_but_never_flips():
    """Clearance moves the box; it must not change which way Y runs."""
    fp = soic(8)
    pad = fp.pad_by_number("1")
    _, plain = solver_pad_offset(fp, pad, Reserve(side=0, bottom=0, top=0))
    _, padded = solver_pad_offset(fp, pad, Reserve(side=0, bottom=250_000, top=0))
    assert padded - plain == 250_000


def test_solver_offsets_agree_with_the_geometry_that_gets_emitted():
    """The offset and the written pad must describe the same point.

    `board_pads` is the geometry the router and the emitter use. Converting a
    solver offset back through the part's anchor must land on it, for every
    pad of every placed part, to within grid quantisation.
    """
    from silkscreen.netlist import parse_circuit_spec

    spec = parse_circuit_spec(
        """{
        "devices": {"U1": {"pins": {"A": "1", "B": "2", "C": "3", "D": "4",
                                     "E": "5", "F": "6", "G": "7", "H": "8"}}},
        "passives": {"C1": {"type": "capacitor", "value": "10uF"}},
        "nets": {"N1": ["U1.A", "C1.1"], "N2": ["U1.E", "C1.2"]}
        }"""
    )
    result = build_board(spec, time_limit_s=10.0)
    emitted = {}
    for pad in board_pads(result):
        emitted.setdefault(pad.net, []).append((pad.x_nm, pad.y_nm))

    for part in result.parts:
        if part.rotated:
            continue  # rotation has its own test in test_rotated_anchor.py
        anchor_x, anchor_y = part_anchor(part)
        fp = part.footprint
        for pad in fp.pads:
            # Where the emitter puts it, computed here from the anchor.
            want = (anchor_x + pad.x_nm, anchor_y - pad.y_nm)
            # Where the solver was told it is, relative to the same anchor.
            _ox, oy = solver_pad_offset(fp, pad, Reserve(side=0, bottom=0, top=0))
            got_y = anchor_y - fp.courtyard_h_nm + oy
            assert got_y == want[1], f"{part.ref} pad {pad.number}"


def test_the_advertised_packages_are_exactly_the_ones_that_build():
    """The prompt must not offer the model a package the builder refuses.

    `supported_packages_text` exists precisely so the propose prompt and the
    refusal message cannot drift; the string said "4-28 pins even (SOIC)",
    which advertises 12, 18, 22 and 26, and `_footprint_for_device` accepts
    none of them. Every one of those costs a repair round or a failed run.
    """
    import re

    from silkscreen.board import (
        UnsupportedPackage,
        _footprint_for_device,
        supported_packages_text,
    )

    text = supported_packages_text()
    # Whole numbers only: a substring check reads the "2" inside "32/".
    advertised = {int(n) for n in re.findall(r"\b\d+\b", text)}
    for pin_count in range(2, 40):
        try:
            _footprint_for_device("U_X", pin_count, {})
        except UnsupportedPackage:
            builds = False
        else:
            builds = True
        if builds:
            assert pin_count in advertised, (
                f"{pin_count} pins builds but is not advertised"
            )
        else:
            assert pin_count not in advertised, (
                f"{pin_count} pins is advertised but the builder refuses it"
            )

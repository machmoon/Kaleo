"""Footprint generation and board emission.

The end-to-end test here is the one that matters: a circuit spec becomes a
``.kicad_pcb`` that KiCad's own parser reads back, with pads on the right nets
and no overlapping courtyards.
"""

from __future__ import annotations

import itertools

import pytest
from silkscreen.board import build_board, emit_kicad_pcb, write_board
from silkscreen.footprints import (
    CHIP_SIZES,
    Footprint,
    UnsupportedPackage,
    chip_passive,
    for_passive,
    lqfp,
    silk_segments,
    soic,
    sot23,
    sot223,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.units import mm, to_mm


def _spec():
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


# ---------------------------------------------------------------- footprints


def test_chip_passive_sizes_are_physically_right():
    fp = chip_passive("0603")
    assert len(fp.pads) == 2
    # An 0603 land pattern is about 3mm across including both pads.
    assert 2.5 <= to_mm(fp.courtyard_w_nm * 2) <= 3.5
    assert 1.0 <= to_mm(fp.courtyard_h_nm * 2) <= 2.0


def test_every_chip_size_generates():
    for size in CHIP_SIZES:
        fp = chip_passive(size)
        assert len(fp.pads) == 2
        assert fp.courtyard_w_nm > 0 and fp.courtyard_h_nm > 0


def test_pad_count_matches_package():
    assert len(sot223().pads) == 4          # 3 pins + tab
    assert len(soic(8).pads) == 8
    assert len(lqfp(48).pads) == 48
    assert len(lqfp(100).pads) == 100


def test_lqfp_pins_are_unique_and_contiguous():
    fp = lqfp(48)
    numbers = sorted(int(p.number) for p in fp.pads)
    assert numbers == list(range(1, 49))


def test_lqfp_courtyard_matches_a_real_footprint():
    """A real LQFP-48 7x7mm courtyard is ~10.3mm. Ours should be close."""
    fp = lqfp(48, body_mm=7.0)
    assert 9.5 <= to_mm(fp.courtyard_w_nm * 2) <= 11.5


def test_no_two_pads_overlap_in_a_generated_footprint():
    for fp in (sot223(), soic(8), soic(16), lqfp(32), lqfp(48), chip_passive("0805")):
        boxes = [
            (p.x_nm - p.w_nm // 2, p.y_nm - p.h_nm // 2,
             p.x_nm + p.w_nm // 2, p.y_nm + p.h_nm // 2)
            for p in fp.pads
        ]
        for a, b in itertools.combinations(boxes, 2):
            dx = min(a[2], b[2]) - max(a[0], b[0])
            dy = min(a[3], b[3]) - max(a[1], b[1])
            assert not (dx > 0 and dy > 0), f"{fp.name} has overlapping pads"


def test_pads_are_inside_their_courtyard():
    for fp in (sot223(), soic(14), lqfp(44), chip_passive("1206")):
        for p in fp.pads:
            assert abs(p.x_nm) + p.w_nm // 2 <= fp.courtyard_w_nm + 1, fp.name
            assert abs(p.y_nm) + p.h_nm // 2 <= fp.courtyard_h_nm + 1, fp.name


def test_silk_segments_stay_clear_of_every_pad():
    """Regression: the emitters used to stroke the raw body rectangle, which
    put 0.06 mm of ink (half the 0.12 mm pen) on every pad the body edge
    touches. Independent math: widen each segment by the pen half-width and
    measure axis-aligned separation from each pad's own rectangle.
    """
    pen_half = mm(0.12) // 2
    clearance = mm(0.2)
    fps = [chip_passive(size) for size in CHIP_SIZES]
    fps += [sot23(), sot223(), soic(8), soic(16), lqfp(32), lqfp(48)]
    for fp in fps:
        for x0, y0, x1, y1 in silk_segments(fp):
            sx0, sx1 = min(x0, x1) - pen_half, max(x0, x1) + pen_half
            sy0, sy1 = min(y0, y1) - pen_half, max(y0, y1) + pen_half
            for p in fp.pads:
                gap_x = max(p.x_nm - p.w_nm // 2 - sx1, sx0 - p.x_nm - p.w_nm // 2)
                gap_y = max(p.y_nm - p.h_nm // 2 - sy1, sy0 - p.y_nm - p.h_nm // 2)
                assert max(gap_x, gap_y) >= clearance, (
                    f"{fp.name}: silk ({x0}, {y0})..({x1}, {y1}) is within "
                    f"{max(gap_x, gap_y)} nm of pad {p.number}"
                )


def test_silk_segments_lie_on_the_body_outline():
    """Clipping cuts spans out of the four body edges; it never invents ink
    somewhere else, and a body the pads swallow entirely (an 0603 is barely
    wider than its own land pattern) legitimately gets no outline at all.
    """
    for fp in (sot23(), soic(8), lqfp(32), chip_passive("0805")):
        segments = silk_segments(fp)
        assert segments, f"{fp.name} has room for an outline"
        bw, bh = fp.body_w_nm, fp.body_h_nm
        for x0, y0, x1, y1 in segments:
            lo_x, hi_x = min(x0, x1), max(x0, x1)
            lo_y, hi_y = min(y0, y1), max(y0, y1)
            on_h = y0 == y1 and abs(y0) == bh and -bw <= lo_x <= hi_x <= bw
            on_v = x0 == x1 and abs(x0) == bw and -bh <= lo_y <= hi_y <= bh
            assert on_h or on_v, f"{fp.name}: ({x0}, {y0})..({x1}, {y1})"
    assert silk_segments(chip_passive("0603")) == []
    assert silk_segments(Footprint(name="bodyless")) == []


def test_emitted_board_reviews_clean_of_silk_over_pad(tmp_path):
    """The finding that motivated the fix, checked by the checker that found
    it: a freshly emitted board must produce zero silkscreen-over-pad findings
    from the audit rule, which measures the file independently of the emitter.
    """
    from silkscreen.audit.effort import profile_for
    from silkscreen.audit.geometry import load_audit_board
    from silkscreen.audit.rules import run_rules

    path = tmp_path / "silk.kicad_pcb"
    write_board(build_board(_spec(), time_limit_s=10.0), path)
    findings, ran = run_rules(load_audit_board(path), profile_for("standard"))
    assert "silkscreen-over-pad" in ran, "the guarding rule must have run"
    hits = [f for f in findings if f.rule == "silkscreen-over-pad"]
    assert hits == [], [f.title for f in hits]


def test_large_capacitor_gets_a_larger_package():
    """A 22uF part does not fit an 0603, and pretending it does is a dead board."""
    small = for_passive("capacitor", "100nF")
    large = for_passive("capacitor", "22uF")
    assert large.courtyard_w_nm > small.courtyard_w_nm


def test_unsupported_package_raises_rather_than_guessing():
    with pytest.raises(UnsupportedPackage):
        lqfp(47)
    with pytest.raises(UnsupportedPackage):
        soic(7)
    with pytest.raises(UnsupportedPackage):
        chip_passive("0201")


# ---------------------------------------------------------------- board


def test_build_board_places_every_part():
    board = build_board(_spec(), time_limit_s=15.0)
    assert len(board.parts) == 6
    refs = {p.ref for p in board.parts}
    assert refs == {"U1", "U2", "C1", "C2", "C3", "R1"}


def test_reference_designators_follow_kicad_convention():
    board = build_board(_spec(), time_limit_s=15.0)
    for part in board.parts:
        assert part.ref[0] in "URCLDY"


def test_emitted_board_reparses_and_is_geometrically_valid(tmp_path):
    """The one test that proves 'generate a PCB' actually happened."""
    from kiutils.board import Board
    from silkscreen.kicad import extract_parts, footprint_ref

    board = build_board(_spec(), time_limit_s=15.0)
    path = write_board(board, tmp_path / "out.kicad_pcb")
    assert path.stat().st_size > 2000

    reloaded = Board.from_file(str(path))
    assert len(reloaded.footprints) == len(board.parts)

    # Every reference survived the round trip.
    assert {footprint_ref(f) for f in reloaded.footprints} == {
        p.ref for p in board.parts
    }

    # Pads carry their nets.
    pad_nets = {
        pad.net.name
        for fp in reloaded.footprints
        for pad in fp.pads
        if pad.net and pad.net.name
    }
    assert {"GND", "+3V3", "VIN"} <= pad_nets

    # A board outline exists.
    edges = [
        g for g in reloaded.graphicItems if getattr(g, "layer", None) == "Edge.Cuts"
    ]
    assert len(edges) == 4

    # No two courtyards overlap, measured on the written geometry.
    infos = extract_parts(reloaded)
    boxes = [
        (i.ref,
         fp.position.X + i.min_x_nm / 1e6, fp.position.Y + i.min_y_nm / 1e6,
         fp.position.X + i.max_x_nm / 1e6, fp.position.Y + i.max_y_nm / 1e6)
        for i, fp in zip(infos, reloaded.footprints, strict=True)
    ]
    for a, b in itertools.combinations(boxes, 2):
        dx = min(a[3], b[3]) - max(a[1], b[1])
        dy = min(a[4], b[4]) - max(a[2], b[2])
        assert not (dx > 0.01 and dy > 0.01), f"courtyard overlap {a[0]}/{b[0]}"


def test_net_zero_exists_for_kicad():
    """KiCad requires net 0 (the unconnected net) to be declared."""
    board = build_board(_spec(), time_limit_s=10.0)
    text = emit_kicad_pcb(board)
    assert '(net 0 "")' in text


def test_output_is_byte_identical_across_runs():
    """UUIDs are seeded, so a regenerated board diffs cleanly in git."""
    a = emit_kicad_pcb(build_board(_spec(), time_limit_s=10.0))
    b = emit_kicad_pcb(build_board(_spec(), time_limit_s=10.0))
    assert a == b


def test_part_name_containing_a_dot_is_handled():
    """Regression: 'AMS1117-3.3' split on the first dot, losing the part."""
    spec = parse_circuit_spec({
        "devices": {"LM317-2.5": {"pins": {"ADJ": "1", "OUT": "2", "IN": "3"}}},
        "passives": {
            "c1": {"type": "capacitor", "value": "10uF"},
            "c2": {"type": "capacitor", "value": "22uF"},
        },
        # ADJ was on GND and VOUT both, which validation now rejects: a pin
        # joins exactly one net. Nothing about the dot-in-a-name regression
        # needed it, so it goes on GND alone and c2 gives VOUT its second
        # endpoint.
        "nets": {
            "VIN": ["LM317-2.5.IN", "c1.1"],
            "GND": ["LM317-2.5.ADJ", "c1.2", "c2.2"],
            "VOUT": ["LM317-2.5.OUT", "c2.1"],
        },
    })
    board = build_board(spec, time_limit_s=10.0)
    # By ref, not by count: the claim is that the dotted part survived at all.
    assert "U1" in {p.ref for p in board.parts}
    assert [p.value for p in board.parts if p.ref == "U1"] == ["LM317-2.5"]


def test_unknown_pin_count_refuses_rather_than_guessing():
    spec = parse_circuit_spec({
        "devices": {"WEIRD": {"pins": {f"P{i}": str(i) for i in range(1, 38)}}},
        "passives": {"c1": {"type": "capacitor", "value": "1uF"}},
        "nets": {"A": ["WEIRD.P1", "c1.1"], "B": ["WEIRD.P2", "c1.2"]},
    })
    with pytest.raises(UnsupportedPackage, match="No package rule"):
        build_board(spec, time_limit_s=5.0)


def test_two_sided_board_emits_footprints_on_both_copper_layers(tmp_path):
    from kiutils.board import Board

    board = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    path = write_board(board, tmp_path / "two.kicad_pcb")
    reloaded = Board.from_file(str(path))
    layers = {fp.layer for fp in reloaded.footprints}
    assert layers == {"F.Cu", "B.Cu"}


def test_bottom_side_pads_are_on_bottom_layers(tmp_path):
    from kiutils.board import Board

    board = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    path = write_board(board, tmp_path / "two.kicad_pcb")
    reloaded = Board.from_file(str(path))
    for fp in reloaded.footprints:
        if fp.layer != "B.Cu":
            continue
        for pad in fp.pads:
            assert "B.Cu" in pad.layers, "a bottom footprint's pads must be on B.Cu"
            assert "F.Cu" not in pad.layers


def test_two_sided_is_smaller_than_single_sided():
    single = build_board(_spec(), two_sided=False, time_limit_s=15.0)
    both = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    assert both.width_nm * both.height_nm < single.width_nm * single.height_nm


def test_ics_stay_on_top_even_when_two_sided():
    """An IC underneath complicates assembly and rework for little area saved."""
    board = build_board(_spec(), two_sided=True, time_limit_s=15.0)
    from silkscreen.packing import Layer

    for part in board.parts:
        if part.ref.startswith("U"):
            assert part.layer is Layer.TOP


# --- Dispatch on kind --------------------------------------------------------
#
# `_footprint_for_device` used to look only at the pin count, so a two-pin
# power connector was padded to four pins and drawn as a SOIC-4 -- a
# surface-mount chip where the board's power input should have been. Nothing
# raised: DRC passed, parity passed, and the case cut no hole for a connector
# it could not see. These pin the ordering that fixes it.


def _connector_spec():
    """An IC and a barrel jack, wired together."""
    from silkscreen.netlist import parse_circuit_spec as _parse

    return _parse(
        {
            "devices": {
                "U1": {"pins": {"IN": "1", "GND": "2", "OUT": "3"}},
                "PWR": {
                    "kind": "connector",
                    "package": "Barrel_Jack_5.5x2.1mm",
                    "pins": {"VIN": "1", "GND": "2"},
                },
            },
            "nets": {
                "VIN": ["U1.IN", "PWR.VIN"],
                "GND": ["U1.GND", "PWR.GND"],
            },
        }
    )


def test_a_connector_reaches_the_connector_generator():
    """Kind first, count second -- the ordering is the whole fix.

    Compared against ``footprints.connector`` directly rather than against a
    hand-written pad list: the assertion is that dispatch *arrives* there, and
    the geometry is Lane A's own test.
    """
    from silkscreen.board import _footprint_for_device
    from silkscreen.footprints import CONNECTOR_PACKAGES, connector

    for package in CONNECTOR_PACKAGES:
        got = _footprint_for_device(
            "PWR", 2, {}, kind="connector", package=package
        )
        want = connector(package)
        assert got.name == want.name == package
        assert [p.number for p in got.pads] == [p.number for p in want.pads]


def test_a_battery_reaches_the_battery_generator():
    from silkscreen.board import _footprint_for_device
    from silkscreen.footprints import BATTERY_PACKAGES, battery_holder

    for package in BATTERY_PACKAGES:
        got = _footprint_for_device(
            "CELL", 2, {}, kind="battery", package=package
        )
        want = battery_holder(package)
        assert got.name == want.name == package
        assert [p.number for p in got.pads] == [p.number for p in want.pads]


def test_a_switch_and_a_test_point_reach_their_own_generators():
    """The dispatch is the point again. A tactile switch has four pads and a
    test point one, and both used to be the pin-count rule's problem: two
    distinct pad numbers would have been padded to a SOIC-4, and one would
    have been refused outright."""
    from silkscreen.board import _footprint_for_device
    from silkscreen.footprints import (
        SWITCH_PACKAGES,
        TESTPOINT_PACKAGES,
        probe_point,
        switch,
    )

    for package in SWITCH_PACKAGES:
        got = _footprint_for_device("BTN", 2, {}, kind="switch", package=package)
        want = switch(package)
        assert got.name == want.name == package
        assert [p.number for p in got.pads] == [p.number for p in want.pads]
        assert "soic" not in got.name.lower()
    for package in TESTPOINT_PACKAGES:
        got = _footprint_for_device("TP", 1, {}, kind="testpoint", package=package)
        want = probe_point(package)
        assert got.name == want.name == package
        assert [p.number for p in got.pads] == [p.number for p in want.pads]


def test_a_two_pin_connector_is_not_drawn_as_a_soic():
    """The exact bug: two pins used to fall through to the SOIC rule."""
    from silkscreen.board import _footprint_for_device

    fp = _footprint_for_device(
        "PWR", 2, {}, kind="connector", package="TerminalBlock_2P_5.08mm"
    )
    assert "soic" not in fp.name.lower()
    assert fp.name == "TerminalBlock_2P_5.08mm"
    assert len(fp.pads) == 2


def test_an_ic_still_dispatches_on_pin_count():
    """The default path is unchanged, which is why old specs keep working."""
    from silkscreen.board import _footprint_for_device

    assert _footprint_for_device("U1", 3, {}).name == sot223().name
    assert _footprint_for_device("U1", 8, {}).name == soic(8).name


def test_an_unknown_connector_package_refuses_and_names_the_alternatives():
    """A wrong footprint is worse than a refusal, and a refusal must teach."""
    from silkscreen.board import _footprint_for_device
    from silkscreen.footprints import CONNECTOR_PACKAGES

    with pytest.raises(UnsupportedPackage) as exc:
        _footprint_for_device("PWR", 2, {}, kind="connector", package="DB9")
    assert "DB9" in str(exc.value)
    assert sorted(CONNECTOR_PACKAGES)[0] in str(exc.value)


def test_a_connector_with_no_package_refuses():
    from silkscreen.board import _footprint_for_device

    with pytest.raises(UnsupportedPackage):
        _footprint_for_device("PWR", 2, {}, kind="connector", package=None)


def test_an_unknown_kind_refuses():
    from silkscreen.board import _footprint_for_device

    with pytest.raises(UnsupportedPackage) as exc:
        _footprint_for_device("X1", 2, {}, kind="transformer")
    assert "transformer" in str(exc.value)


def test_a_connector_declaring_more_pins_than_it_has_refuses():
    """The module rule again: the name says which part this is.

    Fewer pins than the package is legitimate -- a USB-C power receptacle
    wired for VBUS and GND alone leaves the rest unconnected -- but more means
    one of the two is wrong, and building either puts copper where there are
    no pins.
    """
    from silkscreen.board import _footprint_for_device

    with pytest.raises(UnsupportedPackage):
        _footprint_for_device("J1", 9, {}, kind="connector", package="JST_PH_2P")


def test_the_advertised_text_lists_the_connector_and_battery_packages():
    """Rendered from the tables, never hand-written, so it cannot drift."""
    from silkscreen.board import supported_packages_text
    from silkscreen.footprints import (
        BATTERY_PACKAGES,
        CONNECTOR_PACKAGES,
        SWITCH_PACKAGES,
        TESTPOINT_PACKAGES,
    )

    text = supported_packages_text()
    for package in (
        list(CONNECTOR_PACKAGES)
        + list(BATTERY_PACKAGES)
        + list(SWITCH_PACKAGES)
        + list(TESTPOINT_PACKAGES)
    ):
        assert package in text


def test_package_errors_reports_a_bad_connector_at_proposal_time():
    """The footprint rule run early, where the model can still fix it."""
    from silkscreen.board import package_errors
    from silkscreen.netlist import CircuitSpec, Connection, Device

    spec = CircuitSpec(
        devices=[
            Device(
                name="PWR",
                pins={"A": "1", "B": "2"},
                kind="connector",
                package="DB9",
            )
        ],
        connections=[Connection(net="N1", endpoints=("PWR.A", "PWR.B"))],
    )
    assert [e for e in package_errors(spec) if "DB9" in e]


def test_a_board_with_a_connector_places_and_emits():
    """End to end: the connector gets a J ref, its own pads and real copper."""
    board = build_board(_connector_spec(), time_limit_s=15.0)
    refs = {part.ref for part in board.parts}
    assert refs == {"U1", "J1"}
    jack = next(part for part in board.parts if part.ref == "J1")
    assert jack.footprint.name == "Barrel_Jack_5.5x2.1mm"
    text = emit_kicad_pcb(board)
    assert '"J1"' in text

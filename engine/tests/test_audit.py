"""Tests for the optional visual design review.

Board files here are written as literal s-expressions rather than produced by
``silkscreen.board``. The point of a checker is to catch geometry the emitter
got wrong, so a test that builds its input with the emitter and checks it with
the checker shares whatever blind spot the two have in common. Every expected
coordinate below is stated in the file and repeated in the assertion.
"""

from __future__ import annotations

import json
import xml.dom.minidom

import pytest
from silkscreen.agents.model import ModelError, ScriptedModel
from silkscreen.audit import (
    PROFILES,
    Effort,
    Origin,
    Severity,
    load_audit_board,
    review_board,
    write_reports,
)
from silkscreen.audit.cli import main as review_main
from silkscreen.audit.effort import profile_for, slider
from silkscreen.audit.render import render_svg
from silkscreen.audit.report import html_report, json_report, text_report
from silkscreen.audit.rules import RULES, rules_for

_HEADER = """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (47 "F.CrtYd" user "F.Courtyard")
  )
"""


def _pad(number, x, y, w, h, net_index, net_name):
    net = f' (net {net_index} "{net_name}")' if net_index else ""
    return (
        f'    (pad "{number}" smd rect (at {x} {y}) (size {w} {h}) '
        f'(layers "F.Cu" "F.Paste" "F.Mask"){net})'
    )


def _footprint(ref, x, y, *, pads, courtyard=None, silk=()):
    """One footprint. ``courtyard`` is (half_w, half_h) in mm, anchor-centred."""
    lines = [
        f'  (footprint "test:{ref}_FP"',
        '    (layer "F.Cu")',
        f"    (at {x} {y})",
        f'    (property "Reference" "{ref}" (at 0 -1 0) (layer "F.SilkS")'
        f" (effects (font (size 0.8 0.8) (thickness 0.12))))",
        f'    (property "Value" "{ref}v" (at 0 1 0) (layer "F.Fab")'
        f" (effects (font (size 0.8 0.8) (thickness 0.12))))",
    ]
    if courtyard:
        cw, ch = courtyard
        corners = [(-cw, -ch), (cw, -ch), (cw, ch), (-cw, ch)]
        for i in range(4):
            sx, sy = corners[i]
            ex, ey = corners[(i + 1) % 4]
            lines.append(
                f"    (fp_line (start {sx} {sy}) (end {ex} {ey})"
                f' (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
            )
    for sx, sy, ex, ey in silk:
        lines.append(
            f"    (fp_line (start {sx} {sy}) (end {ex} {ey})"
            f' (stroke (width 0.12) (type solid)) (layer "F.SilkS"))'
        )
    lines.extend(pads)
    lines.append("  )")
    return "\n".join(lines)


def _footprint_with_graphic(graphic, *, at="10 10"):
    return "\n".join(
        [
            '  (footprint "test:U1_FP"',
            '    (layer "F.Cu")',
            f"    (at {at})",
            '    (property "Reference" "U1" (at 0 -1 0) (layer "F.SilkS")',
            "      (effects (font (size 0.8 0.8) (thickness 0.12))))",
            f"    {graphic}",
            "  )",
        ]
    )


def _board_text(
    footprints, nets, *, outline=(0, 0, 30, 30), segments=(), vias=()
):
    out = [_HEADER]
    out.append('  (net 0 "")')
    for index, name in enumerate(nets, start=1):
        out.append(f'  (net {index} "{name}")')
    if outline:
        x0, y0, x1, y1 = outline
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        for i in range(4):
            sx, sy = corners[i]
            ex, ey = corners[(i + 1) % 4]
            out.append(
                f"  (gr_line (start {sx} {sy}) (end {ex} {ey})"
                f' (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))'
            )
    out.extend(footprints)
    for sx, sy, ex, ey, width, net_index in segments:
        out.append(
            f"  (segment (start {sx} {sy}) (end {ex} {ey}) (width {width})"
            f' (layer "F.Cu") (net {net_index}))'
        )
    for x, y, size, drill, net_index in vias:
        out.append(
            f"  (via (at {x} {y}) (size {size}) (drill {drill})"
            f' (layers "F.Cu" "B.Cu") (net {net_index}))'
        )
    out.append(")")
    return "\n".join(out) + "\n"


def _write(tmp_path, text, name="board.kicad_pcb"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def clean_board(tmp_path):
    """Two parts, well apart, on a fully routed two-pad net."""
    u1 = _footprint(
        "U1", 8, 8,
        pads=[_pad("1", -1, 0, 1, 1, 1, "SIG"), _pad("2", 1, 0, 1, 1, 2, "GND")],
        courtyard=(2, 2),
    )
    r1 = _footprint(
        "R1", 20, 8,
        pads=[_pad("1", -1, 0, 1, 1, 1, "SIG"), _pad("2", 1, 0, 1, 1, 2, "GND")],
        courtyard=(2, 2),
    )
    text = _board_text(
        [u1, r1],
        ["SIG", "GND"],
        segments=[(7, 8, 19, 8, 0.4, 1), (9, 8, 21, 8, 0.4, 2)],
    )
    return _write(tmp_path, text)


@pytest.fixture
def overlapping_board(tmp_path):
    """U1 at x=8 and C1 at x=10, both with 2 mm half-courtyards: 2 mm overlap."""
    u1 = _footprint("U1", 8, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    c1 = _footprint("C1", 10, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    return _write(tmp_path, _board_text([u1, c1], ["SIG"]))


# --------------------------------------------------------------------------
# independent geometry reader
# --------------------------------------------------------------------------


def test_pad_local_rotation_rotates_shape_without_moving_centre(tmp_path):
    footprint = "\n".join(
        [
            '  (footprint "test:U1_FP"',
            '    (layer "F.Cu")',
            "    (at 10 10 90)",
            '    (property "Reference" "U1" (at 0 -1 0) (layer "F.SilkS")',
            "      (effects (font (size 0.8 0.8) (thickness 0.12))))",
            '    (pad "1" smd rect (at 2 0 90) (size 4 2)',
            '      (layers "F.Cu" "F.Paste" "F.Mask"))',
            "  )",
        ]
    )
    board = load_audit_board(_write(tmp_path, _board_text([footprint], [])))
    pad = board.parts[0].pads[0]
    assert (pad.rect.x0, pad.rect.y0, pad.rect.x1, pad.rect.y1) == (
        8_000_000,
        7_000_000,
        12_000_000,
        9_000_000,
    )
    assert pad.centre == (10_000_000, 8_000_000)


@pytest.mark.parametrize(
    ("graphic", "expected"),
    [
        (
            '(fp_circle (center 0 0) (end 2 0) (stroke (width 0.05) '
            '(type solid)) (fill none) (layer "F.CrtYd"))',
            (8_000_000, 8_000_000, 12_000_000, 12_000_000),
        ),
        (
            '(fp_arc (start -2 0) (mid 0 -2) (end 2 0) '
            '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))',
            (8_000_000, 8_000_000, 12_000_000, 10_000_000),
        ),
        (
            '(fp_poly (pts (xy -2 -1) (xy 2 -1) (xy 1 2)) '
            '(stroke (width 0.05) (type solid)) (fill none) '
            '(layer "F.CrtYd"))',
            (8_000_000, 9_000_000, 12_000_000, 12_000_000),
        ),
        (
            '(fp_curve (pts (xy -2 0) (xy -1 -2) (xy 1 -2) (xy 2 0)) '
            '(stroke (width 0.05) (type solid)) (layer "F.CrtYd"))',
            (8_000_000, 8_500_000, 12_000_000, 10_000_000),
        ),
    ],
)
def test_non_line_footprint_graphics_bound_courtyard(tmp_path, graphic, expected):
    footprint = _footprint_with_graphic(graphic)
    board = load_audit_board(_write(tmp_path, _board_text([footprint], [])))
    courtyard = board.parts[0].courtyard
    assert courtyard is not None
    assert (courtyard.x0, courtyard.y0, courtyard.x1, courtyard.y1) == expected


def test_rotated_rect_courtyard_uses_all_four_corners(tmp_path):
    graphic = (
        '(fp_rect (start -2 -1) (end 2 1) (stroke (width 0.05) '
        '(type solid)) (fill none) (layer "F.CrtYd"))'
    )
    footprint = _footprint_with_graphic(graphic, at="10 10 45")
    board = load_audit_board(_write(tmp_path, _board_text([footprint], [])))
    courtyard = board.parts[0].courtyard
    assert courtyard is not None
    assert (courtyard.x0, courtyard.y0, courtyard.x1, courtyard.y1) == (
        7_878_680,
        7_878_680,
        12_121_320,
        12_121_320,
    )


@pytest.mark.parametrize(
    ("graphic", "expected"),
    [
        (
            '(gr_rect (start 2 3) (end 8 9) (stroke (width 0.1) '
            '(type solid)) (fill none) (layer "Edge.Cuts"))',
            (2_000_000, 3_000_000, 8_000_000, 9_000_000),
        ),
        (
            '(gr_circle (center 5 5) (end 8 5) (stroke (width 0.1) '
            '(type solid)) (fill none) (layer "Edge.Cuts"))',
            (2_000_000, 2_000_000, 8_000_000, 8_000_000),
        ),
        (
            '(gr_arc (start 2 5) (mid 5 2) (end 8 5) '
            '(stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
            (2_000_000, 2_000_000, 8_000_000, 5_000_000),
        ),
        (
            '(gr_poly (pts (xy 2 3) (xy 8 4) (xy 6 9)) '
            '(stroke (width 0.1) (type solid)) (fill none) '
            '(layer "Edge.Cuts"))',
            (2_000_000, 3_000_000, 8_000_000, 9_000_000),
        ),
        (
            '(gr_curve (pts (xy 2 5) (xy 3 2) (xy 7 2) (xy 8 5)) '
            '(stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))',
            (2_000_000, 2_750_000, 8_000_000, 5_000_000),
        ),
    ],
)
def test_non_line_board_graphics_bound_outline(tmp_path, graphic, expected):
    text = _HEADER + '  (net 0 "")\n  ' + graphic + "\n)\n"
    board = load_audit_board(_write(tmp_path, text))
    assert board.outline is not None
    assert (
        board.outline.x0,
        board.outline.y0,
        board.outline.x1,
        board.outline.y1,
    ) == expected


# --------------------------------------------------------------------------
# the slider
# --------------------------------------------------------------------------


def test_effort_levels_are_strictly_nested():
    """Deeper must mean more, never merely different."""
    quick = PROFILES[Effort.QUICK]
    standard = PROFILES[Effort.STANDARD]
    deep = PROFILES[Effort.DEEP]
    assert quick.groups < standard.groups < deep.groups
    assert set(rules_for(quick)) < set(rules_for(standard)) < set(rules_for(deep))
    assert quick.judgment_passes < standard.judgment_passes < deep.judgment_passes
    assert deep.refute_rounds > standard.refute_rounds
    assert deep.per_part_focus and not standard.per_part_focus


def test_deeper_thresholds_are_stricter():
    quick, deep = PROFILES[Effort.QUICK], PROFILES[Effort.DEEP]
    assert deep.decoupling_max_nm < quick.decoupling_max_nm
    assert deep.edge_margin_nm > quick.edge_margin_nm
    assert deep.clearance_nm >= quick.clearance_nm


def test_slider_marks_the_chosen_level():
    assert slider(Effort.QUICK).startswith("quick ●")
    assert "●" in slider("deep").split("─")[-1]
    assert slider(Effort.STANDARD).count("●") == 1


def test_every_rule_belongs_to_a_group_a_profile_runs():
    reachable = set()
    for profile in PROFILES.values():
        reachable |= profile.groups
    assert {rule.group for rule in RULES} <= reachable


# --------------------------------------------------------------------------
# deterministic rules
# --------------------------------------------------------------------------


def test_clean_board_reports_nothing(clean_board):
    result = review_board(clean_board, effort=Effort.QUICK)
    assert result.findings == []
    assert result.rules_run
    assert result.skipped_reason  # quick is deterministic, and says so


def test_overlap_is_proven_located_and_measured(overlapping_board):
    result = review_board(overlapping_board, effort=Effort.QUICK)
    overlaps = [f for f in result.findings if f.rule == "courtyard-overlap"]
    assert len(overlaps) == 1
    finding = overlaps[0]
    assert finding.severity is Severity.BLOCKER
    assert finding.origin is Origin.PROVEN
    assert set(finding.refs) == {"U1", "C1"}
    assert finding.located
    # U1 spans x 6..10, C1 spans x 8..12: the overlap is x 8..10, 2 mm wide.
    assert finding.extent is not None
    assert finding.extent.width_nm == 2_000_000
    # 2 mm of x by 4 mm of y.
    assert finding.extent.height_nm == 4_000_000
    assert "8.000 mm^2" in finding.evidence


def test_unrouted_net_is_a_blocker(tmp_path):
    u1 = _footprint("U1", 8, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    r1 = _footprint("R1", 20, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    path = _write(tmp_path, _board_text([u1, r1], ["SIG"]))
    result = review_board(path, effort=Effort.QUICK)
    unrouted = [f for f in result.findings if f.rule == "unrouted-net"]
    assert [f.severity for f in unrouted] == [Severity.BLOCKER]
    assert unrouted[0].nets == ("SIG",)


def test_split_net_is_caught_even_though_copper_exists(tmp_path):
    """Two pads, two stubs, no join: the file says one net, the copper does not."""
    u1 = _footprint("U1", 6, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    r1 = _footprint("R1", 20, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    path = _write(
        tmp_path,
        _board_text(
            [u1, r1],
            ["SIG"],
            segments=[(6, 8, 9, 8, 0.4, 1), (17, 8, 20, 8, 0.4, 1)],
        ),
    )
    result = review_board(path, effort=Effort.QUICK)
    split = [f for f in result.findings if f.rule == "net-not-connected"]
    assert len(split) == 1
    assert "2 islands" in split[0].evidence


def test_part_outside_the_outline(tmp_path):
    u1 = _footprint("U1", 29, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    r1 = _footprint("R1", 10, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    path = _write(tmp_path, _board_text([u1, r1], ["SIG"], outline=(0, 0, 30, 30)))
    result = review_board(path, effort=Effort.QUICK)
    off = [f for f in result.findings if f.rule == "part-off-board"]
    assert [f.refs for f in off] == [("U1",)]


def test_missing_outline_is_reported_once(tmp_path):
    u1 = _footprint("U1", 8, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")],
                    courtyard=(2, 2))
    path = _write(tmp_path, _board_text([u1], ["SIG"], outline=None))
    result = review_board(path, effort=Effort.QUICK)
    assert [f.rule for f in result.findings].count("no-board-outline") == 1


def test_silkscreen_over_pad_groups_by_part(tmp_path):
    """Three silk lines across two pads is one finding, not six."""
    silk = [(-2, -0.4, 2, -0.4), (-2, 0, 2, 0), (-2, 0.4, 2, 0.4)]
    c1 = _footprint(
        "C1", 8, 8,
        pads=[_pad("1", -0.8, 0, 1, 1, 1, "A"), _pad("2", 0.8, 0, 1, 1, 2, "B")],
        courtyard=(2, 2),
        silk=silk,
    )
    path = _write(tmp_path, _board_text([c1], ["A", "B"]))
    result = review_board(path, effort=Effort.STANDARD, model=None)
    silk_findings = [f for f in result.findings if f.rule == "silkscreen-over-pad"]
    assert len(silk_findings) == 1
    assert "C1.1" in silk_findings[0].title and "C1.2" in silk_findings[0].title


def test_pads_of_one_footprint_are_not_a_clearance_violation(tmp_path):
    """A land pattern's own spacing is not something a layout edit can fix."""
    u1 = _footprint(
        "U1", 8, 8,
        pads=[_pad("1", -0.55, 0, 1, 1, 1, "A"), _pad("2", 0.55, 0, 1, 1, 2, "B")],
        courtyard=(2, 2),
    )
    path = _write(tmp_path, _board_text([u1], ["A", "B"]))
    result = review_board(path, effort=Effort.STANDARD, model=None)
    assert [f for f in result.findings if f.rule == "pad-clearance"] == []


def test_pads_of_different_parts_too_close_are_reported(tmp_path):
    """Pad edges 0.1 mm apart across two footprints, well under clearance."""
    a = _footprint("R1", 8, 8, pads=[_pad("1", 0, 0, 1, 1, 1, "A")],
                   courtyard=(0.6, 0.6))
    b = _footprint("R2", 9.1, 8, pads=[_pad("1", 0, 0, 1, 1, 2, "B")],
                   courtyard=(0.6, 0.6))
    path = _write(tmp_path, _board_text([a, b], ["A", "B"]))
    result = review_board(path, effort=Effort.STANDARD, model=None)
    close = [f for f in result.findings if f.rule == "pad-clearance"]
    assert len(close) == 1
    assert "0.100 mm" in close[0].evidence


def test_track_width_counts_when_checking_copper_at_board_edge(tmp_path):
    path = _write(
        tmp_path,
        _board_text(
            [],
            ["SIG"],
            outline=(0, 0, 10, 10),
            segments=[(1, 5, 10, 5, 0.4, 1)],
        ),
    )
    result = review_board(path, effort=Effort.STANDARD, model=None)
    off_board = [f for f in result.findings if f.rule == "copper-off-board"]
    assert len(off_board) == 1
    assert off_board[0].extent.x1 == 10_200_000


def test_foreign_via_touching_track_is_a_clearance_blocker(tmp_path):
    path = _write(
        tmp_path,
        _board_text(
            [],
            ["A", "B"],
            outline=(0, 0, 10, 10),
            segments=[(2, 5, 8, 5, 0.2, 1)],
            vias=[(5, 5, 0.6, 0.3, 2)],
        ),
    )
    result = review_board(path, effort=Effort.STANDARD, model=None)
    clashes = [f for f in result.findings if f.rule == "via-track-clearance"]
    assert len(clashes) == 1
    assert clashes[0].severity is Severity.BLOCKER


def test_via_radius_counts_when_checking_copper_at_board_edge(tmp_path):
    path = _write(
        tmp_path,
        _board_text(
            [],
            ["SIG"],
            outline=(0, 0, 10, 10),
            vias=[(9.9, 5, 0.6, 0.3, 1)],
        ),
    )
    result = review_board(path, effort=Effort.STANDARD, model=None)
    off_board = [f for f in result.findings if f.rule == "copper-off-board"]
    assert len(off_board) == 1
    assert off_board[0].extent.x1 == 10_200_000


def test_via_can_connect_smd_pad_directly_to_opposite_layer_track(tmp_path):
    top = _footprint(
        "U1", 5, 5, pads=[_pad("1", 0, 0, 1, 1, 1, "SIG")], courtyard=(1, 1)
    )
    bottom_pad = _pad("1", 0, 0, 1, 1, 1, "SIG").replace("F.Cu", "B.Cu")
    bottom = _footprint(
        "R1", 15, 5, pads=[bottom_pad], courtyard=(1, 1)
    ).replace('(layer "F.Cu")', '(layer "B.Cu")', 1)
    text = _board_text([top, bottom], ["SIG"], outline=(0, 0, 20, 10))
    text = text[:-2] + """
  (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (segment (start 5 5) (end 15 5) (width 0.2) (layer "B.Cu") (net 1))
)
"""
    result = review_board(
        _write(tmp_path, text), effort=Effort.QUICK, model=None
    )
    assert [f for f in result.findings if f.rule == "net-not-connected"] == []


def test_decoupling_distance_tightens_with_effort(tmp_path):
    """The same board, two levels: 2.5 mm passes at standard, fails at deep."""
    pads = [
        _pad("1", -1, 0, 1, 1, 1, "VCC"),
        _pad("2", 0, 1, 1, 1, 2, "GND"),
        _pad("3", 1, 0, 1, 1, 3, "SIG"),
        _pad("4", 0, -1, 1, 1, 3, "SIG"),
    ]
    u1 = _footprint("U1", 8, 8, pads=pads, courtyard=(2, 2))
    c1 = _footprint(
        "C1", 9.5, 9, pads=[_pad("1", 0, 0, 1, 1, 1, "VCC"),
                             _pad("2", 1, 0, 1, 1, 2, "GND")],
        courtyard=(1.5, 0.8),
    )
    path = _write(tmp_path, _board_text([u1, c1], ["VCC", "GND", "SIG"]))

    standard = review_board(path, effort=Effort.STANDARD, model=None)
    assert [f for f in standard.findings if f.rule.startswith("decoupling")] == []

    deep = review_board(path, effort=Effort.DEEP, model=None)
    far = [f for f in deep.findings if f.rule == "decoupling-too-far"]
    assert len(far) == 1
    assert far[0].origin is Origin.PROVEN
    assert "limit 2.000 mm" in far[0].evidence


def test_missing_decoupling_is_reported(tmp_path):
    pads = [
        _pad("1", -1, 0, 1, 1, 1, "VCC"),
        _pad("2", 0, 1, 1, 1, 2, "GND"),
        _pad("3", 1, 0, 1, 1, 3, "SIG"),
        _pad("4", 0, -1, 1, 1, 3, "SIG"),
    ]
    u1 = _footprint("U1", 8, 8, pads=pads, courtyard=(2, 2))
    path = _write(tmp_path, _board_text([u1], ["VCC", "GND", "SIG"]))
    result = review_board(path, effort=Effort.STANDARD, model=None)
    missing = [f for f in result.findings if f.rule == "no-decoupling"]
    assert len(missing) == 1
    assert missing[0].nets == ("VCC",)


def test_every_proven_finding_carries_its_measurement(overlapping_board):
    result = review_board(overlapping_board, effort=Effort.DEEP, model=None)
    for finding in result.proven:
        assert finding.evidence, f"{finding.rule} proved nothing"
        assert finding.located, f"{finding.rule} could not be placed on the board"


def test_a_failing_rule_does_not_take_the_review_down(overlapping_board, monkeypatch):
    import silkscreen.audit.rules as rules_module

    def explode(board, profile):
        raise RuntimeError("boom")

    broken = tuple(
        rules_module.Rule(r.name, r.group, r.summary,
                          explode if r.name == "designators" else r.check)
        for r in rules_module.RULES
    )
    monkeypatch.setattr(rules_module, "RULES", broken)
    result = review_board(overlapping_board, effort=Effort.QUICK)
    assert any(f.rule == "courtyard-overlap" for f in result.findings)
    failed = [f for f in result.findings if f.rule == "checker-failed"]
    assert len(failed) == 1 and "RuntimeError" in failed[0].evidence


# --------------------------------------------------------------------------
# the model half
# --------------------------------------------------------------------------

_JUDGMENT = json.dumps(
    {
        "findings": [
            {
                "severity": "blocker",
                "title": "C1 is the wrong dielectric for a regulator output",
                "detail": "X7R here would be stable; this part is Y5V.",
                "refs": ["C1"],
                "nets": ["SIG"],
                "fix": "Use an X7R part.",
            },
            {
                "severity": "note",
                "title": "Q9 has no gate resistor",
                "detail": "Points at a part that is not on this board.",
                "refs": ["Q9"],
                "nets": [],
                "fix": "Add one.",
            },
        ]
    }
)


def test_quick_effort_never_calls_the_model(overlapping_board):
    model = ScriptedModel()  # any call raises ModelError: it has no responses
    result = review_board(overlapping_board, effort=Effort.QUICK, model=model)
    assert model.calls == []
    assert result.suggested == []


def test_model_findings_are_suggested_never_proven(overlapping_board):
    model = ScriptedModel(responses=[_JUDGMENT])
    result = review_board(overlapping_board, effort=Effort.STANDARD, model=model)
    suggested = result.suggested
    assert len(suggested) == 1  # the Q9 finding named no part on this board
    assert suggested[0].origin is Origin.SUGGESTED
    assert suggested[0].evidence == ""
    assert suggested[0].refs == ("C1",)
    assert suggested[0].located, "a finding naming C1 should sit on C1"
    # The proven half is untouched by the model pass.
    assert any(f.rule == "courtyard-overlap" for f in result.proven)


def test_the_model_is_shown_the_proven_findings(overlapping_board):
    model = ScriptedModel(responses=[_JUDGMENT])
    review_board(overlapping_board, effort=Effort.STANDARD, model=model)
    prompt = model.calls[0]["prompt"]
    assert "PROVEN" in prompt
    assert "U1 and C1 overlap" in prompt


def test_deep_effort_refutes_before_reporting(overlapping_board):
    """A refuted claim must not reach the report, whatever it claimed."""
    model = ScriptedModel(
        by_marker={
            "REFUTE": json.dumps(
                {"refuted": True, "reason": "the board has no such part"}
            ),
            "reviewing a PCB": _JUDGMENT,
        }
    )
    result = review_board(overlapping_board, effort=Effort.DEEP, model=model)
    assert result.suggested == []
    assert any("survived" in p or "refuted" in p or "1" in p
               for p in result.model_passes)


def test_deep_effort_keeps_a_claim_that_survives(overlapping_board):
    model = ScriptedModel(
        by_marker={
            "REFUTE": json.dumps({"refuted": False, "reason": "checked, it holds"}),
            "reviewing a PCB": _JUDGMENT,
        }
    )
    result = review_board(overlapping_board, effort=Effort.DEEP, model=model)
    assert len(result.suggested) == 1
    assert result.suggested[0].checks
    assert "survived" in result.suggested[0].checks[0]


@pytest.mark.parametrize("verdict", ["not JSON", '{"refuted": "false"}'])
def test_deep_effort_drops_claim_when_refutation_is_invalid(
    overlapping_board, verdict
):
    model = ScriptedModel(
        by_marker={"REFUTE": verdict, "reviewing a PCB": _JUDGMENT}
    )
    result = review_board(overlapping_board, effort=Effort.DEEP, model=model)
    assert result.suggested == []
    assert any("0 of" in entry for entry in result.model_passes)


def test_a_model_failure_loses_only_the_model_half(overlapping_board):
    class Broken:
        def generate(self, prompt, **kwargs):
            raise ModelError("no key")

    result = review_board(overlapping_board, effort=Effort.STANDARD, model=Broken())
    assert any(f.rule == "courtyard-overlap" for f in result.proven)
    assert result.suggested == []
    assert "no key" in result.skipped_reason


def test_unparseable_model_output_is_not_a_finding(overlapping_board):
    model = ScriptedModel(responses=["I could not analyse this board, sorry."])
    result = review_board(overlapping_board, effort=Effort.STANDARD, model=model)
    assert result.suggested == []
    assert result.proven


# --------------------------------------------------------------------------
# the visual half
# --------------------------------------------------------------------------


def test_svg_is_valid_xml_and_marks_every_located_finding(overlapping_board):
    result = review_board(overlapping_board, effort=Effort.DEEP, model=None)
    svg = render_svg(result)
    doc = xml.dom.minidom.parseString(svg)  # raises if the SVG is malformed
    marked = {
        g.getAttribute("data-finding")
        for g in doc.getElementsByTagName("g")
        if g.getAttribute("data-finding")
    }
    expected = {f.id for f in result.visible() if f.located}
    assert expected and marked == expected


def test_svg_keeps_kicad_coordinates_without_a_second_flip(overlapping_board):
    """The renderer must not re-flip Y: SVG and KiCad share a handedness."""
    board = load_audit_board(overlapping_board)
    result = review_board(board, effort=Effort.QUICK)
    svg = render_svg(result)
    u1 = board.part_by_ref("U1")
    assert u1 is not None
    # U1's courtyard top edge is at y = 8 - 2 = 6 mm, and appears as such.
    assert f'y="{u1.extent.y0 / 1_000_000:.0f}"' in svg or 'y="6"' in svg


def test_severity_and_origin_are_both_visible_in_the_svg(overlapping_board):
    model = ScriptedModel(responses=[_JUDGMENT])
    result = review_board(overlapping_board, effort=Effort.STANDARD, model=model)
    svg = render_svg(result)
    assert "origin-proven" in svg and "origin-suggested" in svg
    assert "sev-blocker" in svg
    # Suggested findings are dashed; proven ones are not.
    assert "stroke-dasharray" in svg


def test_html_report_is_self_contained_and_separates_the_halves(overlapping_board):
    model = ScriptedModel(responses=[_JUDGMENT])
    result = review_board(overlapping_board, effort=Effort.STANDARD, model=model)
    html = html_report(result)
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html
    assert "<svg" in html
    assert "Proven — measured in the board file" in html
    assert "Suggested — argued by the model, not verified" in html
    for finding in result.visible():
        assert f'data-finding="{finding.id}"' in html


def test_text_report_names_what_did_not_run(overlapping_board):
    result = review_board(overlapping_board, effort=Effort.STANDARD, model=None)
    report = text_report(result)
    assert "no model was supplied" in report
    assert "deterministic rules ran" in report


def test_json_report_is_machine_readable(overlapping_board):
    result = review_board(overlapping_board, effort=Effort.QUICK)
    data = json.loads(json_report(result))
    assert data["effort"] == "quick"
    assert data["counts"]["proven"] == len(result.proven)
    assert all(f["origin"] in ("proven", "suggested") for f in data["findings"])


def test_write_reports_emits_three_files(overlapping_board, tmp_path):
    result = review_board(overlapping_board, effort=Effort.QUICK)
    written = write_reports(result, tmp_path / "out")
    assert sorted(p.suffix for p in written) == [".html", ".json", ".svg"]
    assert all(p.exists() and p.stat().st_size > 200 for p in written)


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------


def test_cli_runs_offline_and_writes_a_report(overlapping_board, tmp_path, capsys):
    code = review_main(
        [str(overlapping_board), "--effort", "quick", "--no-model",
         "-o", str(tmp_path / "review")]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "U1 and C1 overlap" in out
    assert (tmp_path / "review" / "review.html").exists()


def test_cli_fails_on_blocker_only_when_asked(overlapping_board):
    assert review_main([str(overlapping_board), "-e", "quick", "--no-model"]) == 0
    assert (
        review_main(
            [str(overlapping_board), "-e", "quick", "--no-model",
             "--fail-on-blocker"]
        )
        == 1
    )


def test_cli_reports_a_missing_board_rather_than_raising(tmp_path, capsys):
    assert review_main([str(tmp_path / "nope.kicad_pcb"), "--no-model"]) == 2
    assert "No such board file" in capsys.readouterr().err


def test_profile_lookup_accepts_a_string():
    assert profile_for("deep") is PROFILES[Effort.DEEP]

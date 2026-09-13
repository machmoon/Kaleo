"""Tests for the CP-SAT placer.

Several of these are regression tests for specific defects in the original
``packing/rectangles.py``; those are marked with the behaviour they pin down.
"""

from __future__ import annotations

import itertools

import pytest
from silkscreen import Part, Wire, pack
from silkscreen.packing import PackStatus
from silkscreen.units import mm

# ---------------------------------------------------------------- helpers


def _boxes(parts, result):
    """(x0, y0, x1, y1) of each placed part in nanometres."""
    out = []
    for part, placed in zip(parts, result.placements, strict=True):
        w, h = part.width_nm, part.height_nm
        if placed.rotated:
            w, h = h, w
        out.append((placed.x_nm, placed.y_nm, placed.x_nm + w, placed.y_nm + h))
    return out


def _overlap_area(a, b):
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx * dy if dx > 0 and dy > 0 else 0


def _gap(a, b):
    """Edge-to-edge separation; 0 if the boxes touch or overlap."""
    dx = max(a[0] - b[2], b[0] - a[2], 0)
    dy = max(a[1] - b[3], b[1] - a[3], 0)
    return max(dx, dy)


R0603 = (mm(1.6), mm(0.8))
LQFP48 = (mm(9.0), mm(9.0))


# ---------------------------------------------------------------- basics


def test_empty_input_returns_empty_board():
    result = pack([])
    assert result.placements == []
    assert result.board_width_nm == 0
    assert result.board_height_nm == 0


def test_single_part_board_is_at_least_the_part():
    part = Part(*LQFP48, ref="U1")
    result = pack([part], clearance_nm=0)
    assert len(result.placements) == 1
    assert result.board_width_nm >= part.width_nm
    assert result.board_height_nm >= part.height_nm


def test_none_leaves_the_solver_without_a_time_limit():
    result = pack([Part(*R0603, ref="R1")], time_limit_s=None)

    assert result.status is PackStatus.OPTIMAL
    assert not any("Time limit" in warning for warning in result.warnings)


def test_rejects_non_positive_extents():
    with pytest.raises(ValueError, match="non-positive"):
        Part(0, mm(1), ref="BAD")


def test_rejects_out_of_range_wire_index():
    parts = [Part(*R0603, ref="R1")]
    with pytest.raises(ValueError, match="index 5"):
        pack(parts, [Wire(0, 5, (0, 0), (0, 0))])


# ---------------------------------------------------------------- invariants


def test_no_two_parts_overlap():
    parts = [
        Part(*LQFP48, ref="U1"),
        Part(mm(6.0), mm(5.0), ref="U2"),
        Part(*R0603, ref="R1"),
        Part(*R0603, ref="R2"),
        Part(mm(3.2), mm(1.6), ref="C1"),
        Part(mm(2.0), mm(12.0), ref="J1"),
    ]
    result = pack(parts, time_limit_s=10.0)
    boxes = _boxes(parts, result)
    for a, b in itertools.combinations(boxes, 2):
        assert _overlap_area(a, b) == 0


def test_clearance_is_respected():
    """Regression: the original packed parts flush at 0 mm, unmanufacturable."""
    clearance = mm(0.25)
    parts = [Part(*R0603, ref=f"R{i}") for i in range(6)]
    result = pack(parts, clearance_nm=clearance, time_limit_s=10.0)
    boxes = _boxes(parts, result)
    for a, b in itertools.combinations(boxes, 2):
        assert _gap(a, b) >= 2 * (clearance // 2)


def test_all_parts_inside_reported_board():
    parts = [
        Part(*LQFP48, ref="U1"),
        Part(*R0603, ref="R1"),
        Part(mm(2.0), mm(10.0), ref="J1"),
    ]
    result = pack(parts, time_limit_s=10.0)
    for x0, y0, x1, y1 in _boxes(parts, result):
        assert x0 >= 0 and y0 >= 0
        assert x1 <= result.board_width_nm
        assert y1 <= result.board_height_nm


def test_tall_part_does_not_make_model_infeasible():
    """Regression for the `for h, _ in rects` bug.

    The original derived the y-domain from the sum of *widths*. A part taller
    than the total width of the board was therefore unplaceable and the solver
    reported INFEASIBLE.
    """
    parts = [
        Part(mm(2.0), mm(60.0), ref="J1"),  # very tall, very narrow
        Part(*R0603, ref="R1"),
    ]
    result = pack(parts, time_limit_s=10.0)
    assert result.status in (PackStatus.OPTIMAL, PackStatus.FEASIBLE)
    assert result.board_height_nm >= mm(60.0)


# ---------------------------------------------------------------- features


def test_edge_constrained_part_lands_on_an_edge():
    parts = [
        Part(*LQFP48, ref="U1"),
        Part(*LQFP48, ref="U2"),
        Part(mm(9.0), mm(7.0), ref="J1", must_be_on_edge=True),
    ]
    result = pack(parts, clearance_nm=0, time_limit_s=15.0)
    boxes = _boxes(parts, result)
    x0, y0, x1, y1 = boxes[2]
    on_edge = (
        x0 == 0
        or y0 == 0
        or x1 == result.board_width_nm
        or y1 == result.board_height_nm
    )
    assert on_edge, f"J1 at {boxes[2]} is not on any edge of the board"


def test_rotation_lets_a_tall_part_lie_down():
    """A 2x20 part next to a 20x2 part should share a row when rotation is on."""
    parts = [
        Part(mm(20.0), mm(2.0), ref="A"),
        Part(mm(2.0), mm(20.0), ref="B", allow_rotation=True),
    ]
    rotated = pack(parts, clearance_nm=0, time_limit_s=15.0)
    fixed = pack(
        [Part(mm(20.0), mm(2.0), ref="A"), Part(mm(2.0), mm(20.0), ref="B")],
        clearance_nm=0,
        time_limit_s=15.0,
    )
    rot_half_perim = rotated.board_width_nm + rotated.board_height_nm
    fix_half_perim = fixed.board_width_nm + fixed.board_height_nm
    assert rot_half_perim <= fix_half_perim


def test_wires_pull_connected_parts_together():
    """With a strong wire weight, a connected pair should sit closer."""
    def build():
        return [
            Part(*R0603, ref="R1"),
            Part(*R0603, ref="R2"),
            Part(*LQFP48, ref="U1"),
        ]

    centre_r = (R0603[0] // 2, R0603[1] // 2)
    wires = [Wire(0, 1, centre_r, centre_r)]

    tight = pack(
        build(), wires, clearance_nm=0, wire_weight=100, size_weight=1,
        balance_objective=False, time_limit_s=15.0,
    )
    assert tight.wirelength_nm is not None
    # R1 and R2 should be adjacent: their centre-to-centre Manhattan distance
    # ought to be on the order of one part, not the whole board.
    assert tight.wirelength_nm <= mm(12.0)


def test_max_board_too_small_is_infeasible():
    parts = [Part(*LQFP48, ref="U1"), Part(*LQFP48, ref="U2")]
    with pytest.raises(RuntimeError, match="do not fit"):
        pack(parts, max_board_nm=(mm(10.0), mm(10.0)), clearance_nm=0)


def test_impossible_time_limit_falls_back_instead_of_raising():
    """Regression: the original raised RuntimeError on UNKNOWN, discarding work."""
    parts = [Part(*R0603, ref=f"R{i}") for i in range(40)]
    result = pack(parts, time_limit_s=0.001, grid_nm=1000)
    assert result.status is PackStatus.FALLBACK
    assert len(result.placements) == len(parts)
    assert result.warnings
    boxes = _boxes(parts, result)
    for a, b in itertools.combinations(boxes, 2):
        assert _overlap_area(a, b) == 0


def test_fallback_layout_is_itself_valid():
    parts = [
        Part(mm(1.0 + i), mm(2.0), ref=f"P{i}") for i in range(12)
    ]
    result = pack(parts, time_limit_s=0.001, grid_nm=1000)
    assert result.status is PackStatus.FALLBACK
    boxes = _boxes(parts, result)
    assert len({p.ref for p in result.placements}) == len(parts)
    for a, b in itertools.combinations(boxes, 2):
        assert _overlap_area(a, b) == 0


def test_results_are_deterministic_for_a_fixed_seed():
    parts = [
        Part(*LQFP48, ref="U1"),
        Part(*R0603, ref="R1"),
        Part(mm(3.2), mm(1.6), ref="C1"),
        Part(mm(2.0), mm(8.0), ref="J1"),
    ]
    a = pack(parts, seed=7, workers=1, time_limit_s=10.0)
    b = pack(parts, seed=7, workers=1, time_limit_s=10.0)
    assert [(p.x_nm, p.y_nm, p.rotated) for p in a.placements] == [
        (p.x_nm, p.y_nm, p.rotated) for p in b.placements
    ]


def test_refs_are_preserved_in_order():
    parts = [Part(*R0603, ref=f"R{i}") for i in range(5)]
    result = pack(parts, time_limit_s=10.0)
    assert [p.ref for p in result.placements] == [p.ref for p in parts]


# ---------------------------------------------------------------- units


def test_mm_round_trip():
    from silkscreen.units import to_mm

    assert mm(1.6) == 1_600_000
    assert to_mm(mm(2.54)) == pytest.approx(2.54)


# ---------------------------------------------------------------- domain rules


def test_decoupling_cap_is_placed_next_to_its_ic():
    """Regression: the most consequential placement rule in PCB layout.

    A decoupling capacitor's only connections to the IC it bypasses are the
    power rails. An earlier version dropped power nets from the objective
    entirely, which left the cap with no objective term at all -- it drifted to
    wherever the packer found room. A 100nF part 10 mm from its IC carries
    enough loop inductance to do nothing above a few MHz.
    """
    from silkscreen import Net

    ic_pin = (mm(4.5), mm(4.5))
    pad = (mm(0.8), mm(0.4))

    parts = [Part(mm(9), mm(9), ref="U1"), Part(*R0603, ref="C1")]
    parts += [Part(*R0603, ref=f"R{i}") for i in range(6)]

    nets = [Net(terminals=((0, ic_pin), (1, pad)), name="VCC", weight=0.25)]
    for i in range(6):
        src = 0 if i == 0 else i + 1
        nets.append(
            Net(
                terminals=((src, ic_pin if src == 0 else pad), (i + 2, pad)),
                name=f"SIG{i}",
            )
        )

    result = pack(parts, nets=nets, time_limit_s=15.0)
    pos = {p.ref: (p.x_nm, p.y_nm) for p in result.placements}
    ux, uy = pos["U1"]
    cx, cy = pos["C1"]
    vcc_len = abs((ux + ic_pin[0]) - (cx + pad[0])) + abs(
        (uy + ic_pin[1]) - (cy + pad[1])
    )
    # U1's pin sits at its centre, so ~5 mm is the cap sitting against the IC
    # edge. Anything past 8 mm means it went somewhere else on the board.
    assert vcc_len <= mm(7.0), (
        f"decoupling cap is {vcc_len / 1e6:.2f} mm from its IC pin"
    )


def test_power_net_still_influences_placement():
    """A weighted power net must not be silently equivalent to no net."""
    from silkscreen import Net

    ic_pin = (mm(4.5), mm(4.5))
    pad = (mm(0.8), mm(0.4))
    parts = [Part(mm(9), mm(9), ref="U1"), Part(*R0603, ref="C1")]
    parts += [Part(*R0603, ref=f"R{i}") for i in range(4)]

    def run(weight):
        nets = [Net(terminals=((0, ic_pin), (1, pad)), name="VCC", weight=weight)]
        r = pack(parts, nets=nets, time_limit_s=10.0)
        pos = {p.ref: (p.x_nm, p.y_nm) for p in r.placements}
        ux, uy = pos["U1"]
        cx, cy = pos["C1"]
        return abs((ux + ic_pin[0]) - (cx + pad[0])) + abs(
            (uy + ic_pin[1]) - (cy + pad[1])
        )

    assert run(0.25) < run(0.0), "weighting the power net changed nothing"


def test_fractional_wire_weight_is_not_truncated_to_zero():
    """Regression: int(round(0.4)) == 0 silently disabled the whole wire term."""
    from silkscreen import Net

    pad = (mm(0.8), mm(0.4))
    parts = [Part(*R0603, ref=f"R{i}") for i in range(6)]
    nets = [Net(terminals=((0, pad), (5, pad)), name="N1")]

    near = pack(parts, nets=nets, wire_weight=0.4, size_weight=0.0, time_limit_s=10.0)
    none = pack(parts, nets=nets, wire_weight=0.0, size_weight=1.0, time_limit_s=10.0)
    assert near.wirelength_nm is not None
    assert near.wirelength_nm <= none.wirelength_nm


def test_size_weight_zero_disables_the_size_term():
    """Regression: max(1, ...) clamped size_weight=0 back up to 1."""
    from silkscreen import Net

    pad = (mm(0.8), mm(0.4))
    parts = [Part(*R0603, ref=f"R{i}") for i in range(5)]
    nets = [Net(terminals=((0, pad), (4, pad)), name="N1")]
    wire_only = pack(
        parts, nets=nets, size_weight=0.0, wire_weight=1.0, time_limit_s=10.0
    )
    size_only = pack(
        parts, nets=nets, size_weight=1.0, wire_weight=0.0, time_limit_s=10.0
    )
    assert wire_only.wirelength_nm < size_only.wirelength_nm, (
        "size_weight=0 did not hand the objective to the wire term"
    )


def test_empty_objective_is_rejected():
    with pytest.raises(ValueError, match="Objective is empty"):
        pack([Part(*R0603, ref="R1")], size_weight=0.0, wire_weight=0.0)


def test_odd_clearance_is_met_not_missed():
    """Regression: clearance // 2 lost a nanometre on odd values."""
    clearance = mm(0.25) + 1
    parts = [Part(*R0603, ref=f"R{i}") for i in range(4)]
    result = pack(parts, clearance_nm=clearance, time_limit_s=10.0)
    boxes = _boxes(parts, result)
    for a, b in itertools.combinations(boxes, 2):
        assert _gap(a, b) >= clearance


def test_max_board_is_a_ceiling_not_a_suggestion():
    """Regression: cells_ceil overshot a hard cap by up to one grid cell."""
    parts = [Part(*R0603, ref=f"R{i}") for i in range(3)]
    cap = (mm(9.0), mm(9.0))
    r = pack(
        parts, max_board_nm=cap, grid_nm=700_000, clearance_nm=0, time_limit_s=10.0
    )
    assert r.board_width_nm <= cap[0]
    assert r.board_height_nm <= cap[1]


def test_rotation_offered_when_extents_differ_even_if_cells_match():
    """Regression: the guard compared rounded cells, not true extents."""
    parts = [
        Part(mm(2.0), mm(1.9), ref="A", allow_rotation=True),
        Part(mm(2.0), mm(1.9), ref="B", allow_rotation=True),
    ]
    # Coarse grid: both dimensions round to the same cell count.
    r = pack(parts, grid_nm=500_000, clearance_nm=mm(0.25), time_limit_s=10.0)
    assert len(r.placements) == 2  # model built without raising


def test_fallback_warns_when_it_drops_hard_constraints():
    """A fallback that silently ignores must_be_on_edge is worse than a failure."""
    parts = [Part(*R0603, ref=f"R{i}") for i in range(40)]
    parts[0] = Part(*R0603, ref="J1", must_be_on_edge=True)
    r = pack(parts, time_limit_s=0.001, grid_nm=1000)
    assert r.status is PackStatus.FALLBACK
    assert any("must_be_on_edge" in w for w in r.warnings)


# ------------------------------------------------- pinned parts and keepouts


def _overlaps(a, b):
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx > 0 and dy > 0


def test_a_pinned_part_lands_where_it_was_pinned():
    """Without this the tool cannot be used iteratively: every re-solve
    reshuffles the board and you lose the placement you liked."""
    grid = 50_000
    parts = [
        Part(*LQFP48, ref="U1"),
        Part(mm(5), mm(5), ref="J1", fixed_at_nm=(mm(2), mm(3))),
        Part(*R0603, ref="R1"),
    ]
    result = pack(parts, grid_nm=grid, time_limit_s=10.0)
    j1 = next(p for p in result.placements if p.ref == "J1")
    # Snapped to the grid, so allow one cell of slack.
    assert abs(j1.x_nm - mm(2)) <= grid
    assert abs(j1.y_nm - mm(3)) <= grid


def test_pinning_is_stable_across_a_resolve():
    """Re-solving with a part pinned to its previous spot keeps it there."""
    parts = [Part(*LQFP48, ref="U1"), Part(*R0603, ref="R1"),
             Part(*R0603, ref="R2"), Part(mm(4), mm(4), ref="J1")]
    first = pack(parts, time_limit_s=10.0)
    j1 = next(p for p in first.placements if p.ref == "J1")

    repinned = [
        Part(mm(4), mm(4), ref="J1", fixed_at_nm=(j1.x_nm, j1.y_nm))
        if p.ref == "J1" else p
        for p in parts
    ]
    second = pack(repinned, time_limit_s=10.0)
    j1b = next(p for p in second.placements if p.ref == "J1")
    assert abs(j1b.x_nm - j1.x_nm) <= 50_000
    assert abs(j1b.y_nm - j1.y_nm) <= 50_000


def test_pinned_part_still_respects_clearance():
    parts = [
        Part(*R0603, ref="R1", fixed_at_nm=(mm(1), mm(1))),
        Part(*R0603, ref="R2"),
        Part(*R0603, ref="R3"),
    ]
    clearance = mm(0.25)
    result = pack(parts, clearance_nm=clearance, time_limit_s=10.0)
    boxes = _boxes(parts, result)
    for a, b in itertools.combinations(boxes, 2):
        assert _gap(a, b) >= clearance


def test_pinning_below_the_clearance_ring_raises_with_a_usable_message():
    parts = [Part(*R0603, ref="R1", fixed_at_nm=(0, 0))]
    with pytest.raises(ValueError, match="minimum pinnable coordinate"):
        pack(parts, clearance_nm=mm(0.25), time_limit_s=5.0)


def test_pinning_outside_the_board_raises():
    parts = [Part(*R0603, ref="R1", fixed_at_nm=(mm(500), mm(500)))]
    with pytest.raises(ValueError, match="outside the solvable area"):
        pack(parts, max_board_nm=(mm(20), mm(20)), time_limit_s=5.0)


def test_no_part_occupies_a_keepout():
    """Mounting holes and connector envelopes are regions, not parts."""
    from silkscreen import Keepout

    keep = Keepout(mm(5), mm(5), mm(4), mm(4), name="MH1")
    parts = [Part(*R0603, ref=f"R{i}") for i in range(8)]
    result = pack(parts, keepouts=[keep], clearance_nm=0, time_limit_s=15.0)

    region = (keep.x_nm, keep.y_nm,
              keep.x_nm + keep.width_nm, keep.y_nm + keep.height_nm)
    for box in _boxes(parts, result):
        assert not _overlaps(box, region), f"a part sits in keepout {keep.name}"


def test_keepout_is_inside_the_reported_board():
    from silkscreen import Keepout

    keep = Keepout(mm(20), mm(0), mm(3), mm(3), name="MH1")
    result = pack([Part(*R0603, ref="R1")], keepouts=[keep], time_limit_s=10.0)
    assert result.board_width_nm >= keep.x_nm + keep.width_nm


def test_keepouts_and_pins_compose():
    from silkscreen import Keepout

    parts = [
        Part(*LQFP48, ref="U1"),
        Part(mm(5), mm(5), ref="J1", fixed_at_nm=(mm(1), mm(1))),
        Part(*R0603, ref="R1"),
        Part(*R0603, ref="R2"),
    ]
    keep = Keepout(mm(14), mm(14), mm(3.2), mm(3.2), name="MH1")
    result = pack(parts, keepouts=[keep], clearance_nm=0, time_limit_s=15.0)

    region = (keep.x_nm, keep.y_nm,
              keep.x_nm + keep.width_nm, keep.y_nm + keep.height_nm)
    boxes = _boxes(parts, result)
    for box in boxes:
        assert not _overlaps(box, region)
    for a, b in itertools.combinations(boxes, 2):
        assert not _overlaps(a, b)


def test_degenerate_keepout_is_rejected():
    from silkscreen import Keepout

    with pytest.raises(ValueError, match="non-positive extent"):
        Keepout(0, 0, 0, mm(1), name="bad")


def test_fallback_warns_about_pins_and_keepouts_it_cannot_honour():
    from silkscreen import Keepout

    parts = [Part(*R0603, ref=f"R{i}") for i in range(40)]
    parts[0] = Part(*R0603, ref="J1", fixed_at_nm=(mm(1), mm(1)))
    result = pack(
        parts, keepouts=[Keepout(mm(5), mm(5), mm(2), mm(2))],
        time_limit_s=0.001, grid_nm=1000,
    )
    assert result.status is PackStatus.FALLBACK
    assert any("fixed_at_nm" in w for w in result.warnings)
    assert any("keepout" in w for w in result.warnings)


# ------------------------------------------------------- two-sided placement


def test_parts_on_opposite_sides_may_share_xy():
    """That is the entire point of a two-sided board."""
    from silkscreen import Layer

    parts = [
        Part(*LQFP48, ref="U1", layer=Layer.TOP),
        Part(*LQFP48, ref="U2", layer=Layer.BOTTOM),
    ]
    result = pack(parts, clearance_nm=0, time_limit_s=10.0)
    # Both 9x9 parts fit in a 9x9 board because they are on opposite faces.
    assert result.board_width_nm <= mm(9.5)
    assert result.board_height_nm <= mm(9.5)


def test_parts_on_the_same_side_still_may_not_overlap():
    from silkscreen import Layer

    parts = [
        Part(*LQFP48, ref="U1", layer=Layer.BOTTOM),
        Part(*LQFP48, ref="U2", layer=Layer.BOTTOM),
    ]
    result = pack(parts, clearance_nm=0, time_limit_s=10.0)
    boxes = _boxes(parts, result)
    assert _overlap_area(boxes[0], boxes[1]) == 0


def test_either_layer_halves_the_board():
    from silkscreen import Layer

    single = pack(
        [Part(*LQFP48, ref=f"U{i}") for i in range(4)], time_limit_s=10.0
    )
    both = pack(
        [Part(*LQFP48, ref=f"U{i}", layer=Layer.EITHER) for i in range(4)],
        time_limit_s=10.0,
    )
    single_area = single.board_width_nm * single.board_height_nm
    both_area = both.board_width_nm * both.board_height_nm
    assert both_area < single_area


def test_placement_reports_the_side_it_chose():
    from silkscreen import Layer

    parts = [Part(*LQFP48, ref=f"U{i}", layer=Layer.EITHER) for i in range(4)]
    result = pack(parts, time_limit_s=10.0)
    sides = {p.layer for p in result.placements}
    assert sides <= {Layer.TOP, Layer.BOTTOM}
    assert len(sides) == 2, "with 4 equal parts the solver should use both sides"


def test_a_fixed_layer_is_honoured():
    from silkscreen import Layer

    parts = [
        Part(*R0603, ref="R1", layer=Layer.BOTTOM),
        Part(*R0603, ref="R2", layer=Layer.TOP),
        Part(*R0603, ref="R3", layer=Layer.EITHER),
    ]
    result = pack(parts, time_limit_s=10.0)
    by_ref = {p.ref: p.layer for p in result.placements}
    assert by_ref["R1"] is Layer.BOTTOM
    assert by_ref["R2"] is Layer.TOP


def test_a_keepout_blocks_both_sides():
    """A mounting hole goes through the board."""
    from silkscreen import Keepout, Layer

    keep = Keepout(mm(5), mm(5), mm(4), mm(4), name="MH1")
    parts = [Part(*R0603, ref=f"R{i}", layer=Layer.EITHER) for i in range(8)]
    result = pack(parts, keepouts=[keep], clearance_nm=0, time_limit_s=15.0)
    region = (keep.x_nm, keep.y_nm,
              keep.x_nm + keep.width_nm, keep.y_nm + keep.height_nm)
    for box in _boxes(parts, result):
        assert not _overlaps(box, region)


def test_fallback_warns_that_it_is_single_sided():
    from silkscreen import Layer

    parts = [Part(*R0603, ref=f"R{i}", layer=Layer.EITHER) for i in range(40)]
    result = pack(parts, time_limit_s=0.001, grid_nm=1000)
    assert result.status is PackStatus.FALLBACK
    assert any("single-sided" in w for w in result.warnings)

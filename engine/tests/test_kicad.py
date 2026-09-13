"""KiCad interop tests, run against a real board file.

The fixture is ``tests/fixtures/ref.kicad_pcb`` -- the hand-placed board the original
pipeline pasted into KiCad through the clipboard. Using it here is deliberate:
it proves the same board can be placed by writing a file instead of by driving
a GUI.
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path

import pytest
from silkscreen import pack
from silkscreen.kicad import (
    apply_placements,
    extract_nets,
    extract_parts,
    extract_wires,
    footprint_ref,
    is_power_net,
    load_board,
    save_board,
    to_parts,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ref.kicad_pcb"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="board fixture not present"
)


@pytest.fixture(scope="module")
def board():
    return load_board(FIXTURE)


@pytest.fixture(scope="module")
def infos(board):
    return extract_parts(board)


def test_reads_every_footprint(infos):
    assert len(infos) == 11
    assert all(i.ref for i in infos), "every footprint should have a reference"


def test_footprint_extents_are_physically_plausible(infos):
    for info in infos:
        # Nothing on a board this size is smaller than 0.3 mm or larger than 30 mm.
        assert 300_000 <= info.width_nm <= 30_000_000, info.ref
        assert 300_000 <= info.height_nm <= 30_000_000, info.ref


def test_lqfp48_is_about_9mm_including_courtyard(infos):
    """An LQFP-48 7x7mm body has ~9mm courtyard including leads and margin."""
    big = max(infos, key=lambda i: i.width_nm * i.height_nm)
    assert 8_000_000 <= big.width_nm <= 11_000_000
    assert 8_000_000 <= big.height_nm <= 11_000_000


def test_pads_carry_net_names(infos):
    assert any(info.pad_nets for info in infos), "expected nets on pads"


def test_power_net_detection():
    assert is_power_net("GND", 2, 6)
    assert is_power_net("+3V3", 2, 6)
    assert is_power_net("VCC", 2, 6)
    assert is_power_net("/AVDD", 2, 6)
    assert not is_power_net("SWDIO", 2, 6)
    # Fans out too far to be a signal net, whatever it is called.
    assert is_power_net("SOME_BUS", 40, 6)


def test_power_nets_are_excluded_from_wires(infos):
    """Regression: the original expanded every net, ground included, into a
    clique, which swamped the objective and merged the whole board."""
    wires = extract_wires(infos)
    all_pairs = sum(
        len(v) * (len(v) - 1) // 2
        for v in _nets_by_name(infos).values()
        if len(v) > 1
    )
    assert len(wires) < all_pairs, "star + power exclusion should shrink the graph"
    assert len(wires) > 0, "but some signal nets should survive"


def _nets_by_name(infos):
    out: dict[str, list] = {}
    for idx, info in enumerate(infos):
        for pad, net in info.pad_nets.items():
            out.setdefault(net, []).append((idx, pad))
    return out


def test_end_to_end_replace_produces_a_valid_board(board, infos, tmp_path):
    """Load a real board, re-place it with the solver, write it back out."""
    parts = to_parts(infos)
    wires = extract_wires(infos)

    result = pack(parts, wires, time_limit_s=20.0)
    assert len(result.placements) == len(parts)

    moved = apply_placements(board, infos, result.placements, result.board_height_nm)
    assert moved == len(infos), "every footprint should have been placed"

    out = save_board(board, tmp_path / "placed.kicad_pcb")
    assert out.exists() and out.stat().st_size > 1000

    # And it round-trips: KiCad's own parser should accept what we wrote.
    reloaded = load_board(out)
    assert len(reloaded.footprints) == len(board.footprints)

    # No two placed courtyards overlap on the written board.
    #
    # This must be computed as anchor + LOCAL courtyard bbox. Deriving the box
    # as (fp.position, fp.position + size) assumes fp.position is the bbox
    # corner -- it is the anchor -- and a test that makes that assumption is
    # self-consistently wrong: it passes while the written board overlaps.
    new_infos = extract_parts(reloaded)
    boxes = []
    for info, fp in zip(new_infos, reloaded.footprints, strict=True):
        x0 = fp.position.X + info.min_x_nm / 1e6
        y0 = fp.position.Y + info.min_y_nm / 1e6
        x1 = fp.position.X + info.max_x_nm / 1e6
        y1 = fp.position.Y + info.max_y_nm / 1e6
        boxes.append((info.ref, x0, y0, x1, y1))
    for (ra, ax0, ay0, ax1, ay1), (rb, bx0, by0, bx1, by1) in itertools.combinations(
        boxes, 2
    ):
        dx = min(ax1, bx1) - max(ax0, bx0)
        dy = min(ay1, by1) - max(ay0, by0)
        assert not (dx > 0.01 and dy > 0.01), (
            f"courtyard overlap {ra}/{rb}: {dx:.2f} x {dy:.2f} mm"
        )

    # And the board stays in the positive quadrant it reported.
    min_x = min(b[1] for b in boxes)
    min_y = min(b[2] for b in boxes)
    assert min_x >= -0.01, f"board spills to x={min_x:.2f} mm"
    assert min_y >= -0.01, f"board spills to y={min_y:.2f} mm"


def test_courtyard_is_anchor_centred_in_the_fixture(infos):
    """Guards the assumption the placement math depends on.

    If a footprint's courtyard started at its anchor, min_x would be ~0 and the
    anchor/bbox distinction would be invisible. It is not: courtyards straddle
    the anchor, which is why apply_placements must subtract the local corner.
    """
    for info in infos:
        assert info.min_x_nm < 0, f"{info.ref} courtyard does not straddle anchor"
        assert info.max_x_nm > 0, f"{info.ref} courtyard does not straddle anchor"


def test_mixed_valid_and_missing_refs_land_correctly(board, infos):
    """A bogus ref must not shift the valid ones onto the wrong footprints."""
    from silkscreen.packing import Placement

    target = infos[0]
    placements = [
        Placement(ref="NOT_ON_BOARD", x_nm=0, y_nm=0),
        Placement(ref=target.ref, x_nm=5_000_000, y_nm=3_000_000),
    ]
    moved = apply_placements(board, infos, placements, 40_000_000)
    assert moved == 1

    from silkscreen.kicad import footprint_ref
    fp = next(f for f in board.footprints if footprint_ref(f) == target.ref)
    expected_x = (5_000_000 - target.min_x_nm) / 1e6
    assert pytest.approx(expected_x, abs=1e-6) == fp.position.X


def test_missing_ref_is_skipped_not_misaligned(board, infos):
    """Regression: a missing footprint used to desync the size and ref lists,
    shifting every subsequent placement onto the wrong part."""
    from silkscreen.packing import Placement

    bogus = [Placement(ref="NOT_ON_BOARD", x_nm=0, y_nm=0)]
    moved = apply_placements(board, infos, bogus, 10_000_000)
    assert moved == 0


def test_board_outline_is_written_and_encloses_every_part(board, infos, tmp_path):
    """Without an outline, must_be_on_edge is solved against nothing.

    Edge.Cuts is what KiCad measures board-edge clearance against, so a file
    with no outline cannot be meaningfully design-rule checked.
    """
    from silkscreen.kicad import set_board_outline

    parts = to_parts(infos)
    result = pack(parts, nets=extract_nets(infos), time_limit_s=15.0)
    apply_placements(board, infos, result.placements, result.board_height_nm)
    set_board_outline(board, result.board_width_nm, result.board_height_nm)

    out = save_board(board, tmp_path / "outlined.kicad_pcb")
    reloaded = load_board(out)

    edges = [
        g for g in reloaded.graphicItems
        if getattr(g, "layer", None) == "Edge.Cuts"
    ]
    assert len(edges) == 4, f"expected a 4-sided outline, got {len(edges)}"

    xs = [p.X for g in edges for p in (g.start, g.end)]
    ys = [p.Y for g in edges for p in (g.start, g.end)]
    ex0, ex1, ey0, ey1 = min(xs), max(xs), min(ys), max(ys)

    # Every courtyard must sit inside the outline we drew.
    new_infos = extract_parts(reloaded)
    for info, fp in zip(new_infos, reloaded.footprints, strict=True):
        assert fp.position.X + info.min_x_nm / 1e6 >= ex0 - 0.01, info.ref
        assert fp.position.X + info.max_x_nm / 1e6 <= ex1 + 0.01, info.ref
        assert fp.position.Y + info.min_y_nm / 1e6 >= ey0 - 0.01, info.ref
        assert fp.position.Y + info.max_y_nm / 1e6 <= ey1 + 0.01, info.ref


def test_extract_nets_keeps_power_rails_at_reduced_weight():
    """Power nets must stay in the objective, not be dropped."""
    from silkscreen.kicad import FootprintInfo, extract_nets

    infos = [
        FootprintInfo("U1", 9_000_000, 9_000_000,
                      {"1": (0, 0), "2": (10, 10)}, {"1": "GND", "2": "SDA"}),
        FootprintInfo("C1", 1_600_000, 800_000,
                      {"1": (0, 0), "2": (10, 10)}, {"1": "GND", "2": "SDA"}),
    ]
    nets = extract_nets(infos)
    by_name = {n.name: n for n in nets}
    assert "GND" in by_name, "power net was dropped instead of down-weighted"
    assert by_name["GND"].weight < by_name["SDA"].weight


def test_unknown_edge_ref_raises_instead_of_silently_doing_nothing(infos):
    """A typo'd ref used to produce no constraint and no signal."""
    with pytest.raises(ValueError, match="not on this board"):
        to_parts(infos, edge_refs={"J1"})


# --- Footprints that arrive already rotated ---------------------------------
#
# A courtyard is stored unrotated in the file; the footprint's ``angle`` turns it
# on the board. Reading the stored geometry literally models a part turned 90
# degrees with its width and height the wrong way round, and apply_placements
# leaves the angle alone -- so the written board really does overlap.


def _truth_box(fp):
    """On-board courtyard bbox from raw file geometry, honouring the angle.

    Deliberately does not use anything in ``kicad.py``: a check written in terms
    of the code under test shares its blind spot and passes while the board is
    wrong.
    """
    angle = math.radians(-(fp.position.angle or 0))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    pts = []
    for item in fp.graphicItems:
        if "CrtYd" not in (getattr(item, "layer", "") or ""):
            continue
        for attr in ("start", "end"):
            pt = getattr(item, attr, None)
            if pt is not None:
                pts.append((pt.X * cos_a - pt.Y * sin_a, pt.X * sin_a + pt.Y * cos_a))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (
        fp.position.X + min(xs), fp.position.Y + min(ys),
        fp.position.X + max(xs), fp.position.Y + max(ys),
    )


def test_pre_rotated_footprint_is_measured_as_it_sits():
    """U3 is a SOT-223: 8.8 x 7.2 mm flat, 7.2 x 8.8 mm turned 90 degrees."""
    board = load_board(FIXTURE)
    flat = next(i for i in extract_parts(board) if i.ref == "U3")

    next(f for f in board.footprints if footprint_ref(f) == "U3").position.angle = 90
    turned = next(i for i in extract_parts(board) if i.ref == "U3")

    assert (turned.width_nm, turned.height_nm) == (flat.height_nm, flat.width_nm)
    assert flat.width_nm != flat.height_nm, "fixture part must be non-square here"


def test_pre_rotated_pads_move_with_their_footprint():
    """Pad offsets feed the wirelength objective; a stale offset aims a net at
    the corner the pad used to be in."""
    board = load_board(FIXTURE)
    flat = next(i for i in extract_parts(board) if i.ref == "U3")
    next(f for f in board.footprints if footprint_ref(f) == "U3").position.angle = 90
    turned = next(i for i in extract_parts(board) if i.ref == "U3")

    assert turned.pad_offsets != flat.pad_offsets
    # Offsets stay inside the (rotated) courtyard they are measured against.
    for ref, (ox, oy) in turned.pad_offsets.items():
        assert 0 <= ox <= turned.width_nm, ref
        assert 0 <= oy <= turned.height_nm, ref


def test_pre_rotated_board_places_without_real_overlap(tmp_path):
    """The regression in full: rotate two non-square parts as a user would in
    KiCad, then place. This overlapped by 2.5 mm before rotation was read."""
    board = load_board(FIXTURE)
    for ref in ("U2", "U3"):
        next(f for f in board.footprints if footprint_ref(f) == ref).position.angle = 90

    new_infos = extract_parts(board)
    result = pack(to_parts(new_infos), extract_wires(new_infos), time_limit_s=20.0)
    assert apply_placements(
        board, new_infos, result.placements, result.board_height_nm
    ) == len(new_infos)

    reloaded = load_board(save_board(board, tmp_path / "rotated.kicad_pcb"))
    boxes = [(footprint_ref(f),) + _truth_box(f) for f in reloaded.footprints]
    for (ra, ax0, ay0, ax1, ay1), (rb, bx0, by0, bx1, by1) in itertools.combinations(
        boxes, 2
    ):
        dx = min(ax1, bx1) - max(ax0, bx0)
        dy = min(ay1, by1) - max(ay0, by0)
        assert not (dx > 0.01 and dy > 0.01), (
            f"courtyard overlap {ra}/{rb}: {dx:.2f} x {dy:.2f} mm"
        )


# --- Courtyards that are not made of straight lines --------------------------


class _Graphics:
    """Minimal stand-in for a footprint carrying only courtyard graphics."""

    def __init__(self, items):
        self.graphicItems = items


def test_circle_courtyard_is_bounded_by_its_radius():
    """``fp_circle``'s ``end`` is a point on the circumference, not a corner.
    Read as a bbox pair it gives a half-width, zero-height box."""
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpCircle
    from silkscreen.kicad import _courtyard_extent

    circle = FpCircle(
        center=Position(X=1.0, Y=2.0), end=Position(X=4.0, Y=2.0), layer="F.CrtYd"
    )
    assert _courtyard_extent(_Graphics([circle])) == (-2.0, -1.0, 4.0, 5.0)


def test_polygon_courtyard_is_not_mistaken_for_no_courtyard():
    """``fp_poly`` keeps its vertices in ``coordinates``. Missing them made the
    footprint fall back to its bare pad box -- zero courtyard clearance."""
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpPoly
    from silkscreen.kicad import _courtyard_extent

    poly = FpPoly(
        layer="F.CrtYd",
        coordinates=[
            Position(X=-2.0, Y=-2.0), Position(X=2.0, Y=-2.0),
            Position(X=2.0, Y=2.0), Position(X=-2.0, Y=2.0),
        ],
    )
    assert _courtyard_extent(_Graphics([poly])) == (-2.0, -2.0, 2.0, 2.0)


def test_arc_courtyard_includes_its_bulge():
    """An arc's extreme is ``mid``; its endpoints alone miss it.

    ``mid`` is not the whole story either. This arc sweeps past horizontal on
    both sides, so it reaches 0.125 mm further out than either endpoint: three
    samples name none of the four places an arc can actually be widest.
    """
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpArc
    from silkscreen.kicad import _courtyard_extent

    arc = FpArc(
        start=Position(X=-3.0, Y=0.0), mid=Position(X=0.0, Y=-4.0),
        end=Position(X=3.0, Y=0.0), layer="F.CrtYd",
    )
    assert _courtyard_extent(_Graphics([arc])) == pytest.approx(
        (-3.125, -4.0, 3.125, 0.0)
    )


def test_circle_courtyard_is_exact_at_any_angle():
    """Two opposite corners of a box stop bounding it the moment it turns.

    A circle is its own image under rotation, but the corner pair standing in
    for it is not: rotated 45 degrees, the box those two points span has *zero*
    height, so the solver reserves a line where a round part sits.
    """
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpCircle
    from silkscreen.kicad import _courtyard_extent

    circle = FpCircle(
        center=Position(X=0.0, Y=0.0), end=Position(X=3.0, Y=0.0), layer="F.CrtYd"
    )
    for angle in (0.0, 30.0, 45.0, 90.0, 137.5):
        assert _courtyard_extent(_Graphics([circle]), angle) == pytest.approx(
            (-3.0, -3.0, 3.0, 3.0)
        ), angle


def test_rect_courtyard_keeps_its_other_two_corners_when_rotated():
    """``fp_rect`` stores opposite corners of a *region*, unlike ``fp_line``,
    whose pair is the drawn segment itself. Turning only the stored pair made a
    4 x 2 mm courtyard 1.41 mm tall at 45 degrees instead of 4.24 mm."""
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpRect
    from silkscreen.kicad import _courtyard_extent

    rect = FpRect(
        start=Position(X=-2.0, Y=-1.0), end=Position(X=2.0, Y=1.0), layer="F.CrtYd"
    )
    reach = 3.0 / math.sqrt(2)  # the (2, 1) corner swings out to (|x| + |y|)/sqrt2
    assert _courtyard_extent(_Graphics([rect]), 45.0) == pytest.approx(
        (-reach, -reach, reach, reach)
    )
    assert _courtyard_extent(_Graphics([rect]), 0.0) == pytest.approx(
        (-2.0, -1.0, 2.0, 1.0)
    )


def test_arc_courtyard_reaches_past_all_three_sampled_points():
    """A 270 degree arc passes through every cardinal extreme of its circle.
    Bounded by ``start``/``mid``/``end`` it comes out 0.29 mm short on two
    sides of a 1 mm radius -- room enough to pack a 0402 into the keep-out."""
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpArc
    from silkscreen.kicad import _courtyard_extent

    corner = math.sqrt(0.5)
    arc = FpArc(
        start=Position(X=0.0, Y=1.0), mid=Position(X=-corner, Y=-corner),
        end=Position(X=1.0, Y=0.0), layer="F.CrtYd",
    )
    assert _courtyard_extent(_Graphics([arc])) == pytest.approx((-1.0, -1.0, 1.0, 1.0))


def test_rotated_arc_is_bounded_in_the_board_frame():
    """Which point of a curve is the rightmost one depends on the frame you ask
    in, so the extremes are found after the angle is applied, not turned
    afterwards. Swung 45 degrees, this arc stops short of its circle's right
    edge and its own endpoint becomes the bound."""
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpArc
    from silkscreen.kicad import _courtyard_extent

    corner = math.sqrt(0.5)
    arc = FpArc(
        start=Position(X=0.0, Y=1.0), mid=Position(X=-corner, Y=-corner),
        end=Position(X=1.0, Y=0.0), layer="F.CrtYd",
    )
    assert _courtyard_extent(_Graphics([arc]), 45.0) == pytest.approx(
        (-1.0, -1.0, corner, 1.0)
    )


def test_quarter_arc_is_not_padded_out_to_its_whole_circle():
    """Conservative is not the same as correct: the bound stays tight where it
    can. A corner-rounding arc reserves its own quadrant, not the full circle
    it belongs to, which would inflate every rounded courtyard by its radius."""
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpArc
    from silkscreen.kicad import _courtyard_extent

    corner = math.sqrt(0.5)
    arc = FpArc(
        start=Position(X=1.0, Y=0.0), mid=Position(X=corner, Y=corner),
        end=Position(X=0.0, Y=1.0), layer="F.CrtYd",
    )
    assert _courtyard_extent(_Graphics([arc])) == pytest.approx((0.0, 0.0, 1.0, 1.0))


def test_collinear_arc_degrades_to_its_endpoints():
    """Three points on a line have no circumcentre. The arc is a segment, and
    must not be handed the enormous circle a near-zero determinant implies."""
    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpArc
    from silkscreen.kicad import _courtyard_extent

    arc = FpArc(
        start=Position(X=-1.0, Y=0.0), mid=Position(X=0.0, Y=0.0),
        end=Position(X=1.0, Y=0.0), layer="F.CrtYd",
    )
    assert _courtyard_extent(_Graphics([arc])) == pytest.approx((-1.0, 0.0, 1.0, 0.0))


def test_curved_courtyards_are_never_under_reserved():
    """The property behind the arc cases above, over arbitrary sweeps and angles.

    The truth here is a densely sampled arc rotated by hand, deliberately
    without calling anything in ``kicad.py`` -- a check written in terms of the
    code under test shares its blind spot. Both directions matter: an extent
    that is even slightly small is a gap the solver will pack a neighbour into,
    and one that is much too large silently inflates every board.
    """
    import random

    from kiutils.items.common import Position
    from kiutils.items.fpitems import FpArc
    from silkscreen.kicad import _courtyard_extent

    rng = random.Random(20260830)
    for _ in range(60):
        cx, cy = rng.uniform(-20.0, 20.0), rng.uniform(-20.0, 20.0)
        radius = rng.uniform(0.1, 15.0)
        base = rng.uniform(0.0, math.tau)
        sweep = rng.choice([1, -1]) * rng.uniform(0.05, math.tau * 0.99)
        angle = rng.choice([0.0, 17.5, 30.0, 45.0, 90.0, 180.0, -60.0])

        def on_arc(t, cx=cx, cy=cy, radius=radius, base=base):
            return cx + radius * math.cos(base + t), cy + radius * math.sin(base + t)

        arc = FpArc(
            start=Position(*on_arc(0.0)),
            mid=Position(*on_arc(sweep / 2)),
            end=Position(*on_arc(sweep)),
            layer="F.CrtYd",
        )
        got = _courtyard_extent(_Graphics([arc]), angle)

        # KiCad's angle turns a footprint counter-clockwise on a Y-down screen,
        # so the board-frame image is a mathematical rotation by -angle.
        rad = math.radians(-angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        xs, ys = [], []
        for step in range(721):
            x, y = on_arc(sweep * step / 720)
            xs.append(x * cos_a - y * sin_a)
            ys.append(x * sin_a + y * cos_a)
        truth = (min(xs), min(ys), max(xs), max(ys))

        assert got == pytest.approx(truth, abs=1e-3), (radius, sweep, angle)
        assert got[0] <= truth[0] + 1e-9, ("left", radius, sweep, angle)
        assert got[1] <= truth[1] + 1e-9, ("bottom", radius, sweep, angle)
        assert got[2] >= truth[2] - 1e-9, ("right", radius, sweep, angle)
        assert got[3] >= truth[3] - 1e-9, ("top", radius, sweep, angle)


# --- Reference designators as identity ---------------------------------------


def test_duplicate_refs_are_rejected_rather_than_collapsed(infos):
    """Placements are matched back by ref alone. Two footprints sharing one ref
    both land on whichever the lookup kept; the other silently stays put while
    the solver holds empty space for it -- and the moved count still says every
    part was placed."""
    with pytest.raises(ValueError, match="duplicate reference designators"):
        to_parts(list(infos) + [infos[0]])


def test_footprint_without_a_reference_is_still_moved(tmp_path):
    """It is sized and packed under its library name, so it must be looked up
    under that same name on the way back -- not left at its original position
    with the solver reserving a slot for it."""
    board = load_board(FIXTURE)
    fp = next(f for f in board.footprints if footprint_ref(f) == "C4")
    if isinstance(getattr(fp, "properties", None), dict):
        fp.properties.pop("Reference", None)
    else:
        fp.properties = [
            p for p in fp.properties if getattr(p, "key", None) != "Reference"
        ]
    fp.graphicItems = [
        g for g in fp.graphicItems if getattr(g, "type", None) != "reference"
    ]
    before = (fp.position.X, fp.position.Y)

    new_infos = extract_parts(board)
    assert not any(i.ref == "C4" for i in new_infos)
    result = pack(to_parts(new_infos), time_limit_s=15.0)
    moved = apply_placements(
        board, new_infos, result.placements, result.board_height_nm
    )

    assert moved == len(new_infos)
    assert before != (fp.position.X, fp.position.Y)


# --- Which side of the board a footprint is on -------------------------------
#
# The solver enforces no-overlap *per side*: a part on the back may sit directly
# under one on the front, which is the entire point of a two-sided board. That
# only works if the side survives the trip in from the file.


def _flip_to_back(board, refs):
    """Move ``refs`` to the back, the way KiCad stores a flipped footprint."""
    for fp in board.footprints:
        if footprint_ref(fp) in refs:
            fp.layer = "B.Cu"
            for item in fp.graphicItems:
                layer = getattr(item, "layer", "") or ""
                if layer.startswith("F."):
                    item.layer = "B." + layer[2:]


def test_front_footprints_are_read_as_top_side(infos):
    from silkscreen.packing import Layer

    assert all(info.side is Layer.TOP for info in infos)


def test_back_footprints_are_read_as_bottom_side():
    from silkscreen.packing import Layer

    board = load_board(FIXTURE)
    _flip_to_back(board, {"C1", "C2"})
    sides = {i.ref: i.side for i in extract_parts(board)}

    assert sides["C1"] is Layer.BOTTOM
    assert sides["C2"] is Layer.BOTTOM
    assert sides["U1"] is Layer.TOP


def test_side_reaches_the_placer():
    """A FootprintInfo that knows its side is no use if to_parts drops it."""
    from silkscreen.packing import Layer

    board = load_board(FIXTURE)
    _flip_to_back(board, {"C1", "C2"})
    by_ref = {p.ref: p for p in to_parts(extract_parts(board))}

    assert by_ref["C1"].layer is Layer.BOTTOM
    assert by_ref["U1"].layer is Layer.TOP


def test_back_side_parts_may_share_xy_with_front_side_parts():
    """The regression, in the only terms that matter: board area.

    Calling every footprint top-side makes the placer reserve a separate slot
    for parts that never collided, so the board comes out far larger than the
    design needs -- measured at +83% on this fixture.
    """
    from dataclasses import replace

    from silkscreen.packing import Layer

    board = load_board(FIXTURE)
    flipped = {"C1", "C2", "C3", "C4"}
    _flip_to_back(board, flipped)

    infos = extract_parts(board)
    wires = extract_wires(infos)
    honest = to_parts(infos)
    # What the boundary produced before it read the side at all.
    all_top = [replace(p, layer=Layer.TOP) for p in honest]

    area = lambda r: r.board_width_nm * r.board_height_nm  # noqa: E731
    two_sided = pack(honest, wires, time_limit_s=20.0)
    one_sided = pack(all_top, wires, time_limit_s=20.0)

    assert area(two_sided) < area(one_sided), (
        "two-sided board should pack tighter than the all-top assumption: "
        f"{area(two_sided) / 1e12:.1f} vs {area(one_sided) / 1e12:.1f} mm^2"
    )


def test_placement_onto_the_other_side_is_refused_not_written():
    """Flipping means mirroring every pad and graphic, which this writer does
    not do. Writing the position anyway yields a board whose geometry silently
    contradicts the solve it came from."""
    from silkscreen.packing import Layer, Placement

    board = load_board(FIXTURE)
    infos = extract_parts(board)
    target = infos[0]
    assert target.side is Layer.TOP

    moved_to_back = [
        Placement(ref=target.ref, x_nm=0, y_nm=0, layer=Layer.BOTTOM)
    ]
    with pytest.raises(ValueError, match="not supported"):
        apply_placements(board, infos, moved_to_back, 40_000_000)

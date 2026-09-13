"""Routing tests.

The claim under test is not "the router emitted some segments" -- it is that
the copper in the written file actually joins the pads it says it joins. So the
truth function here, :func:`connected_nets`, re-derives connectivity from the
emitted ``.kicad_pcb`` parsed by ``kiutils``, touching nothing in
:mod:`silkscreen.routing`. A check written in terms of the router would share
whatever blind spot the router has, which is the same reasoning
``test_kicad.py`` uses for courtyard overlap.

The bug class this guards is the quiet one: a run reports "4/4 nets routed",
the file opens, the tracks look plausible, and one of them ends a hair short of
its pad. Nothing raises. The board comes back from the fab dead.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess

import pytest
from kiutils.board import Board
from kiutils.items.brditems import Segment, Via
from silkscreen.board import (
    build_board,
    emit_kicad_pcb,
    route_board,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.packing import Layer
from silkscreen.routing import (
    DEFAULT_EDGE_CLEARANCE_NM,
    DEFAULT_ROUTE_CLEARANCE_NM,
    DEFAULT_ROUTE_GRID_NM,
    DEFAULT_TRACK_WIDTH_NM,
    DEFAULT_VIA_DIAMETER_NM,
    RoutePad,
    route,
)
from silkscreen.units import mm

#: Coordinates in the file are millimetre decimals; compare at 1 nm.
EPS = 1e-6


REGULATOR = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "Cin": {"type": "capacitor", "value": "10uF"},
        "Cout": {"type": "capacitor", "value": "22uF"},
        "Rled": {"type": "resistor", "value": "1k"},
        "D1": {"type": "diode", "value": "LED"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "Cin.1"],
        "GND": ["AMS1117-3.3.GND", "Cin.2", "Cout.2", "D1.2"],
        "VOUT": ["AMS1117-3.3.VOUT", "Cout.1", "Rled.1"],
        "LED_A": ["Rled.2", "D1.1"],
    },
}


@pytest.fixture(scope="module")
def routed():
    spec = parse_circuit_spec(REGULATOR)
    board = build_board(spec, time_limit_s=5.0)
    result = route_board(board)
    return board, result


# --------------------------------------------------------------------------
# The independent truth function
# --------------------------------------------------------------------------


def _on_segment(px, py, ax, ay, bx, by) -> bool:
    """Is (px,py) on the axis-aligned segment a-b, endpoints included?"""
    if abs(ax - bx) < EPS:  # vertical
        return abs(px - ax) < EPS and min(ay, by) - EPS <= py <= max(ay, by) + EPS
    if abs(ay - by) < EPS:  # horizontal
        return abs(py - ay) < EPS and min(ax, bx) - EPS <= px <= max(ax, bx) + EPS
    return False


class _Union:
    def __init__(self):
        self.parent: dict[object, object] = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def board_pads_from_file(brd: Board):
    """Absolute pad rectangles, read back out of the written file.

    Deliberately re-derived from the parsed s-expressions rather than from the
    ``BoardResult`` the router saw, so a placement written to the file
    differently from how the router imagined it shows up as a disconnection.
    """
    pads = []
    for fp in brd.footprints:
        ref = fp.properties.get("Reference", fp.libraryNickname)
        assert fp.position.angle in (None, 0), "fixture must not be rotated"
        for pad in fp.pads:
            layer = "B.Cu" if any("B.Cu" in v for v in pad.layers) else "F.Cu"
            pads.append(
                {
                    "id": ("pad", ref, pad.number),
                    "net": pad.net.name if pad.net else "",
                    "layer": layer,
                    "x": fp.position.X + pad.position.X,
                    "y": fp.position.Y + pad.position.Y,
                    "w": pad.size.X,
                    "h": pad.size.Y,
                }
            )
    return pads


def connected_nets(text: str) -> dict[str, list[set]]:
    """``{net: [component, ...]}`` over the copper in ``text``.

    A net that came out fully routed has exactly one component containing all
    of its pads.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "b.kicad_pcb"
        path.write_text(text, encoding="utf-8")
        brd = Board().from_file(str(path))

    pads = board_pads_from_file(brd)
    segs = [t for t in brd.traceItems if isinstance(t, Segment)]
    vias = [t for t in brd.traceItems if isinstance(t, Via)]

    uf = _Union()
    for index, seg in enumerate(segs):
        sid = ("seg", index)
        uf.find(sid)
        for pad in pads:
            if pad["layer"] != seg.layer:
                continue
            for x, y in ((seg.start.X, seg.start.Y), (seg.end.X, seg.end.Y)):
                inside = (
                    abs(x - pad["x"]) <= pad["w"] / 2 + EPS
                    and abs(y - pad["y"]) <= pad["h"] / 2 + EPS
                )
                if inside:
                    uf.union(sid, pad["id"])
        for other, seg2 in enumerate(segs):
            if other <= index or seg2.layer != seg.layer:
                continue
            touch = any(
                _on_segment(px, py, a.X, a.Y, b.X, b.Y)
                for (px, py), (a, b) in (
                    ((seg2.start.X, seg2.start.Y), (seg.start, seg.end)),
                    ((seg2.end.X, seg2.end.Y), (seg.start, seg.end)),
                    ((seg.start.X, seg.start.Y), (seg2.start, seg2.end)),
                    ((seg.end.X, seg.end.Y), (seg2.start, seg2.end)),
                )
            )
            if touch:
                uf.union(sid, ("seg", other))
    for vindex, via in enumerate(vias):
        vid = ("via", vindex)
        uf.find(vid)
        for index, seg in enumerate(segs):
            if via.layers and seg.layer not in via.layers:
                continue
            if _on_segment(
                via.position.X, via.position.Y, seg.start.X, seg.start.Y,
                seg.end.X, seg.end.Y,
            ):
                uf.union(vid, ("seg", index))
        # A through via carries copper on every layer it spans, not just the
        # layer of whatever track happens to end on it. A path that reaches
        # its target by switching layers exactly at the target's own lattice
        # node -- legal, since that node sits inside the pad's own copper --
        # commits a via there and draws no further stub track, because none is
        # needed: the via's round copper on the pad's layer already overlaps
        # it. Missing this made a real, DRC-clean connection (a via landing in
        # its own net's pad) look like an open net.
        for pad in pads:
            if via.layers and pad["layer"] not in via.layers:
                continue
            inside = (
                abs(via.position.X - pad["x"]) <= pad["w"] / 2 + EPS
                and abs(via.position.Y - pad["y"]) <= pad["h"] / 2 + EPS
            )
            if inside:
                uf.union(vid, pad["id"])

    # A via landing inside a pad is on that pad's copper: the barrel goes
    # through every layer, so it joins the pad whichever side the pad is on.
    # Without this a route that reaches an SMD pad through a via in the pad
    # (electrically sound, and what KiCad's own connectivity reports) reads as
    # an island here.
    for vindex, via in enumerate(vias):
        for pad in pads:
            inside = (
                abs(via.position.X - pad["x"]) <= pad["w"] / 2 + EPS
                and abs(via.position.Y - pad["y"]) <= pad["h"] / 2 + EPS
            )
            if inside:
                uf.union(("via", vindex), pad["id"])

    by_net: dict[str, list[set]] = {}
    for pad in pads:
        if not pad["net"]:
            continue
        by_net.setdefault(pad["net"], [])
    for net in by_net:
        groups: dict[object, set] = {}
        for pad in pads:
            if pad["net"] != net:
                continue
            groups.setdefault(uf.find(pad["id"]), set()).add(pad["id"])
        by_net[net] = list(groups.values())
    return by_net


# --------------------------------------------------------------------------


def test_the_regulator_board_routes_every_net(routed):
    board, result = routed
    assert result.unrouted == {}, result.unrouted
    assert result.tracks, "a routed board must have copper"
    assert board.is_routed


def test_the_emitted_copper_actually_joins_each_net(routed):
    """The claim the router makes, checked against the file it produced."""
    board, result = routed
    components = connected_nets(emit_kicad_pcb(board))
    assert set(components) == set(result.routed)
    for net, groups in components.items():
        assert len(groups) == 1, (
            f"net {net} is in {len(groups)} disconnected pieces: {groups}"
        )


def test_a_placed_board_has_no_copper_at_all():
    """Placement alone must not look routed. This was every run before now."""
    spec = parse_circuit_spec(REGULATOR)
    board = build_board(spec, time_limit_s=5.0)
    text = emit_kicad_pcb(board)
    assert "(segment" not in text
    assert "(via " not in text
    assert not board.is_routed
    # ...and the pads still carry their nets, which is what KiCad draws as the
    # ratsnest that made an unrouted board look finished.
    assert '(net 1 "' in text


def test_routing_is_deterministic():
    spec = parse_circuit_spec(REGULATOR)
    outputs = []
    for _ in range(2):
        board = build_board(spec, time_limit_s=5.0)
        route_board(board)
        outputs.append(emit_kicad_pcb(board))
    assert outputs[0] == outputs[1]


def test_every_track_is_axis_aligned(routed):
    board, _ = routed
    for t in board.tracks:
        assert t.start_x_nm == t.end_x_nm or t.start_y_nm == t.end_y_nm, t


def test_no_copper_leaves_the_board_outline(routed):
    board, _ = routed
    margin = mm(2.0)
    lo_x, lo_y = -margin, -margin
    hi_x, hi_y = board.width_nm + margin, board.height_nm + margin
    for t in board.tracks:
        for x, y in (
            (t.start_x_nm, t.start_y_nm),
            (t.end_x_nm, t.end_y_nm),
        ):
            assert lo_x <= x <= hi_x and lo_y <= y <= hi_y, t
    for v in board.vias:
        assert lo_x <= v.x_nm <= hi_x and lo_y <= v.y_nm <= hi_y, v


#: A board with two layers in play, so vias exist to be checked. The regulator
#: fixture above routes entirely on the front, so every via rule it claims to
#: enforce went untested until this was added.
VIA_BOARD = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "Cin": {"type": "capacitor", "value": "10uF"},
        "Cout": {"type": "capacitor", "value": "22uF"},
        "Rled": {"type": "resistor", "value": "1k"},
        "D1": {"type": "diode", "value": "LED"},
        "L1": {"type": "inductor", "value": "10uH"},
        "Y1": {"type": "crystal", "value": "8MHz"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "Cin.1", "L1.1"],
        "GND": ["AMS1117-3.3.GND", "Cin.2", "Cout.2", "D1.2", "Y1.2"],
        "VOUT": ["AMS1117-3.3.VOUT", "Cout.1", "Rled.1"],
        "LED_A": ["Rled.2", "D1.1"],
        "XTAL": ["L1.2", "Y1.1"],
    },
}


@pytest.fixture(scope="module")
def via_routed():
    board = build_board(parse_circuit_spec(VIA_BOARD), time_limit_s=10.0)
    result = route_board(board)
    assert result.vias, "fixture is meant to exercise vias and produced none"
    return board, result


def _copper_discs(board):
    """Every piece of copper as (net, layer, x, y, radius), in millimetres.

    A disc per sample point, so one distance rule covers track-to-track,
    track-to-via and via-to-via without three separate geometries. Sampled
    rather than solved analytically for the same reason the original did it:
    the point is to catch copper that is too close.
    """
    step = mm(0.05)
    out = []
    for tr in board.tracks:
        r = tr.width_nm / 2
        dx = (tr.end_x_nm > tr.start_x_nm) - (tr.end_x_nm < tr.start_x_nm)
        dy = (tr.end_y_nm > tr.start_y_nm) - (tr.end_y_nm < tr.start_y_nm)
        length = abs(tr.end_x_nm - tr.start_x_nm) + abs(tr.end_y_nm - tr.start_y_nm)
        for k in range(0, length + 1, step):
            out.append(
                (tr.net, tr.layer, tr.start_x_nm + dx * k, tr.start_y_nm + dy * k, r)
            )
    for via in board.vias:
        # A barrel pierces both layers, so it is copper on each of them.
        for layer in (Layer.TOP, Layer.BOTTOM):
            out.append((via.net, layer, via.x_nm, via.y_nm, via.diameter_nm / 2))
    return out


def test_copper_of_different_nets_keeps_its_clearance(via_routed):
    """The rule the router claims to enforce, on a board that has vias.

    The original check sampled tracks only, on a fixture with no vias at all,
    so nothing tested the via rules -- and KiCad's own DRC found a via shorting
    a foreign track plus two sub-clearance gaps on a board this suite passed.
    The clearance is a property of the search now rather than of whatever the
    clearance halo happened to win, and this is what says so.
    """
    required = mm(0.2)  # netclass default, the same number KiCad checks
    discs = _copper_discs(via_routed[0])
    for a in range(len(discs)):
        net_a, layer_a, ax, ay, ra = discs[a]
        for b in range(a + 1, len(discs)):
            net_b, layer_b, bx, by, rb = discs[b]
            if net_a == net_b or layer_a is not layer_b:
                continue
            gap = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 - ra - rb
            assert gap >= required - 1, (
                f"{net_a} and {net_b} are {gap / 1e6:.3f} mm apart on "
                f"{layer_a}, under the {required / 1e6:.2f} mm rule"
            )


def test_copper_keeps_its_clearance_from_foreign_pads(via_routed):
    """Copper must clear a pad it does not belong to, not only other copper."""
    from silkscreen.board import board_pads

    required = mm(0.2)
    board = via_routed[0]
    pads = [p for p in board_pads(board) if p.net]
    for net, layer, x, y, r in _copper_discs(board):
        for pad in pads:
            if pad.net == net or pad.layer is not layer:
                continue
            dx = max(abs(x - pad.x_nm) - pad.w_nm / 2, 0)
            dy = max(abs(y - pad.y_nm) - pad.h_nm / 2, 0)
            gap = (dx * dx + dy * dy) ** 0.5 - r
            assert gap >= required - 1, (
                f"{net} copper is {gap / 1e6:.3f} mm from {pad.ref} pad "
                f"{pad.number} [{pad.net}], under the {required / 1e6:.2f} mm rule"
            )


def test_the_via_defaults_clear_kicads_own_minimums():
    """KiCad 8 defaults to a 0.5 mm via and a 0.3 mm hole.

    The emitted board carries no design-settings block, so a reader gets those
    defaults -- and every via written at 0.4/0.2 came back a DRC error.
    """
    from silkscreen.routing import DEFAULT_VIA_DIAMETER_NM, DEFAULT_VIA_DRILL_NM

    # Read as "KiCad's minimum is at most ours". Ruff treats the SCREAMING_CASE
    # module constants as the constant side, so they go on the right.
    assert mm(0.5) <= DEFAULT_VIA_DIAMETER_NM
    assert mm(0.3) <= DEFAULT_VIA_DRILL_NM


def test_an_unroutable_net_is_reported_and_not_half_laid():
    """A net with no path must be named, and must contribute no copper.

    Half a net's tracks is the worst outcome available: the board looks routed
    everywhere a person happens to look.
    """
    # Two pads on opposite sides of a wall of a third net's pads, on a
    # single-layer board so there is no way around or under it.
    pads = [RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
            RoutePad(net="A", x_nm=mm(19.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0))]
    for k in range(0, 21):
        pads.append(
            RoutePad(
                net="WALL",
                x_nm=mm(10.0),
                y_nm=mm(0.5) * k,
                w_nm=mm(1.0),
                h_nm=mm(1.0),
            )
        )
    result = route(
        pads,
        min_x_nm=0,
        min_y_nm=0,
        max_x_nm=mm(20.0),
        max_y_nm=mm(10.0),
        two_layer=False,
    )
    assert "A" in result.unrouted
    assert "A" not in result.routed
    assert not [t for t in result.tracks if t.net == "A"]
    assert any("unrouted" in w for w in result.warnings)


def test_the_back_layer_lets_a_blocked_net_through():
    """The same wall, with two layers available, is routable via a via pair."""
    pads = [RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
            RoutePad(net="A", x_nm=mm(19.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0))]
    for k in range(0, 21):
        pads.append(
            RoutePad(net="WALL", x_nm=mm(10.0), y_nm=mm(0.5) * k,
                     w_nm=mm(1.0), h_nm=mm(1.0))
        )
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(20.0), max_y_nm=mm(10.0)
    )
    assert result.unrouted.get("A") is None
    assert len([v for v in result.vias if v.net == "A"]) == 2


def test_a_rotated_part_routes_now_that_the_anchor_is_right():
    """This used to assert the opposite, and that was correct at the time.

    ``route_board`` refused any board with a rotated part, because
    ``emit_kicad_pcb`` misplaced a rotated footprint's anchor and routing to
    those coordinates would have produced copper landing on bare laminate.
    With the anchor fixed -- one ``part_anchor`` read by the emitter and by
    ``board_pads`` alike -- the refusal has nothing left to protect, so a
    rotated board routes like any other.

    The geometry itself is covered in ``test_rotated_anchor.py``; this only
    pins that the refusal is gone and did not leave a half-state behind.
    """
    spec = parse_circuit_spec(REGULATOR)
    board = build_board(spec, time_limit_s=5.0)
    board.parts[0].rotated = True
    result = route_board(board)

    assert not any("rotated" in w for w in result.warnings), result.warnings
    assert not any("rotated" in w for w in board.warnings), board.warnings
    assert result.tracks, "a rotated board produced no copper at all"
    assert board.route_completion > 0.0
    # Whatever the router could not finish is still named, as for any board.
    assert set(board.unrouted_nets) == set(result.unrouted)


def test_a_single_terminal_net_is_reported_not_silently_skipped():
    result = route(
        [RoutePad(net="LONE", x_nm=mm(2.0), y_nm=mm(2.0), w_nm=mm(1.0), h_nm=mm(1.0))],
        min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0),
    )
    assert "LONE" in result.unrouted
    assert result.completion == 0.0


def test_pads_with_no_net_are_obstacles_only():
    """An unnetted pad must block copper without becoming something to route."""
    pads = [
        RoutePad(net="", x_nm=mm(5.0), y_nm=mm(5.0), w_nm=mm(2.0), h_nm=mm(2.0)),
        RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="A", x_nm=mm(9.0), y_nm=mm(5.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0)
    )
    assert "" not in result.routed and "" not in result.unrouted
    assert "A" in result.routed


def test_a_grid_too_coarse_for_the_pads_says_so_instead_of_shorting():
    """Two pads collapsing onto one lattice node is reported, not routed."""
    pads = [
        RoutePad(net="A", x_nm=mm(5.0), y_nm=mm(5.0), w_nm=mm(0.2), h_nm=mm(0.2)),
        RoutePad(net="A", x_nm=mm(5.05), y_nm=mm(5.0), w_nm=mm(0.2), h_nm=mm(0.2)),
    ]
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0),
        grid_nm=mm(1.0),
    )
    assert "too coarse" in result.unrouted["A"]


def test_a_refused_route_names_the_nets_it_gave_up_on():
    """A refusal is still a result about those nets.

    Naming none of them left routed and unrouted both empty, which
    RouteResult.completion reads as 1.0 -- a board with no copper at all
    reporting as fully routed, the exact class this module exists to prevent.
    """
    pads = [
        RoutePad(net="A", x_nm=mm(10.0), y_nm=mm(10.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="A", x_nm=mm(300.0), y_nm=mm(300.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="B", x_nm=mm(20.0), y_nm=mm(20.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="B", x_nm=mm(280.0), y_nm=mm(280.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        # One terminal only: nothing to route, so nothing to report.
        RoutePad(net="LONE", x_nm=mm(50.0), y_nm=mm(50.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(400.0), max_y_nm=mm(400.0)
    )

    assert not result.tracks
    assert sorted(result.unrouted) == ["A", "B"]
    assert all("node budget" in reason for reason in result.unrouted.values())
    assert result.completion == 0.0


def test_an_empty_board_area_names_its_nets_too():
    pads = [
        RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(1.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="A", x_nm=mm(2.0), y_nm=mm(2.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    result = route(pads, min_x_nm=0, min_y_nm=0, max_x_nm=0, max_y_nm=0)
    assert list(result.unrouted) == ["A"]
    assert result.completion == 0.0


def _boxed_in(cx_mm: float, cy_mm: float) -> list[RoutePad]:
    """A net with one pad walled in by a ring of foreign pads.

    Provably unroutable, so A* only gives up after exhausting everything it can
    reach -- which is the expensive case the budget exists to bound.
    """
    pads = [
        RoutePad(net="BLOCKED", x_nm=mm(cx_mm), y_nm=mm(cy_mm),
                 w_nm=mm(0.4), h_nm=mm(0.4)),
        RoutePad(net="BLOCKED", x_nm=mm(cx_mm + 40), y_nm=mm(cy_mm + 30),
                 w_nm=mm(0.4), h_nm=mm(0.4)),
    ]
    for a in range(-6, 7):
        for b in range(-6, 7):
            if max(abs(a), abs(b)) == 6:
                pads.append(
                    RoutePad(net="RING", x_nm=mm(cx_mm + a * 0.25),
                             y_nm=mm(cy_mm + b * 0.25),
                             w_nm=mm(0.2), h_nm=mm(0.2))
                )
    return pads


def test_a_hopeless_net_cannot_search_forever():
    """The node guard bounds the lattice; this bounds the work done on it.

    An unroutable net only returns once A* has exhausted everything reachable,
    and before the budget existed a large board paid that in full for every
    failing net -- 76 seconds measured, with nothing to stop it.
    """
    result = route(
        _boxed_in(20.0, 20.0),
        min_x_nm=0, min_y_nm=0, max_x_nm=mm(160.0), max_y_nm=mm(120.0),
        max_expansions=5_000,
    )
    assert "BLOCKED" in result.unrouted
    assert "budget" in result.unrouted["BLOCKED"]


def test_a_hopeless_net_does_not_starve_the_ones_behind_it():
    """Reported unrouted having never been tried is honest but avoidable."""
    pads = _boxed_in(20.0, 20.0) + [
        RoutePad(net="EASY", x_nm=mm(100.0), y_nm=mm(60.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="EASY", x_nm=mm(104.0), y_nm=mm(60.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    result = route(
        pads,
        min_x_nm=0, min_y_nm=0, max_x_nm=mm(160.0), max_y_nm=mm(120.0),
        max_expansions=12_000, max_expansions_per_net=4_000,
    )
    assert "BLOCKED" in result.unrouted
    assert "EASY" in result.routed, "the per-net share did not protect it"


def test_the_budget_keeps_routing_deterministic():
    """A count, not a clock: two runs of one design must give one board.

    A wall-clock cutoff would have been simpler and would have made the copper
    depend on how busy the machine was.
    """
    pads = _boxed_in(20.0, 20.0)
    runs = [
        route(pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(160.0), max_y_nm=mm(120.0),
              max_expansions=5_000)
        for _ in range(2)
    ]
    assert runs[0].tracks == runs[1].tracks
    assert runs[0].unrouted == runs[1].unrouted


# --------------------------------------------------------------------------
# Plated through-holes
# --------------------------------------------------------------------------
#
# A plated hole is copper on every layer, whichever side its part sits on. The
# router used to reserve only the placed side, so the back layer stayed free
# and a foreign net was laid straight through the barrel: KiCad reported it as
# "Items shorting two nets", and the suite passed. Expected geometry below is
# computed from the raw literals, never from RoutePad or board_pads.

#: Header pin on net A, centred at (5, 5) mm, 1.7 mm square with a 1 mm hole.
HOLE_X, HOLE_Y, HOLE_PAD = 5.0, 5.0, 1.7
#: Net B's two pads sit on the back layer directly above and below the hole,
#: so the shortest back-layer track runs through it.
B_ABOVE, B_BELOW = 9.0, 1.0


def _hole_board(*, a_extra: list[RoutePad] | None = None) -> list[RoutePad]:
    pads = [
        RoutePad(
            net="A", x_nm=mm(HOLE_X), y_nm=mm(HOLE_Y), w_nm=mm(HOLE_PAD),
            h_nm=mm(HOLE_PAD), through_hole=True, ref="J1", number="1",
        ),
        RoutePad(net="B", x_nm=mm(HOLE_X), y_nm=mm(B_ABOVE), w_nm=mm(1.0),
                 h_nm=mm(1.0), layer=Layer.BOTTOM),
        RoutePad(net="B", x_nm=mm(HOLE_X), y_nm=mm(B_BELOW), w_nm=mm(1.0),
                 h_nm=mm(1.0), layer=Layer.BOTTOM),
    ]
    return pads + list(a_extra or [])


def _samples(result, net: str):
    """Every point of ``net``'s copper as (layer, x_nm, y_nm, radius_nm)."""
    step = mm(0.05)
    for tr in result.tracks:
        if tr.net != net:
            continue
        dx = (tr.end_x_nm > tr.start_x_nm) - (tr.end_x_nm < tr.start_x_nm)
        dy = (tr.end_y_nm > tr.start_y_nm) - (tr.end_y_nm < tr.start_y_nm)
        length = abs(tr.end_x_nm - tr.start_x_nm) + abs(tr.end_y_nm - tr.start_y_nm)
        for k in range(0, length + 1, step):
            yield (
                tr.layer, tr.start_x_nm + dx * k, tr.start_y_nm + dy * k,
                tr.width_nm // 2,
            )
    for via in result.vias:
        if via.net == net:
            for layer in (Layer.TOP, Layer.BOTTOM):
                yield layer, via.x_nm, via.y_nm, via.diameter_nm // 2


def _gap_to_square(x_nm, y_nm, radius_nm, cx_nm, cy_nm, side_nm) -> float:
    """Distance from a copper disc to an axis-aligned square pad, in nm.

    Raw-literal geometry, independent of anything routing.py or board.py
    computes about pads.
    """
    half = side_nm / 2
    dx = max(abs(x_nm - cx_nm) - half, 0)
    dy = max(abs(y_nm - cy_nm) - half, 0)
    return (dx * dx + dy * dy) ** 0.5 - radius_nm


def _gap_to_hole_pad(x_nm: int, y_nm: int, radius_nm: int) -> float:
    return _gap_to_square(
        x_nm, y_nm, radius_nm, mm(HOLE_X), mm(HOLE_Y), mm(HOLE_PAD)
    )


def test_a_plated_hole_is_an_obstacle_on_the_back_layer_too():
    """Net B must go around the hole or be reported -- never through it."""
    result = route(
        _hole_board(), min_x_nm=0, min_y_nm=0, max_x_nm=mm(10), max_y_nm=mm(10)
    )
    required = mm(0.2)
    if "B" in result.unrouted:
        assert not [t for t in result.tracks if t.net == "B"]
        return
    assert "B" in result.routed
    for layer, x, y, r in _samples(result, "B"):
        gap = _gap_to_hole_pad(x, y, r)
        assert gap >= required - 1, (
            f"B copper on {layer.value} at ({x / 1e6:.2f}, {y / 1e6:.2f}) is "
            f"{gap / 1e6:.3f} mm from J1's plated hole"
        )


def test_the_same_board_shorts_when_the_hole_is_declared_smd():
    """The control: without the flag the router still lays B through the pad.

    Not a behaviour to keep -- it is what proves the test above is testing the
    flag rather than passing by luck of the grid.
    """
    pads = _hole_board()
    pads[0] = RoutePad(
        net="A", x_nm=mm(HOLE_X), y_nm=mm(HOLE_Y), w_nm=mm(HOLE_PAD),
        h_nm=mm(HOLE_PAD), ref="J1", number="1",
    )
    result = route(pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10), max_y_nm=mm(10))
    assert "B" in result.routed
    assert any(
        _gap_to_hole_pad(x, y, r) < 0 for _, x, y, r in _samples(result, "B")
    )


def test_a_plated_hole_is_reachable_from_either_layer_without_a_via():
    """The hole is the layer change: a back-side pad on the same net joins it
    with back-side copper and no via, and a front-side pad with front copper."""
    back = RoutePad(net="A", x_nm=mm(HOLE_X), y_nm=mm(B_BELOW), w_nm=mm(1.0),
                    h_nm=mm(1.0), layer=Layer.BOTTOM)
    front = RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(HOLE_Y), w_nm=mm(1.0),
                     h_nm=mm(1.0))
    pads = [p for p in _hole_board(a_extra=[back, front]) if p.net != "B"]
    result = route(pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10), max_y_nm=mm(10))
    assert result.unrouted == {}
    assert "A" in result.routed
    assert not result.vias
    assert {t.layer for t in result.tracks} == {Layer.TOP, Layer.BOTTOM}


def test_no_via_lands_inside_a_plated_hole():
    """A via through a hole is a drill through a drill, whatever net owns it.

    Net A has to change layers somewhere between a front pad and a back pad;
    the cheapest place would be its own header pin, and that is forbidden.
    """
    pads = [
        RoutePad(net="A", x_nm=mm(HOLE_X), y_nm=mm(HOLE_Y), w_nm=mm(HOLE_PAD),
                 h_nm=mm(HOLE_PAD), through_hole=True, ref="J1", number="1"),
        RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(HOLE_Y), w_nm=mm(1.0),
                 h_nm=mm(1.0)),
        RoutePad(net="A", x_nm=mm(9.0), y_nm=mm(HOLE_Y), w_nm=mm(1.0),
                 h_nm=mm(1.0), layer=Layer.BOTTOM),
    ]
    result = route(pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10), max_y_nm=mm(10))
    assert "A" in result.routed
    for via in result.vias:
        # The barrel must not overlap the pad's copper at all.
        assert _gap_to_hole_pad(via.x_nm, via.y_nm, via.diameter_nm // 2) >= 0


def test_a_single_plated_hole_is_one_terminal_not_two():
    """Both layers of one hole are the same pad, so a net with only that pad
    is still a one-terminal net -- not a two-node net "routed" with no copper."""
    result = route(
        _hole_board()[:1], min_x_nm=0, min_y_nm=0, max_x_nm=mm(10), max_y_nm=mm(10)
    )
    assert "A" in result.unrouted
    assert "A" not in result.routed


def test_board_pads_marks_drilled_pads_as_through_hole():
    from silkscreen.board import BoardResult, PlacedPart, board_pads
    from silkscreen.footprints import chip_passive, dual_row_header

    header = dual_row_header(4, row_spacing_mm=2.54, nets={"1": "A"})
    chip = chip_passive("0603", net1="A")
    board = BoardResult(
        parts=[PlacedPart("J1", header), PlacedPart("R1", chip, x_nm=mm(10))],
        nets=["A"], width_nm=mm(20), height_nm=mm(20), solver_status="manual",
    )
    flags = {(p.ref, p.number): p.through_hole for p in board_pads(board)}
    assert all(flags[("J1", n)] for n in ("1", "2", "3", "4"))
    assert not flags[("R1", "1")] and not flags[("R1", "2")]


# ------------------------------------------------------- real KiCad DRC gate

#: Gated on the binary exactly the way test_spice.py gates on ngspice: without
#: it the test skips, and a green run does not by itself mean DRC was run.
KICAD_CLI = shutil.which("kicad-cli") or next(
    (
        p for p in (
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            os.path.expanduser(
                "~/AppData/Local/Programs/KiCad/8.0/bin/kicad-cli.exe"
            ),
        )
        if os.path.exists(p)
    ),
    None,
)
needs_kicad_cli = pytest.mark.skipif(
    KICAD_CLI is None, reason="kicad-cli is not installed on this machine"
)

#: Header pin 1 on the board below: anchor (10, 10) minus the 1.27 mm half
#: pitch in both axes, from dual_row_header's own literals.
HDR_PIN1_X, HDR_PIN1_Y, HDR_PAD = 10.0 - 1.27, 10.0 - 1.27, 1.7


def _header_board():
    """A 2x2 header whose pin 1 (net A) sits at (8.73, 8.73) mm, with net B's
    two back-side 0603 pads in that same column above and below it."""
    from silkscreen.board import BoardResult, PlacedPart
    from silkscreen.footprints import chip_passive, dual_row_header

    j1 = dual_row_header(4, row_spacing_mm=2.54, nets={"1": "A"})
    r1 = chip_passive("0603", net1="A", net2="X")
    c1 = chip_passive("0603", net1="B")
    c2 = chip_passive("0603", net2="B")
    pin1_x, pin1_y = mm(HDR_PIN1_X), mm(HDR_PIN1_Y)
    parts = [
        PlacedPart("J1", j1, x_nm=mm(10) - j1.courtyard_w_nm,
                   y_nm=mm(10) - j1.courtyard_h_nm),
        PlacedPart("R1", r1, x_nm=mm(2) - r1.courtyard_w_nm,
                   y_nm=pin1_y - r1.courtyard_h_nm),
        PlacedPart("C1", c1, x_nm=pin1_x - c1.courtyard_w_nm - c1.pads[0].x_nm,
                   y_nm=mm(17) - c1.courtyard_h_nm, layer=Layer.BOTTOM),
        PlacedPart("C2", c2, x_nm=pin1_x - c2.courtyard_w_nm - c2.pads[1].x_nm,
                   y_nm=mm(3) - c2.courtyard_h_nm, layer=Layer.BOTTOM),
    ]
    return BoardResult(parts=parts, nets=["A", "B", "X"], width_nm=mm(20),
                       height_nm=mm(20), solver_status="manual")


def test_the_header_board_routes_b_clear_of_the_hole():
    """Offline half of the DRC gate: same board, raw-literal geometry."""
    board = _header_board()
    result = route_board(board)
    assert "B" in result.routed
    for _, x, y, r in _samples(result, "B"):
        gap = _gap_to_square(
            x, y, r, mm(HDR_PIN1_X), mm(HDR_PIN1_Y), mm(HDR_PAD)
        )
        assert gap >= mm(0.2) - 1


@needs_kicad_cli
def test_kicad_drc_finds_no_short_on_the_header_board(tmp_path):
    """The authority the offline checks are calibrated against.

    Before the fix this board reported ``shorting_items`` (nets A and B) and
    two rear solder-mask bridges; a clean error-severity report is the claim.
    """
    from silkscreen.board import write_board

    board = _header_board()
    route_board(board)
    src = write_board(board, tmp_path / "header.kicad_pcb")
    report = tmp_path / "drc.json"
    run = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--severity-error", "--format", "json",
         "-o", str(report), str(src)],
        capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    assert [v["type"] for v in data["violations"]] == [], [
        v["description"] for v in data["violations"]
    ]
    assert data.get("unconnected_items", []) == []


# --------------------------------------------------------------------------
# Rip-up and retry
# --------------------------------------------------------------------------


def _wall(fixed_mm: float, lo_mm: float, hi_mm: float, axis: str,
          skip: tuple[float, ...] = ()) -> list[RoutePad]:
    """A line of unnetted 0.5 mm obstacle pads every 0.5 mm, with gaps."""
    pads = []
    v = lo_mm
    while v <= hi_mm + 1e-9:
        if not any(abs(v - s) < 0.01 for s in skip):
            x, y = (fixed_mm, v) if axis == "y" else (v, fixed_mm)
            pads.append(
                RoutePad(net="", x_nm=mm(x), y_nm=mm(y),
                         w_nm=mm(0.5), h_nm=mm(0.5))
            )
        v += 0.5
    return pads


def _order_trap(escape: bool) -> list[RoutePad]:
    """A board whose shortest-first order is a trap.

    A vertical wall with one door is the only crossing between the halves,
    and a horizontal wall splits the left half so the mouth in front of the
    door is shared: it is net A's cheapest crossing and net B's only one.
    A is short, routes first, and plugs the mouth. With ``escape`` the
    horizontal wall stops short of the board edge, so A has a longer
    alternative and rip-up can save both nets; without it the channel has
    capacity one and someone must honestly lose.

    The horizontal wall reaches 8.5 mm rather than 8.0 mm because the trap has
    to be a trap under *exact* pad clearance. It was drawn while the keep-out
    was rasterised outward to whole lattice cells, which quietly widened every
    obstacle by up to a grid step, and the 0.5 mm corridor between this wall's
    end and the vertical one was sealed by that rounding rather than by the
    geometry. With the keep-out exact the corridor is open, A takes it, and
    both nets route with nothing contested -- the fixture stops testing rip-up
    at all. One more obstacle pad closes the corridor for real, and the four
    premises the tests below assert hold again.
    """
    pads = _wall(10.0, 0.0, 20.0, "y", skip=(9.0, 9.5, 10.0, 10.5, 11.0))
    pads += _wall(10.0, 1.5 if escape else 0.0, 8.5, "x")
    pads += [
        RoutePad(net="A", x_nm=mm(6.0), y_nm=mm(9.0), w_nm=mm(0.5), h_nm=mm(0.5)),
        RoutePad(net="A", x_nm=mm(6.0), y_nm=mm(11.0), w_nm=mm(0.5), h_nm=mm(0.5)),
        RoutePad(net="B", x_nm=mm(2.0), y_nm=mm(4.0), w_nm=mm(1.0), h_nm=mm(1.0)),
        RoutePad(net="B", x_nm=mm(17.0), y_nm=mm(10.0), w_nm=mm(1.0), h_nm=mm(1.0)),
    ]
    return pads


_TRAP_AREA = dict(min_x_nm=0, min_y_nm=0, max_x_nm=mm(20.0), max_y_nm=mm(20.0))


def test_rip_up_rescues_a_net_an_earlier_net_blocked():
    """The order trap, escaped: lift A, route B, re-route A the long way.

    The premise is asserted too -- with rip-up disabled this board must fail,
    or the rescue below proves nothing.
    """
    pads = _order_trap(escape=True)
    old = route(pads, **_TRAP_AREA, two_layer=False, max_ripups=0)
    assert old.routed == ["A"], "premise: A must route first and plug the mouth"
    assert "B" in old.unrouted, "premise: the trap must actually trap B"

    new = route(pads, **_TRAP_AREA, two_layer=False)
    assert sorted(new.routed) == ["A", "B"]
    assert new.unrouted == {}
    assert [t for t in new.tracks if t.net == "A"]
    assert [t for t in new.tracks if t.net == "B"]


def test_rip_up_copper_keeps_the_clearance_rule():
    """Re-routed copper obeys the same clearance the first pass did."""
    result = route(_order_trap(escape=True), **_TRAP_AREA, two_layer=False)
    required = mm(0.2)
    discs = _copper_discs(result)
    for a in range(len(discs)):
        net_a, layer_a, ax, ay, ra = discs[a]
        for b in range(a + 1, len(discs)):
            net_b, layer_b, bx, by, rb = discs[b]
            if net_a == net_b or layer_a is not layer_b:
                continue
            gap = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 - ra - rb
            assert gap >= required - 1, (
                f"{net_a} and {net_b} are {gap / 1e6:.3f} mm apart after "
                f"rip-up, under the {required / 1e6:.2f} mm rule"
            )


def test_a_channel_with_no_alternative_stays_honest_under_rip_up():
    """Capacity one, two claimants: someone loses, and the loss is named.

    The bound matters as much as the honesty -- without it the two nets
    would trade the one channel until the budget died.
    """
    result = route(_order_trap(escape=False), **_TRAP_AREA, two_layer=False)
    assert len(result.routed) == 1
    loser = ({"A", "B"} - set(result.routed)).pop()
    assert "ripped up to free a channel" in result.unrouted[loser]
    assert not [t for t in result.tracks if t.net == loser], (
        "the loser must contribute no copper, not half a net's worth"
    )
    assert result.completion == 0.5


def test_rip_up_keeps_routing_deterministic():
    """The promise the module makes, kept through the rip-up pass too."""
    runs = [
        route(_order_trap(escape=True), **_TRAP_AREA, two_layer=False)
        for _ in range(2)
    ]
    assert runs[0].tracks == runs[1].tracks
    assert runs[0].vias == runs[1].vias
    assert runs[0].routed == runs[1].routed
    assert runs[0].unrouted == runs[1].unrouted


def test_a_starved_rip_up_blames_the_budget_not_the_board():
    """A probe that dies of budget must not claim the board is unroutable.

    Reporting "no path even with copper lifted" on a budget death would send
    someone rearranging a board that routes fine given more search -- the
    same lie the first pass already refuses to tell.
    """
    result = route(
        _order_trap(escape=False), **_TRAP_AREA, two_layer=False,
        max_expansions=2_500,
    )
    reason = result.unrouted["B"]
    assert "budget" in reason
    assert "lifted" not in reason


# ------------------------------------------------- via versus foreign pad


def _via_pad_gaps(result, pads):
    """Every (via, foreign pad) pair's copper gap and hole-to-copper gap, in nm.

    Independent math: rectangle-to-point distance computed here from the pads
    handed in and the vias handed back, calling nothing in
    :mod:`silkscreen.routing`. A check written in terms of the router's own
    keep-out maps would share whatever blind spot they have -- which is
    exactly the blind spot this function was written to find.
    """
    gaps = []
    for via in result.vias:
        for pad in pads:
            if pad.net and pad.net == via.net:
                continue  # a via on its own net's pad is a connection
            dx = max(0.0, abs(via.x_nm - pad.x_nm) - pad.w_nm / 2)
            dy = max(0.0, abs(via.y_nm - pad.y_nm) - pad.h_nm / 2)
            edge = math.hypot(dx, dy)
            gaps.append(
                (
                    via,
                    pad,
                    edge - via.diameter_nm / 2,  # copper to copper
                    edge - via.drill_nm / 2,  # drill hole to copper
                )
            )
    return gaps


def _fine_pitch_field():
    """One row of 0.5 mm-pitch SMD pads with a target below, forcing vias.

    The shape of an LQFP's pad field: pads too close together for the 0.25 mm
    lattice to thread, so the router reaches for the back layer -- and the
    only place to drop the via is among the pads.
    """
    pads = []
    pitch, y = mm(0.5), mm(6.0)
    for k in range(12):
        x = mm(2.0) + k * pitch
        net = f"N{k}" if k % 3 == 0 else ""
        pads.append(
            RoutePad(net=net, x_nm=x, y_nm=y, w_nm=mm(0.3), h_nm=mm(1.2),
                     ref="U1", number=str(k + 1))
        )
    # Each netted pad has a far partner, so every net must escape the field...
    for k in range(0, 12, 3):
        pads.append(
            RoutePad(net=f"N{k}", x_nm=mm(2.0) + k * pitch, y_nm=mm(1.0),
                     w_nm=mm(0.9), h_nm=mm(0.9), ref=f"R{k}", number="1")
        )
    # ...and an unnetted bar across the whole board between the two, so the
    # only way down is the back layer. Every net therefore buys two vias, in
    # the open band above the bar -- far from the pad field, which is the
    # point: the test needs vias the router is *allowed* to place, or "no
    # via sits on a pad" would be true of a board with no vias at all.
    pads.append(
        RoutePad(net="", x_nm=mm(6.0), y_nm=mm(3.5), w_nm=mm(11.0),
                 h_nm=mm(0.4), ref="BAR", number="1")
    )
    return pads


_FIELD_AREA = dict(min_x_nm=0, min_y_nm=0, max_x_nm=mm(12.0), max_y_nm=mm(9.0))


def test_a_via_never_lands_inside_a_foreign_pads_clearance():
    """A via barrel is 0.6 mm wide and its drill 0.3 mm; the pad keep-out the
    search enforces was sized for a 0.2 mm track.

    So a via centre one lattice step outside that halo still sits well inside
    the barrel's own clearance of a foreign pad. KiCad calls the result
    ``hole_clearance`` and ``solder_mask_bridge`` -- a via shorting a pad it
    has no business touching, on a board every Python test passed. Found by
    the first ``kicad-cli pcb drc`` run over a generated LQFP-32 board
    (2026-09-07): 14 hole-clearance errors and 7 mask bridges, seven distinct
    vias sitting on U1's pads.
    """
    pads = _fine_pitch_field()
    result = route(pads, **_FIELD_AREA)
    assert result.vias, "this board must need vias for the test to mean anything"
    # Two rules, because the barrel and the drill are different circles. The
    # copper one is ours; the hole one is KiCad's own default board constraint
    # (0.25 mm), which is what actually fired -- the emitted board carries no
    # design-settings block, so a reader gets KiCad's defaults.
    kicad_hole_clearance_nm = 250_000
    bad = [
        (v.net, p.ref, p.number, copper, hole)
        for v, p, copper, hole in _via_pad_gaps(result, pads)
        if copper < DEFAULT_ROUTE_CLEARANCE_NM or hole < kicad_hole_clearance_nm
    ]
    assert not bad, (
        f"{len(bad)} via/pad pair(s) inside clearance "
        f"(net, ref, pad, copper gap nm, hole gap nm): {bad[:5]}"
    )


# --------------------------------------------------------------------------
# Pad escape on a fine-pitch package
#
# The pad keep-out used to be rasterised outward to whole lattice cells, which
# is up to a full 0.25 mm grid step of clearance nobody asked for around every
# pad in every direction. On a 0.5 mm-pitch package that seals the pads: the
# node one step off a pad centre belongs to its neighbour's rounded-out halo on
# all four sides, so the net can never leave the pad and is reported "blocked
# by pad clearance" on a board with a perfectly good escape lane.
#
# Measured on the LQFP-32 board this repo's own emitter builds, 2026-09-08:
# 19 of 54 pads sealed and 2 of 22 nets routed before, 13 of 22 after.
# --------------------------------------------------------------------------


def _escape_row():
    """A 0.5 mm-pitch pad row and a partner for each, in the LQFP's geometry.

    Pads 1.5 x 0.3 mm on a 0.5 mm pitch, long axis pointing away from the
    package -- which is exactly the shape ``footprints.lqfp`` emits, and the
    shape whose only escape is straight out along the pad's own row.
    """
    pads = []
    for k in range(8):
        y = mm(2.0) + k * mm(0.5)
        pads.append(
            RoutePad(net=f"N{k}", x_nm=mm(5.0), y_nm=y, w_nm=mm(1.5),
                     h_nm=mm(0.3), ref="U1", number=str(k + 1))
        )
        pads.append(
            RoutePad(net=f"N{k}", x_nm=mm(1.0), y_nm=y, w_nm=mm(0.3),
                     h_nm=mm(0.3), ref=f"R{k}", number="1")
        )
    return pads


_ESCAPE_AREA = dict(min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(8.0))


def test_every_pad_of_a_fine_pitch_row_escapes_onto_the_lattice():
    """The escape lane is legal geometry, so the router must be able to use it.

    A track running out along a pad's own row sits 0.5 mm from the neighbouring
    pad's centre, i.e. 0.35 mm from its copper -- 0.25 mm of gap once the
    0.2 mm track's own half-width is taken off, against a 0.2 mm rule. Nothing
    about that is marginal; it was lost purely to rounding the keep-out out to
    the next node. Before the fix this board routed 2 of its 8 nets.
    """
    result = route(_escape_row(), **_ESCAPE_AREA)
    assert not result.unrouted, (
        f"only {len(result.routed)} of 8 escape lanes were used: "
        f"{result.unrouted}"
    )


def test_the_fine_pitch_escape_does_not_buy_completion_with_a_short():
    """Completion is worthless if the copper that bought it is illegal.

    Independent math, the discipline this file keeps everywhere: gaps are
    computed here from the pads handed in and the tracks handed back, calling
    nothing in :mod:`silkscreen.routing`. Unrouting a pad keep-out is exactly
    the change that could go wrong in the other direction, so this is the half
    of the pair that says it did not.
    """
    pads = _escape_row()
    result = route(pads, **_ESCAPE_AREA)
    required = DEFAULT_ROUTE_CLEARANCE_NM
    step = mm(0.05)
    bad = []
    for tr in result.tracks:
        r = tr.width_nm / 2
        dx = (tr.end_x_nm > tr.start_x_nm) - (tr.end_x_nm < tr.start_x_nm)
        dy = (tr.end_y_nm > tr.start_y_nm) - (tr.end_y_nm < tr.start_y_nm)
        length = abs(tr.end_x_nm - tr.start_x_nm) + abs(tr.end_y_nm - tr.start_y_nm)
        for k in range(0, length + 1, step):
            x = tr.start_x_nm + dx * k
            y = tr.start_y_nm + dy * k
            for pad in pads:
                if pad.net == tr.net:
                    continue
                ex = max(0.0, abs(x - pad.x_nm) - pad.w_nm / 2)
                ey = max(0.0, abs(y - pad.y_nm) - pad.h_nm / 2)
                gap = math.hypot(ex, ey) - r
                if gap < required - 1:
                    bad.append((tr.net, pad.ref, pad.number, pad.net, gap))
    assert not bad, (
        f"{len(bad)} sample(s) of copper inside a foreign pad's clearance "
        f"(net, ref, pad, pad net, gap nm): {bad[:5]}"
    )


def test_a_net_with_one_pad_is_not_blamed_on_the_grid():
    """Two ways to end up with one terminal, two different fixes.

    A net carrying a single pad has nothing to connect and no grid pitch would
    change that; a net whose several pads all landed on one lattice node really
    is a grid too coarse for the footprint. Saying the second when the first was
    true was a measured lie: on ``engine/tests/fixtures/ref.kicad_pcb``, 48 of
    its 54 nets carry exactly one pad and every one of them was reported as a
    footprint the grid could not resolve -- sending a reader to change
    ``grid_nm`` for a net that has one pad.
    """
    result = route(
        [
            RoutePad(net="LONE", x_nm=mm(2.0), y_nm=mm(2.0), w_nm=mm(1.0),
                     h_nm=mm(1.0)),
            RoutePad(net="PAIR", x_nm=mm(6.0), y_nm=mm(2.0), w_nm=mm(0.2),
                     h_nm=mm(0.2)),
            RoutePad(net="PAIR", x_nm=mm(6.05), y_nm=mm(2.0), w_nm=mm(0.2),
                     h_nm=mm(0.2)),
        ],
        min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0),
        grid_nm=mm(1.0),
    )
    assert "only one pad" in result.unrouted["LONE"]
    assert "too coarse" not in result.unrouted["LONE"]
    assert "too coarse" in result.unrouted["PAIR"]


def test_a_pad_smaller_than_the_lattice_still_blocks_copper():
    """A keep-out narrower than a grid step must not become invisible.

    Making the pad keep-out exact removes the outward rounding, and outward
    rounding was the only thing guaranteeing a small obstacle marked *any*
    node: at a coarse grid a narrow keep-out can fall between two nodes and
    mark nothing, and copper then steps straight over the pad. That is the
    silent-geometry bug this module exists to prevent, arriving through the
    back door of a fix, so the band is widened to the nearest node instead and
    this is what says so. Half a millimetre of pad against a 2 mm lattice, with
    the pad deliberately off-node.
    """
    blocker = RoutePad(net="", x_nm=mm(5.1), y_nm=mm(4.9), w_nm=mm(0.5),
                       h_nm=mm(0.5), ref="X1", number="1")
    pads = [
        blocker,
        RoutePad(net="A", x_nm=mm(1.0), y_nm=mm(5.0), w_nm=mm(0.5), h_nm=mm(0.5)),
        RoutePad(net="A", x_nm=mm(9.0), y_nm=mm(5.0), w_nm=mm(0.5), h_nm=mm(0.5)),
    ]
    result = route(
        pads, min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0),
        grid_nm=mm(2.0),
    )
    # Independent math again: does any emitted segment pass within the
    # blocker's own clearance? Computed here from the pad and the tracks.
    required = DEFAULT_ROUTE_CLEARANCE_NM
    step = mm(0.05)
    worst = None
    for tr in result.tracks:
        dx = (tr.end_x_nm > tr.start_x_nm) - (tr.end_x_nm < tr.start_x_nm)
        dy = (tr.end_y_nm > tr.start_y_nm) - (tr.end_y_nm < tr.start_y_nm)
        length = abs(tr.end_x_nm - tr.start_x_nm) + abs(tr.end_y_nm - tr.start_y_nm)
        for k in range(0, length + 1, step):
            ex = max(0.0, abs(tr.start_x_nm + dx * k - blocker.x_nm)
                     - blocker.w_nm / 2)
            ey = max(0.0, abs(tr.start_y_nm + dy * k - blocker.y_nm)
                     - blocker.h_nm / 2)
            gap = math.hypot(ex, ey) - tr.width_nm / 2
            worst = gap if worst is None else min(worst, gap)
    assert worst is None or worst >= required - 1, (
        f"copper passes {worst / 1e6:.3f} mm from an unnetted pad the lattice "
        f"is too coarse to resolve, under the {required / 1e6:.2f} mm rule"
    )


def _two_vias_in_one_band(gap_mm: float) -> list[RoutePad]:
    """Two nets forced to change layer side by side.

    An unnetted bar across the whole board on the front layer, and two nets
    whose pads sit above and below it ``gap_mm`` apart. Neither net can pass on
    the front, so each buys a via on its own column -- putting two barrels of
    different nets exactly ``gap_mm`` apart, which is the geometry the search
    has to refuse.
    """
    pads = [
        RoutePad(net="", x_nm=mm(5.0), y_nm=mm(5.0), w_nm=mm(10.0),
                 h_nm=mm(0.4), ref="BAR", number="1")
    ]
    for net, x in (("A", 2.0), ("B", 2.0 + gap_mm)):
        for number, y in (("1", 2.0), ("2", 8.0)):
            pads.append(
                RoutePad(net=net, x_nm=mm(x), y_nm=mm(y), w_nm=mm(0.4),
                         h_nm=mm(0.4), ref=f"P{net}", number=number)
            )
    return pads


def test_two_vias_of_different_nets_keep_the_barrels_clearance():
    """Barrel to barrel is the one pairing where neither half-width is a track.

    Every disc in this module is a centre-to-centre distance, so its radius is
    both neighbours' half-widths plus the clearance. The via disc is sized for
    via-against-track (0.3 + 0.2 + 0.1 = 0.6 mm at the defaults) and was doing
    duty for via-against-via as well, which needs 0.8 mm. Two barrels 0.75 mm
    apart therefore passed the search with a 0.15 mm gap against a 0.2 mm rule
    -- the same class of error, and the same 0.05 mm sort of number, that
    KiCad's own DRC found in track-against-via before the clearance moved
    inside the search.

    Independent math: the gap is computed here from the vias handed back,
    calling nothing in :mod:`silkscreen.routing`.
    """
    result = route(
        _two_vias_in_one_band(0.75), min_x_nm=0, min_y_nm=0,
        max_x_nm=mm(10.0), max_y_nm=mm(10.0),
    )
    assert not result.unrouted, f"premise: both nets must route: {result.unrouted}"
    pairs = [
        (a, b)
        for index, a in enumerate(result.vias)
        for b in result.vias[index + 1:]
        if a.net != b.net
    ]
    assert pairs, "premise: this board must produce vias on both nets"
    for a, b in pairs:
        gap = (
            math.hypot(a.x_nm - b.x_nm, a.y_nm - b.y_nm)
            - a.diameter_nm / 2
            - b.diameter_nm / 2
        )
        assert gap >= DEFAULT_ROUTE_CLEARANCE_NM - 1, (
            f"{a.net} and {b.net} barrels are {gap / 1e6:.3f} mm apart, under "
            f"the {DEFAULT_ROUTE_CLEARANCE_NM / 1e6:.2f} mm rule"
        )


# --------------------------------------------------------------------------
# The off-grid pad escape (fanout)
#
# Making the pad keep-out exact unsealed the pads whose neighbours had been
# rounded outward onto them. What it could not fix is a pad row that is simply
# out of phase with the lattice: an LQFP-32 laid out by this repo's own emitter
# puts U1's pads at x = 9.025 mm on a 0.25 mm lattice, so the node nearest each
# pad centre is 0.125 mm off it -- far enough to fall inside the *neighbouring*
# pad's clearance, on every node around it, on a 0.5 mm pitch.
#
# Two things followed from forcing that node anyway. The nets could not leave
# the pads at all (9 of 22 routed). And where one did route, the track began
# 0.125 mm from a foreign pad against a 0.2 mm rule -- a real clearance
# violation, found by KiCad's own DRC on that board, 2026-09-08.
#
# The fix is a fanout: leave the pad on a short piece of real copper from its
# exact centre and join the lattice at a node the pad may legally own. The
# name and the ordering are FreeRouting's (``BatchFanout.fanout_board`` runs
# its escape passes before ``BatchAutorouter`` lays a net, and ``isPinEscaped``
# counts a pin escaped only when its contact has no clearance violations); the
# anchor is KiCad PNS's (``SOLID::Anchor`` in ``pcbnew/router/pns_solid.cpp``
# returns the pad's own ``m_pos``, never a quantised point).
#
# Measured on that LQFP-32 board with the placement pinned, 2026-09-08:
# 9/22 nets and one KiCad clearance violation before, 22/22 and none after,
# with the LDO and 555-blinker demo boards byte-identical either way.
# --------------------------------------------------------------------------


def _off_phase_row(count: int = 6) -> list[RoutePad]:
    """A 0.5 mm-pitch pad row whose centres miss the lattice, and its partners.

    The numbers are copied from the LQFP-32 board ``build_board`` produces, not
    invented: 1.5 x 0.3 mm pads on a 0.5 mm pitch with centres at x = 9.025 mm
    and y = 2.125 + 0.5k, which on the 0.25 mm default lattice puts every pad
    centre 0.025 mm off in x and 0.125 mm off in y. That phase is the whole
    fixture; move the row onto the lattice and the board routes without any of
    this.
    """
    pads: list[RoutePad] = []
    for k in range(count):
        pads.append(
            RoutePad(net=f"N{k}", x_nm=mm(9.025), y_nm=mm(2.125) + k * mm(0.5),
                     w_nm=mm(1.5), h_nm=mm(0.3), ref="U1", number=str(k + 1))
        )
        pads.append(
            RoutePad(net=f"N{k}", x_nm=mm(2.0), y_nm=mm(1.5) + k * mm(1.0),
                     w_nm=mm(0.6), h_nm=mm(0.6), ref=f"R{k}", number="1")
        )
    return pads


_OFF_PHASE_AREA = dict(
    min_x_nm=0, min_y_nm=0, max_x_nm=mm(14.0), max_y_nm=mm(8.0)
)


def _foreign_pad_gaps(pads: list[RoutePad], tracks) -> list[tuple]:
    """Every sampled point of copper that sits inside a foreign pad's clearance.

    Independent math, the discipline this file keeps everywhere: computed from
    the pads handed in and the tracks handed back, calling nothing in
    :mod:`silkscreen.routing`. An escape is copper that does not lie on the
    routing lattice, so a check written in terms of the lattice would share
    exactly the blind spot the escape introduces -- this samples the segment
    itself.

    Layers are honoured, and that is not a detail: a back-layer track running
    under a front-layer SMD pad is legal copper, and a check that ignored the
    layer would report the router's own correct answer as a short.
    """
    required = DEFAULT_ROUTE_CLEARANCE_NM
    both = (Layer.TOP, Layer.BOTTOM)
    step = mm(0.01)
    bad = []
    for tr in tracks:
        r = tr.width_nm / 2
        length = abs(tr.end_x_nm - tr.start_x_nm) + abs(tr.end_y_nm - tr.start_y_nm)
        dx = (tr.end_x_nm > tr.start_x_nm) - (tr.end_x_nm < tr.start_x_nm)
        dy = (tr.end_y_nm > tr.start_y_nm) - (tr.end_y_nm < tr.start_y_nm)
        for k in range(0, length + 1, step):
            x = tr.start_x_nm + dx * k
            y = tr.start_y_nm + dy * k
            for pad in pads:
                if pad.net == tr.net:
                    continue
                if tr.layer not in pad.copper_layers(both):
                    continue
                ex = max(0.0, abs(x - pad.x_nm) - pad.w_nm / 2)
                ey = max(0.0, abs(y - pad.y_nm) - pad.h_nm / 2)
                gap = math.hypot(ex, ey) - r
                if gap < required - 1:
                    bad.append((tr.net, pad.ref, pad.number, pad.net, round(gap)))
    return bad


def test_a_pad_row_out_of_phase_with_the_lattice_still_routes():
    """The phase of the grid must not decide whether a board can be routed.

    Every escape lane here is legal geometry: a track along a pad's own
    centreline sits 0.5 mm from the neighbouring pad's centre, 0.35 mm from its
    copper, 0.25 mm of gap once the track's own half-width is taken off,
    against a 0.2 mm rule. Nothing is marginal about it. Before the escape
    existed this fixture routed 2 of its 6 nets, because the node nearest each
    pad centre belonged to the neighbour and the search could not start.
    """
    result = route(_off_phase_row(), **_OFF_PHASE_AREA)
    assert not result.unrouted, (
        f"only {len(result.routed)} of 6 off-phase pads escaped: "
        f"{result.unrouted}"
    )


def test_the_escape_does_not_buy_completion_with_a_short():
    """Completion is worthless if the copper that bought it is illegal.

    This is the half of the pair that matters most for this change. An escape
    is the one piece of copper in the module that does not run node to node,
    so it is exactly the kind of segment that passes a grid-based clearance
    check and still shorts two pads in reality -- and a 0.5 mm-pitch row is
    where that would happen. Sampled at 0.01 mm, against every foreign pad.
    """
    pads = _off_phase_row()
    result = route(pads, **_OFF_PHASE_AREA)
    bad = _foreign_pad_gaps(pads, result.tracks)
    assert not bad, (
        f"{len(bad)} sample(s) of copper inside a foreign pad's clearance "
        f"(net, ref, pad, pad net, gap nm): {bad[:5]}"
    )


def test_the_escape_actually_starts_on_its_pad():
    """An escape that misses its own pad is a track ending in bare laminate.

    The whole point of anchoring at the pad's exact centre rather than a
    lattice node is that the copper reaches the pad; a stub drawn from a
    rounded point could leave a hair of gap and the run would still report the
    net routed. So: every netted pad of a routed net must have copper of its
    own net whose endpoint lies inside its rectangle.
    """
    pads = _off_phase_row()
    result = route(pads, **_OFF_PHASE_AREA)
    ends = {
        (t.net, t.start_x_nm, t.start_y_nm) for t in result.tracks
    } | {(t.net, t.end_x_nm, t.end_y_nm) for t in result.tracks}
    missing = [
        f"{pad.ref}.{pad.number}"
        for pad in pads
        if pad.net in result.routed
        and not any(
            net == pad.net
            and 2 * abs(x - pad.x_nm) <= pad.w_nm
            and 2 * abs(y - pad.y_nm) <= pad.h_nm
            for net, x, y in ends
        )
    ]
    assert not missing, (
        f"{len(missing)} pad(s) of a routed net have no copper touching them: "
        f"{missing}"
    )


def _walled_in_pad() -> list[RoutePad]:
    """One off-phase pad buried in a field of foreign pads it cannot leave.

    A 0.5 mm-pitch array of unnetted pads two millimetres across in every
    direction. Every lattice node inside it lies in some pad's keep-out, so
    there is no node the buried pad may own and no escape to be cut -- which is
    the case the router has to *name* rather than fall back on.
    """
    pads = [
        RoutePad(net="A", x_nm=mm(5.025), y_nm=mm(5.125), w_nm=mm(0.3),
                 h_nm=mm(0.3), ref="U1", number="1"),
        RoutePad(net="A", x_nm=mm(10.0), y_nm=mm(10.0), w_nm=mm(0.6),
                 h_nm=mm(0.6), ref="R1", number="1"),
    ]
    for i in range(-5, 6):
        for j in range(-5, 6):
            if i == 0 and j == 0:
                continue
            pads.append(
                RoutePad(net="", x_nm=mm(5.025) + i * mm(0.5),
                         y_nm=mm(5.125) + j * mm(0.5), w_nm=mm(0.3),
                         h_nm=mm(0.3), ref="X1", number=f"{i},{j}")
            )
    return pads


def test_a_pad_with_no_escape_is_named_and_not_quietly_dropped():
    """Three ways to fail to route are three different fixes, so three reasons.

    "No clear path" says rearrange the copper. "Grid too coarse" says change
    ``grid_nm``. Neither is true of a pad that has no legal foothold on the
    lattice at all, and neither is what a reader should be told: the fix there
    is to move the part or to change the lattice off the pad pitch. The old
    behaviour was worse than a wrong reason -- it forced the node anyway and
    laid copper 0.125 mm from a foreign pad -- so the one thing this must never
    do is fall back to it silently.
    """
    result = route(
        _walled_in_pad(), min_x_nm=0, min_y_nm=0,
        max_x_nm=mm(14.0), max_y_nm=mm(14.0),
    )
    assert "A" in result.unrouted, "premise: net A must not come out routed"
    reason = result.unrouted["A"]
    assert "no legal way onto the routing lattice" in reason, reason
    assert "U1.1" in reason, f"the reason must name the pad: {reason}"
    assert not [t for t in result.tracks if t.net == "A"], (
        "a net whose pad has no escape must have no copper at all; half a net "
        "laid down is a board that looks routed where you happen to look"
    )


def test_a_later_net_may_not_run_over_an_escape():
    """An escape is copper, so the maps have to hold it against everyone else.

    Escapes are chosen before any net is routed, which is what makes them
    cheap to check exactly -- but it also means every net routed afterwards has
    to be kept off them, and the disc tests cannot see off-lattice copper. This
    drives a second net through the corridor the escapes occupy and asks
    whether any of its copper came within clearance of a foreign pad or of
    anything else. Independent math again.
    """
    pads = _off_phase_row()
    pads += [
        RoutePad(net="Z", x_nm=mm(11.0), y_nm=mm(1.0), w_nm=mm(0.6),
                 h_nm=mm(0.6), ref="RZ", number="1"),
        RoutePad(net="Z", x_nm=mm(11.0), y_nm=mm(7.0), w_nm=mm(0.6),
                 h_nm=mm(0.6), ref="RZ", number="2"),
    ]
    result = route(pads, **_OFF_PHASE_AREA)
    assert "Z" in result.routed, f"premise: Z must route: {result.unrouted}"
    assert not _foreign_pad_gaps(pads, result.tracks)
    # And Z's own copper against every escape, computed here segment to
    # segment rather than node to node, because that is the thing at issue.
    theirs = [t for t in result.tracks if t.net != "Z"]
    required = DEFAULT_ROUTE_CLEARANCE_NM
    worst = None
    for a in (t for t in result.tracks if t.net == "Z"):
        for b in theirs:
            ex = max(
                0,
                min(b.start_x_nm, b.end_x_nm) - max(a.start_x_nm, a.end_x_nm),
                min(a.start_x_nm, a.end_x_nm) - max(b.start_x_nm, b.end_x_nm),
            )
            ey = max(
                0,
                min(b.start_y_nm, b.end_y_nm) - max(a.start_y_nm, a.end_y_nm),
                min(a.start_y_nm, a.end_y_nm) - max(b.start_y_nm, b.end_y_nm),
            )
            gap = math.hypot(ex, ey) - a.width_nm / 2 - b.width_nm / 2
            worst = gap if worst is None else min(worst, gap)
    assert worst is None or worst >= required - 1, (
        f"two nets' copper come {worst / 1e6:.3f} mm apart, under the "
        f"{required / 1e6:.2f} mm rule"
    )


def test_an_off_phase_board_routes_the_same_way_twice():
    """The escape must not make the copper depend on anything but the design.

    Candidate nodes are generated in a fixed order and the first legal one
    wins, so this is a property of the code rather than of the machine -- but
    it is the property the whole module is built on, and the escape is new
    machinery on the path that decides where every net starts.
    """
    first = route(_off_phase_row(), **_OFF_PHASE_AREA)
    second = route(_off_phase_row(), **_OFF_PHASE_AREA)
    assert first.tracks == second.tracks
    assert first.vias == second.vias
    assert first.routed == second.routed


# --------------------------------------------------------------------------
# Copper against the board edge
#
# The rectangle handed to route() *is* Edge.Cuts -- board.py draws the outline
# on exactly those four lines -- and until 2026-09-08 the lattice ran right out
# to it. A track on the outermost column therefore had its centreline on the
# board edge and half its width off the board, and no rule in the module could
# notice: every clearance there is copper-to-copper, and the edge is not
# copper.
#
# Latent, and invisible only because the router was bad. Reproduced on a
# generated LQFP-64 board (a synthetic 20-resistor design over the repo's own
# LQFP-64 land pattern, placement pinned): ``kicad-cli pcb drc
# --severity-error`` reported ten ``copper_edge_clearance`` violations, three
# at "actual 0.0000 mm" -- copper across the outline -- and the pad-escape work
# that landed the same day is what brought them out, by completing four times
# as many nets so the outer columns were used at all.
#
# The rule is KiCad's own ``EDGE_CLEARANCE_CONSTRAINT``, and the two facts
# about how KiCad measures it that these tests depend on were read out of its
# source, not assumed: ``DRC_TEST_PROVIDER_EDGE_CLEARANCE::Run`` in
# ``pcbnew/drc/drc_test_provider_edge_clearance.cpp`` does
# ``if( item->IsOnLayer( Edge_Cuts ) ) stroke.SetWidth( 0 );``, so the boundary
# is the Edge.Cuts *centreline*; and ``testAgainstEdge`` collides the copper
# item's own shape against it, so the quantity is edge-of-copper to centreline.
# The default, 0.5 mm, is ``#define DEFAULT_COPPEREDGECLEARANCE   0.5`` in
# KiCad's ``include/board_design_settings.h``.
#
# The geometry below is computed from the rectangle and the emitted segments
# with its own arithmetic and never calls routing.py -- the discipline
# ``connected_nets`` keeps for connectivity, applied to the edge.
# --------------------------------------------------------------------------


def _edge_gaps(result, area: dict) -> list[tuple[float, str]]:
    """Every piece of emitted copper, with its own gap to the four sides.

    Independent math: a track is a segment of ``width_nm``, so its copper
    reaches half that beyond its centreline on every side of its extent; a via
    is a disc of ``diameter_nm``. Both are compared against the rectangle's
    lines directly. Nothing here asks the router what it thinks the distance
    is, which is the point -- a check phrased in lattice indices would share
    exactly the blind spot the lattice had.
    """
    gaps: list[tuple[float, str]] = []
    for t in result.tracks:
        half = t.width_nm / 2
        lo_x, hi_x = min(t.start_x_nm, t.end_x_nm), max(t.start_x_nm, t.end_x_nm)
        lo_y, hi_y = min(t.start_y_nm, t.end_y_nm), max(t.start_y_nm, t.end_y_nm)
        gaps.append((
            min(lo_x - half - area["min_x_nm"], area["max_x_nm"] - hi_x - half,
                lo_y - half - area["min_y_nm"], area["max_y_nm"] - hi_y - half),
            f"track {t.net} ({lo_x / 1e6}, {lo_y / 1e6})",
        ))
    for v in result.vias:
        half = v.diameter_nm / 2
        gaps.append((
            min(v.x_nm - half - area["min_x_nm"], area["max_x_nm"] - v.x_nm - half,
                v.y_nm - half - area["min_y_nm"], area["max_y_nm"] - v.y_nm - half),
            f"via {v.net} ({v.x_nm / 1e6}, {v.y_nm / 1e6})",
        ))
    return gaps


#: A corridor whose only way past the wall is the bottom margin.
#:
#: The wall's lowest obstacle pad sits at y = 1.25 mm, which seals every node
#: from y = 0.75 to 1.75 (a 0.5 mm pad plus the 0.3 mm keep-out margin, so
#: |node - 1.25| < 0.55). What is left under it is y = 0.25 and 0.50 -- both
#: inside a 0.5 mm copper-to-edge band, neither inside a zero one. So the same
#: board answers the two settings differently and the fixture cannot go stale
#: quietly: if the numbers ever stop lining up, ``test_the_corridor_is_a_real
#: _corridor`` fails rather than the property tests passing vacuously.
_EDGE_AREA = dict(min_x_nm=0, min_y_nm=0, max_x_nm=mm(20.0), max_y_nm=mm(20.0))


def _edge_corridor() -> list[RoutePad]:
    return _wall(10.0, 1.25, 20.0, "y") + [
        RoutePad(net="A", x_nm=mm(5.0), y_nm=mm(10.0),
                 w_nm=mm(1.0), h_nm=mm(1.0), ref="R1", number="1"),
        RoutePad(net="A", x_nm=mm(15.0), y_nm=mm(10.0),
                 w_nm=mm(1.0), h_nm=mm(1.0), ref="R1", number="2"),
    ]


def test_the_corridor_is_a_real_corridor():
    """Without the rule this fixture lays copper 0.4 mm from the outline.

    The premise every test below rests on. A regression test for a clearance
    is worthless if the fixture never wanted to violate it.
    """
    loose = route(_edge_corridor(), two_layer=False, edge_clearance_nm=0,
                  **_EDGE_AREA)
    assert loose.routed == ["A"]
    worst = min(_edge_gaps(loose, _EDGE_AREA))
    assert worst[0] == mm(0.4), worst


def test_copper_keeps_the_board_edge_clearance():
    result = route(_edge_corridor(), two_layer=False, **_EDGE_AREA)
    for gap, what in _edge_gaps(result, _EDGE_AREA):
        assert gap >= DEFAULT_EDGE_CLEARANCE_NM, (gap, what)


def test_a_channel_only_the_edge_band_closes_is_refused_in_words():
    """The corridor with the rule on: no legal way past, said so, not faked.

    The honest answer here is a named unrouted net, not copper laid over the
    outline. What must never happen is the third thing: a net reported routed
    because half of it was laid.
    """
    result = route(_edge_corridor(), two_layer=False, **_EDGE_AREA)
    assert result.routed == []
    assert result.tracks == []
    assert "A" in result.unrouted


def test_the_first_legal_column_is_exactly_where_the_arithmetic_puts_it():
    """Not one node in, not one node out.

    Computed here from the rule and the lattice pitch alone: a track's copper
    reaches half its width past its centreline, so the smallest legal
    centreline offset is ``edge + width/2`` and the first node at or beyond it
    is ``ceil`` of that over the grid. Rounding the band outward would cost a
    routing channel for nothing -- the mistake that sealed the pads when the
    keep-out was rasterised to whole cells -- and rounding it inward would put
    copper back inside the fab's cut.
    """
    need = DEFAULT_EDGE_CLEARANCE_NM + DEFAULT_TRACK_WIDTH_NM // 2
    steps = -(-need // DEFAULT_ROUTE_GRID_NM)
    assert steps * DEFAULT_ROUTE_GRID_NM == mm(0.75)
    # A row of nodes at exactly that offset must still be usable, or the band
    # is one step too conservative.
    area = dict(min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0))
    pads = [
        RoutePad(net="A", x_nm=mm(2.0), y_nm=mm(0.75), w_nm=mm(0.4),
                 h_nm=mm(0.4), ref="R1", number="1"),
        RoutePad(net="A", x_nm=mm(8.0), y_nm=mm(0.75), w_nm=mm(0.4),
                 h_nm=mm(0.4), ref="R2", number="1"),
    ]
    result = route(pads, two_layer=False, **area)
    assert result.routed == ["A"]
    # The track really does run on that column: its centreline at 0.75 mm
    # leaves 0.65 mm of copper-free board, a hair over the 0.60 mm the rule
    # asks of a centreline, because the lattice cannot land on 0.60 exactly.
    assert min(_edge_gaps(result, area))[0] == mm(0.65)


def test_a_via_is_held_further_off_the_edge_than_a_track():
    """A barrel is wider than a track, so it owes the outline more room.

    The discriminator is a two-layer net whose pads sit on the first
    track-legal column: at 0.75 mm the track is legal (copper edge 0.65 mm in)
    and the barrel is not (0.6 mm diameter would put its edge 0.45 mm in,
    inside the 0.5 mm rule), so the via has to step inward while the track does
    not. With the rule off, the via stays on the pad column -- which is how
    this test knows it is measuring the rule and not the geometry.
    """
    area = dict(min_x_nm=0, min_y_nm=0, max_x_nm=mm(8.0), max_y_nm=mm(8.0))
    pads = [
        RoutePad(net="A", x_nm=mm(0.75), y_nm=mm(4.0), w_nm=mm(0.4),
                 h_nm=mm(0.4), layer=Layer.TOP, ref="R1", number="1"),
        RoutePad(net="A", x_nm=mm(0.75), y_nm=mm(6.0), w_nm=mm(0.4),
                 h_nm=mm(0.4), layer=Layer.BOTTOM, ref="R2", number="1"),
    ]
    loose = route(pads, edge_clearance_nm=0, **area)
    assert [v.x_nm for v in loose.vias] == [mm(0.75)]

    strict = route(pads, **area)
    assert strict.routed == ["A"]
    assert [v.x_nm for v in strict.vias] == [mm(1.0)]
    # The track is still allowed on the column the via was pushed off.
    assert min(min(t.start_x_nm, t.end_x_nm) for t in strict.tracks) == mm(0.75)
    for v in strict.vias:
        assert v.x_nm - DEFAULT_VIA_DIAMETER_NM // 2 >= DEFAULT_EDGE_CLEARANCE_NM


def test_an_escape_stub_keeps_the_edge_clearance_too():
    """The one piece of copper in the module that is not on the lattice.

    A stub is chosen before any net is routed and drawn from the pad's exact
    centre, so a rule enforced only on lattice nodes would leave it free to run
    out over the outline -- the same reason ``stub_keepout`` exists for
    copper-to-copper. The off-phase LQFP row is put 1.0 mm from the left edge,
    which is where its escapes want to go.

    Non-vacuous by assertion, not by hope: the run is required to contain
    copper that genuinely misses the lattice, and the same board with the rule
    off is required to violate it.
    """
    area = dict(min_x_nm=mm(1.0), min_y_nm=0,
                max_x_nm=mm(14.0), max_y_nm=mm(8.0))

    def off_lattice(result) -> int:
        grid = DEFAULT_ROUTE_GRID_NM
        return sum(
            any((c - o) % grid for c, o in (
                (t.start_x_nm, area["min_x_nm"]), (t.start_y_nm, area["min_y_nm"]),
                (t.end_x_nm, area["min_x_nm"]), (t.end_y_nm, area["min_y_nm"]),
            ))
            for t in result.tracks
        )

    loose = route(_off_phase_row(), edge_clearance_nm=0, **area)
    assert off_lattice(loose)
    assert min(_edge_gaps(loose, area))[0] < DEFAULT_EDGE_CLEARANCE_NM

    strict = route(_off_phase_row(), **area)
    assert off_lattice(strict)
    assert strict.routed == loose.routed
    for gap, what in _edge_gaps(strict, area):
        assert gap >= DEFAULT_EDGE_CLEARANCE_NM, (gap, what)


def test_a_pad_inside_the_edge_clearance_is_named_not_dropped():
    """An edge connector is a legitimate design; silently losing it is not.

    The pad is where placement put it and the router does not move pads, so
    the only two honest answers are "routed" and "here is the pad and the
    number". Routing round it -- connecting the net's other pads and reporting
    success -- is the silent-disconnection bug the module exists to avoid.
    """
    area = dict(min_x_nm=0, min_y_nm=0, max_x_nm=mm(10.0), max_y_nm=mm(10.0))
    pads = [
        RoutePad(net="A", x_nm=mm(0.4), y_nm=mm(5.0), w_nm=mm(0.6),
                 h_nm=mm(1.5), ref="J1", number="1"),
        RoutePad(net="A", x_nm=mm(5.0), y_nm=mm(5.0), w_nm=mm(0.6),
                 h_nm=mm(1.5), ref="R1", number="1"),
    ]
    result = route(pads, **area)
    assert result.routed == []
    assert result.tracks == []
    reason = result.unrouted["A"]
    # The pad's own copper reaches x = 0.1 mm: 0.4 mm centre less half of a
    # 0.6 mm pad. Computed here, not read back from the router.
    assert "J1.1" in reason
    assert "0.100 mm from the board edge" in reason
    assert "0.50 mm" in reason


def test_a_rectangle_with_no_room_for_the_rule_refuses_naming_its_nets():
    """A refusal is still a result about the nets it did not route.

    ``completion`` reads an empty result as 100%, so a refusal that named
    nothing would report a board with no copper at all as fully routed -- the
    rule :func:`route`'s own ``refuse`` states, applied to this new way of
    having nowhere to go.
    """
    pads = [
        RoutePad(net="A", x_nm=0, y_nm=0, w_nm=mm(0.3), h_nm=mm(0.3),
                 ref="R1", number="1"),
        RoutePad(net="A", x_nm=mm(0.5), y_nm=0, w_nm=mm(0.3), h_nm=mm(0.3),
                 ref="R1", number="2"),
    ]
    result = route(pads, min_x_nm=0, min_y_nm=0,
                   max_x_nm=mm(1.0), max_y_nm=mm(1.0))
    assert result.routed == []
    assert "A" in result.unrouted
    assert "board-edge clearance" in result.unrouted["A"]
    assert result.warnings
    assert result.completion == 0.0


def test_the_edge_rule_keeps_routing_deterministic():
    first = route(_edge_corridor(), two_layer=False, **_EDGE_AREA)
    second = route(_edge_corridor(), two_layer=False, **_EDGE_AREA)
    assert first.tracks == second.tracks
    assert first.vias == second.vias
    assert first.unrouted == second.unrouted

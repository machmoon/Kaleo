"""Generated boards are placed, not compressed.

A board packed at the bare solver clearance is legal and looks amateur: parts
touching at 0.25 mm, the outline hugging the courtyards, and every reference
designator -- which the emitter puts just above its part -- drawn across the
part above it. These tests pin the spacing ``build_board`` reserves and the
designator placement ``emit_kicad_pcb`` writes.

Every expected geometry here is computed from the written file with math that
never calls ``board.py``: the courtyards come from the ``F.CrtYd`` lines, the
pads from the ``pad`` tokens, the designator's box from its ``at`` and font
tokens, each mapped through the footprint's own rotation independently. A
check written in terms of ``ref_text_offset_nm`` or ``part_anchor`` would share
their blind spots, which is the lesson ``test_kicad.py`` already records.
"""

from __future__ import annotations

import itertools
import math
import re

import pytest
from silkscreen.board import (
    DEFAULT_BOARD_MARGIN_NM,
    IC_SPACING_NM,
    PASSIVE_SPACING_NM,
    REF_TEXT_BAND_NM,
    build_board,
    emit_kicad_pcb,
    placed_half_extents,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.units import DEFAULT_CLEARANCE_NM, mm, to_mm

# The blinker the sample render was made from: a SOT-223, a SOIC-8 and eight
# chip passives, which is enough parts that the solver has to stack them.
BLINKER = {
    "devices": {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        "NE555D": {"pins": {"GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                             "CTRL": "5", "THR": "6", "DIS": "7", "VCC": "8"}},
    },
    "passives": {
        "c_in": {"type": "capacitor", "value": "10uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
        "c_t": {"type": "capacitor", "value": "10uF"},
        "c_dec": {"type": "capacitor", "value": "100nF"},
        "r_a": {"type": "resistor", "value": "10k"},
        "r_b": {"type": "resistor", "value": "68k"},
        "r_led": {"type": "resistor", "value": "330"},
        "d_led": {"type": "diode", "value": "red LED"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
        "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2", "NE555D.GND", "c_t.2",
                "c_dec.2", "d_led.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "c_out.1", "NE555D.VCC", "NE555D.RESET",
                 "r_a.1", "c_dec.1"],
        "DIS": ["NE555D.DIS", "r_a.2", "r_b.1"],
        "THR": ["NE555D.THR", "NE555D.TRIG", "r_b.2", "c_t.1"],
        "OUT": ["NE555D.OUT", "r_led.1"],
        "LED": ["r_led.2", "d_led.1"],
    },
}

#: What the task asked for, stated here independently of the module's own
#: constants so a change to those has to be reconciled with the intent.
MIN_PASSIVE_GAP_MM = 0.5
MIN_IC_GAP_MM = 1.0
MIN_EDGE_MARGIN_MM = 1.5

#: KiCad's stroke font advances about 0.7 of the size per glyph; one full
#: size per glyph is the conservative box these checks use.
GLYPH_ADVANCE_RATIO = 1.0

# ------------------------------------------------------------ file reading

_FP_HEAD = re.compile(r'\(footprint "[^"]+"\n\s+\(layer "([FB])\.Cu"\)')
_AT = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")
_REF = re.compile(
    r'\(property "Reference" "([^"]+)" \(at (-?[\d.]+) (-?[\d.]+) (-?[\d.]+)\)'
    r'.*?\(font \(size ([\d.]+) ([\d.]+)\) \(thickness ([\d.]+)\)'
)
_CRTYD = re.compile(
    r"\(fp_line \(start (-?[\d.]+) (-?[\d.]+)\) \(end (-?[\d.]+) (-?[\d.]+)\)"
    r'.*?\(layer "[FB]\.CrtYd"\)'
)
_PAD = re.compile(
    r'\(pad "([^"]+)" \w+ \w+ \(at (-?[\d.]+) (-?[\d.]+)\) \(size ([\d.]+) ([\d.]+)\)'
)
_EDGE = re.compile(
    r"\(gr_line \(start (-?[\d.]+) (-?[\d.]+)\) \(end (-?[\d.]+) (-?[\d.]+)\)"
    r'.*?\(layer "Edge.Cuts"\)'
)


def _rot(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """A footprint-local point in the board frame, KiCad's way: Y down, the
    footprint turned counter-clockwise on screen, so the map is a rotation
    by ``-angle``."""
    r = math.radians(angle_deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


class Written:
    """One footprint as the file describes it, in board millimetres."""

    def __init__(self, block: str):
        self.side = _FP_HEAD.search(block).group(1)
        ats = _AT.findall(block)
        # The first (at ...) after the header is the footprint's own.
        ax, ay, angle = ats[0]
        self.x, self.y = float(ax), float(ay)
        self.angle = float(angle or 0)

        ref, tx, ty, tangle, size_w, size_h, stroke = _REF.search(block).groups()
        self.ref = ref
        self.text_angle = float(tangle)
        cx, cy = _rot(float(tx), float(ty), self.angle)
        self.text_centre = (self.x + cx, self.y + cy)
        self.text_size = (float(size_w), float(size_h), float(stroke))

        xs, ys = [], []
        for sx, sy, ex, ey in _CRTYD.findall(block):
            for px, py in ((float(sx), float(sy)), (float(ex), float(ey))):
                bx, by = _rot(px, py, self.angle)
                xs.append(self.x + bx)
                ys.append(self.y + by)
        assert xs, f"{ref} has no courtyard"
        self.courtyard = (min(xs), min(ys), max(xs), max(ys))

        self.pads: list[tuple[float, float, float, float]] = []
        for _, px, py, pw, ph in _PAD.findall(block):
            bx, by = _rot(float(px), float(py), self.angle)
            w, h = float(pw), float(ph)
            if self.angle % 180 == 90:
                w, h = h, w
            self.pads.append(
                (self.x + bx - w / 2, self.y + by - h / 2,
                 self.x + bx + w / 2, self.y + by + h / 2)
            )

    def text_box(self) -> tuple[float, float, float, float]:
        """Glyph box of the designator, in the board frame.

        A text at an absolute angle of 90 is drawn on its side, so its box is
        the upright box with the axes swapped.
        """
        size_w, size_h, stroke = self.text_size
        w = len(self.ref) * size_w * GLYPH_ADVANCE_RATIO + stroke
        h = size_h + stroke
        if self.text_angle % 180 == 90:
            w, h = h, w
        cx, cy = self.text_centre
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _written(text: str) -> list[Written]:
    blocks = text.split("\n  (footprint ")[1:]
    return [Written("(footprint " + b) for b in blocks]


def _outline(text: str) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for sx, sy, ex, ey in _EDGE.findall(text):
        xs += [float(sx), float(ex)]
        ys += [float(sy), float(ey)]
    assert len(xs) == 8, "expected four Edge.Cuts lines"
    return (min(xs), min(ys), max(xs), max(ys))


def _overlap(a, b) -> bool:
    """Strict rectangle intersection; touching edges do not count."""
    return (
        min(a[2], b[2]) - max(a[0], b[0]) > 1e-9
        and min(a[3], b[3]) - max(a[1], b[1]) > 1e-9
    )


def _gap(a, b) -> float:
    """Axis-aligned separation between two rectangles (negative if they
    overlap in both axes)."""
    dx = max(b[0] - a[2], a[0] - b[2])
    dy = max(b[1] - a[3], a[1] - b[3])
    return max(dx, dy)


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def blinker():
    return build_board(parse_circuit_spec(BLINKER), time_limit_s=10.0)


@pytest.fixture(scope="module")
def blinker_text(blinker):
    return emit_kicad_pcb(blinker)


def _is_ic(ref: str) -> bool:
    return ref.startswith("U")


# ------------------------------------------------------------------ spacing


def test_spacing_constants_are_integer_nanometres():
    for value in (PASSIVE_SPACING_NM, IC_SPACING_NM, REF_TEXT_BAND_NM):
        assert isinstance(value, int) and value > 0


def test_courtyards_keep_the_deliberate_spacing(blinker_text):
    """Courtyard-to-courtyard, read from the file: passives at least 0.5 mm
    apart, anything next to an IC at least 1.0 mm from it."""
    parts = _written(blinker_text)
    for a, b in itertools.combinations(parts, 2):
        if a.side != b.side:
            continue
        gap = _gap(a.courtyard, b.courtyard)
        want = MIN_IC_GAP_MM if _is_ic(a.ref) or _is_ic(b.ref) else MIN_PASSIVE_GAP_MM
        assert gap >= want - 1e-6, (
            f"{a.ref} and {b.ref} are {gap:.3f} mm apart, want >= {want} mm"
        )


def test_the_spacing_is_more_than_the_solver_clearance_alone():
    """The constants have to buy something the clearance did not."""
    passive_gap = PASSIVE_SPACING_NM * 2 + DEFAULT_CLEARANCE_NM
    ic_gap = IC_SPACING_NM + PASSIVE_SPACING_NM + DEFAULT_CLEARANCE_NM
    assert passive_gap >= mm(MIN_PASSIVE_GAP_MM)
    assert ic_gap >= mm(MIN_IC_GAP_MM)


def test_every_courtyard_is_well_inside_the_outline(blinker_text):
    parts = _written(blinker_text)
    ox0, oy0, ox1, oy1 = _outline(blinker_text)
    assert to_mm(DEFAULT_BOARD_MARGIN_NM) >= MIN_EDGE_MARGIN_MM
    for p in parts:
        cx0, cy0, cx1, cy1 = p.courtyard
        margin = min(cx0 - ox0, cy0 - oy0, ox1 - cx1, oy1 - cy1)
        assert margin >= MIN_EDGE_MARGIN_MM - 1e-6, (
            f"{p.ref} is {margin:.3f} mm from the outline"
        )


def test_the_outline_is_still_a_rectangle(blinker_text):
    """The enclosure package reads Edge.Cuts as a rectangle; keep it one."""
    lines = _EDGE.findall(blinker_text)
    assert len(lines) == 4
    for sx, sy, ex, ey in lines:
        assert sx == ex or sy == ey, "an outline edge is not axis-aligned"


def test_reservation_survives_the_placement_unpack(blinker):
    """The courtyard the solver kept clear is the one the part reports.

    Two parts' reported courtyards can never be closer than the spacing they
    both carry plus the clearance, in the solver's own frame -- before any
    emitter arithmetic. Catches a wrong unpack that shifts a part inside its
    box by the wrong reservation.
    """
    boxes = []
    for part in blinker.parts:
        half_w, half_h = placed_half_extents(part)
        boxes.append((part, (part.x_nm, part.y_nm,
                             part.x_nm + 2 * half_w, part.y_nm + 2 * half_h)))
    for (pa, a), (pb, b) in itertools.combinations(boxes, 2):
        if pa.layer is not pb.layer:
            continue
        want = mm(MIN_IC_GAP_MM if _is_ic(pa.ref) or _is_ic(pb.ref)
                  else MIN_PASSIVE_GAP_MM)
        assert _gap(a, b) >= want, (pa.ref, pb.ref, _gap(a, b))
    for part, (x0, y0, x1, y1) in boxes:
        assert x0 >= 0 and y0 >= 0, part.ref
        assert x1 <= blinker.width_nm and y1 <= blinker.height_nm, part.ref


# ----------------------------------------------------------- designators


def test_reference_is_on_silkscreen_and_value_on_fab(blinker_text):
    assert re.search(r'"Reference" "[^"]+" \(at [^)]*\) \(layer "F\.SilkS"\)',
                     blinker_text)
    assert re.search(r'"Value" "[^"]+" \(at [^)]*\) \(layer "F\.Fab"\)', blinker_text)
    assert '"Value"' not in "\n".join(
        line for line in blinker_text.splitlines() if "SilkS" in line
    )


def test_reference_text_is_the_asked_size(blinker_text):
    for p in _written(blinker_text):
        assert p.text_size[:2] == (0.8, 0.8), p.ref


def test_reference_sits_just_above_its_own_courtyard(blinker_text):
    """Outside the courtyard, on the top side, horizontally centred."""
    for p in _written(blinker_text):
        tx0, ty0, tx1, ty1 = p.text_box()
        cx0, cy0, cx1, cy1 = p.courtyard
        assert not _overlap(p.text_box(), p.courtyard), f"{p.ref} on its own part"
        if p.angle == 0:
            # KiCad Y is down: above means smaller Y.
            assert ty1 <= cy0 + 1e-6, f"{p.ref} is not above its courtyard"
            assert abs((tx0 + tx1) / 2 - (cx0 + cx1) / 2) < 1e-6, p.ref


def test_no_reference_touches_any_pad_or_foreign_courtyard(blinker_text):
    """The check the sample render failed: designators drawn across pads and
    across the part above."""
    parts = _written(blinker_text)
    for p in parts:
        box = p.text_box()
        for q in parts:
            if q.side != p.side:
                continue
            for pad in q.pads:
                assert not _overlap(box, pad), (
                    f"{p.ref}'s designator {box} crosses a pad of {q.ref} {pad}"
                )
            if q is not p:
                assert not _overlap(box, q.courtyard), (
                    f"{p.ref}'s designator {box} crosses {q.ref}'s courtyard "
                    f"{q.courtyard}"
                )


def test_references_do_not_overlap_each_other(blinker_text):
    parts = _written(blinker_text)
    for a, b in itertools.combinations(parts, 2):
        if a.side == b.side:
            assert not _overlap(a.text_box(), b.text_box()), (a.ref, b.ref)


def test_references_stay_inside_the_outline(blinker_text):
    outline = _outline(blinker_text)
    for p in _written(blinker_text):
        x0, y0, x1, y1 = p.text_box()
        assert outline[0] <= x0 and outline[1] <= y0, p.ref
        assert x1 <= outline[2] and y1 <= outline[3], p.ref


# ----------------------------------------------------------- with rotation


ROTATED = {
    "devices": {"DRIVER": {"pins": {f"P{i}": str(i) for i in range(1, 9)}}},
    "passives": {
        "Cby": {"type": "capacitor", "value": "100nF"},
        "Rs": {"type": "resistor", "value": "10k"},
        "Rt": {"type": "resistor", "value": "4k7"},
    },
    "nets": {
        "VCC": ["DRIVER.P8", "Cby.1", "Rt.1"],
        "GND": ["DRIVER.P4", "Cby.2", "Rt.2"],
        "OUT": ["DRIVER.P1", "Rs.1"],
        "FB": ["DRIVER.P2", "Rs.2"],
    },
}


def _rotated_board():
    """The solver rotates only when it pays, so like ``test_rotated_anchor``
    this turns a part by hand and re-lays the rest on the swapped extents."""
    board = build_board(parse_circuit_spec(ROTATED), time_limit_s=5.0)
    turned = next(p for p in board.parts if p.ref == "U1")
    turned.rotated = True
    cursor = 0
    tallest = 0
    for part in board.parts:
        half_w, half_h = placed_half_extents(part)
        part.x_nm = cursor + IC_SPACING_NM + REF_TEXT_BAND_NM
        part.y_nm = IC_SPACING_NM
        cursor = part.x_nm + 2 * half_w + IC_SPACING_NM
        tallest = max(tallest, part.y_nm + 2 * half_h + REF_TEXT_BAND_NM)
    board.width_nm = cursor
    board.height_nm = tallest + IC_SPACING_NM
    return board


def test_a_rotated_parts_reference_turns_with_it():
    """Written at the part's angle: KiCad stores footprint text angles as
    absolute, so a label left at 0 on a 90-degree part lies across it."""
    text = emit_kicad_pcb(_rotated_board())
    parts = _written(text)
    assert any(p.angle == 90 for p in parts)
    for p in parts:
        assert p.text_angle == p.angle, p.ref


def test_a_rotated_parts_reference_still_clears_everything():
    text = emit_kicad_pcb(_rotated_board())
    parts = _written(text)
    for p in parts:
        box = p.text_box()
        assert not _overlap(box, p.courtyard), p.ref
        for q in parts:
            for pad in q.pads:
                assert not _overlap(box, pad), (p.ref, q.ref, pad)
            if q is not p:
                assert not _overlap(box, q.courtyard), (p.ref, q.ref)


def test_solver_rotation_unpacks_the_reservation_on_the_turned_side():
    """When the *solver* rotates a part, the band it reserved has turned too
    and the courtyard is unpacked from the turned box. A wrong unpack would
    leave the reported courtyard hanging outside the reserved box, so the
    spacing property is the check, on a board where rotation is forced by
    letting every part turn."""
    spec = parse_circuit_spec(ROTATED)
    board = build_board(
        spec, time_limit_s=5.0, rotatable_refs={"U1", "C1", "R1", "R2"}
    )
    text = emit_kicad_pcb(board)
    parts = _written(text)
    for a, b in itertools.combinations(parts, 2):
        gap = _gap(a.courtyard, b.courtyard)
        want = MIN_IC_GAP_MM if _is_ic(a.ref) or _is_ic(b.ref) else MIN_PASSIVE_GAP_MM
        assert gap >= want - 1e-6, (a.ref, b.ref, gap)
    for p in parts:
        box = p.text_box()
        for q in parts:
            for pad in q.pads:
                assert not _overlap(box, pad), (p.ref, q.ref)
            if q is not p:
                assert not _overlap(box, q.courtyard), (p.ref, q.ref)

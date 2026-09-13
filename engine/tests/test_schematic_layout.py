"""Schematic layout tests: the sheet must read like a drawing, not a dump.

Every check here measures the **emitted file** as KiCad's own parser reads it
and computes the geometry with its own arithmetic -- symbol-local Y-up points
rotated and flipped onto the Y-down sheet by trigonometry, text extents from
the declared font size, paper sizes from a table -- rather than asking the
emitter what it meant. The emitter has a ``_cell`` that reserves room and a
``_pin_on_sheet`` that owns the frame flip; a test that called either would
share its blind spots, which is the same discipline ``test_kicad.py`` follows
for courtyard overlap.

What the checks guard: nothing drawn outside the margin or on the title
block, no two symbols on top of each other, one power symbol on every power
pin and no net label for a power net, one ``PWR_FLAG`` per power net so ERC
has a driver, reference and value clear of the body, and byte-stable output.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import pytest
from kiutils.schematic import Schematic
from silkscreen.netlist import parse_circuit_spec
from silkscreen.schematic import build_schematic, emit_kicad_sch

# KiCad landscape paper sizes, mm. A table rather than an import: the point is
# to check the emitter's idea of a page against an independent one.
PAPER_MM = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0)}

#: Required clearance from every page edge.
MARGIN_MM = 20.0

#: KiCad's stock title block, measured off a plotted A4 sheet: about 117 mm
#: wide and 41 mm tall in the bottom-right corner, on every paper size. A
#: little clearance is added so a symbol may not even graze it.
TITLE_W_MM, TITLE_H_MM = 120.0, 44.0

GRID_MM = 1.27

BLINKER = {
    "devices": {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        "NE555D": {
            "pins": {
                "GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                "CTRL": "5", "THR": "6", "DIS": "7", "VCC": "8",
            }
        },
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
        "GND": [
            "AMS1117-3.3.GND", "c_in.2", "c_out.2", "NE555D.GND", "c_t.2",
            "c_dec.2", "d_led.2",
        ],
        "+3V3": [
            "AMS1117-3.3.VOUT", "c_out.1", "NE555D.VCC", "NE555D.RESET",
            "r_a.1", "c_dec.1",
        ],
        "DIS": ["NE555D.DIS", "r_a.2", "r_b.1"],
        "THR": ["NE555D.THR", "NE555D.TRIG", "r_b.2", "c_t.1"],
        "OUT": ["NE555D.OUT", "r_led.1"],
        "LED": ["r_led.2", "d_led.1"],
    },
}

POWER_NETS = {"VIN": "rail", "GND": "ground", "+3V3": "rail"}


def _big(n: int) -> dict:
    """``n`` resistors in one series chain, so every leg is connected."""
    return {
        "devices": {},
        "passives": {f"R{i}": {"type": "resistor", "value": "1k"} for i in range(n)},
        "nets": {
            "N0": ["R0.1", f"R{n - 1}.2"],
            **{f"N{i + 1}": [f"R{i}.2", f"R{i + 1}.1"] for i in range(n - 1)},
        },
    }


def _emit(circuit: dict, tmp_path, name: str = "sheet", **kw):
    spec = parse_circuit_spec(circuit)
    sheet = build_schematic(spec)
    path = tmp_path / f"{name}.kicad_sch"
    path.write_text(emit_kicad_sch(sheet, project_name=name, **kw), encoding="utf-8")
    return spec, sheet, Schematic().from_file(str(path))


# ----------------------------------------------------------------- geometry
#
# Boxes are (x0, y0, x1, y1) in sheet millimetres, Y-down.


Box = tuple[float, float, float, float]


def _to_sheet(at, angle_deg: float, lx: float, ly: float) -> tuple[float, float]:
    """A symbol-local Y-up point onto the Y-down sheet.

    Rotation happens in the library's own frame, then the single flip. This
    is the convention, written out in trigonometry rather than borrowed.
    """
    rad = math.radians(angle_deg)
    rx = lx * math.cos(rad) - ly * math.sin(rad)
    ry = lx * math.sin(rad) + ly * math.cos(rad)
    return (at.X + rx, at.Y - ry)


def _box_of(points: list[tuple[float, float]]) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlap(a: Box, b: Box) -> bool:
    """Strict overlap: sharing an edge or a point is not overlapping."""
    eps = 1e-6
    return (
        a[0] < b[2] - eps and b[0] < a[2] - eps
        and a[1] < b[3] - eps and b[1] < a[3] - eps
    )


def _union(boxes: list[Box]) -> Box:
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def _text_box(x: float, y: float, text: str, h: float, effects) -> Box:
    """Where a string of ``text`` at font height ``h`` lands, anchored at x,y.

    One character is allowed the full font height in width, an upper bound
    for KiCad's stroke font.
    """
    w = len(text) * h
    horiz = effects.justify.horizontally if effects and effects.justify else None
    vert = effects.justify.vertically if effects and effects.justify else None
    if horiz == "left":
        x0, x1 = x, x + w
    elif horiz == "right":
        x0, x1 = x - w, x
    else:
        x0, x1 = x - w / 2, x + w / 2
    if vert == "bottom":
        y0, y1 = y - h, y
    elif vert == "top":
        y0, y1 = y, y + h
    else:
        y0, y1 = y - h / 2, y + h / 2
    return (x0, y0, x1, y1)


@dataclass
class Item:
    """One thing drawn on the sheet, as a set of primitive boxes."""

    name: str
    kind: str  # part | power | flag | label
    body: list[Box]  # graphics only, no pins, no text
    pins: list[Box]
    texts: list[Box]

    @property
    def primitives(self) -> list[Box]:
        return self.body + self.pins + self.texts

    @property
    def bbox(self) -> Box:
        return _union(self.primitives)


def _lib_index(reparsed) -> dict:
    return {lib.libId: lib for lib in reparsed.libSymbols}


def _pin_type(lib) -> str:
    types = {p.electricalType for u in lib.units for p in u.pins}
    assert len(types) == 1, f"{lib.libId} mixes pin types {types}"
    return types.pop()


def _symbol_items(reparsed) -> list[Item]:
    libs = _lib_index(reparsed)
    items = []
    for sym in reparsed.schematicSymbols:
        lib = libs[sym.libId]
        at, angle = sym.position, sym.position.angle or 0
        assert angle in (0, 180), f"{sym.libId} placed at {angle} degrees"
        body: list[Box] = []
        pins: list[Box] = []
        for unit in lib.units:
            for g in unit.graphicItems:
                if hasattr(g, "start"):  # rectangle
                    corners = [
                        (g.start.X, g.start.Y), (g.end.X, g.start.Y),
                        (g.end.X, g.end.Y), (g.start.X, g.end.Y),
                    ]
                else:
                    corners = [(p.X, p.Y) for p in g.points]
                body.append(_box_of([_to_sheet(at, angle, x, y) for x, y in corners]))
            for pin in unit.pins:
                rad = math.radians(pin.position.angle or 0)
                tip = (
                    pin.position.X + pin.length * math.cos(rad),
                    pin.position.Y + pin.length * math.sin(rad),
                )
                pins.append(
                    _box_of(
                        [
                            _to_sheet(at, angle, pin.position.X, pin.position.Y),
                            _to_sheet(at, angle, *tip),
                        ]
                    )
                )
        texts = []
        for prop in sym.properties:
            if prop.effects and prop.effects.hide:
                continue
            h = prop.effects.font.height if prop.effects and prop.effects.font else 1.27
            texts.append(
                _text_box(prop.position.X, prop.position.Y, prop.value, h, prop.effects)
            )
        if not lib.isPower:
            kind = "part"
        elif _pin_type(lib) == "power_out":
            kind = "flag"
        else:
            kind = "power"
        ref = next(p.value for p in sym.properties if p.key == "Reference")
        items.append(Item(ref, kind, body, pins, texts))
    for label in reparsed.globalLabels:
        h = label.effects.font.height
        box = _text_box(
            label.position.X, label.position.Y, label.text, h, label.effects
        )
        items.append(Item(f"label {label.text!r}", "label", [], [], [box]))
    return items


def _drawable(reparsed) -> tuple[float, float]:
    assert reparsed.paper.paperSize in PAPER_MM, reparsed.paper.paperSize
    return PAPER_MM[reparsed.paper.paperSize]


def assert_well_laid_out(reparsed) -> None:
    """The layout invariants, on a reparsed sheet."""
    w, h = _drawable(reparsed)
    items = _symbol_items(reparsed)
    assert items

    for item in items:
        x0, y0, x1, y1 = item.bbox
        assert x0 >= MARGIN_MM and y0 >= MARGIN_MM, f"{item.name} at {item.bbox}"
        assert x1 <= w - MARGIN_MM and y1 <= h - MARGIN_MM, (
            f"{item.name} at {item.bbox} runs off a {w}x{h} sheet"
        )
        assert not (x1 > w - TITLE_W_MM and y1 > h - TITLE_H_MM), (
            f"{item.name} at {item.bbox} sits on the title block"
        )

    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if a.kind == "part" and b.kind == "part":
                assert not _overlap(a.bbox, b.bbox), (
                    f"{a.name} {a.bbox} overlaps {b.name} {b.bbox}"
                )
                continue
            # Anything else is checked stroke by stroke, so a flag and the
            # power symbol it shares a wire end with may meet at that point.
            pa = [a.bbox] if a.kind == "part" else a.primitives
            pb = [b.bbox] if b.kind == "part" else b.primitives
            for box_a in pa:
                for box_b in pb:
                    assert not _overlap(box_a, box_b), (
                        f"{a.name} {box_a} overlaps {b.name} {box_b}"
                    )

    for item in items:
        if item.kind != "part":
            continue
        for text in item.texts:
            for stroke in item.body:
                assert not _overlap(text, stroke), (
                    f"{item.name}: field {text} is drawn on its body {stroke}"
                )


def _on_grid(v: float) -> bool:
    return abs(v / GRID_MM - round(v / GRID_MM)) < 1e-6


# --------------------------------------------------------------------- tests


@pytest.fixture(scope="module")
def blinker(tmp_path_factory):
    return _emit(BLINKER, tmp_path_factory.mktemp("sch"), "blinker")


def test_everything_is_inside_the_margin_and_nothing_overlaps(blinker):
    _, _, reparsed = blinker
    assert reparsed.paper.paperSize == "A4"
    assert_well_laid_out(reparsed)


def test_every_anchor_and_wire_end_sits_on_kicads_grid(blinker):
    """Off-grid is how a pin ends up a hair away from the wire meant for it."""
    _, _, reparsed = blinker
    for sym in reparsed.schematicSymbols:
        assert _on_grid(sym.position.X) and _on_grid(sym.position.Y), sym.libId
    for wire in reparsed.graphicalItems:
        for pt in getattr(wire, "points", []):
            assert _on_grid(pt.X) and _on_grid(pt.Y), wire
    for label in reparsed.globalLabels:
        assert _on_grid(label.position.X) and _on_grid(label.position.Y), label.text


def test_ics_sit_above_the_passives(blinker):
    """A row of ICs across the upper sheet, the grid of passives beneath."""
    _, _, reparsed = blinker
    libs = _lib_index(reparsed)
    ic_y = [
        s.position.Y for s in reparsed.schematicSymbols
        if not libs[s.libId].isPower and s.libId.split(":")[1] not in "RCLDY"
    ]
    passive_y = [
        s.position.Y for s in reparsed.schematicSymbols
        if not libs[s.libId].isPower and s.libId.split(":")[1] in "RCLDY"
    ]
    assert len(ic_y) == 2 and len(passive_y) == 8
    assert len(set(ic_y)) == 1, "the ICs are not in one row"
    assert len(set(passive_y)) == 1, "eight passives should fill one row"
    assert max(ic_y) < min(passive_y)


def test_every_power_pin_gets_a_power_symbol_and_no_label(blinker):
    """A rail drawn as a dozen repeated labels is what made the sheet look
    hand-made by someone who did not know the tool."""
    spec, _, reparsed = blinker
    libs = _lib_index(reparsed)
    labelled = {label.text for label in reparsed.globalLabels}
    assert not labelled & set(POWER_NETS), f"power nets still labelled: {labelled}"

    endpoints = {c.net: len(c.endpoints) for c in spec.connections}
    for net, kind in POWER_NETS.items():
        symbols = [
            s for s in reparsed.schematicSymbols
            if libs[s.libId].isPower
            and _pin_type(libs[s.libId]) == "power_in"
            and next(p.value for p in s.properties if p.key == "Value") == net
        ]
        assert len(symbols) == endpoints[net], (
            f"{net}: {len(symbols)} power symbols for {endpoints[net]} pins"
        )
        lib = libs[symbols[0].libId]
        pin = next(p for u in lib.units for p in u.pins)
        assert pin.hide and pin.name == net, "not a power port KiCad will honour"
        # Ground hangs below its wire end, a rail points above it.
        ys = [
            _to_sheet(s.position, s.position.angle or 0, 0, p.Y)[1] - s.position.Y
            for s in symbols
            for u in lib.units
            for g in u.graphicItems
            for p in g.points
        ]
        if kind == "ground":
            assert min(ys) >= 0 and max(ys) > 0, f"{net} does not hang down"
        else:
            assert max(ys) <= 0 and min(ys) < 0, f"{net} does not point up"

    signal = set(endpoints) - set(POWER_NETS)
    assert signal <= labelled
    horizontal = {(label.position.angle or 0) for label in reparsed.globalLabels}
    assert horizontal == {0}, f"labels rotated: {horizontal}"


def test_each_power_symbol_sits_on_a_wire_end(blinker):
    """A power symbol a hair off the stub connects nothing, and looks fine."""
    _, _, reparsed = blinker
    libs = _lib_index(reparsed)
    wire_ends = {
        (round(pt.X, 4), round(pt.Y, 4))
        for item in reparsed.graphicalItems
        if getattr(item, "points", None)
        for pt in item.points
    }
    for sym in reparsed.schematicSymbols:
        if libs[sym.libId].isPower:
            at = (round(sym.position.X, 4), round(sym.position.Y, 4))
            assert at in wire_ends, f"{sym.libId} at {at} touches no wire"


def test_one_power_flag_per_power_net_drives_erc(blinker):
    """Every generated power symbol is a ``power_in``; ERC asks what drives it.

    KiCad's answer is one ``PWR_FLAG`` per net, and so is this emitter's.
    Two on one net is an ERC error the other way round.
    """
    _, _, reparsed = blinker
    libs = _lib_index(reparsed)
    flags = [s for s in reparsed.schematicSymbols if _is_flag(libs[s.libId])]
    assert len(flags) == len(POWER_NETS)
    power_at = {
        (s.position.X, s.position.Y): next(
            p.value for p in s.properties if p.key == "Value"
        )
        for s in reparsed.schematicSymbols
        if libs[s.libId].isPower and not _is_flag(libs[s.libId])
    }
    nets = [power_at[(f.position.X, f.position.Y)] for f in flags]
    assert sorted(nets) == sorted(POWER_NETS)


def _is_flag(lib) -> bool:
    return lib.isPower and _pin_type(lib) == "power_out"


def test_reference_sits_above_value_and_neither_is_rotated(blinker):
    _, _, reparsed = blinker
    libs = _lib_index(reparsed)
    for sym in reparsed.schematicSymbols:
        if libs[sym.libId].isPower:
            continue
        fields = {p.key: p for p in sym.properties if p.key in ("Reference", "Value")}
        ref, val = fields["Reference"], fields["Value"]
        assert (ref.position.angle or 0) == 0 and (val.position.angle or 0) == 0
        assert ref.position.Y < val.position.Y, f"{ref.value}: value above reference"
        assert ref.effects.font.height == val.effects.font.height == 1.27


def test_an_unconnected_ic_pin_is_marked_no_connect(blinker):
    """NE555 CTRL is left open by the spec; the sheet must say so, not hide it."""
    spec, sheet, reparsed = blinker
    assert len(reparsed.noConnects) == 1
    nc = reparsed.noConnects[0]
    ic = next(s for s in reparsed.schematicSymbols if s.libId.endswith("NE555D"))
    lib = _lib_index(reparsed)[ic.libId]
    ctrl = next(p for u in lib.units for p in u.pins if p.name == "CTRL")
    expect = _to_sheet(
        ic.position, ic.position.angle or 0, ctrl.position.X, ctrl.position.Y
    )
    assert (round(nc.position.X, 4), round(nc.position.Y, 4)) == (
        round(expect[0], 4), round(expect[1], 4)
    )


def test_a_passive_with_ground_on_pin_one_is_turned_over(tmp_path):
    """Ground hangs down and the rail points up, whichever pin they are on."""
    circuit = {
        "devices": {},
        "passives": {
            "Cx": {"type": "capacitor", "value": "1u"},
            "Rx": {"type": "resistor", "value": "1k"},
        },
        "nets": {"GND": ["Cx.1", "Rx.2"], "VCC": ["Cx.2", "Rx.1"]},
    }
    _, _, reparsed = _emit(circuit, tmp_path)
    libs = _lib_index(reparsed)
    parts = {
        s.libId.split(":")[1]: s
        for s in reparsed.schematicSymbols if not libs[s.libId].isPower
    }
    power = {
        next(p.value for p in s.properties if p.key == "Value"): s
        for s in reparsed.schematicSymbols
        if libs[s.libId].isPower and not _is_flag(libs[s.libId])
    }
    assert (parts["C"].position.angle or 0) == 180
    assert (parts["R"].position.angle or 0) == 0
    # Below on a Y-down sheet is a larger Y.
    assert power["GND"].position.Y > parts["C"].position.Y
    assert power["VCC"].position.Y < parts["C"].position.Y
    assert_well_laid_out(reparsed)


def test_a_circuit_too_big_for_a4_grows_the_page_instead_of_overlapping(tmp_path):
    _, sheet, reparsed = _emit(_big(120), tmp_path)
    assert reparsed.paper.paperSize == "A3"
    assert not sheet.warnings
    assert_well_laid_out(reparsed)


def test_a_circuit_too_big_for_the_largest_page_says_so(tmp_path):
    _, sheet, reparsed = _emit(_big(700), tmp_path)
    assert reparsed.paper.paperSize == "A2"
    assert sheet.warnings and "A2" in sheet.warnings[0]
    w, h = PAPER_MM["A2"]
    below = [s for s in reparsed.schematicSymbols if h < s.position.Y]
    assert below, "overflow was drawn somewhere on the page, i.e. on something"


def test_title_block_is_filled(blinker):
    _, _, reparsed = blinker
    tb = reparsed.titleBlock
    assert tb.title == "blinker"
    assert tb.company == "silkscreen"
    assert tb.revision == "A"
    dt.date.fromisoformat(tb.date)
    assert tb.comments[1] == "blinker.kicad_sch"


def test_title_and_date_are_the_callers_when_given(tmp_path):
    _, _, reparsed = _emit(
        BLINKER, tmp_path, "b", title="a 1 Hz LED blinker", today=dt.date(2026, 9, 3)
    )
    assert reparsed.titleBlock.title == "a 1 Hz LED blinker"
    assert reparsed.titleBlock.date == "2026-09-03"


def test_output_is_byte_identical_across_runs_and_days():
    spec = parse_circuit_spec(BLINKER)
    first = emit_kicad_sch(build_schematic(spec), project_name="b")
    second = emit_kicad_sch(
        build_schematic(parse_circuit_spec(BLINKER)), project_name="b"
    )
    assert first == second
    dated = emit_kicad_sch(
        build_schematic(spec), project_name="b", today=dt.date(2026, 9, 3)
    )
    changed = [
        (a, b) for a, b in zip(first.splitlines(), dated.splitlines(), strict=True)
        if a != b
    ]
    assert changed == [('    (date "2026-01-01")', '    (date "2026-09-03")')]

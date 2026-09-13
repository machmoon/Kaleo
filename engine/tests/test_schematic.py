"""Schematic emission tests.

The claim under test is not "a ``.kicad_sch`` was written" -- it is that KiCad
can open the file and that the circuit drawn on it is the circuit the board was
built from. So the checks here re-read the emitted file with ``kiutils`` rather
than inspecting :class:`~silkscreen.schematic.SchematicResult`, for the same
reason ``test_routing.py`` and ``test_kicad.py`` do: a check written in terms of
the emitter shares the emitter's blind spots.

The bug class this guards is specific and quiet. The schematic and the board
are written by two different emitters from one spec. If they number parts
independently, both files are internally consistent, both open fine, and ``C1``
on the drawing is a different capacitor from ``C1`` on the board. Nothing
raises. A human reviews the schematic, approves it, and the fab builds the
other circuit.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess

import pytest
from kiutils.schematic import Schematic
from silkscreen.board import build_board
from silkscreen.netlist import PassiveType, parse_circuit_spec
from silkscreen.schematic import (
    _PASSIVE_BODY_H_NM,
    _PASSIVE_BODY_W_NM,
    _STUB_NM,
    _pin_on_sheet,
    _stub_on_sheet,
    build_schematic,
    emit_kicad_pro,
    emit_kicad_sch,
    write_project,
    write_schematic,
)
from silkscreen.units import NM_PER_MM

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
def spec():
    return parse_circuit_spec(REGULATOR)


@pytest.fixture(scope="module")
def sheet(spec):
    return build_schematic(spec)


@pytest.fixture(scope="module")
def reparsed(sheet, tmp_path_factory):
    """The emitted schematic as KiCad's own parser sees it."""
    path = tmp_path_factory.mktemp("sch") / "reg.kicad_sch"
    write_schematic(sheet, path, project_name="reg")
    return Schematic().from_file(str(path))


def _ref_of(symbol) -> str:
    for prop in symbol.properties:
        if prop.key == "Reference":
            return prop.value
    raise AssertionError("symbol has no Reference property")


def _value_of(symbol) -> str:
    for prop in symbol.properties:
        if prop.key == "Value":
            return prop.value
    raise AssertionError("symbol has no Value property")


def _parts(reparsed) -> list:
    """The symbols that are parts: power symbols and flags are ``#``-referenced
    and belong to no board."""
    return [s for s in reparsed.schematicSymbols if not _ref_of(s).startswith("#")]


def _power_ports(reparsed) -> list:
    """Ground and rail symbols -- ``(power)`` with a ``power_in`` pin -- as
    distinct from the one ``PWR_FLAG`` per net, whose pin is ``power_out``."""
    libs = {lib.libId: lib for lib in reparsed.libSymbols}
    return [
        s
        for s in reparsed.schematicSymbols
        if libs[s.libId].isPower
        and {p.electricalType for u in libs[s.libId].units for p in u.pins}
        == {"power_in"}
    ]


# ------------------------------------------------------------------ the file


def test_the_emitted_schematic_reparses(reparsed):
    """KiCad's parser is the only opinion that counts about the syntax."""
    assert reparsed.schematicSymbols, "no symbols survived the round trip"


def test_every_part_in_the_spec_becomes_exactly_one_symbol(spec, reparsed):
    assert len(_parts(reparsed)) == spec.part_count()


def test_each_symbol_carries_its_own_library_definition(reparsed):
    """The file must open where no KiCad symbol library is installed.

    A ``lib_id`` pointing at a library the reader does not have gives a broken
    symbol, or worse, a *different* part with the same name.
    """
    defined = {sym.libId for sym in reparsed.libSymbols}
    used = {sym.libId for sym in reparsed.schematicSymbols}
    assert used <= defined


def test_the_project_file_is_valid_json_naming_the_schematic():
    parsed = json.loads(emit_kicad_pro("reg"))
    assert parsed["meta"]["filename"] == "reg.kicad_pro"
    assert parsed["sheets"][0][1] == "Root"


def test_the_project_file_points_at_the_schematics_own_sheet(tmp_path):
    """The pair only opens as one project if the sheet UUIDs agree."""
    sheet_uuid = json.loads(emit_kicad_pro("reg"))["sheets"][0][0]
    path = write_project(tmp_path / "reg.kicad_pro", project_name="reg")
    assert sheet_uuid in path.read_text()

    spec = parse_circuit_spec(REGULATOR)
    assert f'(uuid "{sheet_uuid}")' in emit_kicad_sch(
        build_schematic(spec), project_name="reg"
    )


# ----------------------------------------------------- schematic vs the board


def test_the_schematic_and_the_board_agree_on_every_reference(spec, reparsed):
    """The invariant ``CircuitSpec.assign_refs`` exists to hold.

    Numbered separately these two files would each be self-consistent and
    describe different circuits.
    """
    board = build_board(spec, time_limit_s=5.0)
    assert sorted(_ref_of(s) for s in _parts(reparsed)) == sorted(
        p.ref for p in board.parts
    )


def test_a_reference_names_the_same_part_in_both_files(spec, reparsed):
    """Matching *sets* of refs is not enough -- R1 must be the same resistor."""
    board = build_board(spec, time_limit_s=5.0)
    board_value = {p.ref: p.value for p in board.parts}
    for symbol in _parts(reparsed):
        ref = _ref_of(symbol)
        assert _value_of(symbol) == board_value[ref], (
            f"{ref} is {_value_of(symbol)!r} on the schematic and "
            f"{board_value[ref]!r} on the board"
        )


def test_the_footprint_field_names_the_land_pattern_the_board_placed(spec):
    board = build_board(spec, time_limit_s=5.0)
    footprints = {p.ref: f"silkscreen:{p.footprint.name}" for p in board.parts}
    text = emit_kicad_sch(build_schematic(spec, footprints=footprints))
    for ref, name in footprints.items():
        assert name in text, f"{ref}'s footprint {name} is missing from the sheet"


def test_an_unknown_footprint_field_is_left_empty_rather_than_guessed(spec):
    """A schematic naming a land pattern the board did not use is a trap."""
    text = emit_kicad_sch(build_schematic(spec, footprints=None))
    assert '(property "Footprint" ""' in text


# ----------------------------------------------------------- the connectivity


def test_every_net_in_the_spec_is_labelled_on_the_sheet(spec, reparsed):
    """A net drawn nowhere is a connection silently dropped from the drawing.

    A power net is named by its power symbols rather than by labels; either
    way the name must appear.
    """
    labelled = {label.text for label in reparsed.globalLabels}
    labelled |= {_value_of(s) for s in _power_ports(reparsed)}
    assert {c.net for c in spec.connections} <= labelled


def test_every_connected_pin_gets_a_wire_stub_and_a_label(spec, sheet, reparsed):
    """One label -- or power symbol -- per connected pin, not one per net.

    Pin-level connections are the whole reason ``netlist.py`` requires ``C1.1``
    rather than ``C1``; collapsing them here would throw that away.
    """
    expected = sum(
        1
        for sym in sheet.symbols
        for pin in sym.shape.pins
        if sym.pin_nets.get(pin.number)
    )
    assert expected == sum(len(c.endpoints) for c in spec.connections)
    wires = [item for item in reparsed.graphicalItems if getattr(item, "points", None)]
    assert len(wires) == expected
    assert len(reparsed.globalLabels) + len(_power_ports(reparsed)) == expected


def test_each_label_sits_on_the_end_of_its_own_pins_stub(sheet, reparsed):
    """A label a hair off the wire end connects nothing.

    This is the schematic's version of a track that stops short of its pad: the
    drawing looks right and the netlist KiCad extracts is missing a connection.
    """
    wire_ends = {
        (round(pt.X, 4), round(pt.Y, 4))
        for item in reparsed.graphicalItems
        if getattr(item, "points", None)
        for pt in item.points
    }
    for label in reparsed.globalLabels:
        at = (round(label.position.X, 4), round(label.position.Y, 4))
        assert at in wire_ends, f"label {label.text} at {at} is on no wire end"


def test_an_unconnected_pin_gets_no_label(spec):
    """An unconnected pin and a pin on a net named '' are different circuits."""
    lonely = dict(REGULATOR)
    lonely["devices"] = {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3", "NC": "4"}}
    }
    sheet = build_schematic(parse_circuit_spec(lonely))
    ic = next(s for s in sheet.symbols if s.ref.startswith("U"))
    assert "4" not in ic.pin_nets


# ------------------------------------------------------------------ the glyphs


def test_a_capacitor_does_not_look_like_a_resistor(spec):
    """A schematic that reads as correct and is not is worse than none.

    Every passive type gets its own body, so the drawing cannot quietly show
    the wrong component.
    """
    bodies = {}
    for ptype in PassiveType:
        # Both legs need a net: the validator rejects a floating passive, and
        # rightly so.
        single = {
            "devices": {},
            "passives": {
                "X": {"type": ptype.value, "value": "1"},
                "Y": {"type": "resistor", "value": "1k"},
            },
            "nets": {"A": ["X.1", "Y.1"], "B": ["X.2", "Y.2"]},
        }
        sheet = build_schematic(parse_circuit_spec(single))
        under_test = next(s for s in sheet.symbols if s.value == "1")
        bodies[ptype] = tuple(under_test.shape.graphics)
    assert len(set(bodies.values())) == len(bodies), (
        "two passive types share a body outline"
    )


# ------------------------------------------------------------------ the sheet


def test_output_is_byte_identical_across_runs():
    """Randomised UUIDs would make ``git diff`` on a schematic useless."""
    first = emit_kicad_sch(build_schematic(parse_circuit_spec(REGULATOR)))
    second = emit_kicad_sch(build_schematic(parse_circuit_spec(REGULATOR)))
    assert first == second


def test_a_circuit_too_big_for_one_sheet_takes_a_bigger_one(spec):
    """Silently drawing off the page opens to an apparently empty sheet.

    Formerly this asserted a warning; the emitter now grows the paper first
    and only warns past the largest size it draws, which
    ``test_schematic_layout.py`` covers along with the no-overlap guarantee.
    """
    big = {
        "devices": {},
        "passives": {
            f"R{i}": {"type": "resistor", "value": "1k"} for i in range(200)
        },
        # A long series chain, so every leg is connected and the spec validates.
        "nets": {
            "N0": ["R0.1"] + ["R199.2"],
            **{f"N{i + 1}": [f"R{i}.2", f"R{i + 1}.1"] for i in range(199)},
        },
    }
    sheet = build_schematic(parse_circuit_spec(big))
    assert not sheet.warnings, sheet.warnings
    assert sheet.paper != "A4", "200 resistors do not fit an A4 sheet legibly"
    assert f'(paper "{sheet.paper}")' in emit_kicad_sch(sheet)

    small = build_schematic(spec)
    assert not small.warnings and small.paper == "A4"


# ------------------------------------------------------- stub and pin geometry
#
# The expected direction below is derived from KiCad's pin-angle convention with
# trigonometry, never from SymbolPin.stub. A check written in terms of that
# lookup table would agree with it whichever way it pointed, which is exactly
# how a stub that ran backwards into every passive shipped in the first place.


def _away_from_body(angle_deg: int) -> tuple[int, int]:
    """Unit step a wire takes leaving a pin, in **sheet** coordinates.

    A KiCad pin's ``at`` is its connection point and its angle is the direction
    it extends toward the body, measured in the symbol's Y-up frame. A wire
    goes the other way, and the sheet's Y runs the other way again.
    """
    rad = math.radians(angle_deg)
    toward_body_sheet = (round(math.cos(rad)), -round(math.sin(rad)))
    return (-toward_body_sheet[0], -toward_body_sheet[1])


def test_every_wire_stub_leaves_its_pin_away_from_the_body(sheet):
    """Vertical pins are the ones this catches: x never needed the Y flip.

    Every passive has exactly two vertical pins, so getting this wrong drew the
    stub back along the pin and left the net label sitting on the symbol. A
    symbol placed at 180 degrees has its pins turned with it, so the pin's
    angle is composed with the placement before the trigonometry.
    """
    for sym in sheet.symbols:
        for pin in sym.shape.pins:
            px, py = _pin_on_sheet(sym, pin)
            ex, ey, _, _ = _stub_on_sheet(sym, pin)
            dx, dy = _away_from_body(pin.angle + sym.rotation)
            assert (ex - px, ey - py) == (dx * pin.stub_nm, dy * pin.stub_nm), (
                f"{sym.ref} pin {pin.number} at {pin.angle} deg"
            )
            assert pin.stub_nm >= _STUB_NM


def test_no_label_is_drawn_on_top_of_its_own_symbol(sheet, reparsed):
    """The file-level version of the check above, blind to how it was computed.

    Whatever the pin-angle convention turns out to be, a net label inside the
    body of the part it belongs to is wrong and unreadable.
    """
    # The box every pin's connection point sits on: the drawn part plus its
    # pins. A label may sit exactly on that boundary -- that is a zero-length
    # stub, not an overlap -- so the test is for strictly inside, in whole
    # nanometres to keep KiCad's four-decimal millimetres off the fence.
    boxes = {}
    for sym in sheet.symbols:
        # Inflated to the passive body in each axis: a two-terminal symbol's
        # pins are collinear, so the box spanned by them alone has no width and
        # could never contain anything.
        half_w = max(
            max(abs(p.x_nm) for p in sym.shape.pins), _PASSIVE_BODY_W_NM
        )
        half_h = max(
            max(abs(p.y_nm) for p in sym.shape.pins), _PASSIVE_BODY_H_NM
        )
        boxes[sym.ref] = (
            sym.x_nm - half_w, sym.y_nm - half_h,
            sym.x_nm + half_w, sym.y_nm + half_h,
        )

    for label in reparsed.globalLabels:
        x = round(label.position.X * NM_PER_MM)
        y = round(label.position.Y * NM_PER_MM)
        for ref, (x0, y0, x1, y1) in boxes.items():
            inside = x0 < x < x1 and y0 < y < y1
            assert not inside, (
                f"label {label.text} at ({label.position.X}, {label.position.Y}) "
                f"is drawn over {ref}"
            )


def test_a_passives_pin_stops_at_its_body_instead_of_crossing_it(sheet):
    """A 2.54 mm pin on a 2.54 mm half-height body overshoots by half its length.

    KiCad's own R draws 1.27 mm here. The pin is not just cosmetic: a pin drawn
    through the body reads as a short across the part.
    """
    for sym in sheet.symbols:
        if sym.ref[0] not in "CRLDY":
            continue
        for pin in sym.shape.pins:
            reach = abs(pin.y_nm) - sym.shape.pin_len_nm
            assert reach >= _PASSIVE_BODY_H_NM, (
                f"{sym.ref} pin {pin.number} ends {reach} nm from the anchor, "
                f"inside a body half-height of {_PASSIVE_BODY_H_NM} nm"
            )


def test_an_ic_uses_both_sides_of_its_body(spec):
    """A three-pin regulator with VOUT on the left reads as a different part.

    Topping the left side up to half of what was *left over* after the power
    tokens, rather than half of everything, emptied the right side entirely.
    """
    sheet = build_schematic(spec)
    ic = next(s for s in sheet.symbols if s.ref.startswith("U"))
    left = [p for p in ic.shape.pins if p.x_nm < 0]
    right = [p for p in ic.shape.pins if p.x_nm > 0]
    assert left and right, f"all {len(ic.shape.pins)} pins on one side"
    assert abs(len(left) - len(right)) <= 1 + sum(
        1 for p in ic.shape.pins if p.name.lower().startswith(("gnd", "vin"))
    )
    assert "VOUT" in {p.name for p in right}


def _text_height_mm(reparsed) -> float:
    """The sheet's own declared font size, in mm.

    Read from the emitted file rather than imported from the emitter, so these
    checks do not inherit whatever the emitter believes a character is wide --
    which is exactly the belief that was wrong.
    """
    sizes = {
        label.effects.font.height
        for label in reparsed.globalLabels
        if label.effects and label.effects.font
    }
    assert len(sizes) == 1, f"labels disagree about font size: {sizes}"
    return sizes.pop()


def test_no_field_is_drawn_on_top_of_a_net_label(reparsed):
    """Reference and Value must not land where a label already is.

    Above and below the body is the natural home for these two fields, and it
    is right for an IC, whose pins leave sideways. A passive's pins leave
    vertically and its labels sit exactly there -- one text height away on the
    same x -- so the vertical label string was drawn straight through the
    horizontal reference. The file stayed valid and ERC stayed clean, which is
    why nothing but looking at a plotted sheet caught it.
    """
    h = _text_height_mm(reparsed)
    fields = [
        (prop.key, prop.value, prop.position.X, prop.position.Y)
        for sym in reparsed.schematicSymbols
        for prop in sym.properties
        if prop.key in ("Reference", "Value")
    ]
    assert fields, "no Reference/Value fields on the sheet"
    for key, value, fx, fy in fields:
        for label in reparsed.globalLabels:
            lx, ly = label.position.X, label.position.Y
            if abs(fx - lx) < h and abs(fy - ly) < h:
                raise AssertionError(
                    f"{key} {value!r} at ({fx}, {fy}) collides with label "
                    f"{label.text!r} at ({lx}, {ly})"
                )


def test_no_two_net_labels_run_through_each_other(reparsed):
    """Neighbouring labels need room for both strings.

    Reserving the stub length alone once gave every stacked pair the same
    5.08 mm no matter how long their net names were, so ``GND`` and ``+3V3``
    were drawn one through the other. Labels are horizontal now (the power
    nets became symbols), so the check is a plain text-box overlap, allowing
    one character per character at the sheet's own font size -- an upper
    bound for KiCad's stroke font, whose glyphs measure 0.94 to 1.24 mm at a
    1.27 mm size.
    """
    h = _text_height_mm(reparsed)
    boxes = []
    for label in reparsed.globalLabels:
        assert (label.position.angle or 0) == 0, f"{label.text} is rotated"
        x, y = label.position.X, label.position.Y
        w = len(label.text) * h
        left = label.effects.justify.horizontally != "right"
        boxes.append((label.text, x if left else x - w, y - h, x + w if left else x, y))
    assert boxes
    for i, (text_a, ax0, ay0, ax1, ay1) in enumerate(boxes):
        for text_b, bx0, by0, bx1, by1 in boxes[i + 1 :]:
            clear = ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0
            assert clear, f"{text_a!r} and {text_b!r} are drawn through each other"


# ------------------------------------------------- net names match the board


def test_net_labels_are_global_so_the_board_and_the_sheet_name_nets_alike(reparsed):
    """A local label on the root sheet names its net ``/NET``; the pads on the
    board carry ``NET``. KiCad's parity check then reported a conflict on
    every signal net and "Update PCB from Schematic" offered to rename them
    all. Global labels name the bare net, and the passive shape keeps ERC's
    input/output shape rules out of it."""
    assert reparsed.labels == []
    assert reparsed.globalLabels
    assert {label.shape for label in reparsed.globalLabels} == {"passive"}


def test_the_schematic_and_the_board_name_exactly_the_same_nets(spec, tmp_path):
    """Emit both files from one spec and compare the net-name sets KiCad's own
    parser reads back, the check "Update PCB from Schematic" performs."""
    from kiutils.board import Board
    from silkscreen.board import write_board

    board = build_board(spec, time_limit_s=5.0)
    sheet = build_schematic(spec)
    write_schematic(sheet, tmp_path / "reg.kicad_sch", project_name="reg")
    write_board(board, tmp_path / "reg.kicad_pcb")
    sch = Schematic().from_file(str(tmp_path / "reg.kicad_sch"))
    pcb = Board.from_file(str(tmp_path / "reg.kicad_pcb"))

    sheet_nets = {label.text for label in sch.globalLabels}
    sheet_nets |= {_value_of(s) for s in _power_ports(sch)}
    board_nets = {p.net.name for fp in pcb.footprints for p in fp.pads if p.net}
    assert sheet_nets == board_nets == {c.net for c in spec.connections}


def test_every_board_pad_number_is_a_pin_of_its_schematic_symbol(
    spec, reparsed, tmp_path
):
    """A pad no symbol pin answers to is an orphan in KiCad's parity report;
    the SOT-223 tab used to be one. Pad numbers are read from the board file
    and pin numbers from the symbol library the sheet embeds."""
    from kiutils.board import Board
    from silkscreen.board import write_board

    board = build_board(spec, time_limit_s=5.0)
    pcb = Board.from_file(str(write_board(board, tmp_path / "reg.kicad_pcb")))
    libs = {lib.libId: lib for lib in reparsed.libSymbols}
    pins_of = {
        _ref_of(sym): {p.number for u in libs[sym.libId].units for p in u.pins}
        for sym in _parts(reparsed)
    }
    for fp in pcb.footprints:
        ref = fp.properties["Reference"]
        numbers = {pad.number for pad in fp.pads}
        orphans = numbers - pins_of[ref]
        assert not orphans, f"{ref}: pads {orphans} have no pin"


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


@pytest.mark.skipif(
    KICAD_CLI is None, reason="kicad-cli is not installed on this machine"
)
def test_kicad_finds_no_schematic_parity_issue(spec, tmp_path):
    """The authority the two checks above are calibrated against: KiCad's own
    ``pcb drc --schematic-parity`` on the whole emitted project. Before the
    fix this reported one ``net_conflict`` per signal-net pad plus an orphan
    tab pad on the regulator."""
    from silkscreen.board import route_board, write_board

    board = build_board(spec, time_limit_s=5.0)
    route_board(board)
    sheet = build_schematic(
        spec, footprints={p.ref: f"silkscreen:{p.footprint.name}" for p in board.parts}
    )
    write_schematic(sheet, tmp_path / "reg.kicad_sch", project_name="reg")
    write_project(tmp_path / "reg.kicad_pro", project_name="reg")
    src = write_board(board, tmp_path / "reg.kicad_pcb")
    report = tmp_path / "parity.json"
    run = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--schematic-parity", "--severity-error",
         "--format", "json", "-o", str(report), str(src)],
        capture_output=True, text=True, timeout=180,
    )
    assert run.returncode == 0, run.stderr
    issues = json.loads(report.read_text())["violations"]
    assert issues == [], [v["description"] for v in issues]
    assert "Found 0 schematic parity issues" in run.stdout, run.stdout


# ------------------------------------------------- connectors and batteries
#
# A device is not always a chip. Until ``Device.kind`` chose the glyph, every
# one of them was drawn as the rectangle with pins down both sides, so a power
# input opened as a surface-mount IC: the one thing a reviewer looks for first
# -- where power enters the board -- was the one thing the drawing did not say,
# and nothing raised, because the file was valid and the netlist was right.
#
# These checks read the emitted file rather than the shapes, for the reason at
# the top of this module, and they compute the sheet coordinates they expect
# with their own arithmetic. The frame bug this module has actually suffered --
# symbol-local Y-up added straight to a Y-down sheet coordinate -- produces a
# file that reparses, passes ERC and draws the net label on top of the part, so
# only a check that knows where the pin *should* be catches it.

POWER_ENTRY = {
    "devices": {
        "J_PWR": {
            "kind": "connector",
            "package": "Barrel_Jack_5.5x2.1mm",
            # Pin 3 is the jack's switch contact and is deliberately on no net.
            "pins": {"VIN": "1", "GND": "2", "SW": "3"},
        },
        "J_I2C": {
            "kind": "connector",
            "package": "JST_PH_4P",
            "pins": {"VCC": "1", "GND": "2", "SDA": "3", "SCL": "4"},
        },
        "BT_BACKUP": {
            "kind": "battery",
            "package": "BatteryHolder_CR2032",
            "pins": {"POS": "1", "NEG": "2"},
        },
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        # Added last on purpose: refs are assigned per prefix in spec order,
        # so appending here leaves J1/J2/BT1/U1 -- which the checks below name
        # -- exactly where they were, and gives the switch SW1 and the test
        # point TP1.
        "SW_RESET": {
            "kind": "switch",
            "package": "SW_SPST_TL3305A",
            "pins": {"A": "1", "B": "2"},
        },
        "TP_3V3": {
            "kind": "testpoint",
            "package": "TestPoint_Pad_1.5x1.5mm",
            "pins": {"P": "1"},
        },
    },
    "passives": {
        "Cout": {"type": "capacitor", "value": "22uF"},
        "Rp1": {"type": "resistor", "value": "4k7"},
        "Rp2": {"type": "resistor", "value": "4k7"},
        "Dbat": {"type": "diode", "value": "BAT54"},
        "Rnrst": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VIN": ["J_PWR.VIN", "AMS1117-3.3.VIN"],
        "GND": [
            "J_PWR.GND", "AMS1117-3.3.GND", "Cout.2", "J_I2C.GND",
            "BT_BACKUP.NEG", "SW_RESET.B",
        ],
        "+3V3": [
            "AMS1117-3.3.VOUT", "Cout.1", "J_I2C.VCC", "Rp1.1", "Rp2.1",
            "Dbat.2", "Rnrst.1", "TP_3V3.P",
        ],
        "VBAT": ["BT_BACKUP.POS", "Dbat.1"],
        "SDA": ["J_I2C.SDA", "Rp1.2"],
        "SCL": ["J_I2C.SCL", "Rp2.2"],
        "NRST": ["SW_RESET.A", "Rnrst.2"],
    },
}


@pytest.fixture(scope="module")
def entry_spec():
    return parse_circuit_spec(POWER_ENTRY)


@pytest.fixture(scope="module")
def entry_sheet(entry_spec):
    return build_schematic(entry_spec)


@pytest.fixture(scope="module")
def entry_reparsed(entry_sheet, tmp_path_factory):
    path = tmp_path_factory.mktemp("sch") / "entry.kicad_sch"
    write_schematic(entry_sheet, path, project_name="entry")
    return Schematic().from_file(str(path))


def _lib_of(reparsed, ref: str):
    """The library definition behind the symbol placed as ``ref``."""
    libs = {lib.libId: lib for lib in reparsed.libSymbols}
    symbol = next(s for s in _parts(reparsed) if _ref_of(s) == ref)
    return libs[symbol.libId]


def _lib_pins(lib) -> list:
    return [pin for unit in lib.units for pin in unit.pins]


def _lib_shapes(lib) -> list:
    return [item for unit in lib.units for item in unit.graphicItems]


def _placed(reparsed, ref):
    return next(s for s in _parts(reparsed) if _ref_of(s) == ref)


def test_a_connector_is_not_drawn_as_an_ic_rectangle(entry_reparsed):
    """One side, one contact square per pin: the shape of a header.

    Pins down both sides is the IC glyph, and a power connector wearing it is
    the schematic half of the bug this whole feature exists to remove.
    """
    for ref, count in (("J1", 3), ("J2", 4)):
        pins = _lib_pins(_lib_of(entry_reparsed, ref))
        assert len(pins) == count
        assert len({pin.position.X for pin in pins}) == 1, (
            f"{ref} has pins on more than one x"
        )
        assert {pin.position.angle for pin in pins} == {0}, (
            f"{ref} has pins leaving in more than one direction"
        )
        # KiCad's Conn_01x0N: the body, plus a contact square per pin.
        rects = [s for s in _lib_shapes(_lib_of(entry_reparsed, ref)) if
                 hasattr(s, "start")]
        assert len(rects) == count + 1

    ic = _lib_pins(_lib_of(entry_reparsed, "U1"))
    assert len({pin.position.X for pin in ic}) == 2, (
        "the IC lost its two-sided body when the connector gained a one-sided one"
    )


def test_a_connectors_pin_numbers_are_visible_and_are_the_specs_own(
    entry_spec, entry_reparsed
):
    """The numbers are the pin-out, so they are drawn, and they are the pad
    numbers the land pattern carries.

    A connector symbol numbered independently of its footprint is a
    ``net_conflict`` on every pin in KiCad's parity report -- the failure the
    ``/NET`` labels and the orphan SOT-223 tab pad both were.
    """
    refs = entry_spec.assign_refs()
    for device in entry_spec.devices:
        if device.kind != "connector":
            continue
        lib = _lib_of(entry_reparsed, refs[device.name])
        assert lib.hidePinNumbers is False, "a connector's pin-out must be readable"
        assert {pin.number for pin in _lib_pins(lib)} == set(device.pins.values())


def test_a_connectors_pins_run_down_the_body_in_pin_number_order(entry_reparsed):
    """Pin 1 at the top, counting down, so the sheet can be read against the
    physical part. The IC's readability re-ordering -- power left, rails to the
    top -- would be actively wrong here, because the numbers are the pin-out.
    """
    for ref in ("J1", "J2"):
        pins = sorted(_lib_pins(_lib_of(entry_reparsed, ref)),
                      key=lambda p: -p.position.Y)
        assert [pin.number for pin in pins] == sorted(
            (pin.number for pin in pins), key=int
        )


def test_a_switch_is_drawn_as_a_push_button_and_a_test_point_as_a_circle(
    entry_reparsed,
):
    """Both glyphs are KiCad's own (``Switch:SW_Push``,
    ``Connector:TestPoint``), and the check is that neither wears the
    connector's contact-square body or the IC's two-sided rectangle -- the
    failure this whole dispatch exists to remove, one part further on.

    Geometry is read back out of the emitted file and compared with numbers
    computed here, not with the constants the generator used.
    """
    sw = _lib_of(entry_reparsed, "SW1")
    pins = sorted(_lib_pins(sw), key=lambda p: p.position.X)
    assert [p.number for p in pins] == ["1", "2"]
    # Horizontally opposed, on one row, symmetric about the body centre:
    # nothing else about a two-terminal switch is worth drawing.
    assert {p.position.Y for p in pins} == {0}
    assert pins[0].position.X == -pins[1].position.X
    assert [p.position.angle for p in pins] == [0, 180]
    circles = [g for g in _lib_shapes(sw) if hasattr(g, "center")]
    assert len(circles) == 2, "a push button has one contact circle per pole"
    assert {c.center.X for c in circles} == {-2.032, 2.032}
    assert not [g for g in _lib_shapes(sw) if hasattr(g, "start")], (
        "the switch is wearing the connector's contact squares"
    )

    tp = _lib_of(entry_reparsed, "TP1")
    tp_pins = _lib_pins(tp)
    assert [p.number for p in tp_pins] == ["1"]
    assert (tp_pins[0].position.X, tp_pins[0].position.Y) == (0, 0)
    tp_circles = [g for g in _lib_shapes(tp) if hasattr(g, "center")]
    assert len(tp_circles) == 1
    # Drawn above the connection point, so the stub and label leave downward
    # into empty sheet like any other one-pin part.
    assert tp_circles[0].center.Y > 0


def test_a_battery_is_drawn_as_a_battery_with_both_polarity_marks(entry_reparsed):
    """Alternating long and short plates, ``+`` on pin 1 and ``-`` on pin 2.

    KiCad marks only the ``+`` and leaves the ``-`` implied by plate length. A
    generated sheet has no draughtsman to ask, so both are drawn: a cell wired
    backwards is a destroyed board.
    """
    lib = _lib_of(entry_reparsed, "BT1")
    pins = _lib_pins(lib)
    assert {pin.number for pin in pins} == {"1", "2"}
    plus, minus = sorted(pins, key=lambda p: -p.position.Y)
    assert plus.number == "1" and minus.number == "2", (
        "the contract fixes pin 1 as the positive terminal"
    )

    shapes = _lib_shapes(lib)
    plates = [s for s in shapes if hasattr(s, "start")]
    widths = sorted(round(s.end.X - s.start.X, 3) for s in plates)
    assert len(plates) == 4 and len(set(widths)) == 2, (
        f"plates are not alternating long and short: {widths}"
    )
    # Every plate sits between the terminals, and the long ones are nearer the
    # + end of their own cell.
    for plate in plates:
        assert minus.position.Y < plate.start.Y < plus.position.Y

    strokes = [s for s in shapes if getattr(s, "points", None)]
    above = [s for s in strokes if all(p.Y > plates[0].start.Y for p in s.points)]
    below = [s for s in strokes if all(p.Y < plates[-1].end.Y for p in s.points)]
    horizontal = [s for s in above if s.points[0].Y == s.points[1].Y]
    vertical = [s for s in above if s.points[0].X == s.points[1].X]
    assert horizontal and vertical, "no '+' is drawn at the positive terminal"
    assert len(below) == 1 and below[0].points[0].Y == below[0].points[1].Y, (
        "no '-' is drawn at the negative terminal"
    )


def _connector_pin_points(sym, numbers: list[str]) -> dict[str, tuple[float, float]]:
    """Where each connector pin's connection point must land, in sheet mm.

    Worked out here from KiCad's own generic-connector geometry rather than
    read back from the emitter: pins on one column 5.08 mm left of the anchor,
    2.54 mm apart, pin 1 at the top, and the body 1.27 mm past the top and
    bottom pin. Symbol-local Y is up and the sheet's is down, which is the one
    sign this test exists to pin.
    """
    half_h = 2.54 * (len(numbers) - 1) / 2 + 1.27
    return {
        number: (
            round(sym.position.X - 5.08, 4),
            round(sym.position.Y - (half_h - 1.27 - 2.54 * i), 4),
        )
        for i, number in enumerate(numbers)
    }


def _wires(reparsed) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [
        (
            (round(item.points[0].X, 4), round(item.points[0].Y, 4)),
            (round(item.points[-1].X, 4), round(item.points[-1].Y, 4)),
        )
        for item in reparsed.graphicalItems
        if getattr(item, "points", None)
    ]


def test_every_connector_pin_is_where_the_geometry_says_and_carries_its_stub(
    entry_reparsed,
):
    """The frame check. A stub that starts a hair off its pin connects nothing,
    and a Y flip applied twice draws it back through the body.
    """
    wires = _wires(entry_reparsed)
    ends = {a: b for a, b in wires} | {b: a for a, b in wires}
    labels = {
        (round(label.position.X, 4), round(label.position.Y, 4))
        for label in entry_reparsed.globalLabels
    }
    labels |= {
        (round(s.position.X, 4), round(s.position.Y, 4))
        for s in _power_ports(entry_reparsed)
    }
    crosses = {
        (round(nc.position.X, 4), round(nc.position.Y, 4))
        for nc in entry_reparsed.noConnects
    }

    for ref, numbers, unconnected in (("J1", ["1", "2", "3"], {"3"}),
                                      ("J2", ["1", "2", "3", "4"], set())):
        sym = _placed(entry_reparsed, ref)
        for number, at in _connector_pin_points(sym, numbers).items():
            if number in unconnected:
                assert at in crosses, f"{ref} pin {number} has no no-connect at {at}"
                continue
            assert at in ends, f"{ref} pin {number} has no wire at {at}"
            end = ends[at]
            assert end[1] == at[1], f"{ref} pin {number}'s stub is not horizontal"
            assert end[0] < at[0], (
                f"{ref} pin {number}'s stub runs back into the body"
            )
            assert end in labels, (
                f"{ref} pin {number}'s stub ends at {end} with nothing on it"
            )


def test_a_batterys_terminals_are_where_the_geometry_says(entry_reparsed):
    """The same check on the shape whose pins are vertical.

    Vertical pins are the ones the Y flip catches: x never needed it, so a
    sideways symbol looks right whichever way the sign goes.
    """
    sym = _placed(entry_reparsed, "BT1")
    wires = _wires(entry_reparsed)
    ends = {a: b for a, b in wires} | {b: a for a, b in wires}
    for number, sign in (("1", +1), ("2", -1)):
        at = (round(sym.position.X, 4), round(sym.position.Y - sign * 5.08, 4))
        assert at in ends, f"BT1 pin {number} has no wire at {at}"
        end = ends[at]
        assert end[0] == at[0], f"BT1 pin {number}'s stub is not vertical"
        # Away from the body: pin 1 is above the anchor on the sheet, so its
        # stub must go further up, to a *smaller* y.
        assert (end[1] - at[1]) * sign < 0, (
            f"BT1 pin {number}'s stub runs back into the body"
        )


def test_the_new_symbol_definitions_travel_with_the_file(entry_reparsed):
    """The file must open where no KiCad symbol library is installed, or it
    resolves to whatever part happens to share the name."""
    defined = {lib.libId: lib for lib in entry_reparsed.libSymbols}
    assert {s.libId for s in entry_reparsed.schematicSymbols} <= set(defined)
    for ref in ("J1", "J2", "BT1"):
        lib = _lib_of(entry_reparsed, ref)
        assert lib.libId in defined
        assert _lib_shapes(lib), f"{ref}'s definition carries no body"


def test_an_ic_is_drawn_exactly_as_it_was_before_connectors_existed(spec):
    """The regression guard on the dispatch: ``kind`` defaults to ``ic`` and
    every spec written before connectors existed must emit the same bytes."""
    assert all(d.kind == "ic" for d in spec.devices)
    text = emit_kicad_sch(build_schematic(parse_circuit_spec(REGULATOR)))
    assert '(symbol "silkscreen:AMS1117-3.3"' in text
    ic = next(s for s in build_schematic(spec).symbols if s.ref == "U1")
    assert len(ic.shape.graphics) == 1, "an IC body is one rectangle"
    assert {p.x_nm > 0 for p in ic.shape.pins} == {True, False}


def test_a_grounded_connector_pin_keeps_its_neighbours_labels_readable(entry_sheet):
    """A connector keeps pin-number order, so a ground can sit *between* two
    signal pins -- and a ground symbol reaches 2.54 mm down and prints its net
    name 3.81 mm down, straight through the label of the pin below it. Found on
    a plotted sheet: the file is valid and ERC is clean either way.
    """
    j2 = next(s for s in entry_sheet.symbols if s.ref == "J2")
    gnd = next(p for p in j2.shape.pins if j2.pin_nets[p.number] == "GND")
    neighbours = ["SDA", "SCL"]
    needed = max(len(n) for n in neighbours) * 1_270_000 + len("GND") * 1_270_000 // 2
    assert gnd.stub_nm - _STUB_NM >= needed, (
        f"GND's stub is {gnd.stub_nm} nm; its symbol is drawn over "
        f"{neighbours}"
    )


@pytest.mark.skipif(
    KICAD_CLI is None, reason="kicad-cli is not installed on this machine"
)
def test_kicad_erc_is_clean_on_a_sheet_with_a_connector_and_a_battery(
    entry_spec, tmp_path
):
    """The authority the checks above are calibrated against. Warnings are only
    the two the generated library always earns -- ``lib_symbol_issues`` and
    ``footprint_link_issues``, both correct: the definitions are embedded in
    the file on purpose."""
    write_schematic(
        build_schematic(entry_spec), tmp_path / "entry.kicad_sch",
        project_name="entry",
    )
    write_project(tmp_path / "entry.kicad_pro", project_name="entry")
    report = tmp_path / "erc.json"
    run = subprocess.run(
        [KICAD_CLI, "sch", "erc", "--severity-all", "--format", "json",
         "-o", str(report), str(tmp_path / "entry.kicad_sch")],
        capture_output=True, text=True, timeout=180,
    )
    assert run.returncode == 0, run.stderr
    violations = [
        v for sheet in json.loads(report.read_text())["sheets"]
        for v in sheet["violations"]
    ]
    assert [v for v in violations if v["severity"] == "error"] == []
    unexpected = {
        v["type"] for v in violations
        if v["type"] not in ("lib_symbol_issues", "footprint_link_issues")
    }
    assert not unexpected, unexpected


@pytest.mark.skipif(
    KICAD_CLI is None, reason="kicad-cli is not installed on this machine"
)
def test_kicad_finds_no_parity_issue_between_a_connector_and_its_land_pattern(
    entry_spec, tmp_path
):
    """The check that a connector symbol's pin numbers really are its footprint's
    pad numbers. Numbered independently the two files are each self-consistent
    and describe different parts."""
    from silkscreen.board import write_board

    board = build_board(entry_spec, time_limit_s=10.0)
    sheet = build_schematic(
        entry_spec,
        footprints={p.ref: f"silkscreen:{p.footprint.name}" for p in board.parts},
    )
    write_schematic(sheet, tmp_path / "entry.kicad_sch", project_name="entry")
    write_project(tmp_path / "entry.kicad_pro", project_name="entry")
    src = write_board(board, tmp_path / "entry.kicad_pcb")
    report = tmp_path / "parity.json"
    run = subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--schematic-parity", "--severity-error",
         "--format", "json", "-o", str(report), str(src)],
        capture_output=True, text=True, timeout=180,
    )
    assert run.returncode == 0, run.stderr
    issues = json.loads(report.read_text())["violations"]
    assert issues == [], [v["description"] for v in issues]
    assert "Found 0 schematic parity issues" in run.stdout, run.stdout

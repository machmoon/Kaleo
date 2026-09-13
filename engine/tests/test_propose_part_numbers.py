"""The part-number rule, and the repair loop it feeds.

Written after ``scripts/design_quality.py`` measured the defect it exists to
catch: 18 proposals out of 18 named their ICs ``U1``. Offline throughout --
``ScriptedModel``, no key, no network, the suite's own rule.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.agents.propose import (
    part_number_errors,
    propose_circuit,
)
from silkscreen.netlist import parse_circuit_spec


# A three-pin regulator board with one thing wrong with it: the key.
def _board(ic_name: str) -> dict:
    return {
        "devices": {
            ic_name: {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
            "J1": {
                "kind": "connector",
                "package": "TerminalBlock_2P_5.08mm",
                "pins": {"1": "1", "2": "2"},
            },
        },
        "passives": {
            "c_in": {"type": "capacitor", "value": "10uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": [f"{ic_name}.VIN", "c_in.1", "J1.1"],
            "GND": [f"{ic_name}.GND", "c_in.2", "c_out.2", "J1.2"],
            "+3V3": [f"{ic_name}.VOUT", "c_out.1"],
        },
    }


@pytest.mark.parametrize(
    "name",
    ["U1", "U", "IC1", "ic2", "REG", "VR1", "Q3", "X1", "U_1", "A12"],
)
def test_a_reference_designator_is_not_a_part_number(name: str) -> None:
    errors = part_number_errors(parse_circuit_spec(_board(name)))
    assert len(errors) == 1
    assert name in errors[0]
    # instructor's Validator carries a fixed_value alongside the reason; the
    # message must say what to write instead, not only what is wrong.
    assert "AMS1117-3.3" in errors[0]


@pytest.mark.parametrize(
    "name", ["opamp", "REGULATOR", "timer", "MCU", "Driver", "chip"]
)
def test_a_category_word_is_not_a_part_number(name: str) -> None:
    errors = part_number_errors(parse_circuit_spec(_board(name)))
    assert len(errors) == 1 and name in errors[0]


def test_a_name_with_no_digits_is_refused() -> None:
    errors = part_number_errors(parse_circuit_spec(_board("BIGCHIP")))
    assert len(errors) == 1
    assert "no digits" in errors[0]


@pytest.mark.parametrize(
    "name",
    [
        "AMS1117-3.3",
        "NE555D",
        "LM358D",
        "TL072",
        "ATtiny85-20SU",
        "A4988",
        "ICL7660",
        "X9C103",
        "MCP6002",
    ],
)
def test_a_real_part_number_passes(name: str) -> None:
    """The rule must not reject the parts the emitter is meant to build.

    ``A4988``, ``ICL7660`` and ``X9C103`` are the near-collisions the
    designator pattern is deliberately narrow enough to let through.
    """
    assert part_number_errors(parse_circuit_spec(_board(name))) == []


def test_a_part_number_starting_with_a_digit_cannot_be_expressed_at_all() -> None:
    """A pre-existing IR limit, pinned here because this rule sits next to it.

    ``netlist.CircuitSpec.validate`` requires a part name to be a valid Python
    identifier, so ``74HC595`` -- and every other 74-series logic part, the
    most common family there is -- cannot be named in this IR. That is not
    something ``part_number_errors`` can fix, and it is recorded rather than
    worked around: a rule that told the model to write a part number the IR
    then refuses would be a repair loop that cannot converge.
    """
    from silkscreen.netlist import ValidationError

    with pytest.raises(ValidationError, match="not a valid identifier"):
        parse_circuit_spec(_board("74HC595"))


def test_only_ics_are_checked() -> None:
    """A connector is identified by its package, so ``J1`` is its right name.

    Rejecting it would contradict rule 9 and ``board._footprint_for_device``,
    which chooses a connector's land pattern by ``package`` and never by name.
    """
    spec = parse_circuit_spec(_board("AMS1117-3.3"))
    assert any(d.kind == "connector" and d.name == "J1" for d in spec.devices)
    assert part_number_errors(spec) == []


def test_the_repair_loop_sends_the_rule_back_and_accepts_the_fix() -> None:
    """The measured failure, end to end: ``U1`` in, part number out."""
    model = ScriptedModel(
        responses=[json.dumps(_board("U1")), json.dumps(_board("AMS1117-3.3"))]
    )
    spec, attempts = propose_circuit(model, "a 3.3 V supply")
    assert [d.name for d in spec.devices if d.kind == "ic"] == ["AMS1117-3.3"]
    assert len(attempts) == 2 and not attempts[0].accepted
    assert any("part number" in e for e in attempts[0].errors)
    # Batched into the one repair prompt, next to every other problem.
    repair = model.calls[1]["prompt"]
    assert "rejected" in repair and "U1" in repair


def test_the_rule_is_batched_with_the_package_rule() -> None:
    """Two rules, one round -- the netlist.py convention past the IR's edge."""
    board = _board("U1")
    # Nine pins: no land pattern rule covers it, so board.package_errors
    # objects too. Both lists must reach the model in one round.
    board["devices"]["U1"]["pins"] = {f"P{n}": str(n) for n in range(1, 10)}
    board["nets"]["VIN"] = ["U1.P3", "c_in.1", "J1.1"]
    board["nets"]["GND"] = ["U1.P1", "c_in.2", "c_out.2", "J1.2"]
    board["nets"]["+3V3"] = ["U1.P2", "c_out.1"]
    model = ScriptedModel(
        responses=[json.dumps(board), json.dumps(_board("AMS1117-3.3"))]
    )
    _, attempts = propose_circuit(model, "a 3.3 V supply")
    errors = attempts[0].errors
    assert any("part number" in e for e in errors), errors
    assert any("9" in e and "package" in e.lower() for e in errors), errors


def test_the_prompt_states_the_rule() -> None:
    from silkscreen.agents.propose import PROPOSE_PROMPT

    assert "MANUFACTURER PART NUMBER" in PROPOSE_PROMPT
    assert "designator" in PROPOSE_PROMPT

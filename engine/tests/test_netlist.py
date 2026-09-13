"""Tests for the validated circuit IR.

Most of these pin down failures the original pipeline could not detect: it fed
raw model JSON straight into SKiDL, so a bad pin name or a half-connected
capacitor became a corrupt netlist rather than an error.
"""

from __future__ import annotations

import json

import pytest
from silkscreen import (
    CircuitSpec,
    Connection,
    Device,
    Passive,
    PassiveType,
    ValidationError,
    parse_circuit_spec,
)


def _good_spec_dict():
    return {
        "devices": {
            "U1": {
                "symbol": "MCU_ST_STM32F0:STM32F030C8Tx",
                "pins": {"VDD": "1", "VSS": "8", "NRST": "7"},
            }
        },
        "passives": {
            "C1": {"type": "capacitor", "value": "100nF"},
            "R1": {"type": "resistor", "value": "10k"},
        },
        "nets": {
            "VDD": ["U1.VDD", "C1.1", "R1.1"],
            "GND": ["U1.VSS", "C1.2"],
            "NRST": ["U1.NRST", "R1.2"],
        },
    }


# ---------------------------------------------------------------- happy path


def test_parses_a_valid_spec():
    spec = parse_circuit_spec(_good_spec_dict())
    assert spec.part_count() == 3
    assert spec.net_count() == 3
    assert spec.devices[0].symbol == "MCU_ST_STM32F0:STM32F030C8Tx"
    assert spec.passives[0].type is PassiveType.CAPACITOR


def test_accepts_raw_json_text():
    spec = parse_circuit_spec(json.dumps(_good_spec_dict()))
    assert spec.part_count() == 3


def test_tolerates_a_markdown_code_fence():
    """The original prompt's own example was fenced, and json.loads died on it."""
    fenced = "```json\n" + json.dumps(_good_spec_dict()) + "\n```"
    spec = parse_circuit_spec(fenced)
    assert spec.part_count() == 3


def test_passive_ref_prefixes_match_kicad_convention():
    spec = parse_circuit_spec(_good_spec_dict())
    prefixes = {p.name: p.ref_prefix for p in spec.passives}
    assert prefixes == {"C1": "C", "R1": "R"}


# ---------------------------------------------------------------- rejections


def test_rejects_non_json():
    with pytest.raises(ValidationError, match="not valid JSON"):
        parse_circuit_spec("I'm sorry, I can't help with that.")


def test_rejects_unknown_passive_type():
    data = _good_spec_dict()
    data["passives"]["FB1"] = {"type": "ferrite_bead", "value": "600R"}
    with pytest.raises(ValidationError, match="unsupported type"):
        parse_circuit_spec(data)


def test_rejects_endpoint_referring_to_a_nonexistent_pin():
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.AVDD", "C1.1"]  # U1 has no AVDD
    with pytest.raises(ValidationError, match="has no pin named 'AVDD'"):
        parse_circuit_spec(data)


def test_rejects_endpoint_referring_to_an_unknown_part():
    data = _good_spec_dict()
    data["nets"]["VDD"].append("U99.VDD")
    with pytest.raises(ValidationError, match="unknown part 'U99'"):
        parse_circuit_spec(data)


def test_rejects_bare_part_name_as_endpoint():
    """Regression: the original could only connect whole parts to nets.

    That is exactly why it could not express 'cap leg 1 to VDD, leg 2 to GND'.
    Requiring an explicit terminal makes the failure loud instead of silent.
    """
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.VDD", "C1"]
    with pytest.raises(ValidationError, match="must be '<part>.<pin>'"):
        parse_circuit_spec(data)


def test_rejects_passive_pin_outside_1_and_2():
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.VDD", "C1.3"]
    with pytest.raises(ValidationError, match="only .*pins 1 and 2"):
        parse_circuit_spec(data)


def test_rejects_floating_passive():
    """A capacitor wired on one leg is a model error, not a valid circuit."""
    data = _good_spec_dict()
    data["nets"]["GND"] = ["U1.VSS", "R1.2"]  # C1.2 now unconnected
    with pytest.raises(ValidationError, match=r"C1.*no connection on pin\(s\) \['2'\]"):
        parse_circuit_spec(data)


def test_rejects_single_endpoint_net():
    data = _good_spec_dict()
    data["nets"]["DANGLING"] = ["U1.NRST"]
    with pytest.raises(ValidationError, match="fewer than 2 pins"):
        parse_circuit_spec(data)


def test_rejects_name_used_as_both_device_and_passive():
    data = _good_spec_dict()
    data["passives"]["U1"] = {"type": "resistor", "value": "1k"}
    with pytest.raises(ValidationError, match="both a device and a passive"):
        parse_circuit_spec(data)


def test_collects_every_error_at_once():
    """A repair prompt should get all problems in one pass, not one per round."""
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.NOPE", "C1", "U99.X"]
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert len(exc.value.errors) >= 3


def test_error_message_lists_every_problem():
    spec = CircuitSpec(
        devices=[Device(name="U1", pins={"VDD": "1"})],
        passives=[Passive(name="C1", type=PassiveType.CAPACITOR, value="1u")],
        connections=[Connection(net="N1", endpoints=("U1.VDD", "C1.1"))],
    )
    with pytest.raises(ValidationError) as exc:
        spec.validate()
    # C1.2 is floating.
    assert any("C1" in e for e in exc.value.errors)


def test_rejects_a_pin_that_joins_two_nets():
    """One pin, one net. Two nets sharing a pin are electrically one net.

    Nothing downstream raises on this: CircuitSpec.nets_of keeps the last net
    it sees, the schematic labels the pin one way, and the board's pad-to-net
    map can resolve it another. Two self-consistent files, different circuits.
    """
    data = _good_spec_dict()
    data["nets"]["VDD_ALT"] = ["U1.VDD", "R1.2"]  # U1.VDD is already on VDD
    with pytest.raises(ValidationError, match=r"pin 'U1\.VDD' is on 2 nets"):
        parse_circuit_spec(data)


def test_a_pin_repeated_inside_one_net_is_not_an_error():
    """Redundant, not ambiguous: there is still only one net on that pin."""
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.VDD", "U1.VDD", "C1.1", "R1.1"]
    assert parse_circuit_spec(data).net_count() == 3


def test_rejects_two_pin_names_on_one_pin_number():
    """The number is what reaches the footprint and the symbol.

    A second name on the same number silently overwrites the first, so one
    specified connection disappears from both emitted files without raising.
    """
    data = _good_spec_dict()
    data["devices"]["U1"]["pins"]["VDDA"] = "1"  # VDD is already pin 1
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    # Exactly this rule, and only this rule: an unconnected device pin is not
    # itself an error, so nothing else here should fire.
    assert [e for e in exc.value.errors if "pin number '1'" in e]
    assert len(exc.value.errors) == 1


def test_a_pin_number_has_one_spelling():
    """``"01"`` and ``"1"`` are the same physical pin, so they parse the same.

    A model that reads a pin number off two different lines of a datasheet
    writes it two ways, and the two consumers disagreed about which one was
    real: the schematic keyed its nets on whatever string it was handed, while
    the board looked the pad up by exact number, found nothing, and emitted the
    pad with no net at all. Neither raised.
    """
    data = _good_spec_dict()
    data["devices"]["U1"]["pins"]["VDD"] = " 01 "
    spec = parse_circuit_spec(data)
    device = next(d for d in spec.devices if d.name == "U1")
    assert device.pins["VDD"] == "1"


def test_a_non_numeric_pin_number_is_left_alone():
    """BGA numbers are ``"A1"``, not integers, and rewriting them would lie."""
    data = _good_spec_dict()
    data["devices"]["U1"]["pins"]["VDD"] = "A1"
    spec = parse_circuit_spec(data)
    device = next(d for d in spec.devices if d.name == "U1")
    assert device.pins["VDD"] == "A1"


def test_two_spellings_of_one_pin_number_are_still_two_names_on_one_pin():
    """The alias must not be a way around the duplicate-number rule.

    This is the case that made the normalisation worth doing: without it the
    exact-string comparison sees ``"1"`` and ``"01"`` as different pins and
    lets the spec through, and the ambiguity resurfaces as a missing net.
    """
    data = _good_spec_dict()
    data["devices"]["U1"]["pins"]["VDDA"] = "01"  # VDD is already pin "1"
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert [e for e in exc.value.errors if "pin number '1'" in e]


# --- Device kind and package -------------------------------------------------
#
# Before `kind` existed every device was an IC, so a two-pin power connector
# fell through the board's pin-count rule and was drawn as a SOIC-4. These pin
# down the IR half of the fix: the vocabulary, the defaulting that keeps every
# older spec valid, the per-prefix numbering, and -- the one that matters most
# -- that every one of the new failures joins the single batched
# ValidationError rather than being raised on its own.


def _connector_spec_dict():
    """A device and a two-pin JST battery connector, wired to each other."""
    return {
        "devices": {
            "U1": {"pins": {"VDD": "1", "VSS": "2"}},
            "PWR": {
                "kind": "connector",
                "package": "JST_PH_2P",
                "pins": {"VBAT": "1", "GND": "2"},
            },
        },
        "nets": {
            "VCC": ["U1.VDD", "PWR.VBAT"],
            "GND": ["U1.VSS", "PWR.GND"],
        },
    }


def test_a_device_with_no_kind_is_an_ic():
    """Every spec written before connectors existed must stay valid."""
    spec = parse_circuit_spec(_good_spec_dict())
    assert all(d.kind == "ic" for d in spec.devices)
    assert all(d.package is None for d in spec.devices)


def test_refs_are_numbered_per_prefix_not_per_device():
    """U, J and BT each count from 1, in spec order.

    ``assign_refs`` is the only reason the schematic and the board agree about
    what a part is called, so the numbering is asserted here rather than left
    to whichever emitter happens to run first.
    """
    data = {
        "devices": {
            "MCU": {"pins": {"VDD": "1", "VSS": "2"}},
            "PWR": {
                "kind": "connector",
                "package": "JST_PH_2P",
                "pins": {"P": "1", "N": "2"},
            },
            "IO": {
                "kind": "connector",
                "package": "PinHeader_1x02_P2.54mm",
                "pins": {"A": "1", "B": "2"},
            },
            "CELL": {
                "kind": "battery",
                "package": "BatteryHolder_CR2032",
                "pins": {"P": "1", "N": "2"},
            },
            "REG": {"pins": {"IN": "1", "OUT": "2"}},
        },
        "nets": {
            "VCC": ["MCU.VDD", "PWR.P", "IO.A", "CELL.P", "REG.IN"],
            "GND": ["MCU.VSS", "PWR.N", "IO.B", "CELL.N", "REG.OUT"],
        },
    }
    refs = parse_circuit_spec(data).assign_refs()
    assert refs == {
        "MCU": "U1",
        "PWR": "J1",
        "IO": "J2",
        "CELL": "BT1",
        "REG": "U2",
    }


def test_a_connector_with_no_package_is_a_validation_error():
    """A connector's land pattern cannot be guessed from its pin count.

    A 2-pin JST and a 2-pin screw terminal share a number and nothing else,
    which is the whole reason ``package`` exists.
    """
    data = _connector_spec_dict()
    del data["devices"]["PWR"]["package"]
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert [e for e in exc.value.errors if "'PWR'" in e and "package" in e]


def test_a_battery_with_no_package_is_a_validation_error():
    data = _connector_spec_dict()
    data["devices"]["PWR"] = {"kind": "battery", "pins": {"P": "1", "N": "2"}}
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert [e for e in exc.value.errors if "'PWR'" in e and "package" in e]


def test_an_unknown_package_names_the_ones_that_exist():
    """A refusal that does not say what IS supported costs a repair round."""
    data = _connector_spec_dict()
    data["devices"]["PWR"]["package"] = "USB_Mini_B_Whatever"
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    message = "\n".join(exc.value.errors)
    assert "USB_Mini_B_Whatever" in message
    assert "JST_PH_2P" in message  # one it could have picked instead


def test_an_unknown_kind_is_a_validation_error():
    data = _connector_spec_dict()
    data["devices"]["PWR"]["kind"] = "transformer"
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert [e for e in exc.value.errors if "transformer" in e]


def test_a_package_on_an_ic_is_a_validation_error():
    """An IC's land pattern follows from its pin count.

    A package here is either a name the builder ignores -- silently drawing
    something else -- or a connector whose ``kind`` was left off.
    """
    data = _connector_spec_dict()
    data["devices"]["U1"]["package"] = "JST_PH_2P"
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert [e for e in exc.value.errors if "'U1'" in e and "ic" in e]


def test_every_kind_failure_arrives_in_one_validation_error():
    """The batch is this module's reason to exist, and the new checks join it.

    Four devices are wrong in four different ways at once. Raising on the
    first would cost four model round trips to repair what one prompt can
    fix, so the count is asserted, not just that something raised.
    """
    data = {
        "devices": {
            "BADKIND": {"kind": "transformer", "pins": {"A": "1", "B": "2"}},
            "NOPKG": {"kind": "connector", "pins": {"A": "1", "B": "2"}},
            "BADPKG": {
                "kind": "connector",
                "package": "Not_A_Real_Connector",
                "pins": {"A": "1", "B": "2"},
            },
            "ICPKG": {
                "package": "JST_PH_2P",
                "pins": {"A": "1", "B": "2"},
            },
        },
        "nets": {
            "N1": ["BADKIND.A", "NOPKG.A", "BADPKG.A", "ICPKG.A"],
            "N2": ["BADKIND.B", "NOPKG.B", "BADPKG.B", "ICPKG.B"],
        },
    }
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    named = {
        name
        for name in ("BADKIND", "NOPKG", "BADPKG", "ICPKG")
        if any(f"'{name}'" in e for e in exc.value.errors)
    }
    assert named == {"BADKIND", "NOPKG", "BADPKG", "ICPKG"}


def test_a_kind_failure_does_not_hide_the_other_failures():
    """The batch is one batch: a bad package and a bad net come back together."""
    data = _connector_spec_dict()
    data["devices"]["PWR"]["package"] = "Not_A_Real_Connector"
    data["nets"]["VCC"] = ["U1.VDD", "PWR.NOSUCHPIN"]
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert [e for e in exc.value.errors if "Not_A_Real_Connector" in e]
    assert [e for e in exc.value.errors if "NOSUCHPIN" in e]


def test_a_valid_connector_spec_parses():
    spec = parse_circuit_spec(_connector_spec_dict())
    pwr = next(d for d in spec.devices if d.name == "PWR")
    assert pwr.kind == "connector"
    assert pwr.package == "JST_PH_2P"
    assert pwr.ref_prefix == "J"

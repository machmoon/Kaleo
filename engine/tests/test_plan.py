"""The planning stage: does a thin intent become a brief, or say it cannot?

Entirely offline -- every test drives a :class:`ScriptedModel`, the
convention the rest of the agents tests follow. Nothing here reaches a
network, and nothing here reads the live connector tables: the vocabulary is
passed in explicitly so a test asserts what this module does with a list,
not what some other lane's list happens to contain today.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.model import ModelError, ScriptedModel
from silkscreen.agents.plan import (
    PLAN_MARKER,
    POWER_SOURCES,
    BoardPlan,
    PackageVocabulary,
    PlanValidationError,
    package_vocabulary,
    parse_plan_response,
    plan_prompt,
    propose_plan,
)

#: The connector contract's names. Held here rather than imported so this
#: file keeps testing the parser even while Lane A's tables are landing.
VOCAB = PackageVocabulary(
    connectors=(
        "Barrel_Jack_5.5x2.1mm",
        "JST_PH_2P",
        "JST_PH_4P",
        "PinHeader_1x04_P2.54mm",
        "TerminalBlock_2P_5.08mm",
        "USB_C_Receptacle_Power",
    ),
    batteries=("BatteryHolder_AAA_1x", "BatteryHolder_CR2032"),
    known=True,
)


def good_plan() -> dict:
    """A plan that answers every question the stage exists to ask."""
    return {
        "building": "A mains-powered indoor security camera board",
        "power": {
            "source": "barrel_jack",
            "input_voltage": "12V",
            "connector_package": "Barrel_Jack_5.5x2.1mm",
            "battery_package": None,
            "notes": "Wall adapter, reverse-polarity protected",
        },
        "rails": [
            {
                "name": "+5V",
                "voltage": "5V",
                "derived_from": "VIN",
                "purpose": "camera module",
            },
            {
                "name": "+3V3",
                "voltage": "3.3V",
                "derived_from": "+5V",
                "purpose": "MCU and sensors",
            },
        ],
        "blocks": [
            {"name": "power entry", "purpose": "12V in, protection", "rail": "VIN"},
            {"name": "MCU", "purpose": "runs the firmware", "rail": "+3V3"},
        ],
        "connectivity": [
            {
                "purpose": "PIR sensor breakout",
                "package": "JST_PH_4P",
                "signals": ["+3V3", "GND", "SDA", "SCL"],
            }
        ],
        "assumptions": ["Indoor use, so no conformal coating is planned"],
        "open_questions": ["Does the camera module need 5V or 3.3V?"],
    }


def test_a_good_plan_parses_into_every_decision_it_made():
    plan = parse_plan_response(json.dumps(good_plan()), vocab=VOCAB)

    assert isinstance(plan, BoardPlan)
    assert plan.power.source == "barrel_jack"
    assert plan.power.decided
    assert plan.power.connector_package == "Barrel_Jack_5.5x2.1mm"
    assert plan.power.input_voltage == "12V"
    assert [r.name for r in plan.rails] == ["+5V", "+3V3"]
    assert [b.name for b in plan.blocks] == ["power entry", "MCU"]
    assert plan.connectivity[0].package == "JST_PH_4P"
    # Every package the plan asked for, so a caller can check the builder
    # can draw all of them before spending the propose call.
    assert set(plan.packages()) == {"Barrel_Jack_5.5x2.1mm", "JST_PH_4P"}
    assert plan.as_dict()["power"]["decided"] is True


def test_a_fenced_answer_is_tolerated():
    raw = "```json\n" + json.dumps(good_plan()) + "\n```"
    assert parse_plan_response(raw, vocab=VOCAB).power.source == "barrel_jack"


def test_a_battery_plan_names_a_holder_the_builder_can_place():
    data = good_plan()
    data["power"] = {
        "source": "battery",
        "input_voltage": "3.0V",
        "connector_package": None,
        "battery_package": "BatteryHolder_CR2032",
        "notes": None,
    }
    plan = parse_plan_response(json.dumps(data), vocab=VOCAB)
    assert plan.power.battery_package == "BatteryHolder_CR2032"
    assert plan.packages() == ("BatteryHolder_CR2032", "JST_PH_4P")


# --------------------------------------------------------------- batching


def test_every_validation_failure_arrives_in_one_error_not_the_first():
    """The netlist convention: one repair prompt fixes all of them."""
    data = good_plan()
    data["building"] = ""
    data["power"]["source"] = "solar"
    data["power"]["connector_package"] = "Barrel_Jack_2.5mm_Imaginary"
    data["rails"] = []
    data["blocks"] = [{"name": "MCU"}]  # no purpose
    data["connectivity"] = [{"purpose": "sensor", "package": "JST_XH_4P"}]

    with pytest.raises(PlanValidationError) as excinfo:
        parse_plan_response(json.dumps(data), vocab=VOCAB)

    errors = excinfo.value.errors
    joined = "\n".join(errors)
    # Six independent faults, and the parser saw all six -- not the first.
    assert len(errors) >= 6, joined
    assert "building is empty" in joined
    assert "power.source is 'solar'" in joined
    assert "Barrel_Jack_2.5mm_Imaginary" in joined
    assert "rail(s)" in joined
    assert "blocks[0].purpose" in joined
    assert "JST_XH_4P" in joined


def test_an_unbuildable_package_is_named_with_the_ones_that_are():
    """A plan that asks for a part the engine cannot draw is the bug this
    stage exists to avoid, so the message has to be actionable."""
    data = good_plan()
    data["power"]["connector_package"] = "USB_Micro_B"

    with pytest.raises(PlanValidationError) as excinfo:
        parse_plan_response(json.dumps(data), vocab=VOCAB)

    message = "\n".join(excinfo.value.errors)
    assert "USB_Micro_B" in message
    assert "USB_C_Receptacle_Power" in message


def test_a_decided_source_must_name_the_part_that_carries_the_power():
    data = good_plan()
    data["power"]["connector_package"] = None

    with pytest.raises(PlanValidationError) as excinfo:
        parse_plan_response(json.dumps(data), vocab=VOCAB)
    assert any("connector_package" in e for e in excinfo.value.errors)


def test_not_valid_json_is_one_error_not_a_crash():
    with pytest.raises(PlanValidationError) as excinfo:
        parse_plan_response("I think you want a battery", vocab=VOCAB)
    assert len(excinfo.value.errors) == 1
    assert "not valid JSON" in excinfo.value.errors[0]


# ---------------------------------------------------- saying "I don't know"


def test_the_plan_may_say_it_does_not_know_if_it_says_what_it_assumed():
    data = good_plan()
    data["power"] = {
        "source": "unknown",
        "input_voltage": "",
        "connector_package": None,
        "battery_package": None,
        "notes": None,
    }
    data["assumptions"] = ["The request never said how it is powered; a USB-C"
                           " 5V input is the usual choice for a desk device"]

    plan = parse_plan_response(json.dumps(data), vocab=VOCAB)
    assert plan.power.source == "unknown"
    assert plan.power.decided is False
    assert plan.assumptions
    assert "NOT DECIDED" in plan.brief_text()


def test_unknown_with_no_assumption_is_rejected():
    """Otherwise "unknown" is the cheapest answer and the stage decides
    nothing at all."""
    data = good_plan()
    data["power"] = {
        "source": "unknown",
        "input_voltage": "",
        "connector_package": None,
        "battery_package": None,
        "notes": None,
    }
    data["assumptions"] = []

    with pytest.raises(PlanValidationError) as excinfo:
        parse_plan_response(json.dumps(data), vocab=VOCAB)
    assert any("assumptions" in e for e in excinfo.value.errors)


def test_unknown_reaches_the_caller_as_a_warning_not_a_failure():
    data = good_plan()
    data["power"] = {
        "source": "unknown",
        "input_voltage": "",
        "connector_package": None,
        "battery_package": None,
        "notes": None,
    }
    data["assumptions"] = ["Nothing in the request says how it is powered"]

    model = ScriptedModel(responses=[json.dumps(data)])
    result = propose_plan(model, "a thing", vocab=VOCAB)

    assert result.ok
    assert result.plan is not None
    assert any("could not decide a power source" in w for w in result.warnings)


# ----------------------------------------------------------- the prompt


def test_the_prompt_carries_the_marker_verbatim():
    """``ScriptedModel.by_marker`` keys on this."""
    assert PLAN_MARKER == "BOARD-PLAN v1"
    assert PLAN_MARKER in plan_prompt(VOCAB)

    model = ScriptedModel(by_marker={PLAN_MARKER: json.dumps(good_plan())})
    assert propose_plan(model, "a security camera", vocab=VOCAB).ok


def test_the_prompt_names_only_packages_the_builder_can_draw():
    from silkscreen.board import supported_packages_text

    prompt = plan_prompt(VOCAB)
    for name in VOCAB.connectors + VOCAB.batteries:
        assert name in prompt
    # The propose stage's rule 7, applied one stage earlier: the ICs come
    # from board.py's own list so the two cannot drift.
    assert supported_packages_text() in prompt
    for source in POWER_SOURCES:
        assert source in prompt


def test_an_unreadable_vocabulary_degrades_honestly():
    """A list this module could not read is this module's problem: it is
    said in the prompt and it does not become a rejection of the model."""
    blind = PackageVocabulary(known=False)
    assert "does not advertise a list" in plan_prompt(blind)

    data = good_plan()
    data["power"]["connector_package"] = "Something_Unlisted"
    plan = parse_plan_response(json.dumps(data), vocab=blind)
    assert plan.power.connector_package == "Something_Unlisted"


def test_the_live_vocabulary_is_read_rather_than_copied():
    """Whatever footprints.py advertises today, this reads it without
    raising -- including the case where the tables are not there yet."""
    vocab = package_vocabulary()
    assert isinstance(vocab, PackageVocabulary)
    assert vocab.known in (True, False)


def test_the_intent_reaches_the_model():
    model = ScriptedModel(responses=[json.dumps(good_plan())])
    propose_plan(model, "a home security camera system", vocab=VOCAB)
    assert "a home security camera system" in model.calls[0]["prompt"]


# ------------------------------------------------------- the repair loop


def test_one_repair_round_is_spent_and_the_errors_go_back_in_one_prompt():
    broken = good_plan()
    broken["power"]["source"] = "solar"
    broken["building"] = ""
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(good_plan())])

    events: list[dict] = []
    result = propose_plan(
        model, "a camera", vocab=VOCAB, on_event=events.append
    )

    assert result.ok
    assert result.warnings == []
    assert len(model.calls) == 2
    repair = model.calls[1]["prompt"]
    assert "rejected" in repair
    # Both faults in the ONE repair prompt, not one round per fault.
    assert "power.source is 'solar'" in repair
    assert "building is empty" in repair
    rounds = [e for e in events if e["event"] == "plan.round"]
    assert len(rounds) == 1 and rounds[0]["errors"] >= 2
    ready = [e for e in events if e["event"] == "plan.ready"]
    assert ready and ready[0]["power_source"] == "barrel_jack"


def test_it_gives_up_loudly_rather_than_raising():
    """A run with no plan is honest. A run that raised here would lose the
    board, which is still the product."""
    broken = json.dumps({"building": "x"})
    model = ScriptedModel(responses=[broken, broken])

    result = propose_plan(model, "a camera", vocab=VOCAB)

    assert not result.ok
    assert result.plan is None
    assert len(model.calls) == 2  # the first call plus one repair, then stop
    assert len(result.warnings) == 1
    assert "not planned" in result.warnings[0]
    assert "2 attempt(s)" in result.warnings[0]
    assert result.as_dict()["plan"] is None


def test_max_repairs_zero_is_one_call():
    model = ScriptedModel(responses=[json.dumps({"building": "x"})])
    result = propose_plan(model, "a camera", vocab=VOCAB, max_repairs=0)
    assert not result.ok
    assert len(model.calls) == 1


def test_a_model_error_propagates_unwrapped():
    """The propose_circuit convention: an outage is not a bad answer."""
    model = ScriptedModel(responses=[])  # runs out immediately

    with pytest.raises(ModelError):
        propose_plan(model, "a camera", vocab=VOCAB)


# ------------------------------------------------------------ the brief


def test_the_brief_is_deterministic_and_names_the_power_entry():
    plan = parse_plan_response(json.dumps(good_plan()), vocab=VOCAB)
    text = plan.brief_text()

    assert text == plan.brief_text()
    assert "Barrel_Jack_5.5x2.1mm" in text
    assert "input 12V" in text
    assert "+3V3" in text
    assert "JST_PH_4P" in text
    assert "PIR sensor breakout" in text

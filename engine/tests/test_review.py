"""The adversarial critic: the specialist split, the premise filter, the merge.

Every test here is offline. The critic is one model call, so a
``ScriptedModel`` with one response drives a whole review, and nothing in this
file needs a key.

The discipline these tests exist to pin, in one sentence each:

* Nothing the filter removes is removed silently, and nothing the merge folds
  together is folded silently.
* A premise the validated spec refutes drops the finding that rests on it --
  including a well-written blocker, which is the case a reader has to be able
  to see happen and disagree with.
* The merge keeps the harshest severity, never an average, and its result does
  not depend on the order the model emitted its findings in.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.agents.review import (
    REVIEW_PROMPT,
    SPECIALISTS,
    Check,
    CheckStatus,
    Domain,
    Finding,
    ReviewReport,
    ReviewStatus,
    Severity,
    SpecIndex,
    normalise_value,
    review_circuit,
    run_review,
)
from silkscreen.agents.stages import review_stage
from silkscreen.netlist import parse_circuit_spec

CIRCUIT = {
    "devices": {
        "U1": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        "U2": {
            "pins": {
                "IN1": "1", "IN2": "2", "VM": "3", "GND": "4",
                "OUT1": "5", "OUT2": "6", "VCC": "7", "nSLEEP": "8",
            }
        },
    },
    "passives": {
        "c_in": {"type": "capacitor", "value": "22uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
        "c_dec": {"type": "capacitor", "value": "100nF"},
        "r_sleep": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VIN": ["U1.VIN", "c_in.1", "U2.VM"],
        "GND": ["U1.GND", "U2.GND", "c_in.2", "c_out.2", "c_dec.2"],
        "+3V3": ["U1.VOUT", "U2.VCC", "c_out.1", "c_dec.1", "r_sleep.1"],
        "SLEEP": ["U2.nSLEEP", "r_sleep.2"],
        "MOT": ["U2.OUT1", "U2.IN1"],
    },
}


def spec():
    return parse_circuit_spec(CIRCUIT)


def entry(**over):
    base = {
        "domain": "power",
        "severity": "marginal",
        "title": "Output capacitor is ceramic",
        "detail": "",
        "parts": ["c_out"],
        "nets": ["+3V3"],
        "claims": [],
    }
    base.update(over)
    return base


def review(*entries):
    model = ScriptedModel(responses=[json.dumps({"findings": list(entries)})])
    return run_review(model, spec())


# ------------------------------------------------------------- value spelling


@pytest.mark.parametrize(
    "left,right",
    [
        ("10uF", "10 µF"),
        ("10uF", "10.0uF"),
        ("10uF", "10u"),
        ("4k7", "4700"),
        ("4k7", "4.7k"),
        ("100nF", "0.1uF"),
        ("1M", "1000k"),
        ("10k", "10 kOhm"),
        ("10k", "10kΩ"),
    ],
)
def test_two_spellings_of_one_value_compare_equal(left, right):
    """A premise check written against one spelling must not pass on another.

    ``netlist.normalise_pin_number`` exists for the same reason on the pin
    side: a model writes the same thing several ways between one line of a
    datasheet and the next.
    """
    assert normalise_value(left) == normalise_value(right)


@pytest.mark.parametrize(
    "left,right", [("10uF", "10nF"), ("1M", "1m"), ("10k", "10")]
)
def test_values_that_differ_do_not_compare_equal(left, right):
    """``M`` is mega and ``m`` is milli, which is SPICE's own rule.

    Getting this backwards is nine orders of magnitude on a resistor, and it
    would make the premise filter drop true findings.
    """
    assert normalise_value(left) != normalise_value(right)


def test_a_value_that_is_not_a_value_survives_as_its_own_text():
    """"Red LED" must compare equal to itself and to nothing else."""
    assert normalise_value("Red LED") == normalise_value("red led")
    assert normalise_value("Red LED") != normalise_value("Green LED")


# ------------------------------------------------------------ the spec index


def test_the_index_knows_where_every_terminal_is():
    index = SpecIndex.of(spec())
    assert index.net_of_terminal["c_out.1"] == "+3V3"
    assert index.net_of_terminal["U2.nSLEEP"] == "SLEEP"
    # U2.IN2 is declared and on no net. "Not in the map" is how the index says
    # floating, which is what the `floating` premise is checked against.
    assert "U2.IN2" not in index.net_of_terminal
    assert index.pin_count["U2"] == 8
    assert index.value_of["c_dec"] == normalise_value("100nF")


def test_a_terminal_is_recognised_however_its_pin_number_is_spelt():
    index = SpecIndex.of(spec())
    assert index.terminal("c_out.1") == "c_out.1"
    assert index.terminal("c_out.01") == "c_out.1"
    assert index.terminal("c_out.3") is None      # a passive has two legs
    assert index.terminal("U1.SHDN") is None      # U1 declares no such pin
    assert index.terminal("U9.VIN") is None       # no such part
    assert index.terminal("c_out") is None        # not a terminal at all


# ------------------------------------------------------- the premise filter


def test_a_finding_resting_on_a_wrong_value_is_dropped_and_reported():
    """The row this closes: findings were filtered on refs and nothing else.

    A critic that argues about a 10 uF output capacitor on a board whose
    output capacitor is 22 uF is not reviewing this board. Before this, that
    finding reached the engineer looking exactly like one that was.
    """
    outcome = review(
        entry(
            severity="blocker",
            title="10uF output capacitor is too small for stability",
            claims=[{"kind": "value", "part": "c_out", "value": "10uF"}],
        )
    )
    assert outcome.findings == ()
    assert len(outcome.dropped) == 1
    line = outcome.dropped[0]
    assert "c_out" in line and "10uF" in line and normalise_value("22uF") in line


def test_a_finding_resting_on_a_right_value_survives_and_says_so():
    outcome = review(
        entry(claims=[{"kind": "value", "part": "c_out", "value": "22uF"}])
    )
    (finding,) = outcome.findings
    assert outcome.dropped == ()
    assert finding.confirmed_premises == ("c_out is 22uF",)


def test_a_claim_that_a_connected_pin_is_floating_is_refuted():
    outcome = review(
        entry(
            severity="blocker",
            title="nSLEEP is left floating",
            parts=["U2", "r_sleep"],
            nets=[],
            claims=[{"kind": "floating", "terminal": "U2.nSLEEP"}],
        )
    )
    assert outcome.findings == ()
    assert "SLEEP" in outcome.dropped[0]


def test_a_claim_that_a_floating_pin_is_floating_survives():
    outcome = review(
        entry(
            severity="blocker",
            title="IN2 is left floating",
            parts=["U2"],
            nets=[],
            claims=[{"kind": "floating", "terminal": "U2.IN2"}],
        )
    )
    assert [f.title for f in outcome.findings] == ["IN2 is left floating"]


def test_a_claim_about_the_wrong_net_is_refuted():
    outcome = review(
        entry(
            title="The decoupling capacitor is on the input rail",
            parts=["c_dec"],
            nets=["VIN"],
            claims=[{"kind": "connected", "terminal": "c_dec.1", "net": "VIN"}],
        )
    )
    assert outcome.findings == ()
    assert "+3V3" in outcome.dropped[0]


def test_a_claim_about_the_wrong_pin_count_is_refuted():
    outcome = review(
        entry(
            domain="manufacturability",
            title="U2 is drawn as a SOIC-14 but the part is 8-pin",
            parts=["U2"],
            nets=[],
            claims=[{"kind": "pin_count", "part": "U2", "pins": 14}],
        )
    )
    assert outcome.findings == ()
    assert "14" in outcome.dropped[0] and "8" in outcome.dropped[0]


def test_one_refuted_premise_is_enough_to_drop_a_finding():
    """A finding is only as good as the weakest thing it rests on."""
    outcome = review(
        entry(
            claims=[
                {"kind": "value", "part": "c_out", "value": "22uF"},
                {"kind": "floating", "terminal": "U2.nSLEEP"},
            ],
        )
    )
    assert outcome.findings == ()
    assert len(outcome.dropped) == 1


def test_an_undecidable_premise_never_drops_a_finding():
    """The filter may only remove what the spec actually contradicts.

    A premise this cannot decide is recorded and the finding kept. The
    alternative -- treating "I could not check it" as "it is false" -- would
    make the filter's silence indistinguishable from a verdict.
    """
    outcome = review(
        entry(
            claims=[
                {"kind": "esr", "part": "c_out", "max": "0.1"},
                {"kind": "value", "part": "U1", "value": "3.3V"},
                "not an object",
            ]
        )
    )
    (finding,) = outcome.findings
    assert outcome.dropped == ()
    assert len(finding.checks) == 3
    assert all(c.status is CheckStatus.UNCHECKABLE for c in finding.checks)


def test_a_finding_with_no_premise_at_all_says_so_rather_than_saying_nothing():
    """An empty ``checks`` and an unfalsifiable finding must not look alike."""
    outcome = review(entry(claims=[]))
    (finding,) = outcome.findings
    assert len(finding.checks) == 1
    assert finding.checks[0].status is CheckStatus.UNCHECKABLE
    assert "no premise" in finding.checks[0].detail


# ------------------------------------------------- the ref and net filters


def test_invented_parts_and_nets_are_stripped_from_a_real_finding():
    outcome = review(
        entry(parts=["c_out", "c_bulk"], nets=["+3V3", "VBUS"])
    )
    (finding,) = outcome.findings
    assert finding.parts == ("c_out",)
    assert finding.nets == ("+3V3",)


def test_a_finding_naming_only_invented_things_is_dropped_and_reported():
    """Kept with empty ``parts`` this was an unlocatable *blocker*: it books a
    Calendar meeting, reports the board not-orderable and blocks the desktop
    order step, over a part the board does not have.
    """
    outcome = review(
        entry(severity="blocker", title="U7 enable pin floats",
              parts=["U7"], nets=["VBUS"])
    )
    assert outcome.findings == ()
    assert "U7" in outcome.dropped[0] and "VBUS" in outcome.dropped[0]


def test_a_finding_that_names_a_net_but_no_part_is_still_located():
    outcome = review(entry(parts=[], nets=["+3V3"]))
    (finding,) = outcome.findings
    assert finding.nets == ("+3V3",) and finding.parts == ()


# ------------------------------------------------------------- the domains


def test_each_finding_carries_the_specialist_that_raised_it():
    outcome = review(
        entry(domain="power", title="No bulk capacitance on the rail"),
        entry(domain="signal", title="IN2 has no defined level", parts=["U2"],
              nets=[]),
        entry(domain="manufacturability", title="LED polarity is ambiguous",
              parts=["c_in"], nets=[]),
    )
    assert {f.domain for f in outcome.findings} == {
        Domain.POWER, Domain.SIGNAL, Domain.MANUFACTURABILITY
    }
    grouped = ReviewReport(
        status=ReviewStatus.OK, findings=outcome.findings
    ).by_domain()
    assert set(grouped) == {
        Domain.POWER, Domain.SIGNAL, Domain.MANUFACTURABILITY
    }


def test_an_unlabelled_finding_is_general_rather_than_guessed_at():
    """``general`` is honest about not knowing; guessing a domain is not."""
    assert review(entry(domain="")).findings[0].domain is Domain.GENERAL
    assert review(entry(domain="thermal")).findings[0].domain is Domain.GENERAL
    del_domain = {k: v for k, v in entry().items() if k != "domain"}
    model = ScriptedModel(responses=[json.dumps({"findings": [del_domain]})])
    assert run_review(model, spec()).findings[0].domain is Domain.GENERAL


def test_the_prompt_asks_for_all_three_specialists_and_still_refutes():
    """An agent asked "is this correct?" says yes -- the original rule."""
    lowered = REVIEW_PROMPT.lower()
    assert "wrong" in lowered and "do not compliment" in lowered
    assert Domain.GENERAL not in SPECIALISTS  # never something to ask for
    for domain in SPECIALISTS:
        assert domain.value in lowered


def test_the_prompt_forbids_the_finding_that_is_true_of_every_netlist():
    """Measured: the manufacturability lens files "the parts are not specified
    precisely enough to order" on every board, once as a *blocker*. It is true
    of the IR itself -- which carries no packages or part numbers by design --
    so it is a remark about the notation, not a defect in the circuit.
    """
    assert "not specified precisely enough to" in REVIEW_PROMPT
    assert "NETLIST" in REVIEW_PROMPT


# --------------------------------------------------------------- the merge


def test_two_specialists_raising_one_defect_reach_the_engineer_once():
    outcome = review(
        entry(domain="power", title="Ceramic output capacitor may oscillate",
              parts=["c_out"], nets=["+3V3"]),
        entry(domain="manufacturability",
              title="The output capacitor dielectric is unstated",
              parts=["c_out"], nets=["+3V3"]),
    )
    (finding,) = outcome.findings
    assert set(finding.agreed_by) == {Domain.POWER, Domain.MANUFACTURABILITY}
    assert len(outcome.merged) == 1
    assert "power" in outcome.merged[0]
    assert "manufacturability" in outcome.merged[0]


def test_a_merge_keeps_the_harshest_severity_and_names_the_disagreement():
    """Never an average. A second critic filing the same defect as a note has
    not made the board work, so a blocker stays a blocker -- and because the
    two specialists disagreed, the report says so.
    """
    outcome = review(
        entry(domain="signal", severity="note",
              title="Output capacitor value is low", parts=["c_out"], nets=[]),
        entry(domain="power", severity="blocker",
              title="Output capacitor value causes instability",
              parts=["c_out"], nets=[]),
    )
    (finding,) = outcome.findings
    assert finding.severity is Severity.BLOCKER
    assert "blocker" in outcome.merged[0] and "note" in outcome.merged[0]


def test_two_defects_at_one_location_are_not_merged():
    """Location is not identity. Semgrep's key is location *and* message for
    exactly this case.
    """
    outcome = review(
        entry(domain="power", title="Output capacitor dielectric is wrong",
              parts=["c_out"], nets=[]),
        entry(domain="signal", title="Reset pull-up is missing entirely",
              parts=["c_out"], nets=[]),
    )
    assert len(outcome.findings) == 2
    assert outcome.merged == ()


def test_the_same_words_at_two_locations_are_not_merged():
    outcome = review(
        entry(domain="power", title="Capacitor value is wrong",
              parts=["c_out"], nets=[]),
        entry(domain="power", title="Capacitor value is wrong",
              parts=["c_in"], nets=[]),
    )
    assert len(outcome.findings) == 2


def test_the_merge_does_not_depend_on_the_order_the_model_emitted_them():
    """``_merge`` keeps the first of each group, so an unstable order would
    change which wording, fix and citation the engineer is shown -- the same
    hazard semgrep's ``compare_match`` comment describes for autofix.
    """
    a = entry(domain="power", severity="blocker",
              title="Ceramic output capacitor oscillates",
              parts=["c_out"], nets=[], suggested_fix="use a tantalum")
    b = entry(domain="signal", severity="note",
              title="Output capacitor dielectric unstated",
              parts=["c_out"], nets=[])
    c = entry(domain="manufacturability", severity="marginal",
              title="Reset pull-up missing", parts=["r_sleep"], nets=[])
    forwards = review(a, b, c)
    backwards = review(c, b, a)
    assert [(f.title, f.severity, f.suggested_fix) for f in forwards.findings] == [
        (f.title, f.severity, f.suggested_fix) for f in backwards.findings
    ]
    assert forwards.merged == backwards.merged


def test_an_unlocated_pair_merges_only_on_identical_wording():
    """With no parts and no nets there is no strict half of the key left, so
    the loose overlap rule would collapse anything sharing one word.
    """
    same = review(
        entry(domain="power", title="No bulk capacitance", parts=[], nets=[]),
        entry(domain="signal", title="No bulk capacitance", parts=[], nets=[]),
    )
    assert len(same.findings) == 1
    apart = review(
        entry(domain="power", title="No bulk capacitance", parts=[], nets=[]),
        entry(domain="signal", title="No reset pull-up", parts=[], nets=[]),
    )
    assert len(apart.findings) == 2


def test_a_merged_finding_keeps_both_specialists_premise_checks():
    outcome = review(
        entry(domain="power", title="Output capacitor is ceramic",
              parts=["c_out"], nets=[],
              claims=[{"kind": "value", "part": "c_out", "value": "22uF"}]),
        entry(domain="signal", title="Output capacitor is ceramic",
              parts=["c_out"], nets=[],
              claims=[{"kind": "connected", "terminal": "c_out.1",
                       "net": "+3V3"}]),
    )
    (finding,) = outcome.findings
    assert len(finding.confirmed_premises) == 2


# -------------------------------------------------- what the stage reports


def _stage(response, *, review_on=True):
    events: list[dict] = []
    report = review_stage(
        ScriptedModel(responses=[response]),
        spec(),
        facts=[],
        review=review_on,
        emit=events.append,
        enter=lambda _stage: None,
    )
    return report, events


def test_the_stage_announces_every_merge_as_loudly_as_every_drop():
    """A merge hides a row the critic wrote, exactly as a drop does.

    golangci-lint reports its own suppressions rather than only counting them
    (``max_same_issues.go``'s ``Finish``); this is the same instinct, and it is
    why ``dropped`` exists in the first place.
    """
    response = json.dumps({"findings": [
        entry(domain="power", title="Ceramic output cap oscillates",
              parts=["c_out"], nets=[]),
        entry(domain="signal", title="Ceramic output cap unstated",
              parts=["c_out"], nets=[]),
        entry(severity="blocker", title="U7 floats", parts=["U7"], nets=[]),
    ]})
    report, events = _stage(response)
    assert len(report.findings) == 1
    assert len(report.merged) == 1 and len(report.dropped) == 1
    assert any(e["event"] == "review.merged" for e in events)
    assert any(e["event"] == "review.dropped" for e in events)
    done = next(e for e in events if e["event"] == "stage.done")
    assert done["merged"] == 1 and done["dropped"] == 1


def test_the_report_carries_what_was_thrown_away():
    """A consumer that reads only ``findings`` cannot tell a quiet filter from
    a quiet critic, so the report keeps both lists beside them.

    ``as_dict`` -- the shape a service surface puts on the wire -- carries
    them too. It did not when this lane landed, because three tests in
    ``service/tests`` assert that dict by exact equality and that was another
    lane's file; the integration pass widened the dict and those three
    assertions together, which is the only way the two halves agree.
    """
    report, _ = _stage(json.dumps({"findings": [
        entry(severity="blocker", title="U7 floats", parts=["U7"], nets=[]),
    ]}))
    assert report.ok and "U7" in report.dropped[0]
    assert set(report.as_dict()) == {
        "status", "ran", "detail", "note", "dropped", "merged",
    }
    assert report.as_dict()["dropped"] == list(report.dropped)
    assert report.as_dict()["merged"] == list(report.merged)


def test_a_filtered_review_still_reads_as_a_review_that_ran():
    """Dropping everything is not the same as finding nothing, but it is also
    not a failure: the critic answered, and what it said did not survive.
    """
    report, _ = _stage(json.dumps({"findings": [
        entry(severity="blocker", title="U7 floats", parts=["U7"], nets=[]),
    ]}))
    assert report.ok and report.findings == () and report.dropped


# ------------------------------------------------------------ back compat


def test_review_circuit_still_returns_the_pair_callers_unpack():
    """``slackbot/runner.py`` and the tests around it unpack exactly this."""
    findings, dropped = review_circuit(
        ScriptedModel(responses=[json.dumps({"findings": [entry()]})]), spec()
    )
    assert isinstance(findings, list) and isinstance(dropped, tuple)
    assert findings[0].title == "Output capacitor is ceramic"


def test_a_finding_is_still_constructible_with_only_the_old_fields():
    """Every new field is additive with a default, so nothing that built a
    ``Finding`` before this change has to learn about domains.
    """
    finding = Finding(
        severity=Severity.NOTE, title="t", detail="d", parts=("c_out",)
    )
    assert finding.domain is Domain.GENERAL
    assert finding.nets == () and finding.checks == ()
    assert str(finding).startswith("NOTE [c_out]:")


def test_a_check_prints_its_own_verdict():
    assert str(Check(CheckStatus.REFUTED, "c_out is 22uF")).startswith("refuted:")


# --------------------------------------------------- the refutation round


REFUTE_MARKER = "Your job is to REFUTE"


def _refuting(response, verdicts):
    """A model that answers the review with ``response`` and then refutes."""
    return ScriptedModel(
        by_marker={
            REFUTE_MARKER: json.dumps({"verdicts": verdicts}),
            "You are reviewing a circuit": response,
        }
    )


def _reviewed(entries, verdicts):
    model = _refuting(json.dumps({"findings": list(entries)}), verdicts)
    return run_review(model, spec(), refute=True)


def test_refutation_is_off_unless_asked_for():
    """One more model call is one more model call. Every other opt-in stage
    in the pipeline -- sourcing, the case, the agenda -- follows this rule.
    """
    model = ScriptedModel(responses=[json.dumps({"findings": [entry()]})])
    assert len(run_review(model, spec()).findings) == 1
    assert len(model.calls) == 1


def test_a_finding_that_survives_refutation_carries_the_reason():
    outcome = _reviewed(
        [entry(title="Ceramic output capacitor may oscillate")],
        [{"id": 0, "refuted": False, "reason": "the AMS1117 needs output ESR"}],
    )
    (finding,) = outcome.findings
    assert "survived refutation" in finding.checks[-1].detail
    assert "output ESR" in finding.checks[-1].detail


def test_a_refuted_finding_is_dropped_and_the_reason_reported():
    """The measured failure this exists for: every premise true, conclusion
    wrong. A 555 astable declared unable to oscillate on a board where the
    timing network is correct, with every net it quoted confirmed.
    """
    outcome = _reviewed(
        [entry(severity="blocker", title="The astable cannot oscillate")],
        [{"id": 0, "refuted": True, "reason": "the timing network is standard"}],
    )
    assert outcome.findings == ()
    assert "did not survive refutation" in outcome.dropped[0]
    assert "timing network is standard" in outcome.dropped[0]


def test_refutation_is_an_allow_list_not_a_deny_list():
    """A claim survives only on an explicit JSON ``false``.

    ``audit/judgment.py`` states the rule and the reason: malformed output
    must not silently promote an unverified claim into the report. Three ways
    to be malformed, all of which must refute.
    """
    entries = [
        entry(title="Claim with no verdict at all", parts=["c_out"], nets=[]),
        entry(title="Claim whose verdict is a string", parts=["c_in"], nets=[]),
        entry(title="Claim whose verdict is a number", parts=["c_dec"], nets=[]),
    ]
    outcome = _reviewed(
        entries,
        [{"id": 1, "refuted": "no"}, {"id": 2, "refuted": 0}],
    )
    assert outcome.findings == ()
    assert len(outcome.dropped) == 3
    assert any("no verdict" in line for line in outcome.dropped)
    assert any("not a boolean" in line for line in outcome.dropped)


def test_an_unreadable_refuter_refuses_rather_than_promoting():
    """The refuter failing is not the critic failing -- but it must not turn a
    real review into a clean board either, so it says why the list is empty.
    """
    model = ScriptedModel(
        by_marker={
            REFUTE_MARKER: "I am afraid I cannot help with that.",
            "You are reviewing a circuit": json.dumps(
                {"findings": [entry(severity="blocker")]}
            ),
        }
    )
    outcome = run_review(model, spec(), refute=True)
    assert outcome.findings == ()
    assert "refutation round could not be read" in outcome.dropped[0]
    assert "refused rather than promoted unverified" in outcome.dropped[0]


def test_refutation_costs_one_call_however_many_findings_there_are():
    """``audit/judgment.py`` asks per finding, behind an effort slider. The
    pipeline has no slider, so this is batched: two calls total, always.
    """
    model = _refuting(
        json.dumps({"findings": [
            entry(title="One", parts=["c_out"], nets=[]),
            entry(title="Two", parts=["c_in"], nets=[]),
            entry(title="Three", parts=["c_dec"], nets=[]),
        ]}),
        [{"id": i, "refuted": False, "reason": "holds"} for i in range(3)],
    )
    outcome = run_review(model, spec(), refute=True)
    assert len(outcome.findings) == 3
    assert len(model.calls) == 2


def test_refutation_is_not_asked_for_when_nothing_survived_the_filter():
    """A round trip to refute an empty list is a call spent on nothing."""
    model = _refuting(
        json.dumps({"findings": [entry(severity="blocker", title="U7 floats",
                                       parts=["U7"], nets=[])]}),
        [],
    )
    outcome = run_review(model, spec(), refute=True)
    assert outcome.findings == () and outcome.dropped
    assert len(model.calls) == 1


def test_the_refute_prompt_defaults_to_refuting_and_names_the_hard_case():
    from silkscreen.agents.review import REFUTE_PROMPT

    assert "Default to refuting" in REFUTE_PROMPT
    assert "premises are all true" in REFUTE_PROMPT


def test_the_stage_can_refute_and_reports_it_as_a_drop():
    events: list[dict] = []
    report = review_stage(
        _refuting(
            json.dumps({"findings": [entry(severity="blocker")]}),
            [{"id": 0, "refuted": True, "reason": "generic"}],
        ),
        spec(),
        facts=[],
        review=True,
        refute=True,
        emit=events.append,
        enter=lambda _stage: None,
    )
    assert report.ok and report.findings == ()
    assert any("did not survive refutation" in line for line in report.dropped)
    assert any(e["event"] == "review.dropped" for e in events)

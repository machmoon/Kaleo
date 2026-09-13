"""The SPICE verifier inside the agent loop: ``agents/simulate.py`` and the
``simulation`` stage in ``agents/stages.py``.

Offline by contract. The model is a ``ScriptedModel`` keyed on
``SIMULATE_MARKER``; the simulator is a fake behind the ``Simulator``
protocol that answers a closed-form RC response, so the verdict path -- a
failed clause becoming a finding, the margin's sign, the event lane -- runs
on a machine with no ngspice. One test at the bottom is gated on a real
ngspice the ``test_spice.py`` way and checks the measured rise time against
``tau * ln(9)`` computed inline.

The file name is deliberately not ``test_simulate.py`` or ``test_spice_*``:
``scripts/check_docs.py`` keys per-module counts by basename, and this must
not fold into ``spice/``'s count.
"""

from __future__ import annotations

import json
import math

import pytest
from silkscreen.agents import ScriptedModel, generate_pcb
from silkscreen.agents.model import ModelError
from silkscreen.agents.simulate import (
    SIMULATE_MARKER,
    SIMULATION_STATUSES,
    SimulationResult,
    TestbenchValidationError,
    circuit_models,
    parse_testbench_response,
    simulate_circuit,
    unsimulatable_parts,
)
from silkscreen.agents.stages import simulate_stage
from silkscreen.netlist import parse_circuit_spec
from silkscreen.spice.errors import SimulationFailed, SimulatorNotFound
from silkscreen.spice.raw import RawPlot
from silkscreen.spice.registry import ModelRegistry
from silkscreen.spice.simulators import NgspiceSimulator, RunOutcome
from test_agents import GOOD_CIRCUIT, _scripted_pipeline_model

HAS_NGSPICE = NgspiceSimulator().is_available()
needs_ngspice = pytest.mark.skipif(
    not HAS_NGSPICE, reason="ngspice is not installed on this machine"
)

try:
    import google.adk  # noqa: F401

    _HAS_ADK = True
except ImportError:
    _HAS_ADK = False

needs_adk = pytest.mark.skipif(not _HAS_ADK, reason="the 'adk' extra is not installed")


# ---------------------------------------------------------------- fixtures

#: The RC low-pass ``test_spice.py`` verifies end to end, as the model would
#: propose it: passives only, so every part has SPICE behaviour.
RC_LOWPASS = {
    "passives": {
        "Cin": {"type": "capacitor", "value": "1nF"},
        "Rfilt": {"type": "resistor", "value": "1k"},
        "Cfilt": {"type": "capacitor", "value": "100nF"},
        "Rload": {"type": "resistor", "value": "1MEG"},
    },
    "nets": {
        "VIN": ["Cin.1", "Rfilt.1"],
        "VOUT": ["Rfilt.2", "Cfilt.1", "Rload.1"],
        "GND": ["Cin.2", "Cfilt.2", "Rload.2"],
    },
}
RC_INTENT = "an RC low-pass filter that settles to a 5 V step"

# Independent arithmetic, never read back from the code under test: 1 k in
# series, loaded by 1 M, into 100 nF.
R_EFF = 1e3 * 1e6 / (1e3 + 1e6)
TAU = R_EFF * 100e-9
EXPECTED_FINAL = 5.0 * 1e6 / (1e6 + 1e3)
EXPECTED_RISE = TAU * math.log(9.0)
PULSE_WIDTH = 1e-3
#: Where the closed-form charge actually is when the pulse ends: ten time
#: constants in, so 1 - e^-10 of the way there, not all of it.
SETTLED_AT_PULSE_END = EXPECTED_FINAL * (1.0 - math.exp(-PULSE_WIDTH / TAU))

#: What the model answers on the marker: the testbench in the ``spice/spec``
#: JSON format plus three clauses, two critical and one not, so the run's
#: decision about a failed clause can be seen taking both branches.
RC_TESTBENCH = {
    "testbench": {
        "analysis": {"kind": "tran", "step": 1e-6, "stop": 2e-3},
        "sources": [
            {"name": "V1", "positive": "VIN", "negative": "GND", "kind": "pulse",
             "initial": 0.0, "pulsed": 5.0, "width": PULSE_WIDTH, "period": 2e-3}
        ],
        "ground": "GND",
    },
    "assertions": [
        {"name": "VOUT settles to the divided input",
         "measurement": {"kind": "max", "signal": "VOUT", "window": [0.0, PULSE_WIDTH]},
         "op": "within", "value": EXPECTED_FINAL, "tolerance": 0.01, "unit": "V",
         "critical": True},
        {"name": "10-90% rise time is tau*ln(9)",
         "measurement": {"kind": "rise_time", "signal": "VOUT",
                         "window": [0.0, PULSE_WIDTH]},
         "op": "within", "value": EXPECTED_RISE, "tolerance": 0.02, "unit": "s",
         "critical": False},
        {"name": "never exceeds the 5.5 V absolute maximum",
         "measurement": {"kind": "abs_max", "signal": "VOUT"},
         "op": "<", "value": 5.5, "unit": "V", "critical": True},
    ],
}


def rc_spec():
    return parse_circuit_spec(RC_LOWPASS)


def rc_model(answer=None):
    """A model that answers the testbench prompt and nothing else."""
    return ScriptedModel(by_marker={
        SIMULATE_MARKER: json.dumps(RC_TESTBENCH) if answer is None else answer,
    })


def rc_pipeline_model():
    """The full-pipeline model for the passives-only board."""
    return ScriptedModel(by_marker={
        "designing a printed circuit board": json.dumps(RC_LOWPASS),
        "reviewing a circuit someone else designed": json.dumps({"findings": []}),
        SIMULATE_MARKER: json.dumps(RC_TESTBENCH),
    })


class FakeSimulator:
    """A ``Simulator`` that answers the RC step response in closed form.

    ``final`` scales the settled level and ``tau_scale`` the time constant,
    so a test can make a clause fail by a known amount without ngspice. The
    variables are named from the deck's own node map, the way a real
    rawfile would name them, so the relabelling back to net names is
    exercised rather than bypassed.
    """

    name = "fake"

    def __init__(self, *, final=EXPECTED_FINAL, tau_scale=1.0, raise_with=None):
        self.final = final
        self.tau = TAU * tau_scale
        self.raise_with = raise_with
        self.decks: list[str] = []

    def is_available(self):
        return True

    def run(self, deck, *, timeout_s=60.0):
        self.decks.append(deck.text)
        if self.raise_with is not None:
            raise self.raise_with
        vin_node = deck.node_of_net["VIN"]
        vout_node = deck.node_of_net["VOUT"]
        points = 2001
        times = tuple(2e-3 * i / (points - 1) for i in range(points))
        vin = tuple(5.0 if t <= PULSE_WIDTH else 0.0 for t in times)
        vout = tuple(
            self.final * (1.0 - math.exp(-t / self.tau))
            if t <= PULSE_WIDTH
            else self.final * math.exp(-(t - PULSE_WIDTH) / self.tau)
            for t in times
        )
        variables = ("time", f"v({vin_node})", f"v({vout_node})")
        return RunOutcome(
            plot=RawPlot(
                title="fake", plotname="Transient Analysis", flags=("real",),
                variables=variables,
                data={variables[0]: times, variables[1]: vin, variables[2]: vout},
            ),
            log="",
            warnings=(),
            simulator="fake",
            deck_text=deck.text,
        )


def _stage(model, spec=None, *, simulator=None, intent=RC_INTENT):
    events = []
    result = simulate_stage(
        model,
        spec or rc_spec(),
        intent=intent,
        simulator=simulator,
        emit=events.append,
        enter=lambda _stage: None,
    )
    return result, events


def _simulation_lanes(events):
    """The main thread's events and the simulation thread's, split."""
    lane = [
        e for e in events
        if e.get("stage") == "simulation" or e["event"].startswith("simulation")
    ]
    # ``main`` is the *driver* thread's line, so the critic comes out of it
    # too: review is a worker lane now as well, running from the spec inside
    # place's solve, and leaving it in ``main`` would make these tests assert
    # a wall-clock race. Its ordering is pinned by _assert_simulation_bounds.
    main = [
        e for e in events
        if e not in lane and e.get("stage") != "review"
    ]
    return main, lane


def _assert_simulation_bounds(events):
    """The lane's bounds, and the determinism guarantee they exist to protect.

    The verdict is simulated on a worker thread from the moment placement
    lands, so its events interleave with schematic and route in wall-clock
    order and only the bounds across lanes are fixed.

    The upper bound here used to be "settled before review starts". That is no
    longer where the critic is: it needs only the validated spec, so it is
    started before ``place`` -- answering inside the solver budget instead of
    on the tail of the run -- and joined *before* this lane is started. Review
    is therefore upstream of the simulation lane, not downstream, and cannot
    bound it.

    What that old bound really encoded was not "review is last" but "exactly
    one worker model call is ever in flight", which is what lets a
    ``ScriptedModel`` answer a run the same way twice. That guarantee is
    unchanged, and is now asserted directly rather than as a side effect of
    where review happened to sit:

      * the lane opens only after place has closed (it needs the board),
      * the lane closes at all -- a thread with no terminal event is one
        nobody joined, and
      * the critic has closed before the lane opens, so the two never race
        for the model, backed by
        :func:`test_agents.assert_worker_call_order` over the whole stream.
    """
    from test_agents import assert_worker_call_order

    def at(event, stage):
        return next(
            i for i, e in enumerate(events)
            if e["event"] == event and e.get("stage") == stage
        )

    assert at("stage.start", "simulation") > at("stage.done", "place")
    end = [
        i for i, e in enumerate(events)
        if e["event"] == "stage.done" and e.get("stage") == "simulation"
    ]
    assert end
    assert at("stage.done", "review") < at("stage.start", "simulation")
    assert_worker_call_order(events)


# ---------------------------------------------------------------- opt-in


def test_default_run_makes_no_simulation_call_and_emits_no_events(tmp_path):
    model = rc_pipeline_model()
    events = []
    result = generate_pcb(
        model, RC_INTENT, output=tmp_path / "board.kicad_pcb",
        time_limit_s=10.0, on_event=events.append, engine="sdk",
    )
    assert result.simulation is None
    assert not any(e.get("stage") == "simulation" for e in events)
    assert not any(e["event"].startswith("simulation") for e in events)
    assert not any(SIMULATE_MARKER in c["prompt"] for c in model.calls)
    assert "simulation" not in result.summary()


# --------------------------------------------------- the four non-run facts


def test_a_device_the_registry_cannot_cover_is_unsimulatable_before_asking():
    """A device with no registry entry is decided from the spec and the
    registry alone: the part is named, the reason is the registry's own, and
    no call is spent on a testbench nothing could run. The regulator on the
    same board *is* covered, which is exactly why the answer names only the
    part that is missing."""
    model = rc_model()
    result = simulate_circuit(
        model, parse_circuit_spec(GOOD_CIRCUIT), registry=ModelRegistry(files={})
    )
    assert result.status == "unsimulatable"
    assert result.parts == ("DRV8837",)
    assert "DRV8837" in result.detail and "no model" in result.detail
    assert "AMS1117-3.3" not in result.detail
    assert result.passed is None and result.clauses == [] and result.findings == []
    assert model.calls == []


def test_a_covered_device_no_longer_makes_the_circuit_unsimulatable():
    """The registry's whole purpose, stated as a property: the LDO board's
    only device is covered, so nothing is uncovered and the run proceeds to
    ask for a testbench instead of refusing."""
    spec = parse_circuit_spec({
        "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2",
                                             "VIN": "3"}}},
        "passives": {
            "c_in": {"type": "capacitor", "value": "22uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
            "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2"],
            "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
        },
    })
    registry = ModelRegistry(files={})
    parts, _ = unsimulatable_parts(spec, registry)
    assert parts == []
    resolution = circuit_models(spec, registry)
    assert resolution.generic_parts == ("AMS1117-3.3",)
    # And the standing of the model reaches the prompt, so the model does not
    # write clauses the instrument cannot answer.
    model = rc_model()
    simulate_circuit(model, spec, simulator=FakeSimulator(final=3.3),
                     registry=registry)
    assert "GENERIC STAND-IN" in model.calls[0]["prompt"]


def test_a_crystal_is_unsimulatable_too():
    spec = parse_circuit_spec({
        "passives": {
            "Y1": {"type": "crystal", "value": "8MHz"},
            "C1": {"type": "capacitor", "value": "22pF"},
        },
        "nets": {"XA": ["Y1.1", "C1.1"], "GND": ["Y1.2", "C1.2"]},
    })
    parts, reason = unsimulatable_parts(spec, ModelRegistry(files={}))
    assert parts == ["Y1"] and "crystal" in reason


def test_no_simulator_is_unavailable_with_the_install_hint(monkeypatch):
    import silkscreen.agents.simulate as module

    def missing(prefer=None):
        raise SimulatorNotFound(["ngspice"])

    monkeypatch.setattr(module, "find_simulator", missing)
    model = rc_model()
    result = simulate_circuit(model, rc_spec())
    assert result.status == "unavailable"
    assert "install ngspice" in result.detail
    assert result.passed is None
    # Decided before the model was asked: no call spent.
    assert model.calls == []


def test_no_usable_testbench_after_one_repair_gives_up_loudly():
    model = ScriptedModel(responses=[
        "this is not json",
        json.dumps({"testbench": {}, "assertions": []}),
    ])
    events = []
    result = simulate_circuit(
        model, rc_spec(), simulator=FakeSimulator(), on_event=events.append
    )
    assert result.status == "no_testbench"
    assert result.passed is None and result.clauses == []
    assert len(model.calls) == 2
    assert result.repair_rounds == 1
    # The repair prompt carries the batched problems and the rejected answer.
    second = model.calls[1]["prompt"]
    assert "rejected" in second and "not valid JSON" in second
    assert "this is not json" in second
    assert SIMULATE_MARKER in model.calls[0]["prompt"]
    # One warning, saying how many attempts and what the last batch said.
    assert len(result.warnings) == 1
    assert "2 attempt(s)" in result.warnings[0]
    assert "analysis" in result.warnings[0]
    assert [e["event"] for e in events] == ["simulation.round", "simulation.round"]
    assert [e["round"] for e in events] == [1, 2]
    assert all("prompt" not in e and "text" not in e for e in events)


def test_a_transport_failure_is_not_wrapped():
    with pytest.raises(ModelError):
        simulate_circuit(ScriptedModel(), rc_spec(), simulator=FakeSimulator())


# ---------------------------------------------------------------- parsing


def test_parse_batches_every_problem_into_one_error():
    """Bridge errors, deck cross-check errors and a clause on an absent net
    come back together, so one repair prompt can fix all of them."""
    raw = json.dumps({
        "testbench": {
            "analysis": {"kind": "tran", "step": 1e-6},
            "sources": [
                {"name": "V1", "positive": "VBUS", "negative": "GND", "dc": 5.0},
            ],
        },
        "assertions": [
            {"name": "x", "measurement": {"kind": "final", "signal": "VOUT"},
             "op": "<", "value": "five"},
            {"name": "y", "measurement": {"kind": "final", "signal": "VOUT"},
             "op": "<", "value": 5, "critical": "yes"},
        ],
    })
    with pytest.raises(TestbenchValidationError) as exc:
        parse_testbench_response(raw, rc_spec())
    joined = "\n".join(exc.value.errors)
    assert "missing required field 'stop'" in joined
    assert "'value' must be a number" in joined
    assert "'critical' must be true or false" in joined
    assert len(exc.value.errors) >= 3


def test_parse_cross_checks_the_testbench_against_the_circuit():
    raw = json.dumps({
        "testbench": {
            "analysis": {"kind": "op"},
            "sources": [
                {"name": "V1", "positive": "VBUS", "negative": "GND", "dc": 5.0},
            ],
        },
        "assertions": [
            {"name": "x", "measurement": {"kind": "final", "signal": "VMID"},
             "op": "<", "value": 5},
        ],
    })
    with pytest.raises(TestbenchValidationError) as exc:
        parse_testbench_response(raw, rc_spec())
    joined = "\n".join(exc.value.errors)
    assert "'VBUS', which the circuit does not have" in joined


def test_parse_forces_strict_and_tolerates_a_code_fence():
    answer = dict(RC_TESTBENCH)
    answer["testbench"] = {**RC_TESTBENCH["testbench"], "strict": False}
    fenced = "```json\n" + json.dumps(answer) + "\n```"
    proposal = parse_testbench_response(fenced, rc_spec())
    assert proposal.bench.strict is True
    assert [a.name for a in proposal.assertions] == [
        a["name"] for a in RC_TESTBENCH["assertions"]
    ]
    assert proposal.critical == (True, False, True)


def test_parse_needs_at_least_one_clause():
    raw = json.dumps({"testbench": RC_TESTBENCH["testbench"], "assertions": []})
    with pytest.raises(TestbenchValidationError) as exc:
        parse_testbench_response(raw, rc_spec())
    assert any("at least one clause" in e for e in exc.value.errors)


# ------------------------------------------------------ ran: the decision


def test_a_passing_run_has_every_clause_with_a_positive_margin():
    result, events = _stage(rc_model(), simulator=FakeSimulator())
    assert result.status == "ran" and result.ran
    assert result.passed is True
    assert result.findings == []
    assert result.analysis == "tran" and result.simulator == "fake"
    assert [c.name for c in result.clauses] == [
        a["name"] for a in RC_TESTBENCH["assertions"]
    ]
    assert all(c.passed for c in result.clauses)
    # Closed-form input, so the settle clause measures exactly its expectation
    # and the abs-max clause's margin is the headroom to 5.5 V, signed.
    settle, rise, ceiling = result.clauses
    assert settle.measured == pytest.approx(SETTLED_AT_PULSE_END, rel=1e-6)
    assert rise.measured == pytest.approx(EXPECTED_RISE, rel=1e-3)
    assert ceiling.margin == pytest.approx(5.5 - SETTLED_AT_PULSE_END, rel=1e-6)
    assert ceiling.margin > 0
    assert [e["event"] for e in events] == [
        "stage.start", *["simulation.clause"] * 3, "stage.done",
    ]
    done = events[-1]
    assert done["stage"] == "simulation" and done["status"] == "ran"
    assert done["passed"] is True
    assert (done["clauses"], done["failed"], done["blockers"]) == (3, 0, 0)
    assert result.note() == "simulation: 3/3 clause(s) passed"


def test_a_failed_clause_becomes_a_finding_blocker_if_critical_else_warning():
    """The decision, stated once: a failed clause never fails the run; it is
    a finding on the simulation result, severity by the clause's flag."""
    # Settles 20% low (critical clause fails) and twice as slow (the
    # non-critical rise clause fails); the ceiling clause still holds.
    sim = FakeSimulator(final=0.8 * EXPECTED_FINAL, tau_scale=2.0)
    result, events = _stage(rc_model(), simulator=sim)
    assert result.status == "ran"
    assert result.passed is False
    settle, rise, ceiling = result.clauses
    assert (settle.passed, rise.passed, ceiling.passed) == (False, False, True)
    # Signed margins: negative on the two failures, positive on the pass.
    assert settle.margin < 0 and rise.margin < 0 and ceiling.margin > 0
    # Twice the time constant means only 1 - e^-5 of the (already 20% low)
    # level is reached when the pulse ends; the margin is minus that gap.
    reached = 0.8 * EXPECTED_FINAL * (1.0 - math.exp(-PULSE_WIDTH / (2.0 * TAU)))
    assert settle.margin == pytest.approx(-(EXPECTED_FINAL - reached), rel=1e-6)
    assert [(f.severity, f.clause) for f in result.findings] == [
        ("blocker", settle.name),
        ("warning", rise.name),
    ]
    blocker = result.findings[0]
    assert blocker.title.startswith("simulation:")
    assert f"{settle.measured:g}" in blocker.detail
    assert "margin -" in blocker.detail
    assert blocker.measured == settle.measured and blocker.unit == "V"
    assert [f.clause for f in result.blockers] == [settle.name]
    done = events[-1]
    assert (done["clauses"], done["failed"], done["blockers"]) == (3, 2, 1)
    assert done["passed"] is False
    clause_events = [e for e in events if e["event"] == "simulation.clause"]
    assert [(e["passed"], e["critical"]) for e in clause_events] == [
        (False, True), (False, False), (True, True),
    ]
    assert all(e["margin"] is not None for e in clause_events)
    # The strict testbench reached the deck.
    assert sim.decks and "R1 " in sim.decks[0]


def test_a_clause_that_cannot_be_measured_fails_rather_than_passing():
    answer = json.loads(json.dumps(RC_TESTBENCH))
    answer["assertions"][1]["measurement"]["window"] = [1.5e-3, 2e-3]
    # No rising edge in the decay window: the measurement has no answer.
    result, _ = _stage(rc_model(json.dumps(answer)), simulator=FakeSimulator())
    assert result.status == "ran"
    rise = result.clauses[1]
    assert rise.passed is False and rise.measured is None and rise.error
    assert rise.margin is None
    assert [f.severity for f in result.findings] == ["warning"]
    assert "never crosses" in result.findings[0].detail


def test_a_simulator_failure_is_named_not_swallowed():
    sim = FakeSimulator(raise_with=SimulationFailed("ngspice failed (exit 1)"))
    result, events = _stage(rc_model(), simulator=sim)
    assert result.status == "failed"
    assert result.passed is None and result.clauses == []
    assert "SimulationFailed" in result.detail and "exit 1" in result.detail
    assert result.warnings and "not simulated" in result.warnings[0]
    assert [e["event"] for e in events] == [
        "stage.start", "simulation.failed", "stage.done",
    ]
    assert "exit 1" in events[1]["error"]
    assert events[-1]["status"] == "failed"


def test_the_status_vocabulary_is_closed():
    assert set(SIMULATION_STATUSES) == {
        "ran", "unavailable", "no_testbench", "unsimulatable", "failed",
    }
    with pytest.raises(ValueError):
        SimulationResult(status="skipped")


def test_as_dict_is_json_safe_and_complete():
    result, _ = _stage(rc_model(), simulator=FakeSimulator(final=4.0))
    payload = json.loads(json.dumps(result.as_dict()))
    assert set(payload) == {
        "status", "passed", "clauses", "findings", "warnings", "detail",
        "parts", "substitutions", "generic_parts", "analysis", "simulator",
        "repair_rounds",
    }
    assert payload["status"] == "ran" and payload["passed"] is False
    assert set(payload["clauses"][0]) == {
        "name", "passed", "measured", "expected", "op", "unit", "margin",
        "description", "error", "critical",
    }
    assert set(payload["findings"][0]) == {
        "severity", "title", "detail", "clause", "measured", "expected",
        "margin", "unit",
    }


# ---------------------------------------------------------------- pipeline


def test_pipeline_runs_the_lane_beside_schematic_and_route(tmp_path):
    events = []
    result = generate_pcb(
        rc_pipeline_model(), RC_INTENT, output=tmp_path / "board.kicad_pcb",
        time_limit_s=10.0, on_event=events.append, engine="sdk",
        simulate=True, simulator=FakeSimulator(),
    )
    main, lane = _simulation_lanes(events)
    # Two worker lanes are split out of the driver's line: the verdict, and
    # the critic -- which now answers inside place's solve rather than on the
    # tail of the run, so its frames land somewhere in the middle of this
    # stream in wall-clock order. Within each lane the order is frozen;
    # across lanes only the bounds are, and _assert_simulation_bounds pins
    # them along with the one-worker-call-at-a-time guarantee.
    review = [e for e in events if e.get("stage") == "review"]
    assert [e["stage"] for e in main if e["event"].startswith("stage.")] == [
        "propose", "propose", "place", "place",
        "schematic", "schematic", "route", "route",
    ]
    assert [e["event"] for e in review] == [
        "stage.start", "model.call", "stage.done",
    ]
    assert [e["event"] for e in lane] == [
        "stage.start", "model.call", *["simulation.clause"] * 3, "stage.done",
    ]
    _assert_simulation_bounds(events)
    # The thread's model call is attributed to its own stage.
    assert [e["stage"] for e in lane if e["event"] == "model.call"] == ["simulation"]
    assert [e["stage"] for e in main if e["event"] == "model.call"] == ["propose"]
    # The critic is joined before the verdict lane is started, which is why
    # the two never race for the model.
    assert events.index(review[-1]) < events.index(lane[0])
    assert result.simulation is not None and result.simulation.passed is True
    # The critic's list is untouched; the verdict lives on its own result.
    assert result.findings == [] and result.simulation.findings == []
    assert "simulation: 3/3 clause(s) passed" in result.summary()
    assert result.board_path is not None and result.board_path.exists()


def test_pipeline_with_a_device_reports_unsimulatable_and_still_builds(
    tmp_path, offline_pdf_fetch
):
    model = _scripted_pipeline_model()
    model.by_marker[SIMULATE_MARKER] = json.dumps(RC_TESTBENCH)
    events = []
    result = generate_pcb(
        model, "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb", time_limit_s=15.0,
        on_event=events.append, engine="sdk", simulate=True,
    )
    _, lane = _simulation_lanes(events)
    # No model call in the lane: decided from the spec alone.
    assert [e["event"] for e in lane] == ["stage.start", "stage.done"]
    assert lane[-1]["status"] == "unsimulatable"
    assert lane[-1]["parts"] == ["DRV8837"]
    _assert_simulation_bounds(events)
    assert result.simulation is not None
    assert result.simulation.status == "unsimulatable"
    assert "simulation unsimulatable" in result.summary()
    # The critic still ran and its blocker is still its own.
    assert len(result.findings) == 1
    assert result.board_path is not None and result.board_path.exists()


@needs_adk
def test_both_drivers_emit_identical_simulation_events(tmp_path):
    from silkscreen.agents.adk import generate_pcb_adk
    from silkscreen.agents.pipeline import _generate_pcb_sdk

    def run(driver, where):
        where.mkdir()
        events = []
        result = driver(
            rc_pipeline_model(), RC_INTENT, output=where / "board.kicad_pcb",
            time_limit_s=10.0, on_event=events.append,
            simulate=True, simulator=FakeSimulator(),
        )
        return result, events

    sdk_result, sdk_events = run(_generate_pcb_sdk, tmp_path / "sdk")
    adk_result, adk_events = run(generate_pcb_adk, tmp_path / "adk")
    # Three lanes, not two: the critic is a worker lane of its own now, and
    # comparing only main and simulation would have quietly dropped review
    # from this parity contract the moment it moved off the driver thread.
    def review_lane(evs):
        return [e for e in evs if e.get("stage") == "review"]

    picks = (
        lambda evs: _simulation_lanes(evs)[0],
        lambda evs: _simulation_lanes(evs)[1],
        review_lane,
    )
    for pick in picks:
        strip = [
            [{k: v for k, v in e.items() if k != "t_s"} for e in pick(evs)]
            for evs in (sdk_events, adk_events)
        ]
        assert strip[0] == strip[1]
    assert review_lane(sdk_events), "the critic ran, so it has a lane to compare"
    _assert_simulation_bounds(sdk_events)
    _assert_simulation_bounds(adk_events)
    assert adk_result.simulation is not None
    assert adk_result.simulation.as_dict() == sdk_result.simulation.as_dict()


# ---------------------------------------------------------------- /generate


def test_generate_block_is_additive_and_only_on_request():
    from service.app import RequestError, generate
    from service.cache import MemoryFactStore

    def call(payload):
        return generate(payload, model=rc_pipeline_model(), store=MemoryFactStore())

    body = call({"intent": RC_INTENT, "time_limit_s": 5})
    assert "simulation" not in body

    body = call({"intent": RC_INTENT, "time_limit_s": 5, "simulate": True})
    assert "kicad_pcb" in body and "placements" in body
    block = body["simulation"]
    # The service runs no fake simulator, so the block's status is whatever
    # this machine can do; both are honest and both carry the vocabulary.
    assert block["status"] in {"ran", "unavailable"}
    if block["status"] == "ran":
        assert block["passed"] is True and len(block["clauses"]) == 3
    else:
        assert "install ngspice" in block["detail"]
    assert not any("simulation did not run" in w for w in body["warnings"])

    with pytest.raises(RequestError, match="'simulate' must be a boolean"):
        call({"intent": RC_INTENT, "time_limit_s": 5, "simulate": "yes"})


def test_generate_block_names_the_unsimulatable_parts(offline_pdf_fetch):
    from service.app import generate
    from service.cache import MemoryFactStore

    model = _scripted_pipeline_model()
    model.by_marker[SIMULATE_MARKER] = json.dumps(RC_TESTBENCH)
    body = generate(
        {"intent": "a 3.3V motor driver board", "time_limit_s": 5, "simulate": True},
        model=model, store=MemoryFactStore(),
    )
    assert body["simulation"]["status"] == "unsimulatable"
    assert body["simulation"]["parts"] == ["DRV8837"]
    assert body["simulation"]["passed"] is None


# ------------------------------------------------------------ real ngspice


@needs_ngspice
def test_real_ngspice_verifies_the_rc_against_closed_form():
    """The one gated end-to-end run: the scripted testbench through a real
    simulator, the measured rise time checked against ``tau * ln(9)`` and
    the settled level against the divider, both computed inline above."""
    result = simulate_circuit(rc_model(), rc_spec(), intent=RC_INTENT)
    assert result.status == "ran", result.detail
    assert result.passed is True, [c.line() for c in result.failed]
    assert result.simulator.startswith("ngspice")
    settle, rise, ceiling = result.clauses
    assert settle.measured == pytest.approx(EXPECTED_FINAL, rel=0.01)
    assert rise.measured == pytest.approx(EXPECTED_RISE, rel=0.02)
    assert ceiling.measured < 5.5 and ceiling.margin > 0
    assert result.findings == []

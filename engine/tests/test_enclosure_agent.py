"""Workstream C: the enclosure agent loop and its stage wiring.

ScriptedModel-driven and offline throughout, per the seam in
:mod:`silkscreen.agents.model`. The kernel is the only enclosure engine
(docs/ai-cad-plan.md v3, 2026-09-08), so it is faked by default here and the
few tests that want the real build123d gate on it exactly the way
``test_spice.py`` gates on ngspice (``needs_build123d``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from silkscreen.agents import ModelError, ScriptedModel, generate_pcb
from silkscreen.agents import enclosure as agent_enclosure
from silkscreen.agents.enclosure import (
    ENCLOSURE_PROMPT,
    EnclosureProposal,
    EnclosureProposalError,
    propose_enclosure,
)
from silkscreen.enclosure import cad
from silkscreen.enclosure.board_shape import BoardEnvelope, MountingHole, PartExtent
from silkscreen.enclosure.cad import ExportPaths
from silkscreen.enclosure.errors import (
    CutoutError,
    KernelError,
    KernelUnavailable,
)
from silkscreen.enclosure.ir import parse_enclosure_spec
from silkscreen.enclosure.kernel import CLAUSES, Clause, KernelReport
from silkscreen.packing import Layer
from silkscreen.units import mm

# The circuits and the pipeline model are the SDK suite's own, imported rather
# than copied so the enclosure stage is provably driven through the same
# pipeline bytes every other stage test uses.
from test_agents import _scripted_pipeline_model

# ---------------------------------------------------------------- fixtures


# These tests drive the pipeline through the SDK driver; the ADK parity test
# at the bottom exercises the other one explicitly.
@pytest.fixture(autouse=True)
def _pin_sdk_engine(monkeypatch):
    monkeypatch.setenv("SILKSCREEN_ENGINE", "sdk")


HAS_BUILD123D = cad.kernel_available()
needs_build123d = pytest.mark.skipif(
    not HAS_BUILD123D, reason="build123d (the 'cad' extra) is not installed"
)

#: The real kernel entry points, captured before the autouse fake replaces
#: them, so a ``needs_build123d`` test can put them back.
_REAL_BUILD = agent_enclosure.build_enclosure
_REAL_VERIFY = agent_enclosure.verify_model

#: What the fake exporter writes as the STEP assembly. ISO 10303-21 is ASCII,
#: which is what lets the one-shot route ship a whole case inside JSON.
STEP_TEXT = "ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"

INTENT = "a 3.3V motor driver board"
SHEETS = {"AMS1117-3.3": "https://x/ams1117.pdf"}

GOOD_ENCLOSURE = {
    "wall_mm": 2.0,
    "clearance_mm": 1.0,
    "corner_radius_mm": 2.0,
    "lid": "lip",
    "cutouts": [],
    "standoffs": True,
    "vents": False,
    "label": "silkscreen",
}

#: A proposal that parses but cannot print: two independent problems, so the
#: repair-prompt tests can assert the batch arrives whole.
BAD_ENCLOSURE = {
    "wall_mm": 0.5,
    "lid": "zip",
    "cutouts": [],
    "standoffs": True,
    "vents": False,
}


def _envelope() -> BoardEnvelope:
    """A 40 x 30 mm board with two parts of known (non-defaulted) height.

    Built directly rather than via ``board_envelope`` so the agent tests need
    no board file: the loop's contract is against the envelope dataclass.
    ``J1`` sits against the left edge so a left-face cutout is legitimate.
    """
    u1 = PartExtent(
        ref="U1",
        x_min_nm=mm(15.0), y_min_nm=mm(10.0), x_max_nm=mm(25.0), y_max_nm=mm(20.0),
        height_nm=mm(1.75), height_default=False,
    )
    j1 = PartExtent(
        ref="J1",
        x_min_nm=mm(0.5), y_min_nm=mm(12.0), x_max_nm=mm(8.0), y_max_nm=mm(18.0),
        height_nm=mm(3.2), height_default=False,
    )
    return BoardEnvelope(
        outline_nm=((0, 0), (mm(40.0), 0), (mm(40.0), mm(30.0)), (0, mm(30.0))),
        x_min_nm=0, y_min_nm=0, x_max_nm=mm(40.0), y_max_nm=mm(30.0),
        thickness_nm=mm(1.6),
        parts=(u1, j1),
        max_height_nm=mm(3.2),
    )


# ---------------------------------------------------------------- propose


def test_propose_accepts_a_valid_spec_first_try():
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    result = propose_enclosure(model, _envelope())
    assert isinstance(result, EnclosureProposal)
    spec, rounds = result.spec, result.repair_rounds
    assert rounds == 0
    assert result.brief.startswith("Board outline: 40.00 x 30.00 mm")
    assert spec.lid == "lip"
    assert spec.wall_nm == mm(2.0)
    # The kernel report is the receipt, and it rides along rather than being
    # discarded: no caller re-verifies a spec the loop already verified.
    assert result.kernel is not None and result.kernel.passed


def test_prompt_carries_the_frozen_marker_and_measured_facts():
    """The model chooses style within bounds; the measured board is injected
    into the prompt deterministically, never invented by the model."""
    assert "ENCLOSURE-SPEC v2" in ENCLOSURE_PROMPT
    # Other workstreams' ScriptedModels key on the v1 literal; it stays.
    assert "ENCLOSURE-SPEC v1" in ENCLOSURE_PROMPT
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    propose_enclosure(model, _envelope(), style_hint="rounded corners")
    prompt = model.calls[0]["prompt"]
    assert "ENCLOSURE-SPEC v2" in prompt
    assert "40.00 x 30.00 mm" in prompt          # outline size
    assert "U1" in prompt and "J1" in prompt     # part rects by ref
    assert "3.20 mm tall" in prompt              # heights
    assert "near faces: left" in prompt          # edge-adjacent ref -> face
    assert "rounded corners" in prompt           # the style hint


def test_repair_prompt_batches_every_validation_error():
    model = ScriptedModel(
        responses=[json.dumps(BAD_ENCLOSURE), json.dumps(GOOD_ENCLOSURE)]
    )
    result = propose_enclosure(model, _envelope())
    spec, rounds = result.spec, result.repair_rounds
    assert rounds == 1
    repair = model.calls[1]["prompt"]
    # Both problems in one prompt -- the batched-ValidationError convention.
    assert "below the printable FDM minimum" in repair
    assert "'lid' is 'zip'" in repair
    assert "Fix ALL of these" in repair
    # The previous proposal rides along so the model can diff itself.
    assert json.dumps(BAD_ENCLOSURE) in repair
    assert spec.wall_nm == mm(2.0)


# ------------------------------------------------------------- fast vs rigorous


def test_fast_mode_makes_at_most_one_repair_round():
    """Fast-mode budget: one repair round, then give up loudly. The third
    (good) response proves the loop stopped asking, not that it ran out."""
    bad = json.dumps(BAD_ENCLOSURE)
    model = ScriptedModel(responses=[bad, bad, json.dumps(GOOD_ENCLOSURE)])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope())
    assert excinfo.value.attempts == 2
    assert len(model.calls) == 2


def test_rigorous_mode_keeps_the_three_round_budget():
    bad = json.dumps(BAD_ENCLOSURE)
    model = ScriptedModel(responses=[bad, bad, bad, bad])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope(), rigorous=True)
    assert excinfo.value.attempts == 4


def test_fast_mode_still_repairs_spec_validation_errors():
    """Speed skips the fit gate, not the parser: an unparseable proposal is
    still sent back, within the one-round budget."""
    model = ScriptedModel(
        responses=[json.dumps(BAD_ENCLOSURE), json.dumps(GOOD_ENCLOSURE)]
    )
    result = propose_enclosure(model, _envelope())
    assert result.repair_rounds == 1
    assert result.spec.wall_nm == mm(2.0)


def test_each_rejected_round_emits_an_enclosure_round_event():
    model = ScriptedModel(
        responses=[json.dumps(BAD_ENCLOSURE), json.dumps(GOOD_ENCLOSURE)]
    )
    events = []
    propose_enclosure(model, _envelope(), on_event=events.append)
    rounds = [e for e in events if e["event"] == "enclosure.round"]
    assert len(rounds) == 1
    event = rounds[0]
    assert event["round"] == 1
    assert event["errors"] == 2
    assert event["first_error"]
    assert len(event["first_error"]) <= 160


def test_budget_exhaustion_raises_carrying_the_attempt_count():
    bad = json.dumps(BAD_ENCLOSURE)
    model = ScriptedModel(responses=[bad, bad, bad])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope(), max_repairs=2)
    assert excinfo.value.attempts == 3
    assert "below the printable FDM minimum" in str(excinfo.value)


def test_model_error_propagates_unwrapped():
    """An upstream outage is not a bad proposal; FallbackModel and the
    service's 502 both depend on ModelError leaving as itself."""
    with pytest.raises(ModelError):
        propose_enclosure(ScriptedModel(responses=[]), _envelope())


# ---------------------------------------------------------------- v2 prompt


def test_v2_prompt_offers_the_new_fields_and_explains_them():
    for field in ('"mount"', '"insert"', '"material"', '"snap"'):
        assert field in ENCLOSURE_PROMPT
    for choice in ("holes", "pins", "corners", "M2.5", "self_tap", "PETG"):
        assert choice in ENCLOSURE_PROMPT
    # The three v2 rules the v1 emitter violated (plan v2, "what the field says").
    assert "PLUG" in ENCLOSURE_PROMPT                       # openings from the plug
    assert "mounting holes" in ENCLOSURE_PROMPT             # standoffs at the holes
    assert "Every dimension is measured" in ENCLOSURE_PROMPT


def _v2_envelope() -> BoardEnvelope:
    """The 40 x 30 board with two mounting holes, a USB-C connector on the
    left edge, and a bottom-side part."""
    base = _envelope()
    usb = PartExtent(
        ref="J1",
        x_min_nm=mm(0.5), y_min_nm=mm(12.0), x_max_nm=mm(8.0), y_max_nm=mm(18.0),
        height_nm=mm(3.2), height_default=False,
        connector="USB_C", lib_id="Connector_USB:USB_C_Receptacle",
    )
    c1 = PartExtent(
        ref="C1",
        x_min_nm=mm(30.0), y_min_nm=mm(5.0), x_max_nm=mm(32.0), y_max_nm=mm(6.0),
        height_nm=mm(1.0), height_default=False, side=Layer.BOTTOM,
    )
    return BoardEnvelope(
        outline_nm=base.outline_nm,
        x_min_nm=base.x_min_nm, y_min_nm=base.y_min_nm,
        x_max_nm=base.x_max_nm, y_max_nm=base.y_max_nm,
        thickness_nm=base.thickness_nm,
        parts=(base.parts[0], usb, c1),
        max_height_nm=base.max_height_nm,
        max_height_bottom_nm=mm(1.0),
        mounting_holes=(
            MountingHole(ref="H1", x_nm=mm(4.0), y_nm=mm(4.0), drill_nm=mm(3.2)),
            MountingHole(ref="H2", x_nm=mm(36.0), y_nm=mm(26.0), drill_nm=mm(3.2)),
        ),
    )


def test_facts_block_states_holes_plugs_and_sides_in_the_emitter_frame():
    facts = agent_enclosure._facts_block(_v2_envelope())
    # front = KiCad max-Y: H1 at y=4 on a 30 mm board is 26 mm from the front.
    assert "H1: 3.20 mm drill, 4.00 mm from left, 26.00 mm from front" in facts
    assert "H2: 3.20 mm drill, 36.00 mm from left, 4.00 mm from front" in facts
    assert "No mounting holes" not in facts
    assert "connector USB_C (opening sized from the USB-C plug)" in facts
    assert "C1: 2.00 x 1.00 mm footprint, 1.00 mm tall; bottom side" in facts
    assert "Tallest bottom-side part: 1.00 mm below the board" in facts
    # Byte-stable: the same envelope renders the same text.
    assert facts == agent_enclosure._facts_block(_v2_envelope())


def test_facts_block_says_no_holes_in_the_exact_sentence():
    facts = agent_enclosure._facts_block(_envelope())
    assert (
        "No mounting holes: choose mount corners, pins or none, never holes."
        in facts
    )
    assert "drill" not in facts
    assert "bottom side" not in facts and "bottom-side" not in facts


# ---------------------------------------------------------------- kernel loop


def _report(failed: tuple[str, ...] = ()) -> KernelReport:
    """A KernelReport over every clause, the named ones failing."""
    return KernelReport(
        clauses=tuple(
            Clause(
                name=name,
                passed=name not in failed,
                margin_nm=-mm(0.5) if name in failed else mm(1.0),
                detail=f"{name} measured",
            )
            for name in CLAUSES
        )
    )


class _FakeBuilt:
    """Stand-in for ``EnclosureModel``: the loop reads only ``warnings``.

    A bare sentinel would hide the bug this stands in for -- the builder's
    "I widened your clearance" notes never leaving the model -- so the
    stand-in carries the one attribute the loop is supposed to read.
    """

    def __init__(self, name: str = "model", warnings: tuple[str, ...] = ()):
        self.name = name
        self.warnings = tuple(warnings)


class _FakeKernel:
    """Scripted stand-ins for build_enclosure / verify_model / render_grid,
    plus the stage's export_model / render_packet.

    ``builds`` and ``reports`` are consumed per call, so a test can script a
    KernelError on round one and a clean build on round two. The exporters
    are here because the kernel is now the only path: every pipeline test
    goes through them.
    """

    def __init__(self, builds=None, reports=None, png=b"\x89PNG-fake"):
        self.builds = list(builds or [])
        self.reports = list(reports or [])
        self.png = png
        self.build_calls = 0
        self.verify_calls = 0
        self.render_calls = 0
        self.export_calls: list[tuple] = []
        self.packet_calls: list[Path] = []

    def build(self, spec, envelope):
        self.build_calls += 1
        outcome = self.builds.pop(0) if self.builds else _FakeBuilt()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def verify(self, model, spec, envelope):
        self.verify_calls += 1
        return self.reports.pop(0) if self.reports else _report()

    def render(self, model, **_):
        self.render_calls += 1
        if isinstance(self.png, Exception):
            raise self.png
        return self.png

    def export(self, model, directory, stem="enclosure"):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.export_calls.append((directory, stem))
        paths = ExportPaths(
            step=directory / f"{stem}.step",
            base_stl=directory / f"{stem}-base.stl",
            lid_stl=directory / f"{stem}-lid.stl",
        )
        paths.step.write_text(STEP_TEXT, encoding="utf-8")
        paths.base_stl.write_text("solid base\nendsolid base\n")
        paths.lid_stl.write_text("solid lid\nendsolid lid\n")
        return paths

    def packet(self, model, out_dir, **_):
        self.packet_calls.append(Path(out_dir))
        return ()


# The kernel is the only engine, so it is installed for every test in this
# file; the two ``needs_build123d`` tests put the real one back explicitly.
@pytest.fixture(autouse=True)
def _fake_kernel(monkeypatch):
    from silkscreen.agents import stages

    fake = _FakeKernel()
    monkeypatch.setattr(agent_enclosure, "kernel_available", lambda: True)
    monkeypatch.setattr(agent_enclosure, "build_enclosure", fake.build)
    monkeypatch.setattr(agent_enclosure, "verify_model", fake.verify)
    monkeypatch.setattr(agent_enclosure, "render_grid", fake.render)
    monkeypatch.setattr(stages, "export_model", fake.export)
    monkeypatch.setattr(stages, "render_packet", fake.packet)
    return fake


@pytest.fixture
def real_kernel(monkeypatch):
    """Put the genuine build123d entry points back for one test."""
    monkeypatch.setattr(agent_enclosure, "kernel_available", cad.kernel_available)
    monkeypatch.setattr(agent_enclosure, "build_enclosure", _REAL_BUILD)
    monkeypatch.setattr(agent_enclosure, "verify_model", _REAL_VERIFY)


def test_no_kernel_refuses_before_a_model_call_is_spent(monkeypatch):
    """The one unacceptable outcome is a silent degrade to a worse case
    (docs/ai-cad-plan.md v3). Without build123d there is no case at all, and
    the refusal names the extra -- before the model is asked to style
    something that cannot be built."""
    def boom(*a, **k):
        raise AssertionError("the kernel was touched after it was refused")

    monkeypatch.setattr(agent_enclosure, "kernel_available", lambda: False)
    monkeypatch.setattr(agent_enclosure, "build_enclosure", boom)
    monkeypatch.setattr(agent_enclosure, "verify_model", boom)
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    with pytest.raises(KernelUnavailable) as excinfo:
        propose_enclosure(model, _envelope())
    message = str(excinfo.value)
    assert "build123d" in message
    assert 'pip install -e ".[cad]"' in message
    # Truncated to 160 chars on the event wire: the hint has to survive that.
    assert len(message) <= 160
    assert model.calls == []


def test_kernel_returns_the_model_and_its_report(_fake_kernel):
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    result = propose_enclosure(model, _envelope())
    assert result.model.name == "model"
    assert result.kernel is not None and result.kernel.passed
    assert result.repair_rounds == 0
    assert _fake_kernel.build_calls == 1 and _fake_kernel.verify_calls == 1


def test_build_warnings_ride_out_on_the_kernel_report(_fake_kernel):
    """Every "I changed your spec to make it buildable" note the builder
    writes reaches the receipt, prefixed so its author is named.

    Without this the model quietly widens a clearance or shortens a bore and
    the run reports nothing -- the quiet zero, one layer up.
    """
    _fake_kernel.builds = [
        _FakeBuilt(warnings=("clearance widened 1.00 -> 1.60 mm", "label skipped"))
    ]
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    result = propose_enclosure(model, _envelope())
    assert result.kernel.warnings == (
        "build: clearance widened 1.00 -> 1.60 mm",
        "build: label skipped",
    )
    assert "WARN build: clearance widened 1.00 -> 1.60 mm" in result.kernel.text()


def test_a_build_with_nothing_to_report_adds_no_warnings(_fake_kernel):
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    assert propose_enclosure(model, _envelope()).kernel.warnings == ()


@needs_build123d
def test_real_build_warnings_reach_the_kernel_report(real_kernel):
    """The same thing on the real kernel: this envelope has no mounting
    holes, so ``mount: holes`` cannot be honoured and the builder falls back
    to corner standoffs. The user is told, on the report."""
    spec = dict(GOOD_ENCLOSURE, mount="holes", insert="M3")
    model = ScriptedModel(responses=[json.dumps(spec)])
    result = propose_enclosure(model, _envelope())
    build = [w for w in result.kernel.warnings if w.startswith("build: ")]
    assert build, result.kernel.text()
    assert any("fell back" in w for w in build), build


def test_kernel_error_becomes_a_repair_item_naming_the_failure_class(_fake_kernel):
    _fake_kernel.builds = [KernelError("BOOLEAN_FAILED", "cutout usb missed the wall")]
    good = json.dumps(GOOD_ENCLOSURE)
    model = ScriptedModel(responses=[good, good])
    events = []
    result = propose_enclosure(model, _envelope(), on_event=events.append)
    assert result.repair_rounds == 1
    repair = model.calls[1]["prompt"]
    assert "BOOLEAN_FAILED: cutout usb missed the wall" in repair
    assert "Fix ALL of these" in repair
    assert events[0]["event"] == "enclosure.round"
    assert "BOOLEAN_FAILED" in events[0]["first_error"]
    assert result.model.name == "model"


def test_cutout_error_from_the_build_is_repaired_too(_fake_kernel):
    _fake_kernel.builds = [CutoutError("cutout usb: J1 is nowhere near the front face")]
    good = json.dumps(GOOD_ENCLOSURE)
    model = ScriptedModel(responses=[good, good])
    result = propose_enclosure(model, _envelope())
    assert result.repair_rounds == 1
    assert "nowhere near the front face" in model.calls[1]["prompt"]


def test_rigorous_repairs_on_a_failing_clause_and_accepts_a_passing_report(
    _fake_kernel,
):
    _fake_kernel.reports = [_report(failed=("board_clash", "min_wall")), _report()]
    good = json.dumps(GOOD_ENCLOSURE)
    model = ScriptedModel(responses=[good, good])
    events = []
    result = propose_enclosure(
        model, _envelope(), rigorous=True, on_event=events.append
    )
    assert result.repair_rounds == 1
    assert result.kernel.passed
    repair = model.calls[1]["prompt"]
    # The failing clause lines of report.text(), and only those.
    assert "FAIL board_clash margin=-0.500 mm: board_clash measured" in repair
    assert "FAIL min_wall" in repair
    assert "PASS" not in repair
    kernel_events = [e for e in events if e["event"] == "enclosure.kernel"]
    assert [e["passed"] for e in kernel_events] == [False, True]
    assert kernel_events[0]["failed"] == ["board_clash", "min_wall"]
    assert kernel_events[1]["failed"] == []
    assert [e["round"] for e in kernel_events] == [1, 2]


def test_fast_mode_ships_the_first_build_with_a_failing_report_attached(
    _fake_kernel,
):
    """The honest receipt: fast mode accepts what builds, and the failing
    clauses ride ``kernel`` rather than vanishing."""
    _fake_kernel.reports = [_report(failed=("headroom",))]
    # One response: a repair round would raise ModelError.
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    events = []
    result = propose_enclosure(model, _envelope(), on_event=events.append)
    assert result.repair_rounds == 0
    assert result.kernel is not None
    assert not result.kernel.passed
    assert result.kernel.failed == ["headroom"]
    assert not any(e["event"] == "enclosure.round" for e in events)
    assert [e["event"] for e in events] == ["enclosure.kernel"]


def test_fast_mode_repairs_a_kernel_error_once_then_gives_up(_fake_kernel):
    _fake_kernel.builds = [
        KernelError("FILLET_FAILED", "radius exceeds the wall"),
        KernelError("FILLET_FAILED", "radius exceeds the wall"),
        _FakeBuilt(),
    ]
    good = json.dumps(GOOD_ENCLOSURE)
    model = ScriptedModel(responses=[good, good, good])
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope())
    assert excinfo.value.attempts == 2
    assert "FILLET_FAILED" in str(excinfo.value)
    assert len(model.calls) == 2


def test_rigorous_budget_exhaustion_on_kernel_clauses_raises_with_attempts(
    _fake_kernel,
):
    _fake_kernel.reports = [_report(failed=("lid_mates",))] * 4
    good = json.dumps(GOOD_ENCLOSURE)
    model = ScriptedModel(responses=[good] * 4)
    with pytest.raises(EnclosureProposalError) as excinfo:
        propose_enclosure(model, _envelope(), rigorous=True)
    assert excinfo.value.attempts == 4
    assert "FAIL lid_mates" in str(excinfo.value)


# ---------------------------------------------------------------- critic


def test_critic_is_off_by_default_and_skipped_without_the_kernel(_fake_kernel):
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    result = propose_enclosure(model, _envelope())
    assert _fake_kernel.render_calls == 0
    assert result.kernel.warnings == ()
    assert len(model.calls) == 1


def test_critic_findings_are_filtered_and_appended_never_a_gate(_fake_kernel):
    findings = [
        {"ref": "J1", "finding": "the opening is on the wrong side of the board"},
        {"ref": "J9", "finding": "phantom part"},           # not on the board
        {"ref": "usb", "finding": "cutout is too low"},     # a cutout id: kept
        {"ref": "", "finding": "no ref at all"},
    ]
    spec = dict(
        GOOD_ENCLOSURE,
        cutouts=[{"id": "usb", "ref": "J1", "face": "left", "margin_mm": 0.5}],
    )
    model = ScriptedModel(
        responses=[
            json.dumps(spec),
            json.dumps(findings),                        # critic round 1
            json.dumps(findings[:1] + findings[2:3]),    # refutation: stands by both
        ]
    )
    events = []
    result = propose_enclosure(
        model, _envelope(), critic=True, on_event=events.append
    )
    assert result.kernel.passed  # the critic never changes the gate
    assert result.kernel.warnings == (
        "critic: J1: the opening is on the wrong side of the board",
        "critic: usb: cutout is too low",
    )
    # The image travelled as an inline PNG Document, never a URL.
    critic_calls = [c for c in model.calls if "ENCLOSURE-CRITIC" in c["prompt"]]
    assert len(critic_calls) == 2  # findings, then one refutation round
    doc = critic_calls[0]["documents"][0]
    assert doc.mime_type == "image/png" and doc.data == b"\x89PNG-fake"
    assert doc.url is None
    assert "J1, U1, usb" in critic_calls[0]["prompt"]
    assert "still stand by" in critic_calls[1]["prompt"]
    assert [e["event"] for e in events] == [
        "enclosure.kernel", "enclosure.critic", "enclosure.critic",
    ]
    assert events[1]["findings"] == 2 and events[1]["dropped"] == 2


def test_critic_with_no_findings_makes_one_call(_fake_kernel):
    model = ScriptedModel(
        responses=[json.dumps(GOOD_ENCLOSURE)], by_marker={"ENCLOSURE-CRITIC": "[]"}
    )
    result = propose_enclosure(model, _envelope(), critic=True)
    assert result.kernel.warnings == ()
    assert len(model.calls) == 2
    assert _fake_kernel.render_calls == 1


def test_critic_findings_naming_only_unknown_refs_are_all_dropped(_fake_kernel):
    model = ScriptedModel(
        responses=[json.dumps(GOOD_ENCLOSURE)],
        by_marker={"ENCLOSURE-CRITIC": json.dumps([{"ref": "X9", "finding": "x"}])},
    )
    result = propose_enclosure(model, _envelope(), critic=True)
    assert result.kernel.warnings == ()
    assert len(model.calls) == 2  # nothing to refute


def test_critic_failures_become_a_warning_not_an_exception(_fake_kernel):
    # An unreadable answer...
    model = ScriptedModel(
        responses=[json.dumps(GOOD_ENCLOSURE)],
        by_marker={"ENCLOSURE-CRITIC": "I see nothing wrong, honestly."},
    )
    result = propose_enclosure(model, _envelope(), critic=True)
    assert len(result.kernel.warnings) == 1
    assert result.kernel.warnings[0].startswith("critic: unavailable (")
    # ...an unreachable model (the spec round succeeded first)...
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    result = propose_enclosure(model, _envelope(), critic=True)
    assert result.kernel.passed
    assert result.kernel.warnings[0].startswith("critic: unavailable (")
    # ...and a renderer that cannot draw.
    _fake_kernel.png = RuntimeError("no tessellation")
    model = ScriptedModel(responses=[json.dumps(GOOD_ENCLOSURE)])
    result = propose_enclosure(model, _envelope(), critic=True)
    assert result.kernel.warnings == (
        "critic: snapshot unavailable (RuntimeError: no tessellation)",
    )
    assert len(model.calls) == 1


@needs_build123d
def test_real_kernel_builds_and_verifies_the_demo_case():
    """Tier 2: the actual build123d path, gated like ngspice.
    Skips until workstreams A and B land their implementations."""
    from silkscreen.enclosure import kernel as kernel_mod

    spec = parse_enclosure_spec(json.dumps(GOOD_ENCLOSURE))
    try:
        built = cad.build_enclosure(spec, _envelope())
    except NotImplementedError:
        pytest.skip("cad.build_enclosure not implemented yet (workstream A)")
    try:
        report = kernel_mod.verify_model(built, spec, _envelope())
    except NotImplementedError:
        pytest.skip("kernel.verify_model not implemented yet (workstream B)")
    assert set(report.margins_nm) == set(CLAUSES)


# ---------------------------------------------------------------- pipeline


def _lanes(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split a stream into the main thread's events and the enclosure's.

    The case is designed on a worker thread from the moment placement lands,
    so its events interleave with schematic and route in wall-clock order.
    Within each lane the order is frozen; across lanes only the bounds are
    (started after place, and the critic joined before the case is started),
    and those are asserted on the whole stream by
    ``_assert_enclosure_bounds``.

    ``main`` is the *driver* thread's line, so the critic's frames come out of
    it too: review is a worker lane now as well, started at the validated spec
    so it answers inside place's solve, and leaving it in ``main`` would make
    these tests assert a wall-clock race.
    """
    case = [
        e for e in events
        if e.get("stage") == "enclosure" or e["event"].startswith("enclosure")
    ]
    main = [
        e for e in events
        if e not in case and e.get("stage") != "review"
    ]
    return main, case


def _assert_enclosure_bounds(events: list[dict]) -> None:
    """The lane's bounds, and the determinism guarantee they exist to protect.

    The upper bound here used to be "closed before review starts". That is no
    longer where the critic is: it needs only the validated spec, so it is
    started before ``place`` -- answering inside the solver budget instead of
    on the tail of the run -- and joined *before* this lane is started. Review
    is therefore upstream of the enclosure lane, not downstream, and cannot
    bound it.

    What that old bound really encoded was not "review is last" but "exactly
    one worker model call is ever in flight", which is what lets a
    ``ScriptedModel`` answer a run the same way twice. That guarantee is
    unchanged, and is asserted directly here rather than as a side effect of
    where review happened to sit: the lane opens only after place has closed,
    the lane closes at all, the critic has closed before the lane opens, and
    :func:`test_agents.assert_worker_call_order` holds over the whole stream.
    """
    from test_agents import assert_worker_call_order

    def at(event: str, stage: str) -> int:
        return next(
            i for i, e in enumerate(events)
            if e["event"] == event and e.get("stage") == stage
        )

    assert at("stage.start", "enclosure") > at("stage.done", "place")
    case_end = [
        i for i, e in enumerate(events)
        if (e["event"] == "stage.done" and e.get("stage") == "enclosure")
        or e["event"] == "enclosure.failed"
    ]
    assert case_end
    assert at("stage.done", "review") < at("stage.start", "enclosure")
    assert_worker_call_order(events)


def _pipeline_model(enclosure_json: str) -> ScriptedModel:
    model = _scripted_pipeline_model()
    model.by_marker["ENCLOSURE-SPEC"] = enclosure_json
    return model


def test_default_run_has_no_enclosure_and_no_enclosure_events(
    tmp_path, offline_pdf_fetch
):
    """Opt-in, the route_stage pattern: off means silent, not an empty pass."""
    events = []
    result = generate_pcb(
        _scripted_pipeline_model(),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
    )
    assert result.enclosure is None
    assert not any("enclosure" in str(e.get("stage", "")) for e in events)
    assert not any(e["event"].startswith("enclosure") for e in events)
    assert not list(tmp_path.glob("enclosure*"))


def test_enclosure_run_emits_the_frozen_events_and_writes_the_step(
    tmp_path, offline_pdf_fetch
):
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
        enclosure_style="rounded corners",
    )

    main, case = _lanes(events)
    # The frozen ordering, per lane: the main thread's stages are the ones
    # every run has, the case runs beside schematic and route, and the critic
    # runs on a lane of its own inside place's solve.
    review = [e for e in events if e.get("stage") == "review"]
    assert [e["stage"] for e in main if e["event"].startswith("stage.")] == [
        "read", "read", "propose", "propose", "place", "place",
        "schematic", "schematic", "route", "route",
    ]
    assert [e["stage"] for e in case if e["event"].startswith("stage.")] == [
        "enclosure", "enclosure",
    ]
    assert [e["event"] for e in review] == [
        "stage.start", "model.call", "stage.done",
    ]
    # Started after placement; the critic is joined before the case starts.
    _assert_enclosure_bounds(events)
    # The thread's model call is attributed to its own stage, whatever the
    # main thread had entered by the time the call was made.
    assert [e["stage"] for e in case if e["event"] == "model.call"] == ["enclosure"]
    assert [e["stage"] for e in main if e["event"] == "model.call"] == [
        "read", "propose",
    ]
    done = next(
        e for e in events
        if e["event"] == "stage.done" and e["stage"] == "enclosure"
    )
    assert done["cutouts"] == 0
    assert done["lid"] == "lip"
    assert done["wall_mm"] == 2.0
    assert done["repair_rounds"] == 0
    assert done["exports"] == [
        "enclosure.step", "enclosure-base.stl", "enclosure-lid.stl",
    ]

    assert result.enclosure is not None
    assert result.enclosure.repair_rounds == 0
    assert result.enclosure.kernel is not None
    step_path = tmp_path / "enclosure.step"
    assert result.enclosure.step_text == STEP_TEXT
    assert step_path.read_text(encoding="utf-8") == result.enclosure.step_text
    # The written STEP is a first-class artifact, reported like the rest.
    assert result.enclosure.exports.step == step_path
    assert step_path in result.artifacts
    assert result.artifacts.index(step_path) < result.artifacts.index(
        result.board_path
    ), "artifacts stay in the order the stages produced them"
    # The board is still the headline artifact.
    assert (tmp_path / "board.kicad_pcb").exists()


def test_enclosure_without_output_returns_step_text_but_writes_nothing(
    tmp_path, offline_pdf_fetch, _fake_kernel
):
    """The service path: no output means nothing durable, but a whole case
    still ships as STEP text."""
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        time_limit_s=15.0,
        enclosure=True,
    )
    assert result.enclosure is not None
    assert result.enclosure.step_text == STEP_TEXT
    assert result.enclosure.exports is None
    assert result.board_path is None
    assert list(tmp_path.iterdir()) == []
    # It was exported into a scratch directory, which is gone again.
    assert len(_fake_kernel.export_calls) == 1
    assert not _fake_kernel.export_calls[0][0].exists()
    assert _fake_kernel.packet_calls == []


def test_board_only_case_into_a_missing_directory_still_delivers_the_board(
    tmp_path, offline_pdf_fetch
):
    """Regression: ``--board-only --case`` into a not-yet-existing output
    directory used to die on the case write (FileNotFoundError escaping
    before ``_finish``); the run must complete and write the board, and
    ``--board-only`` promises only the routed board -- no case files."""
    out = tmp_path / "brand" / "new" / "board.kicad_pcb"
    assert not out.parent.exists()
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=out,
        time_limit_s=15.0,
        emit_stages=False,
        enclosure=True,
    )
    assert out.exists()
    assert result.enclosure is not None
    assert result.enclosure.step_text == STEP_TEXT  # the text still ships
    assert result.enclosure.exports is None
    assert not list(out.parent.glob("enclosure*"))
    assert result.artifacts == [out]


def test_board_envelope_valueerror_degrades_to_enclosure_failed(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    """A ValueError from measuring the board is the stage's own failure, not
    the run's: it must surface as enclosure.failed and the run must finish."""
    from silkscreen.agents import stages

    def broken_envelope(path):
        raise ValueError("board has no Edge.Cuts outline")

    monkeypatch.setattr(stages, "board_envelope", broken_envelope)
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
    )
    assert result.enclosure is None
    failed = [e for e in events if e["event"] == "enclosure.failed"]
    assert len(failed) == 1
    assert "Edge.Cuts" in failed[0]["error"]
    # The run finished: the critic ran to completion and the board was
    # written. This used to be spelled "review closed the stream", which was
    # only true while review was the last stage; it now answers inside
    # place's solve, so what is asserted is that it closed at all -- a case
    # failure must not take the critic down with it.
    assert [
        e["event"] for e in events if e.get("stage") == "review"
    ][-1] == "stage.done"
    assert (tmp_path / "board.kicad_pcb").exists()


def test_enclosure_failure_degrades_and_the_board_still_ships(
    tmp_path, offline_pdf_fetch
):
    """Plan decision 5: exhausted repair budget -> enclosure None + a visible
    enclosure.failed event; the run continues, the board is the product."""
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(BAD_ENCLOSURE)),  # every round rejected
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
    )

    assert result.enclosure is None
    failed = [e for e in events if e["event"] == "enclosure.failed"]
    assert len(failed) == 1
    assert failed[0]["error"]
    assert len(failed[0]["error"]) <= 160
    # Two visible rounds -- the fast default's one-repair budget -- preceded
    # the give-up: an honest fix-it loop, just a short one.
    assert len([e for e in events if e["event"] == "enclosure.round"]) == 2
    # The run finished: the critic ran to completion and the board was
    # written. This used to be spelled "review closed the stream", which was
    # only true while review was the last stage; it now answers inside
    # place's solve, so what is asserted is that it closed at all -- a case
    # failure must not take the critic down with it.
    assert [
        e["event"] for e in events if e.get("stage") == "review"
    ][-1] == "stage.done"
    assert (tmp_path / "board.kicad_pcb").exists()
    assert not list(tmp_path.glob("enclosure*"))
    assert result.findings  # review still ran after the degradation


def test_enclosure_rigorous_kwarg_restores_the_strict_pipeline_loop(
    tmp_path, offline_pdf_fetch
):
    """enclosure_rigorous=True threads end to end: the old three-repair strict
    loop runs (four visible rounds) before the stage degrades."""
    events = []
    result = generate_pcb(
        _pipeline_model(json.dumps(BAD_ENCLOSURE)),  # every round rejected
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
        enclosure_rigorous=True,
    )
    assert result.enclosure is None
    assert len([e for e in events if e["event"] == "enclosure.round"]) == 4
    assert (tmp_path / "board.kicad_pcb").exists()


def test_placed_snapshot_holds_still_while_the_original_is_routed():
    """The thread measures a copy routing's in-place mutation cannot reach.

    Expected geometry is stated independently: a placed board has no copper,
    and the copy must still have none after the original has been routed.
    """
    from silkscreen.agents.stages import placed_snapshot
    from silkscreen.board import build_board, route_board
    from silkscreen.netlist import parse_circuit_spec
    from test_agents import GOOD_CIRCUIT

    board = build_board(parse_circuit_spec(json.dumps(GOOD_CIRCUIT)), time_limit_s=10.0)
    assert not board.tracks and not board.vias
    snapshot = placed_snapshot(board)
    snapshot_warnings = len(snapshot.warnings)
    route_board(board)
    board.warnings.append("a later stage's warning")

    assert board.tracks, "the fixture must actually produce copper"
    assert not snapshot.tracks and not snapshot.vias
    assert not snapshot.routed_nets and not snapshot.unrouted_nets
    assert len(snapshot.warnings) == snapshot_warnings
    assert [(p.ref, p.x_nm, p.y_nm) for p in snapshot.parts] == [
        (p.ref, p.x_nm, p.y_nm) for p in board.parts
    ]


def test_a_callback_raising_on_the_enclosure_thread_aborts_the_run(
    tmp_path, offline_pdf_fetch
):
    """The service cancels a run by raising from ``on_event``; the enclosure
    thread must honour that at the join exactly as an in-line stage would."""

    class Gone(RuntimeError):
        pass

    def hang_up(event):
        if event["event"] == "stage.start" and event.get("stage") == "enclosure":
            raise Gone("client disconnected")

    with pytest.raises(Gone):
        generate_pcb(
            _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
            INTENT,
            datasheets=SHEETS,
            output=tmp_path / "board.kicad_pcb",
            time_limit_s=15.0,
            on_event=hang_up,
            enclosure=True,
        )
    # Abandoned before review, so the headline board was never written.
    assert not (tmp_path / "board.kicad_pcb").exists()


def test_enclosure_events_carry_no_payload(tmp_path, offline_pdf_fetch):
    """The stream stays a progress signal: no STEP text, no model output."""
    events = []
    generate_pcb(
        _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
    )
    for event in events:
        for value in event.values():
            assert not (isinstance(value, str) and len(value) > 500)


# ---------------------------------------------------------------- ADK parity

# The test_adk.py convention: the third-party import is the only thing the try
# covers, so internal breakage in the driver fails loudly on machines that
# have the extra, and machines without it skip stably.
try:
    import google.adk  # noqa: F401

    _HAS_ADK = True
except ImportError:
    _HAS_ADK = False

needs_adk = pytest.mark.skipif(not _HAS_ADK, reason="the 'adk' extra is not installed")


@pytest.fixture
def reproducible_placement(monkeypatch):
    """Bound the placer by CP-SAT's deterministic time, not by the wall clock.

    A parity test compares two *independent* solves of one input, so it needs
    the solve to be reproducible. Under a wall-clock budget it is not, and the
    distinction is subtle enough to be worth stating: the sequence of improved
    incumbents CP-SAT walks is bit-identical run to run (measured: the same
    52-value objective sequence across three runs at ``workers=1``), but which
    incumbent the clock happens to cut at is not. On a loaded machine one
    driver reaches an incumbent the other never does, and the two boards
    differ -- measured here as 55.35 mm against 56.20 mm of wirelength, which
    are two adjacent incumbents of the same solve, not a driver divergence.
    That is what made this test flake pass/fail/pass on identical code.

    ``max_deterministic_time`` counts CP-SAT's own work units instead of
    seconds, so both drivers stop at the same point in the same sequence and
    every artifact downstream of the placement -- the enclosure geometry among
    them -- comes out byte-identical. Measured on this board: identical
    placement, size and wirelength across three runs at every budget from 0.25
    to 80.0, while the wall clock for one budget ranged 1.4 s to 5.5 s.

    1.0 is chosen because it reaches the same answer the unbounded solve
    proves optimal (55.35 mm, confirmed by a 128.7 s run to ``OPTIMAL``) in
    about 2-4 s, which is faster than the 15 s wall-clock budget it replaces.
    The wall-clock limit is raised rather than removed so a pathologically
    slow machine still cannot hang the suite; at roughly 100x the measured
    time it is a backstop, not the thing that ends the solve.

    Scoped to this test on purpose. Making the production placer deterministic
    this way is a real option but a much larger decision -- see TODO.txt --
    because a deterministic budget buys reproducibility at the cost of a
    wall-clock guarantee, and Cloud Run has a request deadline.
    """
    from ortools.sat.python import cp_model

    original = cp_model.CpSolver.Solve

    def solve(self, model, *args, **kwargs):
        self.parameters.max_deterministic_time = 1.0
        self.parameters.max_time_in_seconds = 600.0
        return original(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "Solve", solve)


@needs_adk
def test_both_drivers_emit_identical_enclosure_events(
    tmp_path, offline_pdf_fetch, reproducible_placement
):
    """The parity contract of test_adk.py, extended to the new stage."""
    from silkscreen.agents.adk import generate_pcb_adk
    from silkscreen.agents.pipeline import _generate_pcb_sdk

    def run(driver, where):
        where.mkdir()
        events = []
        result = driver(
            _pipeline_model(json.dumps(GOOD_ENCLOSURE)),
            INTENT,
            datasheets=SHEETS,
            output=where / "board.kicad_pcb",
            time_limit_s=15.0,
            on_event=events.append,
            enclosure=True,
            enclosure_style="rounded corners",
        )
        return result, events

    sdk_result, sdk_events = run(_generate_pcb_sdk, tmp_path / "sdk")
    adk_result, adk_events = run(generate_pcb_adk, tmp_path / "adk")

    # The placement itself, part by part, asserted before anything derived
    # from it. Everything below -- the event frames' board_mm and
    # wirelength_mm, the enclosure geometry, the STEP bytes -- is downstream
    # of this, so when the two drivers disagree here every later assertion
    # fails too and the report names the symptom rather than the cause. It is
    # also what `reproducible_placement` exists to make true: without it these
    # are two independent wall-clock-bounded solves and this line is a
    # coin-toss on a loaded machine.
    def placement(result):
        return [(p.ref, p.x_nm, p.y_nm, p.rotated) for p in result.board.parts]

    assert placement(adk_result) == placement(sdk_result)
    assert adk_result.board.wirelength_nm == sdk_result.board.wirelength_nm

    # Identical frames per lane; t_s is wall clock and may differ, and both
    # worker lanes interleave with the driver's line at the wall clock's whim
    # under either driver, so the lanes are compared separately and the
    # cross-lane bounds asserted on each stream.
    #
    # Three lanes, not two: the critic is a worker lane of its own now, and
    # comparing only main and enclosure would have quietly dropped review
    # from this parity contract the moment it moved off the driver thread.
    def review_lane(evs):
        return [e for e in evs if e.get("stage") == "review"]

    lanes = (lambda evs: _lanes(evs)[0], lambda evs: _lanes(evs)[1], review_lane)
    for pick in lanes:
        strip = [
            [{k: v for k, v in e.items() if k != "t_s"} for e in pick(evs)]
            for evs in (sdk_events, adk_events)
        ]
        assert strip[0] == strip[1]
    assert review_lane(sdk_events), "the critic ran, so it has a lane to compare"
    _assert_enclosure_bounds(sdk_events)
    _assert_enclosure_bounds(adk_events)
    assert adk_result.enclosure is not None
    assert adk_result.enclosure.step_text == sdk_result.enclosure.step_text
    assert (
        (tmp_path / "adk" / "enclosure.step").read_bytes()
        == (tmp_path / "sdk" / "enclosure.step").read_bytes()
    )
    # Both drivers thread emit_stages into the stage and report the write.
    for result, where in ((sdk_result, "sdk"), (adk_result, "adk")):
        assert result.enclosure.exports.step == tmp_path / where / "enclosure.step"
        assert result.enclosure.exports.step in result.artifacts


@needs_adk
def test_adk_driver_threads_the_rigorous_flag(tmp_path, offline_pdf_fetch):
    """Driver parity for the new kwarg: the ADK context field reaches the
    stage, so rigorous mode runs the same four rounds it does under the SDK
    driver (test_enclosure_rigorous_kwarg_restores_the_strict_pipeline_loop)."""
    from silkscreen.agents.adk import generate_pcb_adk

    events = []
    result = generate_pcb_adk(
        _pipeline_model(json.dumps(BAD_ENCLOSURE)),
        INTENT,
        datasheets=SHEETS,
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
        enclosure_rigorous=True,
    )
    assert result.enclosure is None
    assert len([e for e in events if e["event"] == "enclosure.round"]) == 4
    assert (tmp_path / "board.kicad_pcb").exists()

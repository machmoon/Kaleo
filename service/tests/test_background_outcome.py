"""``background_outcome`` and the agenda's peeked evidence (``service/steps.py``).

Two background jobs start at ``place`` -- the default case and the parts
sourcing -- and until now the only way to learn how one ended was to press
the step that collects it. ``background_outcome`` reports a *finished* job's
outcome on every envelope and on ``GET /steps/<id>``: absent while it runs
(it is in ``background`` then), ``{ok: true, detail: null}`` for a clean
finish, and ``{ok: false, detail: <the warning the collecting step will
say>}`` for a failure. It is peeked, never collected, so observing it
changes nothing about what pressing the step returns afterwards -- pinned
here by pressing the step afterwards and comparing.

The same peek feeds the spec-review agenda: failed kernel clauses and
sourcing gaps reach the model when the jobs have finished, and a job still
running contributes nothing plus a warning saying so, never a wait.

Offline throughout: the scripted models from ``test_steps``, the fake
datasheet probe, ephemeral ports, no bridge.
"""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.agents.specreview import SPECREVIEW_MARKER

from service import steps
from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import scripted
from service.tests.test_steps import (
    AGENDA,
    _Gated,
    _SourcingOutage,
    _start,
    fake_probe,
    get,
    post,
)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """``test_steps.server``, restated (a fixture cannot be imported by name
    without ruff reading every use as a redefinition)."""
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "missing-kicad-cli"))
    monkeypatch.setattr(steps, "PROBE", fake_probe)
    steps.reset_sessions()
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None
    steps.reset_sessions()


def _wait_settled(srv, sid, *names, timeout=90.0):
    """Poll the status route until none of ``names`` is in ``background``.

    The jobs are daemon threads answering a scripted model, so this is a few
    milliseconds; the poll only keeps the assertion from racing the thread.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, state = get(srv, f"/steps/{sid}")
        assert status == 200, state
        if not any(name in state["background"] for name in names):
            return state
        time.sleep(0.02)
    pytest.fail(f"{names} still running after {timeout}s: {state['background']}")


# ------------------------------------------------------------ (b) outcomes


def test_a_running_job_is_in_background_and_absent_from_the_outcome(server):
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))
    try:
        sid = _start(server)["session"]
        status, placed = post(server, f"/steps/{sid}/place", {})
        assert status == 200, placed
        assert placed["background"] == ["sourcing", "case"]
        assert placed["background_outcome"] == {}
        status, state = get(server, f"/steps/{sid}")
        assert state["background"] == ["sourcing", "case"]
        assert state["background_outcome"] == {}
    finally:
        gate.set()


def test_a_clean_finish_is_ok_true_with_no_detail(server):
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))
    gate.set()
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    state = _wait_settled(server, sid, "case", "sourcing")
    assert state["background_outcome"] == {
        "case": {"ok": True, "detail": None},
        "sourcing": {"ok": True, "detail": None},
    }
    # Observing it spent nothing: one design, one lookup.
    assert len([c for c in log if "ENCLOSURE-SPEC" in c["prompt"]]) == 1
    # And the outcome rides the next step's envelope too, unchanged.
    status, routed = post(server, f"/steps/{sid}/route", {})
    assert status == 200, routed
    assert routed["background_outcome"] == state["background_outcome"]


def test_a_failed_case_is_ok_false_with_the_step_warning_and_the_step_agrees(server):
    """The shared scripted model has no case answer, so the background design
    dies with a ModelError. The outcome says so *before* the step is pressed,
    in the words the step will use, and pressing the step afterwards reports
    exactly that -- the peek consumed nothing."""
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    state = _wait_settled(server, sid, "case")
    outcome = state["background_outcome"]["case"]
    assert outcome["ok"] is False
    assert outcome["detail"].startswith("the case designed in the background failed: ")
    assert "ModelError" in outcome["detail"]

    status, cased = post(server, f"/steps/{sid}/case", {})
    assert status == 200, cased
    assert cased["enclosure"] is None
    assert outcome["detail"] in cased["warnings"]
    # Still reported once the step has run, and still the same answer.
    assert cased["background_outcome"]["case"] == outcome
    status, again = get(server, f"/steps/{sid}")
    assert again["background_outcome"]["case"] == outcome


def test_a_failed_sourcing_is_ok_false_with_the_step_warning_and_the_step_agrees(
    server,
):
    Handler.model_factory = staticmethod(_SourcingOutage)
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    state = _wait_settled(server, sid, "sourcing")
    outcome = state["background_outcome"]["sourcing"]
    assert outcome["ok"] is False
    assert outcome["detail"].startswith(
        "parts were not sourced: the lookup in the background failed ("
    )
    assert "ModelError" in outcome["detail"]

    status, body = post(server, f"/steps/{sid}/sourcing", {})
    assert status == 200, body
    assert body["warnings"] == [outcome["detail"]]
    assert [row["ref"] for row in body["sourcing"]["parts"]] == ["U1", "C1", "C2"]
    assert body["background_outcome"]["sourcing"] == outcome


def test_a_lookup_the_stage_caught_itself_is_not_ok_either(server):
    """The model answers the sourcing prompt with something unparseable. The
    stage catches that and returns the plain rows with a ``parts were not
    sourced`` warning -- no exception at the join, but not a BOM either, and
    the outcome must not read ``ok`` over it."""

    def unparseable_sourcing():
        from silkscreen.agents.sourcing import SOURCING_MARKER

        return ScriptedModel(
            by_marker={**scripted().by_marker, SOURCING_MARKER: "not json at all"}
        )

    Handler.model_factory = staticmethod(unparseable_sourcing)
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    state = _wait_settled(server, sid, "sourcing")
    outcome = state["background_outcome"]["sourcing"]
    assert outcome["ok"] is False
    assert outcome["detail"].startswith("parts were not sourced: ")
    status, body = post(server, f"/steps/{sid}/sourcing", {})
    assert status == 200, body
    assert outcome["detail"] in body["warnings"]


def test_the_outcome_is_observed_once_and_never_spends_a_model_call():
    """Unit-level: the peek memoises on the session and does not join a
    running job, so a status poll cannot block on a model call."""

    class Job:
        def __init__(self, running):
            self.running = running
            self.asked = 0

        def result(self):
            self.asked += 1
            raise RuntimeError("boom")

    session = steps.Session(
        id="s", intent="x", stem="x", directory=Path("."),
        kicad_live=False, time_limit_s=None,
    )
    session.case_job = Job(running=True)
    assert session.background_outcome == {}
    assert session.case_job.asked == 0

    session.case_job = Job(running=False)
    first = session.background_outcome["case"]
    assert first == {
        "ok": False,
        "detail": "the case designed in the background failed: RuntimeError: boom",
    }
    assert session.background_outcome["case"] is first
    assert session.case_job.asked == 1, "cached after the first observation"


# ------------------------------------------------------- (d) agenda evidence


def _agenda_calls(model):
    return [c for c in model.calls if SPECREVIEW_MARKER in c["prompt"]]


def test_sourcing_gaps_reach_the_agenda_prompt(server):
    """``sourcing`` is pressed first so the BOM is on the session (no race
    with the background thread), then the review asks for an agenda: the
    honest null the model gave for C2 is evidence the agenda may cite."""
    model = ScriptedModel(
        by_marker={**scripted().by_marker, SPECREVIEW_MARKER: json.dumps(AGENDA)}
    )
    Handler.model_factory = staticmethod(lambda: model)
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    assert post(server, f"/steps/{sid}/sourcing", {})[0] == 200
    assert post(server, f"/steps/{sid}/route", {})[0] == 200
    status, reviewed = post(server, f"/steps/{sid}/review", {"spec_review": True})
    assert status == 200, reviewed
    (call,) = _agenda_calls(model)
    assert "Parts with sourcing gaps:" in call["prompt"]
    assert "C2: no part number found" in call["prompt"]
    assert "agenda prepared before" not in " ".join(reviewed.get("warnings", []))


def test_failed_kernel_clauses_reach_the_agenda_prompt():
    """At the seam: a collected case with a kernel receipt puts its failed
    clauses, with the signed margin, in front of the model, and the clause
    name is in the vocabulary so an item about it survives the ref filter."""
    model = ScriptedModel(by_marker={SPECREVIEW_MARKER: json.dumps(AGENDA)})
    clause = SimpleNamespace(
        name="board_clash", passed=False, margin_nm=-1_250_000,
        detail="the board intersects the base by 1.25 mm",
    )
    passed = SimpleNamespace(
        name="lid_mates", passed=True, margin_nm=200_000, detail=""
    )
    session = steps.Session(
        id="s", intent="a 3.3V regulator", stem="x", directory=Path("."),
        kicad_live=False, time_limit_s=None,
        board=SimpleNamespace(
            parts=[SimpleNamespace(ref="U1"), SimpleNamespace(ref="C1")], nets=["VOUT"]
        ),
        enclosure=SimpleNamespace(kernel=SimpleNamespace(clauses=(clause, passed))),
    )
    block, warnings = steps._agenda(
        session, model=model, emit=lambda e: None, enter=lambda s: None
    )
    (call,) = _agenda_calls(model)
    assert "Enclosure kernel clauses that failed:" in call["prompt"]
    assert "clause board_clash failed by -1.250 mm" in call["prompt"]
    assert "lid_mates" not in call["prompt"], "a passed clause is not evidence"
    assert not any("agenda prepared before" in w for w in warnings)
    assert block is not None


def test_a_case_still_designing_is_not_waited_for_and_the_agenda_says_so(server):
    """The gate holds both background jobs open; the review must answer
    without them, name what it went without, and never join the thread."""
    gate = threading.Event()
    log: list = []

    class GatedWithAgenda(_Gated):
        def __init__(self):
            super().__init__(gate, log)
            self.inner.by_marker[SPECREVIEW_MARKER] = json.dumps(AGENDA)

    Handler.model_factory = staticmethod(GatedWithAgenda)
    try:
        sid = _start(server)["session"]
        assert post(server, f"/steps/{sid}/place", {})[0] == 200
        assert post(server, f"/steps/{sid}/route", {})[0] == 200
        started = time.monotonic()
        status, reviewed = post(server, f"/steps/{sid}/review", {"spec_review": True})
        assert status == 200, reviewed
        assert time.monotonic() - started < 8, "the review waited on the case"
        assert reviewed["background"] == ["sourcing", "case"]
        assert reviewed["background_outcome"] == {}
        warnings = reviewed["warnings"]
        assert any(w.startswith(steps.AGENDA_BEFORE_CASE) for w in warnings)
        assert any(w.startswith(steps.AGENDA_BEFORE_SOURCING) for w in warnings)
        (call,) = [c for c in log if SPECREVIEW_MARKER in c["prompt"]]
        assert "Enclosure kernel clauses" not in call["prompt"]
        assert "Parts with sourcing gaps" not in call["prompt"]
        assert reviewed["spec_review"] is not None
    finally:
        gate.set()


def test_a_finished_case_is_peeked_not_collected_for_the_agenda():
    """A finished job's kernel receipt reaches the prompt without the case
    step having been pressed, and the job is left for that step."""
    model = ScriptedModel(by_marker={SPECREVIEW_MARKER: json.dumps(AGENDA)})
    clause = SimpleNamespace(
        name="min_wall", passed=False, margin_nm=-300_000, detail="wall 1.2 mm < 1.5 mm"
    )
    result = SimpleNamespace(kernel=SimpleNamespace(clauses=(clause,)))

    class Finished:
        running = False

        def __init__(self):
            self.asked = 0

        def result(self):
            self.asked += 1
            return result

    session = steps.Session(
        id="s", intent="x", stem="x", directory=Path("."),
        kicad_live=False, time_limit_s=None,
        board=SimpleNamespace(
            parts=[SimpleNamespace(ref="U1"), SimpleNamespace(ref="C1")], nets=[]
        ),
    )
    session.case_job = Finished()
    _, warnings = steps._agenda(
        session, model=model, emit=lambda e: None, enter=lambda s: None
    )
    (call,) = _agenda_calls(model)
    assert "clause min_wall failed by -0.300 mm" in call["prompt"]
    assert not any("agenda prepared before" in w for w in warnings)
    assert session.enclosure is None, "peeked: the case step still collects it"
    assert session.case_job.asked >= 1

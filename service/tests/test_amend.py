"""Typing at the strip while a run is in flight (``service/amend.py``).

Offline, on the ``test_steps.py`` harness: the scripted model answers propose
and the case, the placer is deterministic, the datasheet probe is a fake.

What these tests are really pinning is a set of *honesty* properties, because
this feature's whole risk is promising the engineer more than the pipeline can
do. Three of them are worth naming:

* a note never reaches a stage that has already run, and never rewrites the
  circuit under a placement that was solved for a different one;
* cancellation lands at an event boundary, so a model call already in flight
  is still paid for -- ``test_cancel_does_not_unspend_a_model_call_in_flight``
  asserts the money was spent rather than pretending otherwise;
* a note typed during a 20 s placement is answered while the placement runs,
  not after it.
"""

import json
import threading
import time

import pytest
from silkscreen.agents.model import ScriptedModel

from service import amend, steps
from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import scripted
from service.tests.test_steps import (
    CASE_MARKER,
    GOOD_ENCLOSURE,
    _case_calls,
    _Gated,
    _start,
    fake_probe,
    get,
    post,
)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """``test_steps.py``'s harness, declared here rather than imported.

    A fixture pulled in by name is a name this module then shadows in every
    test signature, which ruff reads (correctly) as a redefinition. Twenty
    lines of setup are cheaper than a file full of suppressions, and the
    helpers that carry the interesting knowledge -- the gated model, the
    offline probe -- are still imported rather than copied.
    """
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


def _cased():
    """The shared scripted model plus an enclosure answer."""
    return ScriptedModel(
        by_marker={**scripted().by_marker, CASE_MARKER: json.dumps(GOOD_ENCLOSURE)}
    )


# ------------------------------------------------------------------ notes


def test_a_note_is_recorded_and_names_both_doors(server):
    sid = _start(server)["session"]
    status, body = post(server, f"/steps/{sid}/amend", {"text": "make it 5 V"})
    assert status == 200, body

    assert body["amendment"]["text"] == "make it 5 V"
    assert body["amendment"]["status"] == "pending"
    # The stage it arrived at is kept: the same words before and after
    # placement are different requests with different costs.
    assert body["amendment"]["stage"] == "proposed"
    assert body["pending"] == 1

    # Door one: a new run, with the cost stated rather than implied.
    assert body["restart"]["available"] is True
    assert "make it 5 V" in body["restart"]["intent"]
    assert "model call" in body["restart"]["cost"]

    # Door two: named, and empty, because this note was not aimed at a step.
    assert body["applies"] == {"at_step": None, "when": None}
    assert body["consuming_steps"] == ["case"]
    assert "start a new run" in body["headline"].lower()


def test_a_note_reaches_the_envelope_and_the_status_route(server):
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/amend", {"text": "add a vent"})

    status, placed = post(server, f"/steps/{sid}/place", {})
    assert status == 200, placed
    assert [a["text"] for a in placed["amendments"]] == ["add a vent"]
    assert placed["cancelled"] is False

    status, state = get(server, f"/steps/{sid}")
    assert status == 200
    assert [a["text"] for a in state["amendments"]] == ["add a vent"]
    assert state["cancelled"] is False


def test_a_note_can_only_be_aimed_at_a_step_that_reads_text(server):
    """The API refuses to pretend. ``route`` takes no sentence, so saying so
    is a 400 naming what it will accept -- not a note quietly parked where
    nothing will ever read it."""
    sid = _start(server)["session"]
    status, body = post(
        server, f"/steps/{sid}/amend", {"text": "route it differently", "step": "route"}
    )
    assert status == 400, body
    assert "case" in body["error"]
    assert "does not read free text" in body["error"]

    status, body = post(
        server, f"/steps/{sid}/amend", {"text": "vents", "step": "case"}
    )
    assert status == 200, body
    assert body["applies"]["at_step"] == "case"
    assert "case" in body["applies"]["when"]


def test_a_note_aimed_at_a_step_that_already_ran_is_refused(server):
    Handler.model_factory = staticmethod(_cased)
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    assert post(server, f"/steps/{sid}/case", {})[0] == 200

    status, body = post(
        server, f"/steps/{sid}/amend", {"text": "add a vent", "step": "case"}
    )
    assert status == 400, body
    assert "already run" in body["error"]


def test_an_empty_or_oversized_note_is_refused(server):
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/amend", {"text": "   "})[0] == 400
    status, body = post(
        server, f"/steps/{sid}/amend", {"text": "x" * (amend.MAX_NOTE_CHARS + 1)}
    )
    assert status == 400 and str(amend.MAX_NOTE_CHARS) in body["error"]


def test_the_note_cap_refuses_loudly_rather_than_dropping_the_oldest(server):
    sid = _start(server)["session"]
    for i in range(amend.MAX_NOTES):
        assert post(server, f"/steps/{sid}/amend", {"text": f"note {i}"})[0] == 200
    status, body = post(server, f"/steps/{sid}/amend", {"text": "one too many"})
    assert status == 400 and str(amend.MAX_NOTES) in body["error"]
    # Nothing the engineer typed was silently discarded to make room.
    status, state = get(server, f"/steps/{sid}")
    assert len(state["amendments"]) == amend.MAX_NOTES
    assert state["amendments"][0]["text"] == "note 0"


def test_a_note_never_touches_the_spec_or_the_placement(server):
    """The bug class this feature must not create: a placement solved for one
    netlist and a board written from another. A note changes neither."""
    sid = _start(server)["session"]
    before = steps._get(sid).spec
    post(server, f"/steps/{sid}/amend", {"text": "make it 5 V and add an LED"})
    session = steps._get(sid)
    assert session.spec is before
    assert session.spec.part_count() == before.part_count()

    status, placed = post(server, f"/steps/{sid}/place", {})
    assert status == 200
    # The board carries exactly the parts the accepted circuit had -- the note
    # added nothing to it.
    assert len(placed["parts"]) == before.part_count()


# ------------------------------------------- the one place a note is applied


def test_a_note_aimed_at_the_case_becomes_the_enclosure_style(server):
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))
    gate.set()

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    post(server, f"/steps/{sid}/amend", {"text": "rounded", "step": "case"})

    status, cased = post(server, f"/steps/{sid}/case", {})
    assert status == 200, cased
    calls = _case_calls(log)
    assert len(calls) == 2, "the prefetched default plus one designed for the note"
    assert len([c for c in calls if "rounded" in c["prompt"]]) == 1
    # The discarded prefetch is reported, not hidden: it is a model call the
    # engineer paid for and did not get.
    assert any("discarded" in w for w in cased["warnings"])
    # And the note is marked applied, so it is not re-applied by anything else.
    assert cased["amendments"][0]["status"] == "applied"


def test_an_explicit_style_beats_a_pending_note(server):
    """The more recent instruction, aimed at this very button, wins."""
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))
    gate.set()

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    post(server, f"/steps/{sid}/amend", {"text": "rounded", "step": "case"})
    status, cased = post(server, f"/steps/{sid}/case", {"enclosure_style": "vented"})
    assert status == 200, cased
    calls = _case_calls(log)
    assert len([c for c in calls if "vented" in c["prompt"]]) == 1
    assert not [c for c in calls if "rounded" in c["prompt"]]
    # The note was never consumed, so it still stands for a restart.
    assert cased["amendments"][0]["status"] == "pending"


# --------------------------------------------------------------- restart


def test_the_amended_intent_carries_every_note_in_order(server):
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/amend", {"text": "make it 5 V"})
    status, body = post(server, f"/steps/{sid}/amend", {"text": "and add a fuse"})
    assert status == 200
    text = body["restart"]["intent"]
    assert text.startswith("build me a toy car")
    assert text.index("make it 5 V") < text.index("and add a fuse")


def test_a_restart_is_a_new_session_and_leaves_the_old_board_alone(server):
    """(a) cancel-and-redo and (c) queue-a-follow-up are the same mechanism --
    POST /steps with the amended intent -- differing only in when the client
    sends it. Neither mutates the run that produced the note."""
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    status, body = post(server, f"/steps/{sid}/amend", {"text": "make it 5 V"})
    amended = body["restart"]["intent"]

    status, fresh = post(server, "/steps", {"intent": amended, "time_limit_s": 5})
    assert status == 200, fresh
    assert fresh["session"] != sid
    assert "make it 5 V" in fresh["intent"]
    # The first run is untouched and still usable.
    status, state = get(server, f"/steps/{sid}")
    assert state["stage"] == "placed" and state["cancelled"] is False


# ---------------------------------------------------------------- cancel


def test_cancel_refuses_every_remaining_step(server):
    sid = _start(server)["session"]
    status, body = post(server, f"/steps/{sid}/cancel", {})
    assert status == 200, body
    assert body["cancelled"] is True and body["already_cancelled"] is False
    assert body["stage"] == "proposed"
    assert set(body["refused_steps"]) == set(steps.STEPS)
    assert body["in_flight"] is False
    assert body["still_running"] == []
    assert body["restart"]["available"] is True

    status, refused = post(server, f"/steps/{sid}/place", {})
    assert status == 409, refused
    assert "cancelled" in refused["error"]
    status, state = get(server, f"/steps/{sid}")
    assert state["cancelled"] is True and state["stage"] == "proposed"


def test_cancel_is_idempotent(server):
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/cancel", {})[0] == 200
    status, body = post(server, f"/steps/{sid}/cancel", {})
    assert status == 200 and body["already_cancelled"] is True


def test_a_note_after_a_cancel_is_refused(server):
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/cancel", {})
    status, body = post(server, f"/steps/{sid}/amend", {"text": "make it 5 V"})
    assert status == 409, body
    assert "cancelled" in body["error"]


def test_cancel_and_amend_need_no_model_and_so_no_api_key(server):
    """The cancel button must work on the day the API key is exhausted -- that
    is exactly the day someone reaches for it. ``app.py`` routes both
    lifecycle paths *before* it builds a model, the way it routes ``deliver``.

    The step routes themselves are not like that: ``_step`` evaluates
    ``model_factory()`` as an argument, so ``POST /steps/<id>/place`` with a
    broken factory is a 500 even on a cancelled session. That is pre-existing
    and out of this module's scope; it is recorded here so the difference
    between the two families of route is deliberate rather than assumed.
    """

    def no_model():
        raise RuntimeError("no API key configured")

    sid = _start(server)["session"]
    Handler.model_factory = staticmethod(no_model)
    status, body = post(server, f"/steps/{sid}/amend", {"text": "make it 5 V"})
    assert status == 200, body
    status, body = post(server, f"/steps/{sid}/cancel", {})
    assert status == 200, body
    assert body["cancelled"] is True


def test_cancel_reports_the_background_work_it_cannot_stop(server):
    """Honesty rule: the case and sourcing threads are already running and
    cannot be killed. Cancel names them rather than implying they stopped."""
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))

    sid = _start(server)["session"]
    status, placed = post(server, f"/steps/{sid}/place", {})
    assert status == 200, placed
    assert placed["background"] == ["sourcing", "case"]

    status, body = post(server, f"/steps/{sid}/cancel", {})
    assert status == 200, body
    assert set(body["still_running"]) == {"sourcing", "case"}
    assert "not interrupted" in body["not_stoppable"]
    assert "discarded" in body["headline"] or "discarded" in body["not_stoppable"]
    gate.set()


def test_cancel_aborts_the_step_in_flight_at_its_next_event(server):
    """The in-flight step is abandoned through the pipeline's existing seam --
    a callback that raises abandons the run -- and the caller is told 409
    rather than handed a half-finished board."""
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})

    outcome: list = []

    def press_case():
        outcome.append(post(server, f"/steps/{sid}/case", {"enclosure_style": "domed"}))

    thread = threading.Thread(target=press_case)
    thread.start()
    # The case step is now blocked inside the model call the gate holds shut.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not steps._get(sid).lock.locked():
        time.sleep(0.01)
    assert steps._get(sid).lock.locked(), "the case step never took the session"

    status, body = post(server, f"/steps/{sid}/cancel", {})
    assert status == 200, body
    assert body["in_flight"] is True, "a running step is reported as running"
    assert "next event" in body["aborts_at"]

    gate.set()
    thread.join(timeout=20)
    assert outcome, "the case step never returned"
    assert outcome[0][0] == 409, outcome[0]


def test_cancel_does_not_unspend_a_model_call_in_flight(server):
    """The most important test here, and the reason it asserts a *cost*.

    Cancellation lands at an event boundary, so a stage already inside its
    model call finishes that call and pays for it. If this ever starts
    passing with zero calls, the granularity changed and the contract the
    UI shows the engineer must change with it.
    """
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})

    outcome: list = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            post(server, f"/steps/{sid}/case", {"enclosure_style": "domed"})
        )
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not steps._get(sid).lock.locked():
        time.sleep(0.01)

    post(server, f"/steps/{sid}/cancel", {})
    gate.set()
    thread.join(timeout=20)
    assert outcome and outcome[0][0] == 409
    # The call was already in flight when the cancel landed, so it was made.
    assert _case_calls(log), "the in-flight model call is still paid for"


def test_a_note_is_answered_while_a_step_is_still_running(server):
    """The feature is for the 20 s placement, so the note route must not wait
    on ``Session.lock``, which a running step holds for its whole body."""
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})

    thread = threading.Thread(
        target=lambda: post(server, f"/steps/{sid}/case", {"enclosure_style": "domed"})
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not steps._get(sid).lock.locked():
        time.sleep(0.01)
    assert steps._get(sid).lock.locked()

    started = time.monotonic()
    status, body = post(server, f"/steps/{sid}/amend", {"text": "make it 5 V"})
    elapsed = time.monotonic() - started
    assert status == 200, body
    assert elapsed < 5.0, f"the note waited {elapsed:.1f}s on the running step"

    gate.set()
    thread.join(timeout=20)


def test_an_unknown_session_is_a_404_on_both_routes(server):
    assert post(server, "/steps/nosuchid/amend", {"text": "hi"})[0] == 404
    assert post(server, "/steps/nosuchid/cancel", {})[0] == 404


# ------------------------------------------------------- module invariants


def test_only_steps_that_read_free_text_are_consuming_steps():
    """``CONSUMING_STEPS`` is a claim that the step's payload has a field for a
    sentence. Today exactly one does. A name added here without a
    ``take_for`` call in its runner would park notes where nothing reads them.
    """
    assert amend.CONSUMING_STEPS == ("case",)
    assert set(amend.CONSUMING_STEPS) <= set(steps.STEPS)


def test_take_for_returns_nothing_for_a_step_that_reads_no_text(server):
    sid = _start(server)["session"]
    session = steps._get(sid)
    amend.note(session, "make it 5 V")
    assert amend.take_for(session, "route") == []
    assert amend.take_for(session, "case") == [], "the note was not aimed at case"
    assert session.amendments[0].status == "pending"


def test_run_cancelled_is_not_a_value_error():
    """Several stages catch ``ValueError`` to keep a bad model answer from
    failing a run. A cancel caught into a warning would leave the run going."""
    assert not issubclass(amend.RunCancelled, ValueError)
    assert issubclass(amend.RunCancelled, RuntimeError)


@pytest.mark.parametrize("action", ["amend", "cancel"])
def test_is_lifecycle_path_matches_only_the_two_routes(action):
    assert amend.is_lifecycle_path(f"/steps/abc/{action}")
    assert amend.is_lifecycle_path(f"/steps/abc/{action}?x=1")
    assert not amend.is_lifecycle_path("/steps/abc/place")
    assert not amend.is_lifecycle_path("/steps/abc")
    assert not amend.is_lifecycle_path(f"/steps/abc/def/{action}")

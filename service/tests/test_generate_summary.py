"""The spec-review agenda on the one-shot routes.

The desktop sends ``summary: "structured"`` on ``POST /generate`` and
``/generate/stream`` (``app/src/lib/silkscreen/client.ts`` ``normalizeRequest``)
and reads a ``spec_review`` block off the response (``deliver.ts``
``specReviewFrom``). Until 2026-09-06 ``service/app.py`` dropped the field
silently, so the Structured/Prose control did nothing on that path. These tests
pin the fix: the same validation and the same flattened block as the review
step, through ``steps._wants_agenda`` and ``steps._agenda``.

Offline throughout, on a ``ScriptedModel`` keyed by ``SPECREVIEW_MARKER`` --
the ``test_steps.py`` pattern.
"""

import json
import threading
from types import SimpleNamespace

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.agents.specreview import SPECREVIEW_MARKER

from service.app import Handler, _spec_review_block, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import REVIEW, post, post_stream, scripted

#: An agenda about parts ``test_app.CIRCUIT`` actually contains, so the
#: hallucination filter in ``parse_spec_review`` keeps both items.
AGENDA = {
    "title": "Regulator board spec review",
    "summary": "Two open questions the run could not settle on its own.",
    "items": [
        {
            "topic": "Regulator thermal budget",
            "why": "U1 dissipation at the stated load was never bounded",
            "minutes": 20,
            "blocking": True,
            "refs": ["U1"],
        },
        {
            "topic": "Bulk capacitor value",
            "why": "C1 was chosen without a ripple target",
            "minutes": 10,
            "blocking": False,
            "refs": ["C1"],
        },
    ],
    "decisions_needed": ["pick the regulator package"],
    "prepared_from": ["review"],
}

REQUEST = {"intent": "a 3.3V regulator", "time_limit_s": 2}


@pytest.fixture
def server(offline_pdf_fetch):
    """``test_app.server``, restated: pytest cannot import a fixture by name
    without ruff reading every use as a redefinition."""
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None


def _install(monkeypatch, **overrides):
    """One shared ScriptedModel for the whole test, so its calls are readable.

    ``Handler.model_factory`` is called per request; returning the same
    instance is what lets a test assert that no spec-review prompt was sent.
    """
    model = ScriptedModel(by_marker={**scripted().by_marker, **overrides})
    monkeypatch.setattr(Handler, "model_factory", staticmethod(lambda: model))
    return model


def _agenda_calls(model):
    return [c for c in model.calls if SPECREVIEW_MARKER in c["prompt"]]


# ------------------------------------------------------------- default off


@pytest.mark.parametrize("payload", [{}, {"summary": "prose"}])
def test_no_agenda_unless_asked_for(server, monkeypatch, payload):
    """Default prose: no ``spec_review`` key, and no model call spent on one."""
    model = _install(monkeypatch, **{SPECREVIEW_MARKER: json.dumps(AGENDA)})
    status, body = post(server, {**REQUEST, **payload})
    assert status == 200, body
    assert "spec_review" not in body
    assert _agenda_calls(model) == []


# ----------------------------------------------------------- structured on


@pytest.mark.parametrize("payload", [{"summary": "structured"}, {"spec_review": True}])
def test_structured_puts_the_flattened_agenda_on_the_response(
    server, monkeypatch, payload
):
    """Both spellings, and the block is the ``SpecReview`` itself.

    The same shape ``steps._review`` puts on its envelope: the wrapper's
    ``ok``/``review`` keys must not appear, because the desktop's
    ``specReviewFrom`` reads ``.items`` straight off the block.
    """
    model = _install(monkeypatch, **{SPECREVIEW_MARKER: json.dumps(AGENDA)})
    status, body = post(server, {**REQUEST, **payload})
    assert status == 200, body

    block = body["spec_review"]
    assert set(block) >= {"title", "summary", "items", "total_minutes"}
    assert "review" not in block and "ok" not in block
    assert block["title"] == AGENDA["title"]
    assert [i["topic"] for i in block["items"]] == [
        i["topic"] for i in AGENDA["items"]
    ]
    assert block["total_minutes"] == 30
    assert block["needs_meeting"] is True
    assert len(_agenda_calls(model)) == 1
    # The scripted critic's findings are the evidence, and they are still
    # the product: the response carries them beside the agenda.
    assert body["findings"]
    assert not any("spec-review agenda" in w for w in body["warnings"])


def test_the_agenda_rides_the_stream_result_frame(server, monkeypatch):
    """On ``/generate/stream`` the block sits on ``run.done``'s ``result``.

    That is where ``sourcing`` and ``routing`` ride: the frame carries the
    whole one-shot response, so the desktop reads one shape off both routes.
    """
    _install(monkeypatch, **{SPECREVIEW_MARKER: json.dumps(AGENDA)})
    status, _, frames = post_stream(server, {**REQUEST, "summary": "structured"})
    assert status == 200
    assert frames[-1]["event"] == "run.done"
    result = frames[-1]["result"]
    assert result["spec_review"]["title"] == AGENDA["title"]
    assert "routing" in result


# --------------------------------------------------------------- bad values


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"summary": "bullets"}, "summary"),
        ({"summary": 1}, "summary"),
        ({"spec_review": "yes"}, "spec_review"),
    ],
)
def test_a_bad_value_is_a_400_before_any_model_call(
    server, monkeypatch, payload, field
):
    model = _install(monkeypatch, **{SPECREVIEW_MARKER: json.dumps(AGENDA)})
    status, body = post(server, {**REQUEST, **payload})
    assert status == 400, body
    assert field in body["error"]
    assert model.calls == [], "field validation must precede the paid run"


def test_a_bad_value_is_a_400_run_error_frame_on_the_stream_route(
    server, monkeypatch
):
    """On the stream a field error is the shared taxonomy's ``run.error``.

    ``run.accepted`` has already gone out by the time fields are read, so the
    400 rides the error frame -- the same as every other field on this route
    -- and no ``run.done`` follows it.
    """
    model = _install(monkeypatch, **{SPECREVIEW_MARKER: json.dumps(AGENDA)})
    status, _, frames = post_stream(server, {**REQUEST, "summary": "bullets"})
    assert status == 200
    assert frames[-1]["event"] == "run.error"
    assert frames[-1]["status"] == 400 and "summary" in frames[-1]["error"]
    assert all(frame["event"] != "run.done" for frame in frames)
    assert model.calls == []


# ------------------------------------------------------------ model failure


def test_an_unusable_agenda_is_null_with_a_warning_not_a_500(server, monkeypatch):
    """A model that never returns valid JSON loses the agenda, not the run.

    ``null`` plus a warning: distinguishable from "not asked for" (key
    absent) and from an agenda with nothing blocking (``needs_meeting`` false).
    """
    _install(monkeypatch, **{SPECREVIEW_MARKER: "not json at all"})
    status, body = post(server, {**REQUEST, "summary": "structured"})
    assert status == 200, body
    assert body["spec_review"] is None
    # The agents layer's own wording, carried through unchanged.
    assert any("no spec review agenda" in w for w in body["warnings"])
    assert body["kicad_pcb"]


def test_a_model_that_raises_on_the_agenda_is_null_with_a_warning(
    server, monkeypatch
):
    """An exception out of the agenda stage is caught the way the step does it.

    Every other marker answers; only the spec-review prompt has no answer, so
    the ScriptedModel raises ``ModelError`` there and nowhere else.
    """
    markers = dict(scripted().by_marker)
    model = ScriptedModel(by_marker=markers)
    monkeypatch.setattr(Handler, "model_factory", staticmethod(lambda: model))
    status, body = post(server, {**REQUEST, "summary": "structured"})
    assert status == 200, body
    assert body["spec_review"] is None
    warning = next(w for w in body["warnings"] if "spec-review agenda" in w)
    assert "ModelError" in warning
    assert len(_agenda_calls(model)) == 1


# ------------------------------------------------------------- no evidence


def test_no_evidence_means_no_model_call_and_a_stated_empty_agenda():
    """A clean run gets ``_nothing_to_discuss``, without asking the model.

    Pinned at the seam rather than over the socket, because the scripted
    three-net regulator never routes fully on the 0.25 mm grid (VIN and VOUT
    share one grid node under the SOT-223) -- a real run always carries
    evidence. With no findings and no routing at all the evidence block is
    empty, and asking a model to summarise an empty list is how an invented
    agenda is born, so the agents layer does not ask; this pins that the
    one-shot route reaches it through ``steps._agenda`` and gets the same
    stated answer.
    """
    model = ScriptedModel(by_marker={SPECREVIEW_MARKER: json.dumps(AGENDA)})
    result = SimpleNamespace(
        spec=None,
        board=SimpleNamespace(parts=[], nets=[]),
        route=None,
        findings=[],
    )
    events = []
    block, warnings = _spec_review_block(
        result, intent="a clean board", model=model, emit=events.append
    )
    assert block is not None
    assert block["items"] == []
    assert block["needs_meeting"] is False
    assert "flagged nothing" in block["summary"]
    assert warnings == []
    assert model.calls == []
    assert events == []


# ------------------------------------------------------- the review block


REVIEW_MARKER = "reviewing a circuit someone else designed"


def test_the_review_block_rides_both_one_shot_routes(server, monkeypatch):
    """``review`` on ``/generate`` is the review step's block, key for key.

    ``findings: []`` on its own cannot say whether the critic found nothing,
    was never asked, or answered something unreadable; the block can, and
    the desktop reads the same shape off the step envelope, the status route
    and both one-shot routes. ``blockers``/``findings`` stay as they were.
    """
    _install(monkeypatch)
    status, body = post(server, REQUEST)
    assert status == 200, body
    assert body["review"] == {
        "status": "ok",
        "ran": True,
        "detail": None,
        "note": f"the review found {len(body['findings'])} finding(s), "
        f"{len(body['blockers'])} blocker(s)",
        # The filter's two receipts ride the block. Empty here because this
        # scripted critic names only parts the spec has and says each thing
        # once -- and empty is emitted rather than omitted, because a missing
        # key reads as "this surface does not know" rather than "nothing was
        # hidden".
        "dropped": [],
        "merged": [],
    }
    assert body["findings"] and isinstance(body["blockers"], list)

    status, _, frames = post_stream(server, REQUEST)
    assert status == 200
    assert frames[-1]["event"] == "run.done"
    assert frames[-1]["result"]["review"] == body["review"]


def test_the_review_block_names_what_the_filter_threw_away(server, monkeypatch):
    """The join the critic lane could not close from its own side.

    ``review.py`` filters a finding that names only parts the spec does not
    contain, and records one line per drop on ``ReviewReport.dropped``. Those
    lines stopped at the stage boundary: ``as_dict`` carried status, ran,
    detail and note, so a client saw two findings where the critic wrote three
    and had no way to tell that from a critic that only wrote two -- exactly
    the thing the field exists to prevent. The lists are on the wire now, and
    a drop has to be visible there or the field is decorative.
    """
    hallucinated = {
        "findings": [
            *REVIEW["findings"],
            {
                "severity": "blocker",
                "title": "R47 is the wrong value for the feedback divider",
                "detail": "The divider sets the output above the rail.",
                "parts": ["R47"],
                "citation": "",
                "suggested_fix": "Use 10k.",
            },
        ]
    }
    _install(
        monkeypatch,
        **{"reviewing a circuit someone else designed": json.dumps(hallucinated)},
    )
    status, body = post(server, REQUEST)
    assert status == 200, body
    block = body["review"]
    assert block["status"] == "ok"
    # The finding itself is gone...
    assert all("R47" not in f["title"] for f in body["findings"])
    # ...and the reason it is gone is on the wire, naming the part, rather
    # than being a difference in a count nobody can see.
    assert any("R47" in line for line in block["dropped"])
    assert isinstance(block["merged"], list)


def test_a_failed_critic_is_status_failed_never_an_empty_clean_list(
    server, monkeypatch
):
    _install(monkeypatch, **{REVIEW_MARKER: json.dumps({"verdict": "looks fine"})})
    status, body = post(server, REQUEST)
    assert status == 200, body
    assert body["findings"] == [] and body["blockers"] == []
    block = body["review"]
    assert block["status"] == "failed" and block["ran"] is True
    assert isinstance(block["detail"], str) and block["detail"]
    assert "nothing is known about this board" in block["note"]


def test_a_review_turned_off_is_status_skipped(server, monkeypatch):
    _install(monkeypatch)
    status, body = post(server, {**REQUEST, "review": False})
    assert status == 200, body
    assert body["review"] == {
        "status": "skipped",
        "ran": False,
        "detail": None,
        "note": "the review did not run, so nothing is known about this board",
        "dropped": [],
        "merged": [],
    }

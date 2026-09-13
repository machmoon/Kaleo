"""The ``spec_review`` delivery destination (``service/deliver.py``).

Offline, through the real HTTP surface: the Google side is the package's
recording transport installed on ``deliver.transport_factory``, so every
assertion is about the request that would have left the machine and the JSON
the desktop panel actually receives.

The fixtures and helpers come from ``test_deliver.py`` -- one definition of
"a routed session" for both files, so a change to the session shape cannot
leave this file testing a session ``steps.py`` no longer produces.
"""

import json

import pytest
from silkscreen.agents.review import Severity

from googleapps.tests.fakes import RecordingTransport, finding
from googleapps.transport import HttpResponse
from service import deliver
from service.tests import test_deliver as _base

# Bound rather than imported: pytest finds a fixture by name in the module
# namespace either way, and a plain assignment does not read as a redefinition
# when a test then takes the same name as an argument.
server = _base.server
configured = _base.configured
post = _base.post
get = _base.get
routed_session = _base.routed_session

WHEN = "2030-09-08T15:00:00Z"


def blocked(tmp_path, **kwargs):
    kwargs.setdefault(
        "findings", [finding(title="VIN has no bulk capacitor"), finding(Severity.NOTE)]
    )
    return routed_session(tmp_path, **kwargs)


# ---------------------------------------------------------------- booking


def test_a_spec_review_is_booked_with_the_agenda_and_a_meet_link(
    server, tmp_path, configured
):
    blocked(tmp_path, unrouted={"VOUT": "no path at 0.25 mm clearance"})
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"], "when": WHEN},
    )
    assert status == 200, body
    block = body["spec_review"]
    assert block["ok"] is True
    assert block["meet_uri"] == "https://meet.google.com/abc-defg-hij"
    assert block["html_link"].startswith("https://calendar.google.com/")
    # One blocking finding plus the ratsnest: two items, ten minutes each.
    assert block["items"] == 2 and block["blocking"] == 2
    assert block["minutes"] == 20

    (request,) = [r for r in configured.requests if "calendar/v3" in r.url]
    # Without sendUpdates the event is created and nobody is invited.
    assert "sendUpdates=all" in request.url
    assert "conferenceDataVersion=1" in request.url
    event = json.loads(request.body)
    assert event["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {
        "type": "hangoutsMeet"
    }
    assert event["attendees"] == [{"email": "lead@example.com"}]
    assert event["summary"] == "Spec review: a-3-3v-regulator"
    # The requested start, and an end computed from the agenda's own minutes.
    assert event["start"] == {"dateTime": "2030-09-08T15:00:00Z", "timeZone": "UTC"}
    assert event["end"] == {"dateTime": "2030-09-08T15:20:00Z", "timeZone": "UTC"}
    # The agenda, and every unrouted net named verbatim.
    assert "VIN has no bulk capacitor" in event["description"]
    assert "VOUT: no path at 0.25 mm clearance" in event["description"]

    text = json.dumps(body)
    assert "t-secret-token" not in text and "ya29." not in text
    assert "client-secret-value" not in text


def test_the_description_is_html_escaped(server, tmp_path, configured):
    """Calendar renders a subset of HTML in the description."""
    blocked(
        tmp_path,
        findings=[finding(title="VIN <b>needs</b> a bulk capacitor")],
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200 and body["spec_review"]["ok"] is True
    (request,) = [r for r in configured.requests if "calendar/v3" in r.url]
    description = json.loads(request.body)["description"]
    assert "VIN &lt;b&gt;needs&lt;/b&gt; a bulk capacitor" in description
    assert "<b>" not in description


def test_without_when_the_hold_is_still_booked(server, tmp_path, configured):
    blocked(tmp_path)
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"], "when": None},
    )
    assert status == 200, body
    assert body["spec_review"]["ok"] is True
    (request,) = [r for r in configured.requests if "calendar/v3" in r.url]
    event = json.loads(request.body)
    assert event["start"]["dateTime"].endswith("Z")


# ---------------------------------------------------------------- the skips


def test_no_blocking_item_books_nothing_and_says_why(server, tmp_path, configured):
    routed_session(tmp_path, findings=[finding(Severity.NOTE)])
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["spec_review"]["ok"] is False
    assert "nothing needs a meeting" in body["spec_review"]["skipped_reason"]
    assert "error" not in body["spec_review"]
    assert not configured.called("calendar/v3")


def test_without_the_review_step_it_says_so_rather_than_guessing(
    server, tmp_path, configured
):
    routed_session(tmp_path, reviewed=False)
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["spec_review"]["ok"] is False
    assert "the review step has not run" in body["spec_review"]["skipped_reason"]
    assert not configured.called("calendar/v3")


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"spec_review": True}, "'spec_review' needs at least one address"),
        (
            {"spec_review": True, "attendees": ["lead@example.com", "not an address"]},
            "not an address",
        ),
        (
            {
                "spec_review": True,
                "attendees": ["lead@example.com"],
                "when": "next tuesday",
            },
            "RFC 3339",
        ),
        (
            {
                "spec_review": True,
                "attendees": ["lead@example.com"],
                "when": "2030-09-08T15:00:00",
            },
            "timezone offset",
        ),
        (
            {
                "spec_review": True,
                "attendees": ["lead@example.com"],
                "when": "2020-01-01T00:00:00Z",
            },
            "in the past",
        ),
        (
            {"chat": True, "attendees": ["lead@example.com"]},
            "'attendees' only means something",
        ),
        (
            {"chat": True, "when": WHEN},
            "'when' only means something with 'spec_review'",
        ),
        ({"spec_review": "yes"}, "'spec_review' must be a boolean"),
    ],
)
def test_bad_input_is_a_400_before_any_request(
    server, tmp_path, configured, payload, expected
):
    blocked(tmp_path)
    status, body = post(server, "/steps/sess1/deliver", payload)
    assert status == 400, body
    assert expected in body["error"]
    assert configured.requests == []


def test_every_bad_address_is_named_at_once(server, tmp_path, configured):
    blocked(tmp_path)
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["nope", "also bad", "ok@example.com"]},
    )
    assert status == 400
    assert "nope" in body["error"] and "also bad" in body["error"]
    assert configured.requests == []


def test_an_unrouted_session_is_still_a_409(server, tmp_path, configured):
    blocked(tmp_path, stage="placed")
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 409
    assert "needs the run to be 'routed'" in body["error"]
    assert configured.requests == []


def test_an_unknown_session_is_still_a_404(server, configured):
    status, body = post(
        server,
        "/steps/nope/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 404 and "nope" in body["error"]


# ---------------------------------------------------------------- isolation


def test_a_failed_booking_is_one_named_failure_inside_one_200(
    server, tmp_path, configured
):
    """Calendar refusing must not become a 500, nor stop the email."""
    # Everything is configured (the fixture's token is valid); only Calendar
    # refuses.
    transport = RecordingTransport(
        {
            "calendar/v3": HttpResponse(
                403,
                b'{"error": {"code": 403, "message": '
                b'"Calendar API has not been used"}}',
            )
        }
    )
    deliver.transport_factory = lambda: transport
    blocked(tmp_path)
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {
            "spec_review": True,
            "attendees": ["lead@example.com"],
            "email": ["lead@example.com"],
        },
    )
    assert status == 200, body
    assert body["spec_review"]["ok"] is False
    assert "Calendar API has not been used" in body["spec_review"]["error"]
    assert "skipped_reason" not in body["spec_review"]
    # The other destination went out regardless.
    assert body["email"]["ok"] is True
    text = json.dumps(body)
    assert "ya29." not in text and "t-secret-token" not in text


def test_schedule_and_spec_review_are_independent(server, tmp_path, configured):
    """Both may be asked for at once; each reports its own outcome."""
    blocked(tmp_path)
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {
            "schedule": True,
            "spec_review": True,
            "attendees": ["lead@example.com"],
        },
    )
    assert status == 200, body
    assert body["calendar"]["ok"] is True
    assert body["spec_review"]["ok"] is True
    assert len([r for r in configured.requests if "calendar/v3" in r.url]) == 2


# ---------------------------------------------------------------- config


def test_config_reports_the_destination_without_a_new_secret(server, configured):
    status, body = get(server, "/deliver/config")
    assert status == 200
    # The same token as Calendar: usable exactly when signed in.
    assert body["spec_review"] is True and body["calendar"] is True
    assert body["hints"] == []
    text = json.dumps(body)
    assert "client-secret-value" not in text and "t-secret-token" not in text


def test_config_without_sign_in_says_the_destination_is_unusable(server):
    status, body = get(server, "/deliver/config")
    assert status == 200
    assert body["spec_review"] is False


# ------------------------------------------- an agenda that could not be made


def test_a_failed_agenda_falls_back_rather_than_claiming_nothing_to_discuss(
    server, tmp_path, configured
):
    """A model failure must not read as "this board needs no meeting".

    ``service/steps.py::_agenda`` leaves the stage's result on the session, and
    a result whose ``review`` is ``None`` turns into an agenda with no items
    here -- which ``_book_spec_review`` skips as "no blocking item". The run
    below has a blocking finding *and* an unrouted net, so that answer would be
    a model outage reported as a clean board: strictly worse than never having
    asked for an agenda, since an unasked-for one books off the findings.

    The rule is that only a usable agenda reaches the session, so this falls
    back to ``agenda_from_result`` and books what the findings say.
    """
    from silkscreen.specreview import SpecReviewResult

    session = blocked(tmp_path, unrouted={"VOUT": "no path at 0.25 mm clearance"})
    # What a failed stage would have left behind before the fix.
    session.spec_review = SpecReviewResult(
        review=None, warnings=["no spec review agenda: no usable answer"]
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    block = body["spec_review"]
    assert block["ok"] is True, block
    assert block["blocking"] >= 1
    assert "skipped_reason" not in block


def test_the_review_step_never_leaves_an_unusable_agenda_on_the_session(
    server, tmp_path, configured
):
    """The other half of the rule, at the source: ``_agenda`` stores nothing
    when the model gave nothing usable, so the session cannot present an empty
    agenda as the engine's answer."""
    from silkscreen.agents.model import ScriptedModel
    from silkscreen.agents.specreview import SPECREVIEW_MARKER

    from service import steps

    session = blocked(tmp_path)
    session.spec_review = None
    events, emit, enter, tapped, _ = steps._events_sink(0.0, None)
    model = ScriptedModel(by_marker={SPECREVIEW_MARKER: "not json"})
    block, warnings = steps._agenda(session, model=model, emit=emit, enter=enter)
    assert block is None
    assert warnings and "no spec review agenda" in warnings[0]
    assert getattr(session, "spec_review", None) is None

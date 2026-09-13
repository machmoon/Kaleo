"""Calendar: the insert that mints a Meet link, and what comes back."""

from __future__ import annotations

import json

import pytest

from googleapps import calendar
from googleapps.auth import AuthError
from googleapps.tests.fakes import RecordingTransport
from googleapps.transport import GoogleError, HttpResponse

EVENT_RESPONSE = {
    "htmlLink": "https://www.google.com/calendar/event?eid=abc",
    "conferenceData": {
        "entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
            {"entryPointType": "video", "uri": "https://meet.google.com/xyz-abcd-efg"},
        ]
    },
}

NOW = 1_756_600_000.0  # 2025-08-31 00:26:40 UTC


def schedule(transport, **kwargs):
    return calendar.schedule_review(
        "ya29.tok",
        board_name="ldo-board",
        blocker_titles=["VIN has no bulk capacitor"],
        attendees=["lead@example.com", "james@example.com"],
        transport=transport,
        now=NOW,
        request_id="fixed-request-id",
        **kwargs,
    )


def test_the_insert_asks_google_to_mint_a_meet_link():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    event = schedule(transport)
    request = transport.requests[0]
    assert request.method == "POST"
    # conferenceDataVersion=1 is what allows createRequest at all.
    assert request.url == (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        "?conferenceDataVersion=1&sendUpdates=all"
    )
    assert request.headers["Authorization"] == "Bearer ya29.tok"
    body = json.loads(request.body)
    create = body["conferenceData"]["createRequest"]
    assert create["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert create["requestId"] == "fixed-request-id"
    assert event.html_link == EVENT_RESPONSE["htmlLink"]
    assert event.meet_uri == "https://meet.google.com/xyz-abcd-efg"


@pytest.mark.parametrize(
    "status_code, expect_in_note",
    [
        ("pending", "still minting"),
        ("failure", "no way to join"),
    ],
)
def test_a_conference_that_is_not_ready_yet_is_said_out_loud(
    status_code, expect_in_note
):
    """Calendar v3, ``ConferenceRequestStatus.statusCode``: ``pending`` is
    "still being processed", ``failure`` is "there are no entry points". The
    insert returns 200 in both cases and the attendees have already been
    mailed (``sendUpdates=all``), so an empty ``meet_uri`` on its own cannot
    be allowed to stand for "there is no link, and here is why".
    """
    response = {
        "htmlLink": EVENT_RESPONSE["htmlLink"],
        "conferenceData": {
            "createRequest": {
                "requestId": "fixed-request-id",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
                "status": {"statusCode": status_code},
            }
        },
    }
    event = schedule(RecordingTransport({"calendars/primary/events": response}))
    assert event.meet_uri == ""
    assert event.conference_status == status_code
    assert expect_in_note in event.meet_note


def test_a_response_with_entry_points_reports_success_and_no_note():
    event = schedule(RecordingTransport({"calendars/primary/events": EVENT_RESPONSE}))
    assert event.conference_status == "success"
    assert event.meet_note == ""


def test_a_response_carrying_no_conference_at_all_says_so():
    """What an insert without ``conferenceDataVersion=1`` looks like: a 200,
    an event, and no conference data anywhere. ``absent`` keeps that
    distinguishable from a conference Google tried and failed to mint."""
    event = schedule(
        RecordingTransport(
            {"calendars/primary/events": {"htmlLink": EVENT_RESPONSE["htmlLink"]}}
        )
    )
    assert (event.meet_uri, event.conference_status) == ("", "absent")
    assert "no createRequest status" in event.meet_note


def test_the_event_is_titled_after_the_board_and_carries_the_blockers():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    schedule(transport)
    body = json.loads(transport.requests[0].body)
    assert body["summary"] == "Design review: ldo-board"
    assert "VIN has no bulk capacitor" in body["description"]
    assert body["attendees"] == [
        {"email": "lead@example.com"},
        {"email": "james@example.com"},
    ]


def test_the_times_are_rfc3339_a_day_out_for_half_an_hour():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    schedule(transport)
    body = json.loads(transport.requests[0].body)
    assert body["start"] == {"dateTime": "2025-09-01T00:26:40Z", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2025-09-01T00:56:40Z", "timeZone": "UTC"}


def test_hangout_link_is_the_fallback_when_entry_points_are_absent():
    transport = RecordingTransport(
        {"calendars/primary/events": {
            "htmlLink": "L", "hangoutLink": "https://meet.google.com/fallback"}}
    )
    assert schedule(transport).meet_uri == "https://meet.google.com/fallback"


def test_a_401_tells_the_user_to_reauthenticate():
    transport = RecordingTransport(
        {"calendars/primary/events": HttpResponse(401, b"{}")}
    )
    with pytest.raises(AuthError, match="python -m googleapps auth"):
        schedule(transport)


def test_attendees_are_actually_invited():
    """Without sendUpdates the insert succeeds and nobody is told -- a
    calendar entry, not a scheduled review."""
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    schedule(transport)
    assert "sendUpdates=all" in transport.requests[0].url


def test_a_bad_attendee_is_refused_before_any_request():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    with pytest.raises(GoogleError, match="bad_address"):
        calendar.schedule_review(
            "ya29.tok", board_name="b", blocker_titles=["x"],
            attendees=["lead@example.com", "not an address"], transport=transport,
        )
    assert transport.requests == []


def test_no_attendees_is_an_error():
    with pytest.raises(GoogleError, match="attendee"):
        calendar.schedule_review(
            "ya29.tok", board_name="b", blocker_titles=["x"],
            attendees=[], transport=RecordingTransport(),
        )


def test_google_s_own_error_message_is_surfaced():
    transport = RecordingTransport(
        {"calendars/primary/events": HttpResponse(
            403, b'{"error": {"code": 403, "message": '
                 b'"Google Calendar API has not been used in project 123"}}')}
    )
    with pytest.raises(GoogleError, match="has not been used in project 123"):
        schedule(transport)


def test_a_model_authored_blocker_title_is_escaped_in_the_description():
    """Calendar renders a subset of HTML, and these titles come from a model.

    `chat.py` and `specreview.py` both escape for exactly this reason; this
    one interpolated raw. A blocker reading "C3 <100nF> too far from U1"
    reached invited reviewers with the "<100nF>" silently deleted, and
    model-authored markup landed as live content in an invitation mailed to
    every attendee (`sendUpdates=all`).
    """
    transport = RecordingTransport({"calendar": EVENT_RESPONSE})
    calendar.schedule_review(
        "ya29.tok",
        board_name="ldo-board",
        blocker_titles=["C3 <100nF> too far from U1 & U2"],
        attendees=["lead@example.com"],
        transport=transport,
        now=NOW,
    )
    body = json.loads(transport.requests[0].body.decode("utf-8"))
    description = body["description"]
    assert "<100nF>" not in description
    assert "&lt;100nF&gt;" in description
    assert "&amp;" in description

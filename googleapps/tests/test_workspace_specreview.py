"""Booking a spec review: the agenda, the event body, and the refusals.

Offline: the only seam is the recording transport, so every assertion here is
about the request that would actually have left the machine.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.review import Severity

from googleapps import specreview
from googleapps.auth import AuthError
from googleapps.tests.fakes import (
    RecordingTransport,
    fake_result,
    fake_route,
    finding,
)
from googleapps.transport import GoogleError, HttpResponse

EVENT_RESPONSE = {
    "htmlLink": "https://www.google.com/calendar/event?eid=abc",
    "conferenceData": {
        "entryPoints": [
            {"entryPointType": "video", "uri": "https://meet.google.com/xyz-abcd-efg"}
        ]
    },
}

NOW = 1_756_600_000.0  # 2025-08-31 00:26:40 UTC

#: The shape ``SpecReview.as_dict()`` returns (ENGINE contract 5).
SPEC_REVIEW = {
    "title": "Spec review: ldo-board",
    "summary": "Two open questions before this goes to fab.",
    "items": [
        {
            "topic": "VIN has no bulk capacitor",
            "why": "The LDO oscillates below 10 uF <input> ESR",
            "minutes": 15,
            "blocking": True,
            "refs": ["U1", "VIN"],
        },
        {
            "topic": "Silkscreen polarity marks",
            "why": "Nice to have",
            "minutes": 5,
            "blocking": False,
            "refs": ["D1"],
        },
    ],
    "decisions_needed": ["pick the input capacitor"],
    "prepared_from": ["review", "route"],
}


def agenda(**overrides):
    payload = {**SPEC_REVIEW, **overrides}
    return specreview.agenda_from_spec_review(payload)


def book(transport, **kwargs):
    kwargs.setdefault("agenda", agenda())
    kwargs.setdefault("attendees", ["lead@example.com", "james@example.com"])
    return specreview.schedule_spec_review(
        "ya29.tok",
        transport=transport,
        now=NOW,
        request_id="fixed-request-id",
        **kwargs,
    )


# ------------------------------------------------------------------ the insert


def test_the_insert_invites_everyone_and_mints_a_meet_link():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    event = book(transport)
    request = transport.requests[0]
    assert request.method == "POST"
    # conferenceDataVersion=1 allows createRequest at all; without sendUpdates
    # the event is created and no attendee is ever told.
    assert "conferenceDataVersion=1" in request.url
    assert "sendUpdates=all" in request.url
    assert request.headers["Authorization"] == "Bearer ya29.tok"
    body = json.loads(request.body)
    assert body["conferenceData"]["createRequest"] == {
        "requestId": "fixed-request-id",
        "conferenceSolutionKey": {"type": "hangoutsMeet"},
    }
    assert body["attendees"] == [
        {"email": "lead@example.com"},
        {"email": "james@example.com"},
    ]
    assert event.html_link == EVENT_RESPONSE["htmlLink"]
    assert event.meet_uri == "https://meet.google.com/xyz-abcd-efg"


def test_the_duration_is_the_agendas_own_minutes():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    book(transport)
    body = json.loads(transport.requests[0].body)
    # 15 + 5 = 20 minutes, a day out from NOW.
    assert body["start"] == {"dateTime": "2025-09-01T00:26:40Z", "timeZone": "UTC"}
    assert body["end"] == {"dateTime": "2025-09-01T00:46:40Z", "timeZone": "UTC"}


def test_an_explicit_when_is_the_start_time():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    book(transport, when=specreview.parse_when("2026-09-08T15:00:00Z", now=NOW))
    body = json.loads(transport.requests[0].body)
    assert body["start"]["dateTime"] == "2026-09-08T15:00:00Z"
    assert body["end"]["dateTime"] == "2026-09-08T15:20:00Z"


def test_the_agenda_is_html_escaped_in_the_description():
    """Calendar renders a subset of HTML; model text must not be markup."""
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    book(transport)
    body = json.loads(transport.requests[0].body)
    description = body["description"]
    assert "10 uF &lt;input&gt; ESR" in description
    assert "<input>" not in description
    assert body["summary"] == "Spec review: ldo-board"
    assert "1. VIN has no bulk capacitor — 15 min [blocking]" in description
    assert "refs: U1, VIN" in description
    assert "pick the input capacitor" in description


def test_every_unrouted_net_is_named_in_the_description():
    """No invitation may read as 'board ready' over a ratsnest."""
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    book(transport, unrouted={"VOUT": "no path at 0.25 mm clearance"})
    description = json.loads(transport.requests[0].body)["description"]
    assert "VOUT: no path at 0.25 mm clearance" in description


# ------------------------------------------------------------------ refusals


def test_an_agenda_with_nothing_blocking_is_refused_before_any_request():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    quiet = agenda(items=[dict(SPEC_REVIEW["items"][1])])
    with pytest.raises(GoogleError, match="nothing needs a meeting"):
        book(transport, agenda=quiet)
    assert transport.requests == []


def test_a_bad_attendee_is_refused_before_any_request():
    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    with pytest.raises(GoogleError, match="bad_address"):
        book(transport, attendees=["lead@example.com", "not an address"])
    assert transport.requests == []


def test_no_attendees_is_an_error():
    with pytest.raises(GoogleError, match="attendee"):
        book(RecordingTransport(), attendees=[])


def test_a_401_tells_the_user_to_reauthenticate():
    transport = RecordingTransport(
        {"calendars/primary/events": HttpResponse(401, b"{}")}
    )
    with pytest.raises(AuthError, match="python -m googleapps auth"):
        book(transport)


def test_googles_own_error_is_surfaced():
    transport = RecordingTransport(
        {"calendars/primary/events": HttpResponse(
            403, b'{"error": {"code": 403, "message": "Calendar API disabled"}}')}
    )
    with pytest.raises(GoogleError, match="Calendar API disabled"):
        book(transport)


def test_a_spec_review_that_is_not_a_dict_is_refused():
    with pytest.raises(GoogleError, match="bad_agenda"):
        specreview.agenda_from_spec_review(["not", "a", "review"])


# ------------------------------------------------------------------ when


def test_parse_when_accepts_rfc3339_with_an_offset():
    utc = specreview.parse_when("2026-09-08T15:00:00Z", now=NOW)
    offset = specreview.parse_when("2026-09-08T17:00:00+02:00", now=NOW)
    assert utc == 1_788_879_600.0
    assert offset == utc


def test_parse_when_names_what_is_wrong():
    with pytest.raises(ValueError, match="RFC 3339"):
        specreview.parse_when("next tuesday", now=NOW)
    with pytest.raises(ValueError, match="timezone offset"):
        specreview.parse_when("2026-09-08T15:00:00", now=NOW)
    with pytest.raises(ValueError, match="in the past"):
        specreview.parse_when("2020-01-01T00:00:00Z", now=NOW)
    with pytest.raises(ValueError, match="RFC 3339"):
        specreview.parse_when(17, now=NOW)


# ------------------------------------------------------------------ fallback


def test_the_deterministic_agenda_is_the_blockers_and_the_ratsnest():
    result = fake_result(
        findings=[
            finding(title="VIN has no bulk capacitor", parts=("U1",)),
            finding(Severity.NOTE, title="add a test point"),
        ],
        route=fake_route(unrouted={"VOUT": "no path at 0.25 mm clearance"}),
    )
    built = specreview.agenda_from_result(result, board_name="ldo-board")
    assert built.title == "Spec review: ldo-board"
    assert [item.topic for item in built.items] == [
        "VIN has no bulk capacitor",
        "1 net(s) left as ratsnest",
    ]
    assert all(item.blocking for item in built.items)
    assert built.items[1].refs == ("VOUT",)
    # 2 x 10 minutes, inside the IR's 15..60 window.
    assert built.total_minutes() == 20
    assert "1 net(s) unrouted" in built.summary
    assert len(built.summary) <= 400


def test_a_clean_board_yields_an_agenda_with_nothing_blocking():
    """"Nothing needs a meeting" is a result; no item is invented to fill it."""
    built = specreview.agenda_from_result(
        fake_result(findings=[finding(Severity.NOTE)]), board_name="ldo-board"
    )
    assert built.blocking == ()
    # And a caller who books it anyway is still refused.
    transport = RecordingTransport()
    with pytest.raises(GoogleError, match="nothing needs a meeting"):
        book(transport, agenda=built)


def test_the_duration_is_clamped_to_the_irs_window():
    long_agenda = agenda(
        items=[
            {"topic": f"item {n}", "minutes": 30, "blocking": True}
            for n in range(4)
        ]
    )
    assert long_agenda.total_minutes() == specreview.MAX_MINUTES
    short = agenda(items=[{"topic": "one", "minutes": 1, "blocking": True}])
    assert short.total_minutes() == specreview.MIN_MINUTES


# ------------------------------------------------------------------ the real IR


def test_a_real_spec_review_books_the_meeting_it_says_it_needs():
    """The engine's own IR, through ``as_dict()`` and nothing else.

    The duration is the IR's ``total_minutes()``, not a re-addition of the
    items here: the number the engineer was shown is the meeting they agreed
    to, and two definitions of it would eventually disagree.
    """
    from silkscreen.specreview import parse_spec_review

    review = parse_spec_review(
        json.dumps(
            {
                "title": "Spec review: ldo-board",
                "summary": "Two questions before fab.",
                "items": [
                    {
                        "topic": "VIN bulk capacitor",
                        "why": "ESR sets stability",
                        "minutes": 20,
                        "blocking": True,
                        "refs": ["U1"],
                    }
                ],
                "decisions_needed": ["pick the capacitor"],
                "prepared_from": ["review"],
            }
        )
    )
    built = specreview.agenda_from_spec_review(review)
    assert built.total_minutes() == review.total_minutes()
    assert len(built.blocking) == 1

    transport = RecordingTransport({"calendars/primary/events": EVENT_RESPONSE})
    book(transport, agenda=built)
    body = json.loads(transport.requests[0].body)
    assert body["summary"] == "Spec review: ldo-board"
    assert body["end"]["dateTime"] == "2025-09-01T00:46:40Z"  # +20 minutes


def test_a_stage_result_that_produced_no_review_books_nothing():
    """``SpecReviewResult.as_dict()`` with ``review: None`` is not an agenda."""
    built = specreview.agenda_from_spec_review(
        {"ok": False, "review": None, "needs_meeting": False, "warnings": ["no answer"]}
    )
    assert built.blocking == ()
    with pytest.raises(GoogleError, match="nothing needs a meeting"):
        book(RecordingTransport(), agenda=built)

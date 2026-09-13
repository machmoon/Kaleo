"""Scheduling a design-review Calendar event with a Meet link.

One endpoint: insert on the ``primary`` calendar with
``conferenceDataVersion=1``, which is what allows the ``createRequest`` that
makes Google mint a Meet link server-side, and ``sendUpdates=all``, without
which the insert succeeds but no attendee is told -- an event nobody is
invited to is a calendar entry, not a scheduled review. The event is titled
after the board and the description carries the review's blockers -- the
event exists *because* the review found blockers, so they are the agenda.

Nothing here decides whether to schedule; that judgment (blockers or not)
belongs to the caller in ``__main__``, and it says out loud either way.
"""

from __future__ import annotations

import html
import json
import time
import uuid
from dataclasses import dataclass

from .addresses import validate_addresses
from .auth import RERUN_HINT, AuthError
from .transport import (
    GoogleError,
    HttpRequest,
    Transport,
    ensure_google_url,
    error_detail,
)

__all__ = [
    "EVENTS_URL",
    "ScheduledEvent",
    "insert_event",
    "rfc3339",
    "schedule_review",
]

#: **CONFIRMED 2026-09-08** against the Calendar v3 discovery document
#: (``https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest``),
#: ``resources.events.methods.insert``:
#:
#: * ``rootUrl`` ``https://www.googleapis.com/`` + ``servicePath``
#:   ``calendar/v3/`` + ``path`` ``calendars/{calendarId}/events``, POST.
#: * ``conferenceDataVersion``: integer, minimum 0, maximum 1, **default 0**,
#:   "Version 1 enables support for copying of ConferenceData as well as for
#:   creating new conferences using the createRequest field of conferenceData."
#:   Omitting it is the classic silent failure -- the insert succeeds, returns
#:   200, and the event has no Meet link -- because the default *ignores*
#:   conference data in the body rather than rejecting it.
#: * ``sendUpdates``: enum ``all`` / ``externalOnly`` / ``none``, "The default
#:   is false"; ``all`` is "Notifications are sent to all guests." Without it
#:   the insert succeeds and no attendee is told.
EVENTS_URL = (
    "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    "?conferenceDataVersion=1&sendUpdates=all"
)

#: **CONFIRMED** from ``schemas.ConferenceSolutionKey.type`` in the same
#: document: "``hangoutsMeet`` for Google Meet (http://meet.google.com)". The
#: two ``Hangout`` values there are documented as deprecated for new
#: conferences, and ``addOn`` is for third-party providers.
MEET_SOLUTION_TYPE = "hangoutsMeet"

#: Reviews default to tomorrow, same time, for half an hour: soon enough to
#: matter, far enough that attendees can actually come.
DEFAULT_LEAD_S = 24 * 3600
DEFAULT_DURATION_S = 30 * 60


@dataclass(frozen=True)
class ScheduledEvent:
    html_link: str
    meet_uri: str
    #: What Google said about the conference it was asked to mint. Three
    #: documented values plus one of ours. **CONFIRMED** from the Calendar v3
    #: discovery document, ``schemas.ConferenceRequestStatus.statusCode``:
    #: "``pending``: the conference create request is still being processed";
    #: "``success``: the conference create request succeeded, the entry points
    #: are populated"; "``failure``: the conference create request failed,
    #: there are no entry points". ``absent`` is this module's word for a
    #: response that carried no ``createRequest.status`` at all -- which is
    #: what an insert without ``conferenceDataVersion=1`` looks like.
    #:
    #: This field exists because ``meet_uri`` alone cannot tell those apart.
    #: An empty string meant "no Meet link" and nothing else, so an event
    #: booked with ``sendUpdates=all`` -- every attendee mailed -- could go out
    #: with no way to join and no line anywhere saying why. ``pending`` is not
    #: an error and ``failure`` is; both need saying.
    conference_status: str = "absent"

    @property
    def meet_note(self) -> str:
        """One sentence about the Meet link, or ``""`` when there is one."""
        if self.meet_uri:
            return ""
        if self.conference_status == "pending":
            return (
                "Google is still minting the Meet link (createRequest status "
                "'pending'); it will appear on the event shortly"
            )
        if self.conference_status == "failure":
            return (
                "Google failed to mint a Meet link for this event "
                "(createRequest status 'failure'); the event exists and the "
                "attendees were invited, but there is no way to join it"
            )
        return (
            "no Meet link came back and the response carried no createRequest "
            "status; the event exists and the attendees were invited"
        )


def rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


#: Kept for callers written before the name lost its underscore.
_rfc3339 = rfc3339


def insert_event(token: str, body: dict, *, transport: Transport) -> ScheduledEvent:
    """POST one event body to ``primary`` and read back the link and Meet URI.

    The URL is the whole contract: ``conferenceDataVersion=1`` is what allows
    a ``createRequest`` at all, and ``sendUpdates=all`` is what tells the
    attendees -- without it the insert succeeds and nobody is invited. Both
    live in :data:`EVENTS_URL` so every caller gets them, and
    ``ensure_google_url`` refuses the request before a bearer token can travel
    anywhere but Google.
    """
    response = transport(
        HttpRequest(
            "POST",
            ensure_google_url(EVENTS_URL),
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json.dumps(body).encode("utf-8"),
        )
    )
    if response.status == 401:
        raise AuthError(f"Calendar rejected the access token; {RERUN_HINT}")
    if response.status >= 300:
        raise GoogleError(
            f"http_{response.status}",
            error_detail(response, "Calendar refused the insert"),
        )
    payload = response.json()

    conference = payload.get("conferenceData") or {}
    meet = ""
    for entry in conference.get("entryPoints") or []:
        if entry.get("entryPointType") == "video":
            meet = str(entry.get("uri") or "")
            break
    meet = meet or str(payload.get("hangoutLink") or "")
    status = str(
        ((conference.get("createRequest") or {}).get("status") or {}).get(
            "statusCode"
        )
        or ("success" if meet else "absent")
    )
    return ScheduledEvent(
        html_link=str(payload.get("htmlLink", "")),
        meet_uri=meet,
        conference_status=status,
    )


def schedule_review(
    token: str,
    *,
    board_name: str,
    blocker_titles: list[str],
    attendees: list[str],
    transport: Transport,
    now: float | None = None,
    request_id: str | None = None,
) -> ScheduledEvent:
    """Create the review event; returns its link and the Meet URI."""
    if not attendees:
        raise GoogleError("bad_request", "a review event needs at least one attendee")
    attendees = validate_addresses(attendees, what="the attendee list")
    start = (time.time() if now is None else now) + DEFAULT_LEAD_S
    # Escaped, like `chat.py` and `specreview.py`: Calendar renders a subset
    # of HTML in the description, and these titles are model-authored. A
    # blocker reading "C3 <100nF> too far from U1" reached invited reviewers
    # with the "<100nF>" silently deleted, and model-authored markup landed
    # as live content in an invitation mailed to everyone with sendUpdates=all.
    agenda = html.escape("\n".join(f"- {title}" for title in blocker_titles))
    body = {
        "summary": f"Design review: {board_name}",
        "description": (
            "The silkscreen adversarial review found blocking findings on "
            f"{html.escape(board_name)}:\n{agenda}\n\n"
            "Generated by python -m googleapps."
        ),
        "start": {"dateTime": rfc3339(start), "timeZone": "UTC"},
        "end": {"dateTime": rfc3339(start + DEFAULT_DURATION_S), "timeZone": "UTC"},
        "attendees": [{"email": address} for address in attendees],
        "conferenceData": {
            "createRequest": {
                # **CONFIRMED**, schemas.CreateConferenceRequest.requestId:
                # "Clients should regenerate this ID for every new request. If
                # an ID provided is the same as for the previous request, the
                # request is ignored." So the default is a fresh uuid4 per
                # call -- a fixed id here would silently reuse (or drop) a
                # conference. ``request_id`` is for tests, which need
                # determinism and mint no conferences.
                "requestId": request_id or uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": MEET_SOLUTION_TYPE},
            }
        },
    }
    return insert_event(token, body, transport=transport)

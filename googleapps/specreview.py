"""Booking a spec review: an agenda becomes a calendar hold with a Meet link.

The product rule this exists for: a finished run should not end in a wall of
text. The open questions become an agenda, the agenda becomes half an hour on
the calendar with the people who can answer them in the room, and the meeting
decides what a paragraph could not.

This module is the *scheduling* half. The agenda itself is
``silkscreen.specreview.SpecReview`` -- validated model output, built in the
engine -- and the only surface consumed here is its ``as_dict()``, so this
package never imports the engine to schedule (the ``runner.py`` convention
taken one step further: the import is not merely lazy, it is absent). A plain
dict of the same shape works identically, which is what lets the deterministic
fallback below exist without a model call.

The honesty rules of the existing calendar path carry over unchanged and are
the reason several of these functions refuse rather than guess:

* An agenda with **no blocking item does not get a meeting**. "Nothing needs a
  meeting" is a real answer; inventing an item to fill the slot is not.
* If the review has not run, nothing is booked and the reason says "the
  review step has not run". :func:`agenda_from_result` reads that fact from
  the result (``Agenda.reviewed``, ``Agenda.skip_reason``) and
  :func:`schedule_spec_review` refuses such an agenda, so a caller that
  forgot to check (``service/deliver.py``, ``__main__``) cannot book a
  meeting about a board nobody reviewed.
* Every unrouted net is named in the description. A review invitation that
  says the board is done over a ratsnest misleads exactly the person who has
  not opened it yet.
* The description is **HTML-escaped**: Calendar renders a subset of HTML in
  it, the same reason ``chat.py`` escapes card text.
"""

from __future__ import annotations

import datetime
import html
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .addresses import validate_addresses
from .calendar import (
    DEFAULT_LEAD_S,
    MEET_SOLUTION_TYPE,
    ScheduledEvent,
    insert_event,
    rfc3339,
)
from .chat import review_note, review_state
from .transport import GoogleError, Transport

__all__ = [
    "MAX_ITEMS",
    "MAX_MINUTES",
    "MIN_MINUTES",
    "NOT_REVIEWED_REASON",
    "NO_BLOCKERS_REASON",
    "Agenda",
    "AgendaLine",
    "agenda_from_result",
    "agenda_from_spec_review",
    "event_body",
    "parse_when",
    "schedule_spec_review",
]

#: The IR's own bounds (ENGINE contract: 1..8 items, 15..60 total minutes).
#: Repeated rather than imported so this module keeps working when the engine
#: is not beside it; a value outside them is clamped, never rejected -- a
#: meeting that is five minutes too long is not a reason to refuse to book it.
MAX_ITEMS = 8
MIN_MINUTES = 15
MAX_MINUTES = 60

#: How long one blocking finding is worth on the agenda, and one ratsnest.
_MINUTES_PER_ITEM = 10

#: The two reasons an agenda is not booked, in the words the Calendar rule in
#: ``service/deliver.py`` already uses. Three outcomes stay three answers:
#: the review never ran (nothing is known), the review found nothing (a real
#: answer: nothing needs a meeting), or the review found blockers (book).
NOT_REVIEWED_REASON = (
    "the review step has not run, so what the meeting would decide is not "
    "known — nothing scheduled"
)
NO_BLOCKERS_REASON = (
    "the agenda has no blocking item — nothing needs a meeting, so nothing "
    "was scheduled"
)


@dataclass(frozen=True)
class AgendaLine:
    """One thing the meeting has to decide."""

    topic: str
    why: str = ""
    minutes: int = _MINUTES_PER_ITEM
    blocking: bool = False
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Agenda:
    """A ``SpecReview`` in the shape this package schedules from.

    Every field the Calendar body reads is here and nothing else is, which
    makes this class the record of how much of the IR the scheduling depends
    on -- the ``SessionResult`` convention in ``service/deliver.py``.
    """

    title: str
    summary: str
    items: tuple[AgendaLine, ...]
    decisions_needed: tuple[str, ...] = ()
    prepared_from: tuple[str, ...] = ()
    #: The IR's own ``total_minutes()`` when it came with one. Preferred over
    #: re-adding the items here: the panel that showed the engineer "35 min"
    #: read that number, and the hold has to be the meeting they agreed to.
    minutes: int | None = None
    #: Whether the critic actually delivered a verdict for the run this
    #: agenda was built from. False means the blockers list is not "no
    #: blockers" but "unknown", and :func:`schedule_spec_review` refuses it.
    #: An agenda from a real ``SpecReview`` is reviewed by construction: the
    #: engine only produces one from evidence a critic gave it.
    reviewed: bool = True

    @property
    def blocking(self) -> tuple[AgendaLine, ...]:
        return tuple(item for item in self.items if item.blocking)

    @property
    def skip_reason(self) -> str | None:
        """Why this agenda gets no meeting, or ``None`` when it needs one.

        The judgment ``service/deliver.py`` and ``__main__`` make, in one
        place: an unreviewed run is not booked (what the meeting would decide
        is not known), an agenda with nothing blocking is not booked (nothing
        needs a meeting), and anything else is.
        """
        if not self.reviewed:
            return NOT_REVIEWED_REASON
        if not self.blocking:
            return NO_BLOCKERS_REASON
        return None

    def total_minutes(self) -> int:
        """The booked duration, clamped to the IR's range."""
        if self.minutes is not None:
            total = max(0, int(self.minutes))
        else:
            total = sum(max(0, int(item.minutes)) for item in self.items)
        return max(MIN_MINUTES, min(MAX_MINUTES, total or MIN_MINUTES))


# ------------------------------------------------------------------ sources


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(entry) for entry in value)
    except TypeError:
        return ()


def agenda_from_spec_review(source: Any, *, title: str | None = None) -> Agenda:
    """An :class:`Agenda` from a ``SpecReview`` (or its ``as_dict()``).

    Duck-typed on purpose: anything with ``as_dict()`` is asked for it, and a
    plain dict is read directly. Unknown keys are ignored and missing ones
    default, because a model-fed IR that grew a field must not stop a meeting
    from being booked.
    """
    if hasattr(source, "as_dict"):
        source = source.as_dict()
    # ``SpecReviewResult.as_dict()`` wraps the review; a caller holding the
    # stage's result rather than the review itself is not a mistake worth a
    # refusal. ``review: None`` means the stage produced nothing usable, and
    # an agenda with no items is then correctly unbookable.
    if isinstance(source, dict) and "items" not in source and "review" in source:
        source = source.get("review") or {}
    if not isinstance(source, dict):
        raise GoogleError(
            "bad_agenda",
            "a spec review must be a SpecReview or its dict, "
            f"got {type(source).__name__}",
        )
    raw_minutes = source.get("total_minutes")
    try:
        stated_minutes = None if raw_minutes is None else int(raw_minutes)
    except (TypeError, ValueError):
        stated_minutes = None
    raw_items = source.get("items") or ()
    items: list[AgendaLine] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        try:
            minutes = int(entry.get("minutes", _MINUTES_PER_ITEM))
        except (TypeError, ValueError):
            minutes = _MINUTES_PER_ITEM
        items.append(
            AgendaLine(
                topic=_text(entry.get("topic")),
                why=_text(entry.get("why")),
                minutes=minutes,
                blocking=bool(entry.get("blocking")),
                refs=_strings(entry.get("refs")),
            )
        )
    return Agenda(
        title=title or _text(source.get("title")) or "Spec review",
        summary=_text(source.get("summary")),
        items=tuple(items[:MAX_ITEMS]),
        decisions_needed=_strings(source.get("decisions_needed")),
        prepared_from=_strings(source.get("prepared_from")),
        minutes=stated_minutes,
    )


def agenda_from_result(result: Any, *, board_name: str) -> Agenda:
    """The deterministic agenda: what the run already knows, no model call.

    Used when nothing produced a ``SpecReview`` for this run. Blocking
    findings are blocking items, and a ratsnest is one more -- an unrouted net
    is a decision (finish it, or ship it that way), not a detail. No item is
    invented: a reviewed board with no blockers and no ratsnest yields an
    agenda with nothing blocking, which the caller then declines to book.

    Whether the review ran is read through ``chat.review_state``, which knows
    both shapes this is handed -- a ``PipelineResult`` (a ``ReviewReport`` on
    ``review``) and a ``service.deliver.SessionResult`` (a ``reviewed`` flag)
    -- so an unreviewed run yields ``Agenda.reviewed`` False and a summary
    that says so, never "0 blocking finding(s)" over a critic that was never
    asked.
    """
    state = review_state(result)
    items: list[AgendaLine] = []
    blockers = list(getattr(result, "blockers", ()) or ())
    for finding in blockers:
        items.append(
            AgendaLine(
                topic=_text(getattr(finding, "title", finding)),
                why=_text(getattr(finding, "detail", ""))
                or _text(getattr(finding, "suggested_fix", "")),
                minutes=_MINUTES_PER_ITEM,
                blocking=True,
                refs=_strings(getattr(finding, "parts", ())),
            )
        )
    unrouted = unrouted_nets(result)
    if unrouted:
        items.append(
            AgendaLine(
                topic=f"{len(unrouted)} net(s) left as ratsnest",
                why="; ".join(f"{net}: {why}" for net, why in sorted(unrouted.items())),
                minutes=_MINUTES_PER_ITEM,
                blocking=True,
                refs=tuple(sorted(unrouted)),
            )
        )
    prepared: list[str] = []
    if state == "ok":
        prepared.append("review")
    if getattr(result, "route", None) is not None:
        prepared.append("route")
    summary = _summary_line(result, blockers=len(blockers), unrouted=len(unrouted))
    return Agenda(
        title=f"Spec review: {board_name}",
        summary=summary,
        items=tuple(items[:MAX_ITEMS]),
        decisions_needed=tuple(_text(f.title) for f in blockers),
        prepared_from=tuple(prepared),
        reviewed=state == "ok",
    )


def _summary_line(result: Any, *, blockers: int, unrouted: int) -> str:
    """One line, never a wall of text (the IR caps this at 400 characters).

    The review clause is a count only when the critic answered; otherwise it
    is the critic's own note (or the calendar rule's wording), because
    "0 blocking finding(s)" from a review that never ran reads as clean. The
    routing clause says "not routed" when routing was turned off, for the
    same reason "0 net(s) unrouted" would.
    """
    parts: list[str] = []
    spec = getattr(result, "spec", None)
    if spec is not None and hasattr(spec, "part_count"):
        parts.append(f"{spec.part_count()} parts, {spec.net_count()} nets")
    board = getattr(result, "board", None)
    if board is not None and hasattr(board, "size_mm"):
        width, height = board.size_mm
        parts.append(f"board {width:.2f} x {height:.2f} mm")
    if review_state(result) == "ok":
        parts.append(f"{blockers} blocking finding(s)")
    else:
        parts.append(review_note(result))
    if getattr(result, "route", None) is None:
        parts.append("not routed (routing was turned off)")
    else:
        parts.append(f"{unrouted} net(s) unrouted")
    return "; ".join(parts)[:400]


def unrouted_nets(result: Any) -> dict[str, str]:
    """``{net: reason}`` for every net left as ratsnest, or ``{}``."""
    route = getattr(result, "route", None)
    if route is None:
        return {}
    return dict(getattr(route, "unrouted", {}) or {})


# ------------------------------------------------------------------ timing


def parse_when(value: Any, *, now: float | None = None) -> float:
    """An RFC 3339 timestamp to epoch seconds, or a named ``ValueError``.

    A timezone offset is required. A naive string is ambiguous -- the service
    and the engineer are rarely in the same zone -- and a meeting booked in
    the wrong hour is worse than a refusal that names the fix.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "'when' must be an RFC 3339 timestamp such as "
            "'2026-09-08T15:00:00Z' (or null for tomorrow)"
        )
    try:
        moment = datetime.datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"'when' is not an RFC 3339 timestamp: {value!r} "
            "(expected something like '2026-09-08T15:00:00Z')"
        ) from exc
    if moment.tzinfo is None:
        raise ValueError(
            f"'when' needs a timezone offset: {value!r} "
            "(append 'Z' for UTC, or '+02:00')"
        )
    epoch = moment.timestamp()
    current = time.time() if now is None else now
    if epoch <= current:
        raise ValueError(f"'when' is in the past: {value!r}")
    return epoch


# ------------------------------------------------------------------ event


def description(agenda: Agenda, *, unrouted: dict[str, str] | None = None) -> str:
    """The event description: the agenda, escaped, with the ratsnest named.

    Calendar renders a subset of HTML here, so every piece of text that came
    from a model or from a net name goes through :func:`html.escape` -- the
    same rule ``chat.py`` applies to ``textParagraph``.
    """
    lines: list[str] = []
    if agenda.summary:
        lines.append(agenda.summary)
        lines.append("")
    lines.append(f"Agenda ({agenda.total_minutes()} min):")
    for index, item in enumerate(agenda.items, start=1):
        flag = " [blocking]" if item.blocking else ""
        lines.append(f"{index}. {item.topic} — {max(0, int(item.minutes))} min{flag}")
        if item.why:
            lines.append(f"   {item.why}")
        if item.refs:
            lines.append(f"   refs: {', '.join(item.refs)}")
    if agenda.decisions_needed:
        lines.append("")
        lines.append("Decisions needed:")
        lines.extend(f"- {decision}" for decision in agenda.decisions_needed)
    if unrouted:
        lines.append("")
        lines.append("Nets left unrouted (finish these in KiCad):")
        lines.extend(f"  {net}: {why}" for net, why in sorted(unrouted.items()))
    if agenda.prepared_from:
        lines.append("")
        lines.append(f"Prepared from: {', '.join(agenda.prepared_from)}")
    lines.append("")
    lines.append("Generated by Hardy (python -m googleapps).")
    return html.escape("\n".join(lines))


def event_body(
    agenda: Agenda,
    *,
    attendees: list[str],
    start: float,
    unrouted: dict[str, str] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """The Calendar insert body. Built here so a test can read it whole."""
    minutes = agenda.total_minutes()
    return {
        "summary": agenda.title,
        "description": description(agenda, unrouted=unrouted),
        "start": {"dateTime": rfc3339(start), "timeZone": "UTC"},
        "end": {"dateTime": rfc3339(start + minutes * 60), "timeZone": "UTC"},
        "attendees": [{"email": address} for address in attendees],
        "conferenceData": {
            "createRequest": {
                # A fresh id per call, never ``""``. Calendar v3's
                # ``CreateConferenceRequest.requestId``: "Clients should
                # regenerate this ID for every new request. If an ID provided
                # is the same as for the previous request, the request is
                # ignored." An empty string is the same as the previous
                # request every time, so every spec review after the first
                # would have been booked with its conference request ignored
                # -- an event, with attendees mailed, and no Meet link.
                # ``schedule_spec_review`` already mints one; this is the
                # default for anyone calling ``event_body`` directly.
                "requestId": request_id or uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": MEET_SOLUTION_TYPE},
            }
        },
    }


def schedule_spec_review(
    token: str,
    *,
    agenda: Agenda,
    attendees: list[str],
    transport: Transport,
    when: float | None = None,
    unrouted: dict[str, str] | None = None,
    now: float | None = None,
    request_id: str | None = None,
) -> ScheduledEvent:
    """Book the spec review; returns the event link and the Meet URI.

    Refuses an agenda with nothing blocking, and one built from a run whose
    review never ran: the decision not to hold a meeting belongs to the
    caller, and these refusals are the backstop that keeps a caller which
    forgot to check from booking an empty half hour -- or a half hour about a
    board nobody has argued against.
    """
    if not attendees:
        raise GoogleError("bad_request", "a spec review needs at least one attendee")
    if not agenda.reviewed:
        raise GoogleError("not_reviewed", NOT_REVIEWED_REASON)
    if not agenda.blocking:
        raise GoogleError(
            "no_blockers",
            "the agenda has no blocking item, so nothing needs a meeting",
        )
    attendees = validate_addresses(attendees, what="the attendee list")
    if when is None:
        when = (time.time() if now is None else now) + DEFAULT_LEAD_S
    body = event_body(
        agenda,
        attendees=attendees,
        start=when,
        unrouted=unrouted,
        request_id=request_id or uuid.uuid4().hex,
    )
    return insert_event(token, body, transport=transport)

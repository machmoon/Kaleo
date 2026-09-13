"""An inbox of ideas sent from somewhere else, waiting for the desktop to take one.

Someone types "a 3.3 V LDO board for the sensor" into Slack from their phone.
The laptop has no public URL, so Slack cannot call it; the Slack bridge
(``slackbot/bridge.py``, over Socket Mode) runs *on* the laptop and posts the
idea here. The Ada overlay polls ``GET /inbox``, accepts one, and starts the
ordinary approval-gated step run with it -- so the engineer still presses every
paid step after the first, exactly as if the sentence had been typed into the
bar. This module is only the hand-off between those two processes: it stores
ideas, never runs one, and never talks to Slack.

The shape is Buildkite's agent protocol, not an invention. A Buildkite agent
learns a job exists, then must *accept* it before running it, then reports it
*started* -- three separate calls, and an accept for a job someone else already
accepted is refused (``buildkite/agent`` at ``1f02701``, ``api/jobs.go``:
``AcceptJob`` is ``PUT jobs/{id}/accept``, ``StartJob`` is ``PUT
jobs/{id}/start``, ``GetJobState`` is ``GET jobs/{id}``). The same three states
fit here for the same reasons:

* **accept is exclusive.** Two overlays (a laptop and a desktop both running
  Ada) polling one service must not both start a paid run for one message. The
  first accept wins; the second gets 409, this repo's word for "well-formed,
  and the state refused it" (``StepOrderError`` in ``service/app.py``).
* **start carries the step session id.** That id is what lets the Slack side
  follow the run through the *existing* ``GET /steps/<id>`` -- the overlay does
  not have to report progress anywhere, the engine already knows it.
* **a pending idea expires.** A laptop opened four hours after a message was
  sent must not quietly spend on a request the sender has long since stopped
  expecting; ``expired`` is a state the bridge reports in the thread, never a
  silent drop. Buildkite's analogue is a job's scheduled-expiry.

Idempotency follows ``service/runs.py``'s rule that the caller names the thing:
the bridge sends the Slack message identity as ``key``, so a Socket Mode
redelivery of one message (``retry_attempt`` > 0) lands on the idea already
created rather than making a second one.

Memory only, bounded, like ``service/runs.py``: a restart forgets the inbox,
and the bridge reads a 404 as exactly that and says so in the thread.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Inbox",
    "Idea",
    "InboxError",
    "IdeaNotFound",
    "IdeaConflict",
    "INBOX",
    "MAX_IDEAS",
    "MAX_TEXT_CHARS",
    "PENDING_TTL_S",
    "ACCEPT_TTL_S",
    "handle_get",
    "handle_post",
    "is_inbox_path",
]

#: Enough for a busy afternoon; the oldest settled idea is dropped first.
MAX_IDEAS = 100
#: An intent is a sentence or a paragraph. Bounded so a pasted log cannot
#: become the prompt of a paid run.
MAX_TEXT_CHARS = 4000
#: How long an idea waits for a desktop before it expires. Half an hour: long
#: enough to walk back to the laptop, short enough that a request from this
#: morning does not start a run this evening.
PENDING_TTL_S = 30 * 60
#: How long an accepted idea may go without ``start`` or ``fail`` before it is
#: failed on the overlay's behalf. An overlay that crashed between the two
#: calls would otherwise leave the thread saying "picked up" forever.
ACCEPT_TTL_S = 5 * 60

STATES = ("pending", "accepted", "started", "failed", "expired")
_SETTLED = ("started", "failed", "expired")
_ID = re.compile(r"^idea_[0-9a-f]{32}$")
_FIELD_MAX = 200


class InboxError(ValueError):
    """A request to the inbox was malformed (400)."""


class IdeaNotFound(LookupError):
    """No idea by that id -- never sent, dropped, or the service restarted (404)."""


class IdeaConflict(RuntimeError):
    """The idea is not in a state that allows this (409)."""


@dataclass
class Idea:
    id: str
    text: str
    source: str
    key: str
    created_at: float
    #: Where the idea came from, for the reply path. Opaque to this module:
    #: the Slack bridge stores ``{"channel": ..., "thread_ts": ...}``.
    reply_to: dict[str, str] = field(default_factory=dict)
    user: str = ""
    state: str = "pending"
    claimant: str = ""
    session: str = ""
    detail: str = ""
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "source": self.source,
            "user": self.user,
            "reply_to": dict(self.reply_to),
            "state": self.state,
            "claimant": self.claimant,
            "session": self.session,
            "detail": self.detail,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _short(payload: dict[str, Any], name: str, *, required: bool = False) -> str:
    value = payload.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InboxError(f"'{name}' must be a string")
    value = value.strip()
    if required and not value:
        raise InboxError(f"'{name}' is required")
    if len(value) > _FIELD_MAX:
        raise InboxError(f"'{name}' is longer than {_FIELD_MAX} characters")
    return value


class Inbox:
    """Thread-safe, bounded, in-memory. ``clock`` is the test seam."""

    def __init__(self, *, clock: Any = time.time, limit: int = MAX_IDEAS):
        self._clock = clock
        self._limit = limit
        self._lock = threading.Lock()
        self._ideas: OrderedDict[str, Idea] = OrderedDict()
        self._keys: dict[str, str] = {}

    # -- lifecycle ------------------------------------------------------

    def _age(self, now: float) -> None:
        """Apply both timeouts. Called under the lock by every read and write,
        so a state is never reported that time has already overtaken."""
        for idea in self._ideas.values():
            if idea.state == "pending" and now - idea.created_at > PENDING_TTL_S:
                idea.state = "expired"
                idea.detail = (
                    f"no Ada desktop accepted it within {PENDING_TTL_S // 60} minutes"
                )
                idea.updated_at = now
            elif idea.state == "accepted" and now - idea.updated_at > ACCEPT_TTL_S:
                idea.state = "failed"
                idea.detail = (
                    "the desktop accepted it but never reported starting a run"
                )
                idea.updated_at = now

    def _trim(self) -> None:
        while len(self._ideas) > self._limit:
            # Drop the oldest *settled* idea; if none is settled, the oldest.
            victim = next(
                (i for i, idea in self._ideas.items() if idea.state in _SETTLED),
                next(iter(self._ideas)),
            )
            dropped = self._ideas.pop(victim)
            self._keys.pop(dropped.key, None)

    def _get(self, idea_id: str) -> Idea:
        idea = self._ideas.get(idea_id)
        if idea is None:
            raise IdeaNotFound(
                f"no idea {idea_id!r} (never sent, dropped, or the service restarted)"
            )
        return idea

    def add(
        self,
        text: str,
        *,
        source: str,
        key: str = "",
        reply_to: dict[str, str] | None = None,
        user: str = "",
    ) -> tuple[Idea, bool]:
        """Store an idea. Returns ``(idea, created)``; ``created`` is False
        when ``key`` names one already stored (a redelivery)."""
        text = text.strip()
        if not text:
            raise InboxError("'text' is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise InboxError(f"'text' is longer than {MAX_TEXT_CHARS} characters")
        with self._lock:
            now = self._clock()
            self._age(now)
            if key and key in self._keys and self._keys[key] in self._ideas:
                return self._ideas[self._keys[key]], False
            idea = Idea(
                id=f"idea_{uuid.uuid4().hex}",
                text=text,
                source=source,
                key=key,
                created_at=now,
                updated_at=now,
                reply_to=dict(reply_to or {}),
                user=user,
            )
            self._ideas[idea.id] = idea
            if key:
                self._keys[key] = idea.id
            self._trim()
            return idea, True

    def list(self) -> list[Idea]:
        with self._lock:
            self._age(self._clock())
            return list(self._ideas.values())

    def get(self, idea_id: str) -> Idea:
        with self._lock:
            self._age(self._clock())
            return self._get(idea_id)

    def accept(self, idea_id: str, claimant: str) -> Idea:
        with self._lock:
            now = self._clock()
            self._age(now)
            idea = self._get(idea_id)
            if idea.state == "accepted" and idea.claimant == claimant:
                return idea  # the same desktop asking twice is not a conflict
            if idea.state != "pending":
                raise IdeaConflict(
                    f"idea is {idea.state}"
                    + (f" by {idea.claimant}" if idea.claimant else "")
                    + "; only a pending idea can be accepted"
                )
            idea.state, idea.claimant, idea.updated_at = "accepted", claimant, now
            return idea

    def start(self, idea_id: str, claimant: str, session: str) -> Idea:
        with self._lock:
            now = self._clock()
            self._age(now)
            idea = self._get(idea_id)
            if idea.state == "started" and idea.session == session:
                return idea
            self._require_owner(idea, claimant, "start")
            idea.state, idea.session, idea.updated_at = "started", session, now
            return idea

    def fail(self, idea_id: str, claimant: str, detail: str) -> Idea:
        with self._lock:
            now = self._clock()
            self._age(now)
            idea = self._get(idea_id)
            self._require_owner(idea, claimant, "fail")
            idea.state, idea.detail, idea.updated_at = "failed", detail, now
            return idea

    @staticmethod
    def _require_owner(idea: Idea, claimant: str, verb: str) -> None:
        if idea.state != "accepted":
            raise IdeaConflict(
                f"idea is {idea.state}; only an accepted idea can be {verb}ed"
            )
        if idea.claimant != claimant:
            raise IdeaConflict(f"idea was accepted by {idea.claimant}, not {claimant}")


#: The service's one inbox. Replaced wholesale by tests.
INBOX = Inbox()


# ---------------------------------------------------------------- routing


def is_inbox_path(route: str) -> bool:
    return route == "/inbox" or route.startswith("/inbox/")


def _parts(route: str) -> list[str]:
    parts = [p for p in route.split("?")[0].split("/") if p]
    if not parts or parts[0] != "inbox":
        raise IdeaNotFound(f"no route {route}")
    if len(parts) >= 2 and not _ID.match(parts[1]):
        raise IdeaNotFound(f"no idea {parts[1]!r}")
    return parts


def handle_get(route: str, inbox: Inbox | None = None) -> tuple[int, dict[str, Any]]:
    """``GET /inbox`` (pending ideas, oldest first) and ``GET /inbox/<id>``.

    ``GET /inbox`` lists only what a desktop could accept, oldest first, so
    the overlay's poll is "take the head of the list" and never has to filter
    out a run another desktop already started.
    """
    inbox = inbox or INBOX
    try:
        parts = _parts(route)
        if len(parts) == 1:
            pending = [i.as_dict() for i in inbox.list() if i.state == "pending"]
            return 200, {"ideas": pending}
        if len(parts) == 2:
            return 200, inbox.get(parts[1]).as_dict()
        raise IdeaNotFound(f"no route {route}")
    except IdeaNotFound as exc:
        return 404, {"error": str(exc)}


def handle_post(
    route: str, payload: dict[str, Any], inbox: Inbox | None = None
) -> tuple[int, dict[str, Any]]:
    """``POST /inbox``, ``/inbox/<id>/accept``, ``/start``, ``/fail``."""
    inbox = inbox or INBOX
    try:
        parts = _parts(route)
        if len(parts) == 1:
            text = payload.get("text")
            if not isinstance(text, str):
                raise InboxError("'text' must be a string")
            reply_to = payload.get("reply_to") or {}
            if (
                not isinstance(reply_to, dict)
                or not all(
                    isinstance(k, str) and isinstance(v, str) and len(v) <= _FIELD_MAX
                    for k, v in reply_to.items()
                )
                or len(reply_to) > 8
            ):
                raise InboxError("'reply_to' must be a small object of strings")
            idea, created = inbox.add(
                text,
                source=_short(payload, "source", required=True),
                key=_short(payload, "key"),
                reply_to=reply_to,
                user=_short(payload, "user"),
            )
            return (201 if created else 200), idea.as_dict()
        if len(parts) != 3:
            raise IdeaNotFound(f"no route {route}")
        idea_id, verb = parts[1], parts[2]
        claimant = _short(payload, "claimant", required=True)
        if verb == "accept":
            return 200, inbox.accept(idea_id, claimant).as_dict()
        if verb == "start":
            session = _short(payload, "session", required=True)
            return 200, inbox.start(idea_id, claimant, session).as_dict()
        if verb == "fail":
            detail = _short(payload, "detail") or "the desktop could not start a run"
            return 200, inbox.fail(idea_id, claimant, detail).as_dict()
        raise IdeaNotFound(f"no route {route}")
    except InboxError as exc:
        return 400, {"error": str(exc)}
    except IdeaNotFound as exc:
        return 404, {"error": str(exc)}
    except IdeaConflict as exc:
        return 409, {"error": str(exc)}

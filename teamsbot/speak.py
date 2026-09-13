"""How the Teams agent answers: out loud, in the chat, or not at all.

Three speakers behind one Protocol, and the reason there are three is honesty
rather than choice. Speaking *out loud* in a Teams meeting needs a real
participant on the call with a media session — that is the policy-gated Graph
calling bot in ``teamsbot/bot/``, which is a container this repo does not build,
run or vendor. Posting into the **meeting chat** needs only an application
permission on Graph and is the one half that could plausibly be exercised live
first. Saying nothing at all is a legitimate configuration and must still be
reportable.

So:

* :class:`CallingBotSpeaker` — drives the calling bot's local control surface.
  A container that is not there is a **named failure**
  (:class:`SpeakError` with code ``bot_unreachable``), never a silent no-op.
  This is the whole point of the seam: "the agent replied" must never be
  readable as "the agent spoke out loud" when no audio ever left a speaker.
* :class:`ChatSpeaker` — ``POST /chats/{id}/messages`` through
  :mod:`teamsbot.graph`'s transport, so it inherits the exact-host allowlist
  and the refused redirects. A 3xx is refused here too: a redirect is not a
  delivered message, and following one would carry the bearer token off the
  allowlist.
* :class:`NullSpeaker` — records what would have been said, in order, and says
  it out loud to nobody. ``said`` is the record a report can print.

Every speaker carries a ``name``, and :class:`~teamsbot.runner.TeamsReport`
prints it as ``spoke_via``. A report that says ``spoke_via: "chat"`` is making
a smaller claim than one that says ``spoke_via: "calling-bot"``, and the
difference is visible without reading the configuration.

**Unverified live.** Neither speaker has been run against a live Microsoft 365
tenant from this repo. The transports are seams with recorded stand-ins, which
proves the request construction and the refusals and proves nothing about
Microsoft's live behaviour.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .config import Config
from .graph import (
    GraphClient,
    TeamsError,
    Transport,
    ensure_graph_url,
)

__all__ = [
    "AUDIBLE_SPEAKERS",
    "BotTransport",
    "CallingBotSpeaker",
    "ChatSpeaker",
    "DEFAULT_CONTROL_URL",
    "MAX_MESSAGE_CHARS",
    "NullSpeaker",
    "SpeakError",
    "Speaker",
    "Utterance",
    "UrllibBotTransport",
    "ensure_control_url",
    "is_audible",
    "speaker_for",
]

log = logging.getLogger("teamsbot.speak")

#: Where the calling bot's control surface listens by default. Loopback,
#: because that container is a sidecar: it holds the bot's media session and
#: its credentials, and its control API has no authentication of its own. See
#: ``teamsbot/bot/README.md``.
DEFAULT_CONTROL_URL = "http://127.0.0.1:8791"

#: Longest utterance sent anywhere. Graph's chatMessage limit is far larger,
#: but a meeting answer that runs past this is a document, not a remark — and
#: text-to-speech reading four thousand words into a live call is a hazard, not
#: a feature. Longer text is refused rather than truncated: a silently
#: half-sent answer is worse than a stated refusal.
MAX_MESSAGE_CHARS = 3800

#: The only hosts a calling-bot control call may address over plaintext. The
#: control surface is unauthenticated, so plaintext is acceptable exactly as
#: far as the loopback interface and no further.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Speaker names whose output is **audible in the meeting**, as opposed to
#: merely delivered. The counterpart of ``zoombot.speak.AUDIBLE_SPEAKERS``,
#: added on 2026-09-08 when the two packages were compared: zoombot had this
#: whitelist and teamsbot did not, so on this side the only thing separating
#: "posted a message in the chat" from "spoke out loud in the room" was a
#: reader recognising the string ``"chat"`` in a report and knowing what it
#: implied.
#:
#: A **whitelist**, not a flag on the speaker, for the reason zoombot gives:
#: audibility is decided here, from the name, and never from anything a
#: speaker claims about itself. A new speaker is inaudible until someone adds
#: it to this line deliberately.
#:
#: ``calling-bot`` is the only member, and today no calling bot exists — the
#: container in ``teamsbot/bot/`` is a control plane whose ``POST /say``
#: answers 501. So in this tree nothing is audible, and
#: :func:`is_audible` returning ``False`` everywhere is the correct answer
#: rather than a bug.
AUDIBLE_SPEAKERS = frozenset({"calling-bot"})


class SpeakError(RuntimeError):
    """Saying something failed, or was refused before it was tried.

    ``code`` is the specific reason — ``bot_unreachable``, ``bad_host``,
    ``redirect_refused``, ``graph_error``, ``too_long`` — because "the bot said
    nothing" has half a dozen causes and a report that cannot tell them apart
    is a report nobody can act on.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@runtime_checkable
class Speaker(Protocol):
    """The frozen seam. ``name`` is what a report prints as ``spoke_via``."""

    name: str

    def say(self, meeting_id: str, text: str) -> None:
        """Deliver ``text`` to ``meeting_id``, or raise :class:`SpeakError`."""
        ...


def is_audible(speaker_name: str) -> bool:
    """Did output under this speaker name make a sound in the meeting?

    Read the way ``zoombot.speak.is_audible`` is: off :data:`AUDIBLE_SPEAKERS`,
    never off the speaker object, so a speaker cannot promote itself. Pass a
    report's ``spoke_via`` — which is set only after a delivery returned — and
    the answer is about what the room heard, not about what was configured.
    """
    return speaker_name in AUDIBLE_SPEAKERS


def _checked(text: str) -> str:
    body = str(text or "").strip()
    if not body:
        raise SpeakError("empty", "refusing to say nothing at all")
    if len(body) > MAX_MESSAGE_CHARS:
        raise SpeakError(
            "too_long",
            f"{len(body)} characters is past the {MAX_MESSAGE_CHARS}-character "
            f"limit; summarise it rather than sending half",
        )
    return body


# -- the calling bot -------------------------------------------------------


class BotTransport(Protocol):
    """One POST to the calling bot's local control surface.

    Separate from :class:`teamsbot.graph.Transport` because it addresses a
    different thing under different rules: a loopback sidecar, not Microsoft.
    Returns ``(status, body)`` rather than raising on an HTTP status, so the
    speaker can name a 4xx from the container as distinctly as a refused
    connection.
    """

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, bytes]: ...


def ensure_control_url(url: str) -> str:
    """Return ``url`` unchanged if the calling bot may be addressed there.

    https anywhere; plaintext http only on loopback. The control surface is
    unauthenticated by design (it is a sidecar in the same pod), so a plaintext
    control call to another host would hand anyone on the path the ability to
    make the bot speak in a meeting.

    Raises:
        SpeakError: ``bad_host`` for anything else. The refusal happens at
            request-construction time, so it holds for every transport
            including the fakes.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS:
        return url
    raise SpeakError(
        "bad_host",
        f"refusing to address the calling bot at {url!r}: use https, or http "
        f"on loopback ({', '.join(sorted(LOOPBACK_HOSTS))})",
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, for the reason ``graph.py`` refuses them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class UrllibBotTransport:
    """The real control transport. Standard library, redirects refused."""

    def __init__(self, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self.opener = urllib.request.build_opener(_NoRedirect())

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, bytes]:
        ensure_control_url(url)
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                return response.status, response.read(1 << 20)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(1 << 20)
        except urllib.error.URLError as exc:
            # The container is not running, or not listening yet. Named, not
            # swallowed: this is the failure mode that would otherwise let a
            # report claim the agent spoke in a meeting it never joined.
            raise SpeakError(
                "bot_unreachable",
                f"the calling bot at {url} did not answer: {exc.reason}",
            ) from exc


class CallingBotSpeaker:
    """Speaks out loud, by asking the calling bot in ``teamsbot/bot/`` to.

    The container holds the call: it is the participant Teams sees, it owns the
    media session, and it is the only thing in this design that can put audio
    into a meeting. This class does one HTTP POST to its control surface and
    reports exactly what came back.

    **It is not this repo's container.** ``teamsbot/bot/`` states which app
    registration, permissions and tenant policy an operator needs and what is
    not vendored; nothing in the test suite builds it or runs it, and it has
    never been run against a live tenant from here. A missing container is
    therefore the *expected* state, and it produces ``bot_unreachable`` —
    a named failure that a report prints — rather than a quiet success.
    """

    name = "calling-bot"

    def __init__(
        self,
        control_url: str = DEFAULT_CONTROL_URL,
        *,
        transport: BotTransport | None = None,
    ):
        self.control_url = ensure_control_url(str(control_url).rstrip("/"))
        self.transport = transport or UrllibBotTransport()

    def say(self, meeting_id: str, text: str) -> None:
        body = _checked(text)
        url = ensure_control_url(f"{self.control_url}/say")
        payload = json.dumps({"meeting_id": str(meeting_id), "text": body}).encode(
            "utf-8"
        )
        status, raw = self.transport.post(
            url, {"Content-Type": "application/json; charset=utf-8"}, payload
        )
        if 300 <= status < 400:
            raise SpeakError(
                "redirect_refused",
                f"the calling bot answered {status}; a redirect is not a "
                f"delivered utterance",
            )
        if status >= 400 or status < 200:
            raise SpeakError(
                "bot_refused",
                f"the calling bot answered {status}: {_snippet(raw)}",
            )
        # A 2xx is not evidence that a room heard anything. The control surface
        # answers `{"ok": true, "said": true}` only when audio actually left,
        # and the stub in `teamsbot/bot/control.py` answers `{"said": false}`
        # alongside its 501. Checking only the *status* meant a container that
        # answered `200 {"said": false}` — a media backend that failed, a
        # future stub that got polite — would put `spoke_via: "calling-bot"` on
        # a report for a meeting nobody spoke in. That is the single misreading
        # this whole seam exists to prevent, and `zoombot/speak.py`'s
        # `MeetingSdkSpeaker.say` has always required the same affirmative
        # (`payload["spoken"]`). An unreadable or absent field is a refusal:
        # silence must be claimed explicitly, never inferred.
        if not _said(raw):
            raise SpeakError(
                "bot_refused",
                f"the calling bot answered {status} but did not report the "
                f"line spoken: {_snippet(raw)}",
            )


# -- the meeting chat ------------------------------------------------------


class ChatSpeaker:
    """Posts into the meeting chat: ``POST /chats/{id}/messages``.

    **UNVERIFIABLE WITHOUT A TENANT, and deliberately unvalidated.** The
    ``19:meeting_…@thread.v2`` form below is documentation-derived; no public
    schema states it and no fetchable document was found that does, so unlike
    the issuer and key-host constants in ``app.py`` it could not be checked on
    2026-09-08. Nothing here pattern-matches it, and that is the safer
    choice in both directions: a regex written from a guess would refuse
    *valid* ids the moment Microsoft used a shape nobody wrote down, and this
    string is not a security boundary — :func:`message_url` puts the finished
    URL through :func:`teamsbot.graph.ensure_graph_url`, so a malformed id can
    only ever produce a wrong path on an allowlisted host. A wrong id is
    therefore a Graph 404, which arrives as ``TeamsError`` and leaves as
    ``SpeakError("graph_error", …)`` carrying Graph's own message, and
    ``spoke_via`` stays ``"none"``. Loud, named, and never mistaken for a
    message that was delivered.

    ``meeting_id`` here is the **chat thread id** — ``19:meeting_…@thread.v2``
    — which is what Graph addresses a meeting chat by, and what the calling
    notification's ``chatInfo.threadId`` carries. Stated because passing an
    ``onlineMeeting`` id instead produces a 404 whose message does not explain
    itself.

    The URL is built here and put through :func:`teamsbot.graph.ensure_graph_url`
    before a transport sees it, so an off-allowlist ``graph_base`` is refused
    whether or not the request would ever have left the machine. A 3xx is
    refused rather than followed, for the same reason ``graph.py`` refuses one:
    it would carry the bearer token to a host outside the allowlist, and it is
    not evidence that anything was posted.

    The message is sent as ``contentType: text``. Teams renders a subset of
    HTML for ``html`` content, and a board summary contains net names and
    angle-bracketed part values that would then need escaping; plain text
    cannot be mis-rendered and cannot inject markup into somebody's chat.
    """

    name = "chat"

    def __init__(
        self,
        config: Config,
        *,
        transport: Transport | None = None,
        client: Any = None,
    ):
        self.config = config
        # The client owns the client-credentials token; the transport is the
        # network seam it and this class share, so a test that records one
        # records both.
        self.client = client or GraphClient(config, transport)
        self.transport = transport or getattr(self.client, "transport", None)
        if self.transport is None:  # pragma: no cover - defensive
            raise SpeakError("no_transport", "ChatSpeaker needs a transport")

    def message_url(self, chat_id: str) -> str:
        """The Graph URL this speaker would post to. Allowlist-checked."""
        chat = str(chat_id).strip()
        if not chat:
            raise SpeakError("no_chat", "no chat thread id to post into")
        base = self.config.graph_base.rstrip("/")
        chat_path = urllib.parse.quote(chat, safe="")
        return ensure_graph_url(f"{base}/chats/{chat_path}/messages")

    def say(self, meeting_id: str, text: str) -> None:
        body = _checked(text)
        try:
            url = self.message_url(meeting_id)
            token = self.client.access_token()
        except TeamsError as exc:
            # An off-allowlist host, an unreachable login endpoint, a refused
            # client-credentials grant: each is a different sentence, and each
            # is a failure to speak rather than a silence to be ignored.
            raise SpeakError("graph_error", str(exc)) from exc

        payload = json.dumps({"body": {"contentType": "text", "content": body}}).encode(
            "utf-8"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            status, raw = self.transport.post(url, headers, payload)
        except TeamsError as exc:
            raise SpeakError("graph_error", str(exc)) from exc

        if 300 <= status < 400:
            raise SpeakError(
                "redirect_refused",
                f"Graph answered {status} with a redirect; it is not followed "
                f"(a 3xx would carry the bearer token off the allowlist) and it "
                f"is not evidence the message was posted",
            )
        if status not in (200, 201):
            raise SpeakError("graph_error", f"HTTP {status}: {_graph_reason(raw)}")


def _graph_reason(raw: bytes) -> str:
    """Graph's own ``error.message``, or a bounded snippet of whatever came."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return _snippet(raw)
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return f"{error.get('code', 'error')}: {error['message']}"
    return _snippet(raw)


def _said(raw: bytes) -> bool:
    """Did the calling bot affirmatively report the line spoken?

    ``False`` for anything that is not an explicit ``true``: a body that is not
    JSON, not an object, or carries no ``said`` field. The default has to be
    "no" — this answers "was the agent heard in the room", and a body this code
    cannot read is not evidence that it was.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("said") is True


def _snippet(raw: bytes, limit: int = 200) -> str:
    try:
        text = raw.decode("utf-8", "replace")
    except AttributeError:
        text = str(raw)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


# -- saying nothing --------------------------------------------------------


@dataclass(frozen=True)
class Utterance:
    """One thing that would have been said, and where."""

    meeting_id: str
    text: str


@dataclass
class NullSpeaker:
    """Says nothing, and keeps the receipts.

    Not a stub and not a test double: ``TEAMS_SPEAK_MODE=off`` is a real
    configuration — a tenant that has not granted the bot a chat permission, a
    dry run, a first deployment somebody wants to watch before it talks in
    front of colleagues. ``said`` makes that state auditable, which is the
    difference between "the agent chose not to speak" and "the agent broke".
    """

    # "null", not "none": the runner uses "none" as its sentinel for
    # "nothing was said at all", and one string cannot carry both "the
    # operator switched speaking off" and "delivery failed". zoombot's
    # NullSpeaker is named the same way.
    name: str = "null"
    said: list[Utterance] = field(default_factory=list)

    def say(self, meeting_id: str, text: str) -> None:
        utterance = Utterance(str(meeting_id), _checked(text))
        self.said.append(utterance)
        log.info(
            "speak_mode=off; not said in %s: %s", utterance.meeting_id, utterance.text
        )


# -- selection -------------------------------------------------------------


def speaker_for(
    config: Config,
    *,
    transport: Transport | None = None,
    control_url: str = DEFAULT_CONTROL_URL,
    bot_transport: BotTransport | None = None,
) -> Speaker:
    """The speaker ``config.speak_mode`` asks for.

    ``sdk`` -> :class:`CallingBotSpeaker`, ``chat`` -> :class:`ChatSpeaker`,
    ``off`` -> :class:`NullSpeaker`. There is no fallback: an unknown mode
    raises, because a bot that silently degrades from "speaks in the meeting"
    to "says nothing" looks exactly like a bot that is working.
    ``teamsbot.config`` validates the mode at construction, so this raise is
    reachable only for a hand-built Config.

    ``transport`` (Graph) and ``bot_transport`` (calling bot) are the two
    network seams; a test passes recorded ones and never touches a socket.
    """
    mode = str(getattr(config, "speak_mode", "")).lower()
    if mode == "sdk":
        return CallingBotSpeaker(control_url, transport=bot_transport)
    if mode == "chat":
        return ChatSpeaker(config, transport=transport)
    if mode == "off":
        return NullSpeaker()
    raise SpeakError(
        "bad_speak_mode",
        f"unknown speak mode {mode!r}; there is no default, because an agent "
        f"that quietly stops speaking looks like an agent that is working",
    )

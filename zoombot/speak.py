"""Getting a word out of the agent and into a Zoom meeting -- and saying which
way it went.

**The property this module exists for.** There are three completely different
things a hardware agent can do when it has something to tell a room, and a
report that blurs them is a lie:

* it can *speak out loud* as a participant everyone hears (``MeetingSdkSpeaker``),
* it can *post text* into the meeting chat, which nobody may look at
  (``ChatSpeaker``),
* it can do *nothing at all* and record what it would have said
  (``NullSpeaker``).

So every speaker carries a :attr:`Speaker.name`, ``runner.py`` records that
name in :class:`~zoombot.runner.ZoomReport.spoke_via`, and
:data:`AUDIBLE_SPEAKERS` is the only place that says which of those names means
audio actually left a speaker. Without this, "the agent replied" reads as "the
agent spoke out loud" when in fact it dropped a line of text into a chat panel
that scrolled past. :func:`is_audible` is deliberately a whitelist of names
rather than a flag on the object, because a flag is something a future speaker
can set optimistically about itself.

**RTMS is receive-only.** Zoom's Realtime Media Streams deliver a meeting's
audio and transcript *to* you; there is no REST call that makes noise in a
live meeting. The only supported way to emit audio is to be a participant, so
``MeetingSdkSpeaker`` does not talk to Zoom at all -- it drives the headless
Zoom Meeting SDK container in ``zoombot/bot/``, which joins as its own
attendee. That container is modelled on Zoom's open-source
``zoom/meetingsdk-headless-linux-sample``; it is **not built or run by the test
suite and has never been run against a live Zoom account from this repo**
(``zoombot/bot/README.md`` says so at length). ``ChatSpeaker`` is the one path
that is plausibly exercisable live first, because it is an ordinary REST call.

Nothing here degrades quietly. A container that is not listening, a Zoom API
that answers 401, a redirect, a host that is not Zoom -- each raises
:class:`SpeakError` with a distinct ``code``. A speaker that swallowed a
failure would let a run report a delivered message that never arrived, which is
the same bug class as a measurement that returns 0.0 instead of raising.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the runtime import-free
    from collections.abc import Mapping

    from .config import Config

__all__ = [
    "AUDIBLE_SPEAKERS",
    "ALLOWED_HOSTS",
    "BOT_CONTROL_URL",
    "ChatSpeaker",
    "MeetingSdkSpeaker",
    "NullSpeaker",
    "SpeakError",
    "Speaker",
    "Transport",
    "Voice",
    "VoiceUnavailable",
    "ensure_zoom_url",
    "is_audible",
    "loopback_transport",
    "speaker_for",
    "zoom_transport",
]

#: The only hosts a Zoom call may address. Exact matches -- a suffix test would
#: wave through ``api.zoom.us.evil.example``. ``zoom.us`` is here because the
#: Server-to-Server OAuth token endpoint lives there, not under ``api.``.
ALLOWED_HOSTS = frozenset({"api.zoom.us", "zoom.us"})

#: Where the headless container in ``bot/`` listens by default. Loopback on
#: purpose: the control surface has no authentication, so it must never be
#: reachable from off the machine (``bot/README.md`` repeats this).
BOT_CONTROL_URL = "http://127.0.0.1:8781"

#: Speaker names whose output is *audible in the meeting*. The whitelist is the
#: contract: anything not named here produced text, or produced nothing.
AUDIBLE_SPEAKERS = frozenset({"meeting_sdk"})


class SpeakError(RuntimeError):
    """Saying something failed, or was refused before it was attempted.

    ``code`` is a stable, greppable reason -- ``bot_unreachable``,
    ``bad_host``, ``redirect_refused``, ``zoom_api``, ``no_voice``,
    ``bad_speak_mode`` -- so a caller can tell "the container is not running"
    apart from "Zoom said no" apart from "we never tried".
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class VoiceUnavailable(SpeakError):
    """Text-to-speech was asked for and none is configured."""

    def __init__(self, detail: str = "no Voice configured"):
        super().__init__("no_voice", detail)


@runtime_checkable
class Speaker(Protocol):
    """Frozen contract (``docs/integrations-plan.md``, Contract 3).

    ``name`` is not decoration. It is what the run report shows, and
    :func:`is_audible` reads it to decide whether the agent was heard.
    """

    name: str

    def say(self, meeting_id: str, text: str) -> None: ...


class Transport(Protocol):
    """One HTTP round trip. Same shape as ``zoombot.rtms.Transport`` so a
    caller can hand either one the same object; kept structural here so
    ``speak.py`` imports nothing at runtime from a module it does not own."""

    def get(self, url: str, headers: Mapping[str, str]) -> bytes: ...

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> bytes: ...


class Voice(Protocol):
    """Text in, audio bytes out -- the text-to-speech seam.

    Deliberately a callable rather than a vendor: the container in ``bot/``
    runs on whatever machine the operator has, and hard-wiring a cloud TTS key
    into a package that otherwise needs none is a cost and a secret nobody
    asked for. The documented production choice is **Piper**
    (``rhasspy/piper``, MIT, actively maintained), which runs offline on CPU
    and writes 16-bit mono PCM WAV -- exactly what the Meeting SDK's raw audio
    sender wants. ``bot/README.md`` shows the wiring; nothing here downloads a
    model or assumes one exists.

    Returns WAV bytes (RIFF header included). A voice that cannot synthesise
    raises rather than returning ``b""``: an empty buffer is silence, and
    silence is indistinguishable from success.
    """

    def __call__(self, text: str) -> bytes: ...


def is_audible(speaker_name: str) -> bool:
    """Did output under this speaker name actually make a sound in the room?

    The one question a reader of a run report is really asking, answered from
    :data:`AUDIBLE_SPEAKERS` rather than from anything a speaker claims about
    itself.
    """
    return speaker_name in AUDIBLE_SPEAKERS


def ensure_zoom_url(url: str) -> str:
    """Return ``url`` unchanged if it is https to an allowlisted Zoom host.

    Raises:
        SpeakError: ``bad_host`` for any other scheme or host. The refusal
            happens at request construction, before any transport -- including
            a fake one in a test -- sees it, so no OAuth token can leave toward
            a host this package does not know. ``googleapps/transport.py``
            closes the same hole the same way and for the same reason.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise SpeakError("bad_host", f"refusing non-https URL scheme {parsed.scheme!r}")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise SpeakError(
            "bad_host",
            f"refusing to send to {parsed.hostname!r}: not a known Zoom API host",
        )
    return url


def _ensure_loopback_url(url: str) -> str:
    """The container's control surface is unauthenticated, so it is loopback
    only. A ``bot_control_url`` pointing anywhere else is a configuration
    mistake worth refusing loudly rather than a URL worth trying."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http":
        raise SpeakError("bad_host", f"bot control URL must be http, got {url!r}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SpeakError(
            "bad_host",
            f"refusing to drive a bot at {parsed.hostname!r}: "
            "the control surface has no authentication and must stay on loopback",
        )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    ``urllib`` would otherwise follow a 3xx and re-send the request -- with its
    ``Authorization`` header -- to whatever host the redirect names, which is
    precisely the hole the allowlist exists to close. Returning ``None`` makes
    ``urlopen`` raise the 3xx as an ``HTTPError``, which the transport turns
    into a named ``redirect_refused`` failure instead of following it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _urllib_send(
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    method: str,
    timeout: float,
    *,
    unreachable_code: str,
) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:512].decode("utf-8", "replace")
        if 300 <= exc.code < 400:
            raise SpeakError(
                "redirect_refused",
                f"HTTP {exc.code} redirect from {url} was not followed",
            ) from exc
        raise SpeakError("http_error", f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SpeakError(unreachable_code, f"{url}: {exc.reason}") from exc


def zoom_transport(timeout: float = 20.0) -> Transport:
    """The production transport for Zoom REST calls: stdlib, allowlisted,
    redirect-refusing. Injectable so the suite never reaches the network."""

    class _ZoomTransport:
        def get(self, url: str, headers: Mapping[str, str]) -> bytes:
            return _urllib_send(
                ensure_zoom_url(url),
                headers,
                None,
                "GET",
                timeout,
                unreachable_code="network_error",
            )

        def post(self, url: str, headers: Mapping[str, str], body: bytes) -> bytes:
            return _urllib_send(
                ensure_zoom_url(url),
                headers,
                body,
                "POST",
                timeout,
                unreachable_code="network_error",
            )

    return _ZoomTransport()


def loopback_transport(timeout: float = 5.0) -> Transport:
    """The production transport for the ``bot/`` container's control surface.

    Separate from :func:`zoom_transport` because it has the opposite
    allowlist -- loopback only, never Zoom -- and because a container that is
    not running must surface as ``bot_unreachable``, a state a run report
    names, rather than as a generic network error.
    """

    class _LoopbackTransport:
        def get(self, url: str, headers: Mapping[str, str]) -> bytes:
            return _urllib_send(
                _ensure_loopback_url(url),
                headers,
                None,
                "GET",
                timeout,
                unreachable_code="bot_unreachable",
            )

        def post(self, url: str, headers: Mapping[str, str], body: bytes) -> bytes:
            return _urllib_send(
                _ensure_loopback_url(url),
                headers,
                body,
                "POST",
                timeout,
                unreachable_code="bot_unreachable",
            )

    return _LoopbackTransport()


def _decode_json(raw: bytes, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpeakError("bad_response", f"{what} was not JSON") from exc
    if not isinstance(payload, dict):
        raise SpeakError("bad_response", f"{what} was not a JSON object")
    return payload


@dataclass
class NullSpeaker:
    """Says nothing, and is honest about it.

    Used by ``speak_mode="off"`` and by every test that wants to assert on what
    the agent *would* have said. It records rather than discards, because a
    speaker that dropped its input on the floor would make an off run look
    identical to a run where the agent had nothing to say.
    """

    name: str = "null"
    #: ``(meeting_id, text)`` in the order it was offered.
    said: list[tuple[str, str]] = field(default_factory=list)

    def say(self, meeting_id: str, text: str) -> None:
        if not text or not text.strip():
            raise SpeakError("empty_text", "refusing to record an empty utterance")
        self.said.append((meeting_id, text))


@dataclass
class MeetingSdkSpeaker:
    """Speaks out loud, by driving the headless participant in ``zoombot/bot/``.

    This is the only speaker whose output anyone in the room can hear, and the
    only name in :data:`AUDIBLE_SPEAKERS`. It talks to a tiny local HTTP
    control surface -- ``GET /healthz``, ``POST /say`` with
    ``{"meeting_id", "text"}``, ``POST /audio`` with WAV bytes -- rather than
    embedding the Zoom SDK, because that SDK is a Linux C++ binary downloaded
    from Zoom under their licence and cannot be vendored or imported here.

    **It degrades loudly.** If the container is not running, ``say`` raises
    :class:`SpeakError` with code ``bot_unreachable``. It never returns
    quietly: a silent no-op here would put "spoke_via: meeting_sdk" on a report
    for a meeting in which the agent made no sound at all, which is the exact
    misreading this module is built to prevent.

    If a :class:`Voice` is supplied, synthesis happens on this side and WAV
    bytes are posted; otherwise the text goes over and the container uses
    whatever voice the operator configured in it. Both halves are seams so that
    neither this package nor its tests need a TTS engine installed.
    """

    control_url: str = BOT_CONTROL_URL
    transport: Transport | None = None
    voice: Voice | None = None
    name: str = "meeting_sdk"

    def _transport(self) -> Transport:
        if self.transport is None:
            self.transport = loopback_transport()
        return self.transport

    def _url(self, path: str) -> str:
        return _ensure_loopback_url(self.control_url.rstrip("/") + path)

    def health(self) -> dict[str, Any]:
        """Ask the container whether it is in a meeting.

        Raises ``bot_unreachable`` if nothing is listening -- the honest answer
        to "can this agent speak?" before anything is said.
        """
        raw = self._transport().get(
            self._url("/healthz"), {"Accept": "application/json"}
        )
        return _decode_json(raw, "bot /healthz")

    def say(self, meeting_id: str, text: str) -> None:
        if not text or not text.strip():
            raise SpeakError("empty_text", "refusing to speak an empty utterance")
        transport = self._transport()
        if self.voice is not None:
            audio = self.voice(text)
            if not audio:
                raise VoiceUnavailable(
                    "the configured Voice returned no audio; "
                    "silence is not a successful utterance"
                )
            raw = transport.post(
                self._url("/audio"),
                {
                    "Content-Type": "audio/wav",
                    "X-Zoom-Meeting-Id": meeting_id,
                },
                audio,
            )
        else:
            body = json.dumps({"meeting_id": meeting_id, "text": text}).encode("utf-8")
            raw = transport.post(
                self._url("/say"),
                {"Content-Type": "application/json"},
                body,
            )
        payload = _decode_json(raw, "bot /say")
        if not payload.get("spoken"):
            raise SpeakError(
                "bot_refused",
                str(payload.get("error") or "the bot did not report the line spoken"),
            )


@dataclass
class ChatSpeaker:
    """Posts into the live meeting's chat over the Zoom REST API.

    Text, not sound: this speaker's name is **not** in
    :data:`AUDIBLE_SPEAKERS`, and a report that used it says so. It is here
    because it is the one path with no container, no media plumbing and no
    downloaded SDK -- an ordinary Server-to-Server OAuth token and one POST --
    so it is the half that could plausibly be exercised against a live account
    first.

    Endpoints used (Zoom API v2, as documented by Zoom):

    * ``POST https://zoom.us/oauth/token?grant_type=account_credentials`` with
      HTTP Basic ``client_id:client_secret`` -- the Server-to-Server OAuth
      grant, which is why ``Config`` carries an account id.
    * ``POST {api_base}/live_meetings/{meetingId}/chat/messages`` with
      ``{"message": ..., "to_channel": "everyone"}``.

    The token is cached in memory until shortly before it expires, because a
    token request per line would be both slow and rude; it is never logged, and
    never written anywhere.

    **Unverified.** This has never been run against a live Zoom account from
    this repo. It is built against the documented REST surface and tested
    against a recorded transport, the same standing as ``googleapps/``.
    """

    config: Config
    transport: Transport | None = None
    name: str = "zoom_chat"
    _token: str = ""
    _token_expiry: float = 0.0

    def _transport(self) -> Transport:
        if self.transport is None:
            self.transport = zoom_transport()
        return self.transport

    def access_token(self, *, now: float | None = None) -> str:
        """A Server-to-Server OAuth access token, minted on demand and cached.

        Raises ``zoom_auth`` when Zoom refuses -- never returns ``""``, which
        a caller would happily put in an ``Authorization`` header and get a
        confusing 401 for one call later.
        """
        clock = time.time() if now is None else now
        if self._token and clock < self._token_expiry:
            return self._token
        query = urllib.parse.urlencode(
            {
                "grant_type": "account_credentials",
                "account_id": self.config.account_id,
            }
        )
        url = ensure_zoom_url(f"https://zoom.us/oauth/token?{query}")
        basic = base64.b64encode(
            f"{self.config.client_id}:{self.config.client_secret}".encode()
        ).decode("ascii")
        raw = self._transport().post(
            url,
            {
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            b"",
        )
        payload = _decode_json(raw, "Zoom token response")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise SpeakError(
                "zoom_auth",
                str(payload.get("reason") or payload.get("error") or "no access_token"),
            )
        expires_in = payload.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
        # Refresh a minute early: a token that expires mid-flight fails the
        # call it was fetched for.
        self._token = token
        self._token_expiry = clock + max(lifetime - 60.0, 0.0)
        return token

    def say(self, meeting_id: str, text: str) -> None:
        if not text or not text.strip():
            raise SpeakError("empty_text", "refusing to post an empty chat message")
        if not meeting_id:
            raise SpeakError("no_meeting", "a chat message needs a meeting id")
        token = self.access_token()
        base = self.config.api_base.rstrip("/")
        url = ensure_zoom_url(
            f"{base}/live_meetings/{urllib.parse.quote(str(meeting_id))}/chat/messages"
        )
        body = json.dumps({"message": text, "to_channel": "everyone"}).encode("utf-8")
        raw = self._transport().post(
            url,
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            body,
        )
        if not raw.strip():
            # Zoom answers 201 with an empty body on success for some
            # endpoints; an empty body is only acceptable because the
            # transport raises on every non-2xx status.
            return
        payload = _decode_json(raw, "Zoom chat response")
        if payload.get("code") or payload.get("error"):
            raise SpeakError(
                "zoom_api",
                str(
                    payload.get("message")
                    or payload.get("error")
                    or "chat post failed"
                ),
            )


def speaker_for(config: Config, *, transport: Transport | None = None) -> Speaker:
    """The frozen factory: ``config.speak_mode`` picks the speaker.

    ``"sdk"`` -> :class:`MeetingSdkSpeaker` (audible, needs the container),
    ``"chat"`` -> :class:`ChatSpeaker` (text in the meeting chat),
    ``"off"`` -> :class:`NullSpeaker` (records, says nothing).

    An unrecognised mode raises ``bad_speak_mode`` rather than defaulting to
    something: silently falling back to :class:`NullSpeaker` would turn a typo
    in an env var into a meeting where the agent never spoke and nothing said
    why. ``config.py`` validates the mode too; this is the second lock, because
    a ``Config`` can also be built by hand.
    """
    mode = getattr(config, "speak_mode", "")
    if mode == "sdk":
        return MeetingSdkSpeaker(transport=transport)
    if mode == "chat":
        return ChatSpeaker(config=config, transport=transport)
    if mode == "off":
        return NullSpeaker()
    raise SpeakError(
        "bad_speak_mode",
        f"unknown ZOOM_SPEAK_MODE {mode!r}; expected 'sdk', 'chat' or 'off'",
    )

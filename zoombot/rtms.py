"""Zoom webhook verification and Realtime Media Streams (RTMS) ingest.

The protocol handling here follows Zoom's own open-source RTMS samples
(``github.com/zoom/rtms-samples``, MIT, the maintained upstream reference for
Realtime Media Streams) rather than anything invented for this repo: the
``endpoint.url_validation`` HMAC handshake, then a **signalling** WebSocket
that authenticates the stream, then a **media** WebSocket that delivers frames.
Where the upstream sample and a clever alternative disagree, the sample wins --
this is a wire protocol, and being clever about one is how an integration
starts failing silently six months later.

**RTMS is receive-only.** Nothing in this module can make a sound in a meeting;
it hears. Audio out needs a real participant, which is the headless Meeting SDK
container in ``zoombot/bot/`` and is a separate seam (``zoombot/speak.py``).

**Unverified live, but no longer unverified at all.** This has never been run
against a real Zoom account. Every byte of network sits behind the
:class:`Transport` and :class:`Connection` Protocol seams -- the same shape
``meetings/meet.py`` uses -- so the whole test suite runs offline against a
recorded stream.

The frame constants below used to be *documented assumptions* gathered in one
block so a first live run could correct them. On **2026-09-08** they were
instead checked, one at a time, against Zoom's own published sample source --
``github.com/zoom/rtms-samples`` (MIT, Copyright (c) 2025 Zoom Video
Communications), the maintained reference implementation -- and each constant
now carries the file and function it was read out of. That check found real
errors, and they are worth naming because they are the kind that produce a
silent failure rather than a crash:

* :data:`MSG_TYPE` was wrong from ``EVENT_SUBSCRIPTION`` onwards. The sample
  numbers ``EVENT_SUBSCRIPTION`` 5, not 9, and ``STREAM_STATE_UPDATE`` **8**,
  not 5. Under the old map a stream-state frame was read as an event
  subscription and a terminating stream was never noticed.
* :data:`STATUS_OK` was the string ``"STATUS_OK"``. The wire carries an
  **integer** ``status_code``, and success is ``0``. Every handshake would have
  been rejected as refused.
* :data:`TERMINAL_STREAM_STATES` held strings. ``state`` is an integer.
* ``meeting.rtms_started`` does **not** nest its fields under
  ``payload.object`` the way Zoom's older meeting webhooks do -- the sample
  destructures them straight off ``payload``. See
  :func:`session_from_payload`.

What is still unverifiable without a Zoom account is stated at each constant
rather than implied by silence, and in every case the code fails closed and
says which assumption it was standing on.

There is deliberately **no import-time dependency on a WebSocket library**. The
production :func:`websocket_connect` imports ``websocket`` (the
``websocket-client`` package) lazily, inside the call, and says so by name if it
is missing -- a missing optional package, a missing credential and a failed
connection are three different states and stay distinguishable.

Nothing here returns a quiet zero. A stream that was never authorised raises
:class:`HandshakeError` from :func:`open_stream`; a stream that dies mid-way
raises :class:`StreamInterruptedError` while it is being iterated; a stream that
ends cleanly having carried no transcript at all raises :class:`EmptyStreamError`
at the end of iteration. A caller can tell all three apart, and none of them can
be mistaken for "the meeting had nothing to say".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Config

__all__ = [
    "Transport",
    "UrllibTransport",
    "Connection",
    "MediaStream",
    "TranscriptChunk",
    "RtmsSession",
    "SeenEvents",
    "ZoomError",
    "WebhookError",
    "RtmsError",
    "PayloadError",
    "NotAuthorisedError",
    "HandshakeError",
    "FrameError",
    "StreamInterruptedError",
    "EmptyStreamError",
    "verify_webhook",
    "url_validation_reply",
    "open_stream",
    "websocket_connect",
    "MSG_TYPE",
    "MEDIA_TYPE_TRANSCRIPT",
    "STATUS_OK",
    "TERMINAL_STREAM_STATES",
    "TERMINAL_SESSION_STATES",
    "MAX_WEBHOOK_AGE_S",
    "MAX_BODY_BYTES",
    "MAX_FRAME_BYTES",
    "MAX_FRAMES",
    "MAX_HANDSHAKE_FRAMES",
    "ALLOWED_HOSTS",
    "ALLOWED_HOST_SUFFIXES",
]

log = logging.getLogger("zoombot.rtms")

#: Zoom signs a webhook with ``v0`` and this base string. Kept as a constant so
#: a future ``v1`` is a one-line change with a test, not a grep. **CONFIRMED**
#: against ``zoom/rtms-samples``
#: ``library/javascript/webhookManager/zoomWebhookSignature.js``, which builds
#: ```v0:${timestamp}:${req.rawBody}`` and compares against
#: ```v0=${hmacSha256Hex}`` -- see :func:`_signature`.
SIGNATURE_VERSION = "v0"

#: A signed request older than this is refused. **CONFIRMED**: the same file
#: declares ``DEFAULT_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300``, and applies
#: it two-sided (``Math.abs``) as this module does. Slack's window is the same
#: five minutes for the same reason: the HMAC proves the body was not tampered
#: with, but only the age check stops a captured, perfectly valid request from
#: being replayed later. A replayed ``meeting.rtms_started`` is a second *paid*
#: pipeline run.
MAX_WEBHOOK_AGE_S = 300.0

#: A webhook body larger than this is refused unparsed. Zoom's event bodies are
#: a few kilobytes; anything near this is a bug or an attack.
MAX_BODY_BYTES = 1 << 20

#: One media frame larger than this is refused. Transcript frames are text.
MAX_FRAME_BYTES = 1 << 20

#: Hard cap on frames read from one media stream, and on frames read while
#: waiting for a handshake response. ``meetings/meet.py`` caps paging at 50
#: pages for exactly this reason: an unbounded ``while True`` over a remote
#: server's output is one server-side bug away from spinning forever, and here
#: it would spin holding a meeting's worth of state.
MAX_FRAMES = 20_000
MAX_HANDSHAKE_FRAMES = 32

#: Wire message types. **CONFIRMED** against ``zoom/rtms-samples`` (MIT) on
#: 2026-09-08, each value read out of the sample's own code rather than a doc
#: page:
#:
#: * ``1`` ``SIGNALING_HAND_SHAKE_REQ`` --
#:   ``library/javascript/rtmsManager/signalingSocket.js`` sends
#:   ``{ msg_type: 1, ... }``.
#: * ``2`` ``SIGNALING_HAND_SHAKE_RESP`` --
#:   ``signalingSocketMessageHandler.js``: ``case 2: // SIGNALING_HAND_SHAKE_RESP``.
#: * ``3`` ``DATA_HAND_SHAKE_REQ`` -- ``mediaSocket.js``:
#:   ``msg_type: 3, // DATA_HAND_SHAKE_REQ``.
#: * ``4`` ``DATA_HAND_SHAKE_RESP`` -- ``mediaSocketMessageHandler.js``:
#:   ``case 4: // DATA_HAND_SHAKE_RESP``.
#: * ``5`` ``EVENT_SUBSCRIPTION`` -- ``signalingSocketMessageHandler.js`` builds
#:   the subscription payload as ``{ msg_type: 5, events: [...] }``.
#: * ``6`` ``EVENT_UPDATE`` -- same file, ``case 6: // Events``.
#: * ``7`` ``CLIENT_READY_ACK`` -- ``mediaSocketMessageHandler.js`` answers a
#:   successful data handshake with ``{ msg_type: 7, rtms_stream_id }``.
#: * ``8`` ``STREAM_STATE_UPDATE`` -- ``signalingSocketMessageHandler.js``:
#:   ``case 8: // Stream State changed``.
#: * ``9`` ``SESSION_STATE_UPDATE`` -- same file,
#:   ``case 9: // Session State Changed``.
#: * ``12``/``13`` keep-alive -- same file, ``case 12:`` replies
#:   ``{ msg_type: 13, timestamp: msg.timestamp }``.
#: * ``17`` ``MEDIA_DATA_TRANSCRIPT`` -- ``mediaSocketMessageHandler.js``:
#:   ``case 17: // TRANSCRIPT``.
#:
#: ``SESSION_STATE_REQ``/``RESP`` (10/11) are the two values in this table the
#: sample never sends or handles, so they are documented-order inferences from
#: the gap between 9 and 12 rather than reads. Nothing in this module uses
#: them; an unknown ``msg_type`` is ignored by the frame loop, which is the
#: right behaviour for a protocol that adds message types over time.
MSG_TYPE = {
    "SIGNALING_HAND_SHAKE_REQ": 1,
    "SIGNALING_HAND_SHAKE_RESP": 2,
    "DATA_HAND_SHAKE_REQ": 3,
    "DATA_HAND_SHAKE_RESP": 4,
    "EVENT_SUBSCRIPTION": 5,
    "EVENT_UPDATE": 6,
    "CLIENT_READY_ACK": 7,
    "STREAM_STATE_UPDATE": 8,
    "SESSION_STATE_UPDATE": 9,
    "SESSION_STATE_REQ": 10,
    "SESSION_STATE_RESP": 11,
    "KEEP_ALIVE_REQ": 12,
    "KEEP_ALIVE_RESP": 13,
    "MEDIA_DATA_AUDIO": 14,
    "MEDIA_DATA_VIDEO": 15,
    "MEDIA_DATA_SHARE": 16,
    "MEDIA_DATA_TRANSCRIPT": 17,
    "MEDIA_DATA_CHAT": 18,
}

#: The ``media_type`` selector on ``DATA_HAND_SHAKE_REQ``. **CONFIRMED**: it is
#: a **bitmask**, not an ordinal -- ``zoom/rtms-samples``
#: ``library/javascript/rtmsManager/mediaSocket.js`` declares
#: ``const TYPE_FLAGS = { audio: 1, video: 2, sharescreen: 4, transcript: 8,
#: chat: 16 };``. Transcript is ``1 << 3``. Subscribing to transcript *and*
#: audio would therefore be ``8 | 1``, which is why this is a mask and why the
#: value must never be treated as an index.
MEDIA_TYPE_TRANSCRIPT = 8

#: Success on a handshake response. **CONFIRMED as the integer 0**, not a
#: string: ``signalingSocketMessageHandler.js`` gates on
#: ``if (msg.status_code === 0)`` and ``mediaSocketMessageHandler.js`` does the
#: same on ``case 4``. ``utils/rtmsProtocolDefinitions.js`` lists further status
#: codes as integers too (``CHAT_SESSION_KEY_NOT_AVAILABLE: 47``). This was the
#: string ``"STATUS_OK"`` until 2026-09-08; against a real account every
#: handshake would have been read as a refusal.
STATUS_OK = 0

#: Stream states that mean "this stream is over, and that is normal".
#: **CONFIRMED as integers.** ``utils/rtmsEventLookupHelper.js``
#: ``getRtmsStreamState`` enumerates ``0 INACTIVE, 1 ACTIVE, 2 INTERRUPTED,
#: 3 TERMINATING, 4 TERMINATED, 5 PAUSED, 6 RESUMED``, and
#: ``signalingSocketMessageHandler.js`` treats exactly one of them as the end:
#: ``if (msg.state === 4) { ... conn.shouldReconnect = false; ... }``.
#:
#: ``3 TERMINATING`` is deliberately **not** in this set, following the sample:
#: it means the client has been *told* to terminate, and the stream may still
#: deliver frames. Treating it as the end would truncate a meeting, and a
#: truncated meeting is the failure :class:`StreamInterruptedError` exists for.
TERMINAL_STREAM_STATES = frozenset({4})

#: Session states that end a stream. Separate enum, separate numbering:
#: ``getRtmsSessionState`` gives ``0 INACTIVE, 1 INITIALIZE, 2 STARTED,
#: 3 PAUSED, 4 RESUMED, 5 STOPPED``, and the sample disables reconnect on
#: ``if (msg.state === 5 && conn)``. Note that ``4`` means RESUMED here and
#: TERMINATED there -- the two enums must never share a set, which is why this
#: is a second constant rather than four more members of the first.
TERMINAL_SESSION_STATES = frozenset({5})

#: Exact-match hosts. The REST API only ever lives here.
ALLOWED_HOSTS = frozenset({"api.zoom.us", "zoom.us"})

#: RTMS hands back a *dynamically allocated* media host -- the sample's
#: ``server_urls`` are per-stream names like ``rtms-xxxx.zoom.us`` -- so an
#: exact-match list cannot work for the stream sockets and pretending otherwise
#: would mean the allowlist is disabled in production, which is worse than a
#: narrow suffix rule. The suffix is matched against the parsed hostname, so
#: ``https://evil.com/?x=.zoom.us`` and ``https://api.zoom.us@evil.com`` do not
#: match: ``urlsplit().hostname`` is the host, not the userinfo.
ALLOWED_HOST_SUFFIXES = (".zoom.us", ".zoom.com")


# --------------------------------------------------------------------------
# Errors. Three outcomes must stay distinguishable, so they are three types.
# --------------------------------------------------------------------------


class ZoomError(RuntimeError):
    """Anything this module refuses to do."""


class WebhookError(ZoomError):
    """An inbound webhook is not provably from Zoom, or is a replay.

    ``reason`` is a short machine-readable slug (``"bad_signature"``,
    ``"stale"``, ``"replay"``, ...) so a handler can log the specific cause
    without string-matching the message, and ``status`` is the HTTP status the
    caller should answer with.
    """

    def __init__(self, message: str, *, reason: str = "invalid", status: int = 401):
        self.reason = reason
        self.status = status
        super().__init__(message)


class RtmsError(ZoomError):
    """A Realtime Media Stream could not be opened, or did not survive."""


class PayloadError(RtmsError):
    """An ``rtms_started`` payload is missing what a stream needs.

    ``problems`` carries every missing or malformed field, batched into one
    error the way ``netlist.ValidationError`` does -- one round of corrections,
    not four.
    """

    def __init__(self, problems: list[str] | tuple[str, ...]):
        self.problems = tuple(problems)
        super().__init__(
            "this meeting.rtms_started payload cannot open a stream: "
            + "; ".join(self.problems)
        )


class NotAuthorisedError(RtmsError):
    """The stream was refused before any media flowed.

    Either the meeting is outside ``ZOOM_MEETINGS``, or Zoom rejected the
    signature. Distinct from :class:`EmptyStreamError` on purpose: "we were not
    allowed to listen" and "we listened and nobody spoke" are different facts
    about a meeting and must never collapse into one.
    """


class HandshakeError(NotAuthorisedError):
    """Zoom answered a handshake with something other than ``STATUS_OK``."""


class FrameError(RtmsError):
    """A frame off the media socket was truncated, oversized or not JSON."""


class StreamInterruptedError(RtmsError):
    """The media socket closed without the stream ever ending cleanly.

    This is the failure that most wants to be a quiet zero: half a meeting
    read as a whole one produces a board from half a requirement. ``chunks``
    says how many transcript chunks did arrive before the break, so a caller
    can report what it lost.
    """

    def __init__(self, message: str, *, chunks: int = 0):
        self.chunks = chunks
        super().__init__(message)


class EmptyStreamError(RtmsError):
    """The stream ended cleanly and carried no transcript at all.

    Raised at the end of iteration rather than returned as an empty list: an
    empty list is indistinguishable from a meeting nobody spoke in, from
    transcription being switched off, and from a subscription that silently
    matched nothing. Callers that genuinely want to tolerate a silent meeting
    catch this and say so.
    """


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


class Transport(Protocol):
    """The HTTP half: one GET, one POST. Nothing else addresses the network."""

    def get(self, url: str, headers: Mapping[str, str]) -> bytes:
        ...

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> bytes:
        ...


class Connection(Protocol):
    """One WebSocket, reduced to what the RTMS handshake actually needs.

    ``recv`` returns ``None`` when the peer has closed. That is the whole
    reason this is a Protocol and not a websocket object: a recorded connection
    in a test can end a stream mid-frame, which is the interesting case.
    """

    def send(self, text: str) -> None:
        ...

    def recv(self) -> str | None:
        ...

    def close(self) -> None:
        ...


class MediaStream(Protocol):
    """An iterable of :class:`TranscriptChunk`."""

    def __iter__(self) -> Iterator[TranscriptChunk]:
        ...


@dataclass(frozen=True)
class TranscriptChunk:
    """One utterance off the stream.

    ``speaker`` is whatever identity the frame already carried: Zoom's display
    name when the frame has one, otherwise its opaque participant id. Nothing
    here resolves a name through the REST API -- that would mean holding a user
    scope this integration has no business holding, the same rule
    ``meetings/meet.py`` states for Meet's participant ids.
    """

    meeting_id: str
    speaker: str
    text: str
    at_ms: int


@dataclass(frozen=True)
class RtmsSession:
    """The three things an ``rtms_started`` event carries that matter."""

    meeting_uuid: str
    stream_id: str
    signal_url: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    The stdlib follows a 3xx by copying every header onto the new request --
    ``Authorization`` included -- and re-sending it wherever the redirect points.
    A Zoom bearer token must never travel off the allowlist, so a 3xx becomes an
    error instead (``meetings/meet.py`` and ``googleapps/transport.py`` close the
    same hole the same way).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class UrllibTransport:
    """The real HTTP transport. Standard library only, allowlisted, no redirects."""

    def __init__(self, timeout_s: float = 30.0):
        self.timeout_s = timeout_s
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get(self, url: str, headers: Mapping[str, str]) -> bytes:
        return self._open(url, headers, None)

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> bytes:
        return self._open(url, headers, body)

    def _open(self, url: str, headers: Mapping[str, str], body: bytes | None) -> bytes:
        check_url(url, schemes=("https",))
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST" if body else "GET"
        )
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                if response.status >= 300:
                    raise RtmsError(
                        f"Zoom answered {response.status} for {_safe(url)}; "
                        f"a redirect is refused because it would carry the "
                        f"bearer token off the allowlist"
                    )
                return response.read(MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            raise RtmsError(
                f"Zoom answered {exc.code} for {_safe(url)}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RtmsError(f"could not reach {_safe(url)}: {exc.reason}") from exc


def websocket_connect(url: str) -> Connection:
    """Open a real WebSocket. The only production ``connect`` implementation.

    ``websocket-client`` is imported **here**, not at module scope, so this
    package imports, tests and reports its own configuration on a machine that
    has no WebSocket library at all. A missing package says so by name rather
    than surfacing as a connection failure.
    """
    check_url(url, schemes=("wss",))
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RtmsError(
            "opening a live RTMS stream needs the 'websocket-client' package "
            "(pip install websocket-client); the package itself imports and "
            "tests without it, and open_stream(connect=...) takes any "
            "Connection"
        ) from exc

    socket = websocket.create_connection(url, timeout=30)

    class _LiveConnection:
        def send(self, text: str) -> None:
            socket.send(text)

        def recv(self) -> str | None:
            frame = socket.recv()
            if frame is None or frame == "":
                return None
            return frame if isinstance(frame, str) else frame.decode("utf-8", "replace")

        def close(self) -> None:
            socket.close()

    return _LiveConnection()


# --------------------------------------------------------------------------
# URL discipline
# --------------------------------------------------------------------------


def check_url(url: str, *, schemes: tuple[str, ...]) -> str:
    """Return ``url`` if it addresses Zoom over an encrypted scheme, else raise.

    Applied to every URL this module opens, including the one Zoom itself hands
    back in the ``rtms_started`` payload. That URL arrives inside a body we have
    authenticated, but authenticated is not the same as trusted: an allowlist is
    what stops a compromised or mistaken upstream from redirecting a signed
    stream credential to a host of its choosing.
    """
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme not in schemes:
        raise RtmsError(
            f"{_safe(url)} must use {' or '.join(schemes)}, got "
            f"{parsed.scheme or '<none>'!r} -- a stream credential must never "
            f"travel in plaintext"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise RtmsError(f"{_safe(url)} names no host")
    if host in ALLOWED_HOSTS:
        return url
    if any(host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES):
        return url
    raise RtmsError(
        f"refusing to open {_safe(url)}: {host!r} is not a Zoom host "
        f"(allowed: {', '.join(sorted(ALLOWED_HOSTS))} or a subdomain of "
        f"{', '.join(ALLOWED_HOST_SUFFIXES)})"
    )


def _safe(url: str) -> str:
    """A URL with its query stripped -- an RTMS URL's query carries a token."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if not parsed.scheme:
        return "<malformed url>"
    return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"


# --------------------------------------------------------------------------
# Webhook verification
# --------------------------------------------------------------------------


class SeenEvents:
    """Bounded, thread-safe memory of event keys already handled.

    Zoom retries a webhook it did not get a prompt 2xx for. A retry that slips
    through is a second paid pipeline run for one request, and a slow first
    response is indistinguishable from a genuine retry at this end, so the keys
    are remembered rather than inferred (``slackbot/app.py:_SeenEvents``).
    """

    def __init__(self, limit: int = 1024):
        self._limit = limit
        self._lock = threading.Lock()
        self._keys: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, key: str) -> bool:
        """True the first time a key is seen, False every time after."""
        if not key:
            return True
        with self._lock:
            if key in self._keys:
                return False
            self._keys[key] = None
            while len(self._keys) > self._limit:
                self._keys.popitem(last=False)
            return True


#: Process-wide default, so a caller that does not thread a store through still
#: gets replay protection. Tests pass their own.
_DEFAULT_SEEN = SeenEvents()


def _headers_lower(headers: Any) -> dict[str, str]:
    """Case-insensitive view of whatever header mapping the caller has.

    Accepts a plain dict as well as ``http.client.HTTPMessage``, which is what
    ``BaseHTTPRequestHandler.headers`` actually is.
    """
    try:
        items = headers.items()
    except AttributeError as exc:
        raise WebhookError(
            f"headers must be a mapping, got {type(headers).__name__}",
            reason="bad_headers",
            status=400,
        ) from exc
    return {str(k).lower(): str(v) for k, v in items}


def _webhook_timestamp(raw: str) -> float:
    """Zoom's ``x-zm-request-timestamp`` as epoch seconds.

    **CONFIRMED: seconds.** ``zoom/rtms-samples``
    ``library/javascript/webhookManager/zoomWebhookSignature.js``
    ``verifyZoomWebhookRequest`` compares the header directly against a
    seconds-domain clock::

        const timestampSeconds = Number(timestamp);
        const ageSeconds = Math.abs(Math.floor(Date.now() / 1000) - timestampSeconds);

    The same file's ``DEFAULT_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300`` is
    where :data:`MAX_WEBHOOK_AGE_S` comes from, and ``Math.abs`` is why the age
    check here is also two-sided.

    Note this header is **not** the ``event_ts`` field inside the body, which
    *is* milliseconds. Two timestamps, two units, in one delivery -- which is
    exactly how this got hedged in the first place.

    The millisecond reading is kept as a fallback but is now **loud**. Before
    the check above it was a silent reinterpretation, so if Zoom had ever sent
    milliseconds nobody would have learned it; now a live run that hits this
    branch says so once per delivery, and the constant above is wrong and
    should be corrected here in one place.
    """
    value = float(raw)
    if abs(value) > 1e11:
        log.warning(
            "x-zm-request-timestamp %r is too large to be epoch seconds; "
            "reading it as milliseconds. Zoom's own sample "
            "(zoomWebhookSignature.js::verifyZoomWebhookRequest) treats this "
            "header as seconds, so this branch means that assumption is wrong "
            "and _webhook_timestamp needs correcting",
            raw,
        )
        return value / 1000.0
    return value


def _event_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """The dict an event's fields actually live in.

    RTMS events put them directly on ``payload``; Zoom's older meeting events
    nest them under ``payload.object``. One rule, one place, used by both
    :func:`event_key` and :func:`session_from_payload` -- if these two ever
    disagreed about where a stream id lives, the replay guard would be keying
    on a different event than the one being opened.
    """
    body = payload.get("payload")
    if not isinstance(body, dict):
        return {}
    nested = body.get("object")
    return nested if isinstance(nested, dict) else body


def event_key(headers: Mapping[str, str], payload: Mapping[str, Any]) -> str:
    """The identity a replay is recognised by.

    Zoom has no ``event_id`` field the way Slack does, so the key is a digest
    of the event, its timestamp and the stream it names -- deliberately *not*
    of the whole body, since Zoom is free to add a field to a retry and that
    must not read as a new event.

    ``x-zm-trackingid`` is per-delivery and looks like the obvious key, and it
    used to be preferred. It is **not signed**: ``_signature`` covers only
    ``v0:<timestamp>:<body>``. Keying on it meant the same captured bytes,
    replayed inside the 300-second window with that one header varied, were
    accepted every time -- and every acceptance of a ``meeting.rtms_started``
    is another *paid* pipeline run, the exact cost this guard exists to
    prevent. The digest is derived from the signed body, so a replay cannot
    change it without breaking the HMAC. Slack has no equivalent hole: its
    ``event_id`` lives inside the signed body.
    """
    # Same shape rule as `session_from_payload`, and it must stay the same one.
    # While this read `payload.object` against RTMS's flat payload, every field
    # below came back empty, so *every* rtms_started digested to the identical
    # key: the first meeting of the process's life was accepted and every
    # later one was refused as a replay. A replay guard that rejects real
    # events is worse than none, because it fails as silence.
    obj = _event_object(payload)
    parts = "|".join(
        str(x)
        for x in (
            payload.get("event", ""),
            payload.get("event_ts", ""),
            obj.get("meeting_uuid", ""),
            obj.get("rtms_stream_id", ""),
        )
    )
    return "digest:" + hashlib.sha256(parts.encode("utf-8")).hexdigest()


def verify_webhook(
    config: Config,
    headers: Any,
    body: bytes,
    *,
    now: float | None = None,
    seen: SeenEvents | None = None,
) -> dict:
    """Prove an inbound webhook came from Zoom, recently and once, then parse it.

    The order is the whole point and it is the order ``slackbot/app.py`` uses:
    the HMAC is checked against the **raw bytes** before anything looks at what
    they say. Parsing first and verifying after means acting on a forged body's
    shape even when the signature is ultimately rejected.

    Returns:
        The decoded event object.

    Raises:
        WebhookError: for a missing header, an unreadable timestamp, an age
            past :data:`MAX_WEBHOOK_AGE_S`, a signature that does not match, a
            body that is not a JSON object, or a delivery already handled.
            ``reason`` distinguishes them.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise WebhookError(
            f"body must be raw bytes, got {type(body).__name__} -- verifying a "
            f"re-encoded string can differ from the bytes Zoom signed",
            reason="bad_body",
            status=400,
        )
    if len(body) > MAX_BODY_BYTES:
        raise WebhookError(
            f"body is {len(body)} bytes, over the {MAX_BODY_BYTES}-byte limit",
            reason="too_large",
            status=413,
        )

    lower = _headers_lower(headers)
    signature = lower.get("x-zm-signature", "")
    timestamp = lower.get("x-zm-request-timestamp", "")
    if not signature or not timestamp:
        raise WebhookError(
            "missing x-zm-signature or x-zm-request-timestamp",
            reason="unsigned",
        )
    try:
        sent_at = _webhook_timestamp(timestamp)
    except (TypeError, ValueError) as exc:
        raise WebhookError(
            f"x-zm-request-timestamp {timestamp!r} is not a number",
            reason="bad_timestamp",
        ) from exc

    current = time.time() if now is None else now
    age = abs(current - sent_at)
    if age > MAX_WEBHOOK_AGE_S:
        raise WebhookError(
            f"request is {age:.0f}s old, past the {MAX_WEBHOOK_AGE_S:.0f}s "
            f"window -- refusing a possible replay",
            reason="stale",
        )

    if not hmac.compare_digest(
        _signature(config.webhook_secret, timestamp, bytes(body)), signature
    ):
        raise WebhookError(
            "x-zm-signature does not match the body", reason="bad_signature"
        )

    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(
            f"a correctly-signed body was not JSON: {exc}",
            reason="not_json",
            status=400,
        ) from exc
    if not isinstance(payload, dict):
        raise WebhookError(
            f"expected a JSON object, got {type(payload).__name__}",
            reason="not_object",
            status=400,
        )

    # A url_validation is deliberately exempt: it carries a fresh plainToken,
    # answering it twice costs nothing, and remembering it would make
    # re-validating an endpoint from the Zoom dashboard fail the second time.
    if payload.get("event") != "endpoint.url_validation":
        store = _DEFAULT_SEEN if seen is None else seen
        if not store.add_if_new(event_key(lower, payload)):
            raise WebhookError(
                "this delivery has already been handled; refusing to act on it "
                "twice, because a replayed rtms_started is a second paid run",
                reason="replay",
                status=200,
            )
    return payload


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    """``v0=<hex hmac-sha256(secret, "v0:<ts>:<body>")>``, Zoom's scheme."""
    base = b"%s:%s:%s" % (
        SIGNATURE_VERSION.encode("utf-8"),
        timestamp.encode("utf-8"),
        body,
    )
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def url_validation_reply(config: Config, payload: Mapping[str, Any]) -> dict:
    """Answer Zoom's ``endpoint.url_validation`` challenge.

    Zoom posts a ``plainToken`` and expects it back alongside its HMAC-SHA256
    under the webhook secret token, hex-encoded. Getting this wrong is not
    subtle -- the endpoint simply never validates -- but getting it *silently*
    wrong is: an empty ``plainToken`` would otherwise produce a perfectly
    well-formed reply for a challenge that was never issued, so it raises.
    """
    obj = payload.get("payload")
    plain = ""
    if isinstance(obj, dict):
        plain = str(obj.get("plainToken", "") or "")
    if not plain:
        raise WebhookError(
            "endpoint.url_validation carried no payload.plainToken",
            reason="no_plain_token",
            status=400,
        )
    encrypted = hmac.new(
        config.webhook_secret.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {"plainToken": plain, "encryptedToken": encrypted}


# --------------------------------------------------------------------------
# Streams
# --------------------------------------------------------------------------


def session_from_payload(payload: Mapping[str, Any]) -> RtmsSession:
    """Pull the meeting uuid, stream id and signalling URL out of the event.

    Every missing field is collected into one :class:`PayloadError` rather than
    raised on the first, the ``netlist.parse_circuit_spec`` convention: a badly
    shaped event should produce one report, not a sequence of them.
    """
    body = payload.get("payload")
    if not isinstance(body, dict):
        raise PayloadError(["payload is missing or is not an object"])

    # **CORRECTED 2026-09-08.** This read `payload.object`, on the reasonable
    # but wrong assumption that RTMS events use the envelope Zoom's older
    # meeting webhooks do. They do not: `zoom/rtms-samples`
    # `library/javascript/rtmsManager/RTMSManager.js` destructures the fields
    # straight off `payload` --
    #
    #     this.on('meeting.rtms_started', (payload) => {
    #       const { meeting_uuid, rtms_stream_id, server_urls, event_ts } = payload;
    #
    # -- where that `payload` is `body.payload` from the webhook POST, with no
    # `.object` step anywhere in the chain. Against a real account the old code
    # raised PayloadError on every single event: loud, which is the one mercy,
    # but the integration could never have opened a stream.
    #
    # Both shapes are read because Zoom's *other* event families genuinely do
    # nest under `.object`, and this is one webhook endpoint. That is not a
    # silent hedge: if neither shape carries the fields, every missing one is
    # named in a single PayloadError below.
    obj = _event_object(payload)

    problems: list[str] = []
    meeting_uuid = str(obj.get("meeting_uuid", "") or "").strip()
    stream_id = str(obj.get("rtms_stream_id", "") or "").strip()
    signal_url = str(obj.get("server_urls", "") or "").strip()
    if not meeting_uuid:
        problems.append(
            "meeting_uuid is missing (looked on payload and payload.object)"
        )
    if not stream_id:
        problems.append(
            "rtms_stream_id is missing (looked on payload and payload.object)"
        )
    if not signal_url:
        problems.append(
            "server_urls (the signalling URL) is missing "
            "(looked on payload and payload.object)"
        )
    if problems:
        raise PayloadError(problems)
    return RtmsSession(
        meeting_uuid=meeting_uuid, stream_id=stream_id, signal_url=signal_url
    )


def _handshake_signature(config: Config, session: RtmsSession) -> str:
    """``hmac-sha256(client_secret, "client_id,meeting_uuid,rtms_stream_id")``.

    The comma-joined base string is the sample's, character for character; a
    different separator produces a signature Zoom rejects with no explanation
    of why.
    """
    base = f"{config.client_id},{session.meeting_uuid},{session.stream_id}"
    return hmac.new(
        config.client_secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _read_frame(connection: Connection, *, where: str) -> dict | None:
    """One JSON frame, or None when the peer closed. Never a quiet zero."""
    raw = connection.recv()
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    if not isinstance(raw, str):
        raise FrameError(
            f"{where} produced a {type(raw).__name__} frame; expected JSON text"
        )
    if raw == "":
        return None
    if len(raw) > MAX_FRAME_BYTES:
        raise FrameError(
            f"{where} sent a {len(raw)}-byte frame, over the "
            f"{MAX_FRAME_BYTES}-byte limit"
        )
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError as exc:
        # A truncated frame lands here. It is an error, not a frame to skip:
        # skipping means silently losing whatever the meeting said in it.
        raise FrameError(
            f"{where} sent a frame that is not valid JSON ({exc}); "
            f"{len(raw)} bytes, starting {raw[:80]!r}"
        ) from exc
    if not isinstance(frame, dict):
        raise FrameError(
            f"{where} sent a JSON {type(frame).__name__}; expected an object"
        )
    return frame


def _await_response(
    connection: Connection, *, expect: int, where: str
) -> dict:
    """Read frames until the expected message type, within the frame cap."""
    for _ in range(MAX_HANDSHAKE_FRAMES):
        frame = _read_frame(connection, where=where)
        if frame is None:
            raise HandshakeError(
                f"{where} closed before answering the handshake; the stream was "
                f"never authorised"
            )
        kind = frame.get("msg_type")
        if kind == expect:
            # `status_code` is an integer on the wire (see STATUS_OK). A frame
            # that carries no status_code at all, or one that is not a number,
            # is a refusal rather than a pass: "I could not read the status"
            # must never be the same answer as "the status was OK".
            raw_status = frame.get("status_code")
            try:
                status = int(raw_status)
            except (TypeError, ValueError):
                raise HandshakeError(
                    f"{where} answered the handshake with an unreadable "
                    f"status_code {raw_status!r}; refusing to treat an "
                    f"unparseable status as success"
                ) from None
            if status != STATUS_OK:
                raise HandshakeError(
                    f"{where} refused the handshake: status_code={status}, "
                    f"reason={frame.get('reason', '<none>')!r}"
                )
            return frame
        if kind == MSG_TYPE["KEEP_ALIVE_REQ"]:
            _answer_keep_alive(connection, frame)
    raise HandshakeError(
        f"{where} sent {MAX_HANDSHAKE_FRAMES} frames without a handshake "
        f"response; refusing to keep reading"
    )


def _state(frame: Mapping[str, Any]) -> int:
    """A ``state`` field as the integer the wire carries, or ``-1``.

    ``-1`` is deliberately a value no enum uses, so an unreadable state can
    never coincide with a terminal one. Erring this way keeps the stream open
    and lets the socket-close path report it, which is the loud outcome; the
    other way round would end a meeting early and call it clean.
    """
    try:
        return int(frame.get("state"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        log.warning(
            "RTMS state frame carried an unreadable state: %r", frame.get("state")
        )
        return -1


def _answer_keep_alive(connection: Connection, frame: Mapping[str, Any]) -> None:
    connection.send(
        json.dumps(
            {
                "msg_type": MSG_TYPE["KEEP_ALIVE_RESP"],
                "timestamp": frame.get("timestamp", 0),
            }
        )
    )


def _media_url(frame: Mapping[str, Any]) -> str:
    """The media WebSocket URL out of a signalling handshake response.

    **CONFIRMED** against ``zoom/rtms-samples``
    ``library/javascript/rtmsManager/utils/rtmsEntityHelper.js``
    ``getPreferredMediaUrl``, which is the sample's own resolver for this exact
    field and handles the same two shapes: a bare string, or an object keyed by
    media type with an ``all`` key beside the per-type ones. Its order is
    followed here rather than invented: the **requested media type first**,
    then ``all``, then ``audio``, then any value that looks like a WebSocket
    URL.

    The requested type comes first for a reason worth stating. This client
    subscribes to transcript alone, and ``all`` is the socket carrying every
    media type — taking it when a transcript-specific socket was offered means
    asking Zoom to send audio and video frames that are then dropped on the
    floor.

    Note the signalling *response* is an object here, while the
    ``server_urls`` on the ``rtms_started`` *webhook* is a flat string
    (``signalingSocket.js`` guards it with ``typeof serverUrls !== 'string'``).
    Two fields, same name, different shapes, which is why they are read in two
    places rather than one.

    A response carrying neither shape is an error rather than a URL guessed
    from the signalling host.
    """
    server = frame.get("media_server")
    urls = server.get("server_urls") if isinstance(server, dict) else None
    if isinstance(urls, str) and urls:
        return urls
    if isinstance(urls, dict):
        for key in ("transcript", "all", "audio"):
            value = urls.get(key)
            if isinstance(value, str) and value:
                return value
        # getPreferredMediaUrl's last resort, kept so a per-type key this
        # module does not know about still connects rather than refusing.
        for value in urls.values():
            if isinstance(value, str) and value.startswith("ws"):
                return value
    raise HandshakeError(
        "the signalling handshake response carried no "
        "media_server.server_urls to connect to"
    )


class _RtmsStream:
    """A live media socket, presented as an iterator of transcript chunks."""

    def __init__(
        self,
        session: RtmsSession,
        media: Connection,
        signal: Connection | None,
        *,
        max_frames: int = MAX_FRAMES,
    ):
        self.session = session
        self._media = media
        self._signal = signal
        self._max_frames = max_frames
        #: How many chunks this stream produced. Read it after iterating to
        #: report what a run actually heard.
        self.chunks_seen = 0
        #: Set when a STREAM_STATE_UPDATE said the stream is over.
        self.ended_cleanly = False

    def __iter__(self) -> Iterator[TranscriptChunk]:
        try:
            yield from self._frames()
        finally:
            self.close()

    def _frames(self) -> Iterator[TranscriptChunk]:
        for _ in range(self._max_frames):
            frame = _read_frame(self._media, where="the RTMS media socket")
            if frame is None:
                if not self.ended_cleanly:
                    raise StreamInterruptedError(
                        f"the RTMS media socket for meeting "
                        f"{self.session.meeting_uuid} closed after "
                        f"{self.chunks_seen} chunk(s) without ending the "
                        f"stream; this is half a meeting, not a short one",
                        chunks=self.chunks_seen,
                    )
                break
            kind = frame.get("msg_type")
            if kind == MSG_TYPE["MEDIA_DATA_TRANSCRIPT"]:
                chunk = self._chunk(frame)
                if chunk is not None:
                    self.chunks_seen += 1
                    yield chunk
            elif kind == MSG_TYPE["KEEP_ALIVE_REQ"]:
                _answer_keep_alive(self._media, frame)
            elif kind == MSG_TYPE["STREAM_STATE_UPDATE"]:
                state = _state(frame)
                if state in TERMINAL_STREAM_STATES:
                    self.ended_cleanly = True
                    break
                log.info("RTMS stream state: %s", state)
            elif kind == MSG_TYPE["SESSION_STATE_UPDATE"]:
                # A stopped *session* ends the stream just as finally as a
                # terminated stream does, and the sample disables reconnect on
                # it. Without this branch a session that stopped without a
                # STREAM_STATE_UPDATE would run to the socket close and be
                # reported as StreamInterruptedError -- "half a meeting" said
                # about a meeting that ended normally.
                state = _state(frame)
                if state in TERMINAL_SESSION_STATES:
                    self.ended_cleanly = True
                    break
                log.info("RTMS session state: %s", state)
        else:
            raise RtmsError(
                f"the RTMS media socket for meeting {self.session.meeting_uuid} "
                f"sent more than {self._max_frames} frames without ending; "
                f"refusing to keep reading a stream that will not stop"
            )

        if self.chunks_seen == 0:
            raise EmptyStreamError(
                f"the RTMS stream for meeting {self.session.meeting_uuid} ended "
                f"with no transcript at all. That is not the same as a quiet "
                f"meeting: check that transcription is on for the account and "
                f"that the app is subscribed to the transcript media type"
            )

    def _chunk(self, frame: Mapping[str, Any]) -> TranscriptChunk | None:
        content = frame.get("content")
        if not isinstance(content, dict):
            raise FrameError(
                "a transcript frame carried no content object; refusing to "
                "treat a malformed frame as silence"
            )
        text = str(content.get("data", "") or "").strip()
        if not text:
            return None
        # Zoom's own display name when the frame has one, its opaque numeric
        # participant id otherwise. Nothing is looked up.
        speaker = str(content.get("user_name", "") or "").strip()
        if not speaker:
            speaker = str(content.get("user_id", "") or "").strip() or "unknown"
        try:
            at_ms = int(content.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            at_ms = 0
        return TranscriptChunk(
            meeting_id=self.session.meeting_uuid,
            speaker=speaker,
            text=text,
            at_ms=at_ms,
        )

    def close(self) -> None:
        for connection in (self._media, self._signal):
            if connection is None:
                continue
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - a close failure must not mask a result
                log.debug("closing an RTMS socket failed", exc_info=True)


def open_stream(
    config: Config,
    payload: Mapping[str, Any],
    *,
    connect: Callable[[str], Connection] | None = None,
    max_frames: int = MAX_FRAMES,
) -> MediaStream:
    """Open the media stream a ``meeting.rtms_started`` event announces.

    The sequence is the sample's: authenticate on the signalling socket, take
    the media URL out of its response, authenticate again on the media socket,
    then read frames. Both handshakes must answer ``STATUS_OK``; anything else
    raises :class:`HandshakeError` before a single chunk is produced, so "we
    were never authorised" can never be read as "nobody spoke".

    ``connect`` is the seam: it takes a ``wss://`` URL and returns a
    :class:`Connection`. It defaults to :func:`websocket_connect`, which imports
    a WebSocket library lazily; every test passes a recorded connection instead.
    """
    session = session_from_payload(payload)
    if not config.allows(session.meeting_uuid):
        raise NotAuthorisedError(
            f"meeting {session.meeting_uuid} is not in ZOOM_MEETINGS; refusing "
            f"to open a stream for it"
        )
    if not config.rtms_enabled:
        raise NotAuthorisedError(
            "ZOOM_RTMS_ENABLED is off; refusing to open a stream rather than "
            "returning an empty one"
        )

    opener = connect or websocket_connect
    signature = _handshake_signature(config, session)

    signal_url = check_url(session.signal_url, schemes=("wss",))
    signal = opener(signal_url)
    media: Connection | None = None
    try:
        signal.send(
            json.dumps(
                {
                    "msg_type": MSG_TYPE["SIGNALING_HAND_SHAKE_REQ"],
                    "protocol_version": 1,
                    "meeting_uuid": session.meeting_uuid,
                    "rtms_stream_id": session.stream_id,
                    "signature": signature,
                }
            )
        )
        response = _await_response(
            signal,
            expect=MSG_TYPE["SIGNALING_HAND_SHAKE_RESP"],
            where="the RTMS signalling socket",
        )
        media_url = check_url(_media_url(response), schemes=("wss",))

        media = opener(media_url)
        media.send(
            json.dumps(
                {
                    "msg_type": MSG_TYPE["DATA_HAND_SHAKE_REQ"],
                    "protocol_version": 1,
                    "meeting_uuid": session.meeting_uuid,
                    "rtms_stream_id": session.stream_id,
                    "signature": signature,
                    "media_type": MEDIA_TYPE_TRANSCRIPT,
                    "payload_encryption": False,
                }
            )
        )
        _await_response(
            media,
            expect=MSG_TYPE["DATA_HAND_SHAKE_RESP"],
            where="the RTMS media socket",
        )
    except Exception:
        for connection in (media, signal):
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001
                    log.debug("closing an RTMS socket failed", exc_info=True)
        raise

    return _RtmsStream(session, media, signal, max_frames=max_frames)

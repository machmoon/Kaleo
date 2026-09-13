"""Slack Socket Mode: receive events over an outbound WebSocket, no public URL.

The Events API in :mod:`slackbot.app` needs Slack to reach an HTTPS endpoint,
which a laptop does not have. Socket Mode turns the direction around: the app
opens the connection, Slack pushes the same event payloads down it, and the
app acknowledges each one on the same socket. Nothing listens on a port.

This is a stdlib-plus-``websocket-client`` port of Slack's own client, read
from source rather than from the docs (``slackapi/python-slack-sdk`` at
``b9f4666``; ``slackapi/bolt-python`` at ``c793795``):

* **Getting a URL.** ``slack_sdk/socket_mode/client.py::issue_new_wss_url``
  calls ``web_client.apps_connections_open(app_token=...)`` and reads
  ``response["url"]``; ``slack_sdk/web/client.py::apps_connections_open`` is
  ``api_call("apps.connections.open", http_verb="POST", params={"token":
  app_token})``, and ``web/base_client.py::_sync_send`` lifts that ``token``
  into ``Authorization: Bearer`` on a form-encoded POST
  (``_build_urllib_request_headers``). The token is the app-level ``xapp-``
  token, not the ``xoxb-`` bot token -- the bot token cannot open a socket and
  the app token cannot post a message, which is why the bridge needs both.
  The URL is single-use: every reconnect asks for a fresh one
  (``connect_to_new_endpoint`` re-issues before ``connect``).
* **The envelope.** ``slack_sdk/socket_mode/request.py::SocketModeRequest.
  from_dict`` accepts a frame only when it has ``type``, ``envelope_id`` and
  ``payload``; ``type`` is ``events_api``, ``slash_commands`` or
  ``interactive``, and ``retry_attempt``/``retry_reason`` say a delivery is a
  redelivery. The recorded frames in the SDK's own test server
  (``tests/slack_sdk/socket_mode/mock_socket_mode_server.py``) are what
  :mod:`slackbot.tests.test_socket_mode` replays.
* **The acknowledgement.** ``slack_sdk/socket_mode/response.py::
  SocketModeResponse.to_dict`` is ``{"envelope_id": ...}`` plus an optional
  ``payload``, sent as JSON text on the socket
  (``client.py::send_socket_mode_response``). Bolt sends it after its
  listener ran (``slack_bolt/adapter/socket_mode/internals.py::
  send_response``); this client sends it *before* handing the event on,
  because the work here is a thread start and an ack that waited on a slow
  handler would earn a redelivery -- the Events API's three-second rule.
* **Control frames.** ``hello`` opens a session (the SDK test server sends
  ``{"type":"hello","num_connections":...}`` first). ``disconnect`` means
  "reconnect now": ``client.py::process_message`` answers it with
  ``connect_to_new_endpoint(force=True)`` and never passes it to listeners.
* **Liveness.** The SDK's ``websocket_client`` implementation runs
  ``run_forever(ping_interval=10)`` and a monitor that reconnects whenever
  ``current_session.sock is None``. This client is synchronous, so the same
  thing is a receive timeout that sends a ping: a ping that cannot be sent is
  a dead socket, and a dead socket is a reconnect, never a hang.

One deliberate difference: the SDK retries a rate-limited
``apps.connections.open`` recursively after ``Retry-After``; this client backs
off exponentially (``RECONNECT_BACKOFF_S``, capped) for every failure to
connect, and resets the backoff on ``hello``, so a revoked token or a dead
network is a slow, logged retry rather than a hot loop.

``websocket-client`` is imported inside :func:`websocket_connect`, the
``zoombot/rtms.py`` convention, so this module imports and tests on a machine
with no WebSocket library.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .slack import API_ROOT, HttpRequest, SlackClient, SlackError, Transport

__all__ = [
    "Connection",
    "SocketModeClient",
    "SocketRequest",
    "open_connection_url",
    "websocket_connect",
    "RECONNECT_BACKOFF_S",
    "MAX_BACKOFF_S",
]

log = logging.getLogger("slackbot.socket_mode")

#: First wait after a failed connect; doubled per consecutive failure.
RECONNECT_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
#: The SDK's default ``ping_interval``.
PING_INTERVAL_S = 10.0
#: Failures a retry cannot fix: a wrong or revoked token, a missing package.
FATAL_CODES = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "not_allowed_token_type",
        "account_inactive",
        "missing_dependency",
    }
)


class Connection(Protocol):
    """One WebSocket. ``recv`` returns ``None`` once the socket is closed."""

    def send(self, text: str) -> None: ...

    def recv(self) -> str | None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SocketRequest:
    """``SocketModeRequest``, field for field."""

    type: str
    envelope_id: str
    payload: dict[str, Any]
    accepts_response_payload: bool = False
    retry_attempt: int = 0
    retry_reason: str = ""

    @classmethod
    def from_frame(cls, frame: dict[str, Any]) -> SocketRequest | None:
        if not all(k in frame for k in ("type", "envelope_id", "payload")):
            return None
        payload = frame["payload"]
        if isinstance(payload, str):
            payload = {"text": payload}
        if not isinstance(payload, dict):
            return None
        try:
            retry = int(frame.get("retry_attempt") or 0)
        except (TypeError, ValueError):
            retry = 0
        return cls(
            type=str(frame["type"]),
            envelope_id=str(frame["envelope_id"]),
            payload=payload,
            accepts_response_payload=bool(frame.get("accepts_response_payload")),
            retry_attempt=retry,
            retry_reason=str(frame.get("retry_reason") or ""),
        )


def open_connection_url(app_token: str, *, transport: Transport | None = None) -> str:
    """``apps.connections.open`` with the ``xapp-`` token; returns a ``wss://`` URL."""
    if not app_token.startswith("xapp-"):
        # Slack would answer ``not_allowed_token_type``; saying which token is
        # wrong before the round trip is kinder than relaying that string.
        raise SlackError(
            "not_allowed_token_type",
            "SLACK_APP_TOKEN must be an app-level token starting with xapp- "
            "(Basic Information -> App-Level Tokens, scope connections:write)",
        )
    client = SlackClient(app_token, transport=transport)
    request = HttpRequest(
        "POST",
        API_ROOT + "apps.connections.open",
        {
            "Authorization": f"Bearer {app_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        b"",
    )
    data = client._send_api(request, "apps.connections.open")
    url = str(data.get("url", ""))
    if not url.startswith("wss://"):
        raise SlackError("bad_response", "apps.connections.open returned no wss:// URL")
    return url


def websocket_connect(url: str) -> Connection:  # pragma: no cover - live only
    """Open a real WebSocket with ``websocket-client``, imported here."""
    if not url.startswith("wss://"):
        raise SlackError("bad_url", "Socket Mode URLs are wss://")
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SlackError(
            "missing_dependency",
            "Socket Mode needs the 'websocket-client' package "
            "(pip install websocket-client)",
        ) from exc

    socket = websocket.create_connection(url, timeout=PING_INTERVAL_S)

    class _Live:
        def send(self, text: str) -> None:
            socket.send(text)

        def recv(self) -> str | None:
            while True:
                try:
                    frame = socket.recv()
                except websocket.WebSocketTimeoutException:
                    # Quiet is normal; a ping that cannot be sent is not.
                    try:
                        socket.ping()
                    except Exception:  # noqa: BLE001 - any failure means dead
                        return None
                    continue
                except (websocket.WebSocketConnectionClosedException, OSError):
                    return None
                if frame is None or frame == "":
                    return None
                return (
                    frame
                    if isinstance(frame, str)
                    else frame.decode("utf-8", "replace")
                )

        def close(self) -> None:
            with contextlib.suppress(Exception):
                socket.close()

    return _Live()


class SocketModeClient:
    """Connect, acknowledge, hand on, reconnect. Synchronous; run on a thread."""

    def __init__(
        self,
        app_token: str,
        on_request: Callable[[SocketRequest], None],
        *,
        transport: Transport | None = None,
        connect: Callable[[str], Connection] = websocket_connect,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._app_token = app_token
        self._on_request = on_request
        self._transport = transport
        self._connect = connect
        self._sleep = sleep
        self.sessions = 0
        #: Recent envelope ids acknowledged, newest last (bounded; for tests and logs).
        self.acked: collections.deque[str] = collections.deque(maxlen=256)

    def serve(self, stop: threading.Event, *, max_sessions: int | None = None) -> None:
        """Run until ``stop`` is set (or ``max_sessions`` connections ended)."""
        backoff = RECONNECT_BACKOFF_S
        while not stop.is_set():
            if max_sessions is not None and self.sessions >= max_sessions:
                return
            self.sessions += 1
            try:
                url = open_connection_url(self._app_token, transport=self._transport)
                connection = self._connect(url)
            except SlackError as exc:
                if exc.code in FATAL_CODES:
                    # A wrong token does not become right by retrying.
                    raise
                log.warning(
                    "socket mode connect failed: %s; retrying in %.0fs", exc, backoff
                )
                self._sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue
            except Exception as exc:  # noqa: BLE001 - network failures vary
                log.warning(
                    "socket mode connect failed: %s; retrying in %.0fs", exc, backoff
                )
                self._sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue
            try:
                greeted = self._pump(connection, stop)
            finally:
                connection.close()
            if greeted:
                backoff = RECONNECT_BACKOFF_S
            elif not stop.is_set():
                # A socket that closed before ``hello`` is a failed connect,
                # and reconnecting at once would be a hot loop.
                self._sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)

    def _pump(self, connection: Connection, stop: threading.Event) -> bool:
        """Read one session to its end. True if Slack said ``hello``."""
        greeted = False
        while not stop.is_set():
            raw = connection.recv()
            if raw is None:
                log.info("socket closed; reconnecting")
                return greeted
            try:
                frame = json.loads(raw) if raw.startswith("{") else {}
            except json.JSONDecodeError:
                log.warning("ignoring a non-JSON socket frame")
                continue
            if not isinstance(frame, dict):
                continue
            kind = frame.get("type")
            if kind == "hello":
                greeted = True
                log.info(
                    "socket mode connected (%s connections)",
                    frame.get("num_connections"),
                )
                continue
            if kind == "disconnect":
                # Slack is rotating this socket (``reason``: warning,
                # refresh_requested, link_disabled). Fresh URL, same loop.
                log.info("slack asked to reconnect (%s)", frame.get("reason", ""))
                return greeted
            request = SocketRequest.from_frame(frame)
            if request is None:
                continue
            # Ack first: the envelope is ours the moment it arrived.
            connection.send(json.dumps({"envelope_id": request.envelope_id}))
            self.acked.append(request.envelope_id)
            try:
                self._on_request(request)
            except Exception:  # noqa: BLE001 - one bad event must not drop the socket
                log.exception("socket mode handler failed")
        return greeted

"""The HTTP surface Slack calls.

A stdlib server, like ``service/app.py``, exposing:

* ``POST /slack/events``   — the Events API: URL verification and ``app_mention``
* ``POST /slack/commands`` — a slash command (``/silkscreen …``)
* ``GET  /healthz``        — liveness

Every request is signature-verified before it is parsed, and acknowledged
within Slack's three-second window before any work starts. The work then
happens on a worker thread and reports itself into the thread it came from,
which is why the acknowledgement can be immediate and still honest: it promises
a thread, not a result.

Run it::

    SLACK_BOT_TOKEN=xoxb-… SLACK_SIGNING_SECRET=… GOOGLE_API_KEY=… \\
        python -m slackbot
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .commands import CommandError, parse_command
from .config import Config, ConfigError, load_config
from .runner import Runner
from .slack import SlackClient, verify_signature

__all__ = [
    "Dispatcher",
    "delivery_keys",
    "make_handler",
    "make_server",
    "main",
    "MAX_BODY_BYTES",
    "NO_RETRY_HEADER",
]

log = logging.getLogger("slackbot.app")

MAX_BODY_BYTES = 1 << 20
#: How much of an over-length body is read before the connection is closed.
#: Sized so a client that merely overshot the limit still reads its 413, while
#: a body declared as gigabytes is cut off. Bounded is the property that
#: matters: with this and the socket timeout, one connection costs an attacker
#: at least as much as it costs us.
MAX_DRAIN_BYTES = 2 * MAX_BODY_BYTES
#: Socket timeout for one request. Slack's own delivery timeout is three
#: seconds, so nothing legitimate is anywhere near this.
SOCKET_TIMEOUT_S = 15.0
#: Wall-clock ceiling on draining one over-length body. The socket timeout
#: restarts on every read, so on its own it bounds *silence*, not time: a
#: client dribbling one byte per window could hold a handler thread for hours
#: while staying inside MAX_DRAIN_BYTES. Past this deadline the connection is
#: abandoned instead.
DRAIN_DEADLINE_S = 30.0
#: How long a queued run waits for a slot before the channel is told the bot is
#: busy. Long enough to absorb one run ahead of it, short enough that nobody
#: sits watching a thread that will never answer.
SLOT_WAIT_S = 240.0

#: Slack's documented opt-out of the retry timetable. From the Events API
#: docs: "provide an HTTP header in your responses indicating that you'd
#: prefer no further attempts. Provide us this HTTP header and value:
#: ``x-slack-no-retry: 1``". Sent only on a delivery this bot has already
#: handled -- never on a real failure, where a retry is exactly what we want.
NO_RETRY_HEADER = ("X-Slack-No-Retry", "1")


class _NoRetry(dict):
    """A response body that also asks Slack to stop retrying this delivery.

    A marker subclass rather than a third tuple member, so every existing
    caller and test that reads ``(code, body)`` keeps working and compares
    equal to the plain dict it used to get.
    """


#: The body a duplicate delivery is answered with.
_NO_RETRY = _NoRetry({"ok": True})


class _SeenEvents:
    """Bounded set of delivery keys already handled.

    **CONFIRMED** against Slack's Events API documentation on 2026-09-08: "Your
    app should respond to the event request with an HTTP 2xx *within three
    seconds*. If it does not, we'll consider the event delivery attempt
    failed", and Slack will then "retry a failed request up to *3 times* in a
    gradually increasing timetable" (immediately, after a minute, after five
    minutes), each carrying ``x-slack-retry-num`` of ``1``, ``2`` or ``3``.
    A retry that slips through is a second *paid* pipeline run for one request,
    which this repository treats as a hard rule.

    **UNVERIFIABLE WITHOUT A WORKSPACE, and therefore not relied on alone:**
    the published pages do not state that a retried delivery carries the *same*
    ``event_id``. It is widely assumed and probably true, but "probably" is not
    a basis for a spend guard. So two independent keys are remembered for every
    delivery -- the ``event_id``, and a key derived from the event itself
    (``team_id``/``type``/``channel``/``ts``, the message's own identity, which
    cannot change between deliveries of one message) -- and a delivery matching
    *either* is refused. If the ``event_id`` assumption is wrong, the second
    key still holds; if Slack ever omits ``event_id``, the second key is the
    only one, which is why an empty id no longer means "always new".
    """

    def __init__(self, limit: int = 1024):
        self._limit = limit
        self._lock = threading.Lock()
        self._ids: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, *keys: str) -> bool:
        """True when none of ``keys`` has been seen; records them all."""
        present = [key for key in keys if key]
        if not present:
            # No identity at all. Refusing would drop a real request; accepting
            # is the documented-shape-never-seen case, and it is logged by the
            # caller. It cannot be silently deduplicated either way.
            return True
        with self._lock:
            if any(key in self._ids for key in present):
                return False
            for key in present:
                self._ids[key] = None
            while len(self._ids) > self._limit:
                self._ids.popitem(last=False)
            return True


def delivery_keys(payload: dict[str, Any]) -> tuple[str, str]:
    """The two independent identities of one Events API delivery.

    The first is Slack's ``event_id``. The second is the event's own identity:
    a message event's ``ts`` is the message timestamp, unique within a channel
    and fixed for the life of the message, so it is the same across every retry
    of that delivery whatever Slack does with ``event_id``.
    """
    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    # ``ts`` is what makes the derived key an identity rather than a category:
    # without it, every event of one type in one channel would hash the same
    # and the first would swallow the rest -- the exact bug the zoombot lane
    # found on 2026-09-08, where a flat-vs-nested payload read made every
    # meeting hash identically and only the first was ever accepted.
    ts = str(event.get("ts", ""))
    if not ts:
        return str(payload.get("event_id", "")), ""
    derived = "|".join(
        (
            str(payload.get("team_id", "")),
            str(event.get("type", "")),
            str(event.get("channel", "")),
            ts,
        )
    )
    return str(payload.get("event_id", "")), derived


class Dispatcher:
    """Decides what an incoming Slack payload means, and runs it off-thread."""

    def __init__(
        self, config: Config, runner: Runner, *, slot_wait_s: float = SLOT_WAIT_S
    ):
        self.config = config
        self.runner = runner
        self.slot_wait_s = slot_wait_s
        self._seen = _SeenEvents()
        self._threads: list[threading.Thread] = []

    # -- events -----------------------------------------------------------

    def handle_event_payload(
        self, payload: dict[str, Any], *, retry_num: int = 0
    ) -> tuple[int, Any]:
        """Map one Events API body to a response, scheduling any work.

        ``retry_num`` is the ``x-slack-retry-num`` header, logged rather than
        acted on: it says a delivery *is* a retry, which the duplicate keys
        already decide, and a retry whose original never arrived (a restart
        between the two) is a request that still deserves an answer.
        """
        kind = payload.get("type")
        if kind == "url_verification":
            # Answered inline, in plain text: this is the one Slack request
            # that wants a body rather than an acknowledgement.
            return 200, {"challenge": str(payload.get("challenge", ""))}
        if kind != "event_callback":
            return 200, {"ok": True}

        event_id, derived = delivery_keys(payload)
        if not self._seen.add_if_new(event_id, derived):
            # Answering 200 stops the retry timetable for this delivery; the
            # header stops it for the *next* one too. Slack documents it as
            # "provide us this HTTP header and value: x-slack-no-retry: 1".
            log.info(
                "ignoring duplicate delivery (event_id=%s retry=%s)",
                event_id or "<absent>",
                retry_num or "0",
            )
            return 200, _NO_RETRY

        event = payload.get("event") or {}
        if not isinstance(event, dict):
            return 200, {"ok": True}
        if event.get("type") != "app_mention":
            return 200, {"ok": True}
        # Never answer ourselves, or any other bot: two silkscreen bots in one
        # channel would otherwise mention each other indefinitely.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return 200, {"ok": True}

        channel = str(event.get("channel", ""))
        if not channel or not self.config.channel_allowed(channel):
            log.info("ignoring mention in disallowed channel %s", channel)
            return 200, {"ok": True}

        # Replying in the existing thread if there is one keeps a follow-up
        # ("review", "order") attached to the run it refers to.
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
        self._schedule(
            text=str(event.get("text", "")),
            channel=channel,
            thread_ts=thread_ts,
            user=str(event.get("user", "")),
        )
        return 200, {"ok": True}

    # -- slash commands ---------------------------------------------------

    def handle_slash_payload(self, form: dict[str, str]) -> tuple[int, Any]:
        """Map one slash-command body to its immediate acknowledgement.

        A slash command has no message to thread under, so the bot posts a
        visible message into the channel first and threads the run beneath it.
        The team seeing the request is the point; an ephemeral-only run would
        put the result where only one person could read it.
        """
        channel = form.get("channel_id", "")
        if not channel or not self.config.channel_allowed(channel):
            return 200, {
                "response_type": "ephemeral",
                "text": "silkscreen isn't enabled in this channel.",
            }
        self._schedule(
            text=form.get("text", ""),
            channel=channel,
            thread_ts="",
            user=form.get("user_id", ""),
        )
        return 200, {
            "response_type": "ephemeral",
            "text": "Starting — I'll post the run in this channel.",
        }

    # -- scheduling -------------------------------------------------------

    def _schedule(self, *, text: str, channel: str, thread_ts: str, user: str) -> None:
        try:
            command = parse_command(text)
        except CommandError as exc:
            # Parse errors are cheap and need no slot; they answer immediately.
            message = str(exc)
            self._spawn(
                lambda: self.runner.report_error(channel, thread_ts, message)
            )
            return
        self._spawn(
            lambda: self._run_with_slot(command, channel, thread_ts, user)
        )

    def _run_with_slot(
        self, command: Any, channel: str, thread_ts: str, user: str
    ) -> None:
        cheap = command.verb in ("help", "order")
        if cheap:
            self.runner.handle(command, channel=channel, thread_ts=thread_ts, user=user)
            return
        if not self.runner.acquire_slot(timeout=self.slot_wait_s):
            self.runner.report_busy(channel, thread_ts)
            return
        try:
            self.runner.handle(command, channel=channel, thread_ts=thread_ts, user=user)
        finally:
            self.runner.release_slot()

    def _spawn(self, work: Any) -> threading.Thread:
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self._threads.append(thread)
        # Keep the list from growing for the lifetime of the process.
        self._threads = [t for t in self._threads if t.is_alive()]
        return thread

    def join(self, timeout: float = 30.0) -> None:
        """Wait for scheduled work. For tests and shutdown, not the hot path."""
        for thread in list(self._threads):
            thread.join(timeout)


def make_handler(dispatcher: Dispatcher) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to one dispatcher."""

    config = dispatcher.config

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "silkscreen-slack"
        #: Applied to the socket by ``StreamRequestHandler.setup``. Without it
        #: an unauthenticated client that opens a connection and then sends its
        #: body one byte at a time holds a handler thread indefinitely; this
        #: server is thread-per-connection, so that is a way to exhaust it.
        timeout = SOCKET_TIMEOUT_S

        # -- plumbing ----------------------------------------------------

        def _send(self, code: int, payload: Any, content_type: str = "") -> None:
            if isinstance(payload, (dict, list)):
                body = json.dumps(payload).encode("utf-8")
                content_type = content_type or "application/json; charset=utf-8"
            else:
                body = str(payload).encode("utf-8")
                content_type = content_type or "text/plain; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if isinstance(payload, _NoRetry):
                self.send_header(*NO_RETRY_HEADER)
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, "bad Content-Length")
                return None
            if length > MAX_BODY_BYTES:
                # Drain a little before answering, so a well-behaved client
                # gets the 413 explaining itself rather than a reset socket.
                # Bounded, though: this happens before authentication, and
                # reading a declared 10 GB from an unauthenticated client
                # would hand it a handler thread for as long as it cared to
                # dribble bytes. Past the bound — in bytes or in wall-clock
                # time — the connection is closed instead, which is the only
                # safe answer to a body we are never going to read.
                self.close_connection = True
                if self._drain(min(length, MAX_DRAIN_BYTES)):
                    self._send(413, "body too large")
                return None
            return self.rfile.read(length) if length > 0 else b""

        def _drain(self, length: int, chunk: int = 64 << 10) -> bool:
            """Consume up to ``length`` bytes; False when abandoned early.

            Bounded in time as well as bytes. The per-socket timeout restarts
            on every read, so a client dribbling a byte per window would stay
            inside it for hours; DRAIN_DEADLINE_S caps the whole drain. Reads
            go through ``read1`` where the stream offers it — one underlying
            read per call — so the shrinking timeout genuinely bounds each
            iteration rather than being restarted inside a buffered fill.
            """
            deadline = time.monotonic() + DRAIN_DEADLINE_S
            connection = getattr(self, "connection", None)
            read1 = getattr(self.rfile, "read1", self.rfile.read)
            remaining = length
            try:
                while remaining > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    if connection is not None:
                        connection.settimeout(min(SOCKET_TIMEOUT_S, left))
                    block = read1(min(chunk, remaining))
                    if not block:
                        return True
                    remaining -= len(block)
                return True
            except OSError:  # timeout or a client that gave up mid-dribble
                return False
            finally:
                if connection is not None:
                    with contextlib.suppress(OSError):
                        connection.settimeout(SOCKET_TIMEOUT_S)

        def _verified_body(self) -> bytes | None:
            """Read the body and prove it came from Slack, or answer 401.

            Verification happens against the raw bytes, before any parsing.
            Parsing first and verifying after would mean acting on a forged
            body's shape even when the signature is rejected.
            """
            body = self._read_body()
            if body is None:
                return None
            ok = verify_signature(
                config.signing_secret,
                timestamp=self.headers.get("X-Slack-Request-Timestamp", ""),
                signature=self.headers.get("X-Slack-Signature", ""),
                body=body,
            )
            if not ok:
                log.warning("rejected an unsigned or stale request to %s", self.path)
                self._send(401, "bad signature")
                return None
            return body

        # -- routes ------------------------------------------------------

        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/healthz":
                self._send(200, {"ok": True, "service": "silkscreen-slack"})
                return
            self._send(404, "not found")

        def do_POST(self) -> None:
            route = self.path.split("?")[0]
            if route == "/slack/events":
                self._events()
            elif route == "/slack/commands":
                self._slash()
            else:
                self._send(404, "not found")

        def _events(self) -> None:
            body = self._verified_body()
            if body is None:
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, "body was not JSON")
                return
            if not isinstance(payload, dict):
                self._send(400, "expected a JSON object")
                return
            try:
                retry_num = int(self.headers.get("X-Slack-Retry-Num", "0") or 0)
            except ValueError:
                retry_num = 0
            code, response = dispatcher.handle_event_payload(
                payload, retry_num=retry_num
            )
            self._send(code, response)

        def _slash(self) -> None:
            body = self._verified_body()
            if body is None:
                return
            parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            form = {k: v[0] for k, v in parsed.items() if v}
            code, response = dispatcher.handle_slash_payload(form)
            self._send(code, response)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def make_server(config: Config, runner: Runner | None = None) -> ThreadingHTTPServer:
    client = SlackClient(config.bot_token)
    dispatcher = Dispatcher(config, runner or Runner(config, client))
    return ThreadingHTTPServer(("0.0.0.0", config.port), make_handler(dispatcher))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    server = make_server(config)
    for key, value in config.redacted().items():
        log.info("config %s = %s", key, value)
    log.info("listening on :%s (POST /slack/events)", server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

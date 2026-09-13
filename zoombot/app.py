"""The HTTP surface Zoom calls.

A stdlib server, like ``service/app.py`` and ``slackbot/app.py``, exposing:

* ``POST /zoom/events`` — the webhook: the ``endpoint.url_validation``
  handshake, and the ``meeting.rtms_started`` notification that carries the
  signalling URL a media stream is opened from
* ``GET  /healthz``     — liveness

Shaped after Zoom's own open-source ``zoom/rtms`` samples: an HMAC handshake on
the endpoint URL first, then a signalling WebSocket, then a media WebSocket.
Everything about the media half lives in :mod:`zoombot.rtms`; this module is
only the door.

**Three rules, all borrowed from ``slackbot/app.py`` because they were paid for
there.** The signature is verified against the raw bytes *before* the body is
parsed -- parsing first would mean acting on a forged body's shape even when
the signature is ultimately rejected, which is why
:func:`~zoombot.rtms.verify_webhook` returns the parsed payload rather than
letting anything here call ``json.loads``: there is no code path that produces
an unverified payload. The response is sent before any work starts, because
Zoom's delivery deadline is as unforgiving as Slack's and a webhook that blocks
on a pipeline run gets retried. And deliveries are remembered
(:class:`~zoombot.rtms.SeenEvents`), because a retry that slips through is a
second *paid* pipeline run for one meeting.

**Run memory is in-process only.** After a restart the surface says so (see
:data:`~zoombot.runner.NO_RUN_REMEMBERED`) rather than answering about
whichever board happened to be last.

**RTMS is receive-only, and nothing here orders anything.** Audio back into the
room needs a real participant -- the headless Meeting SDK container in
``bot/``, which is not built or run by this test suite and has never been run
against a live Zoom account here. Which speaker was actually used is named in
every :class:`~zoombot.runner.ZoomReport`.

Run it::

    ZOOM_CLIENT_ID=… ZOOM_CLIENT_SECRET=… ZOOM_ACCOUNT_ID=… \\
        ZOOM_WEBHOOK_SECRET_TOKEN=… GOOGLE_API_KEY=… python -m zoombot
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import DEFAULT_PORT as _DEFAULT_PORT
from .config import Config, ConfigError, load_config
from .rtms import (
    MAX_BODY_BYTES,
    SeenEvents,
    WebhookError,
    url_validation_reply,
    verify_webhook,
)
from .runner import ZoomRunner, meeting_id_of

__all__ = [
    "Dispatcher",
    "make_handler",
    "make_server",
    "main",
    "DEFAULT_PORT",
    "SLOT_WAIT_S",
    "SOCKET_TIMEOUT_S",
]

log = logging.getLogger("zoombot.app")

#: Socket timeout for one request. Zoom's own delivery timeout is three
#: seconds, so nothing legitimate is anywhere near this. Without it a client
#: that sends its body one byte at a time holds a handler thread indefinitely,
#: and this server is thread-per-connection.
SOCKET_TIMEOUT_S = 15.0
#: How long a queued meeting waits for a run slot before it is refused. A
#: meeting still waiting after this has usually ended.
SLOT_WAIT_S = 240.0
#: Re-exported from :mod:`zoombot.config`, which owns the port now that
#: ``ZOOM_PORT`` is a declared setting. One definition, because a server that
#: binds a different port than the config reports is a webhook that silently
#: never fires.
DEFAULT_PORT = _DEFAULT_PORT


class Dispatcher:
    """Verifies a delivery, decides what it means, and runs it off-thread."""

    def __init__(
        self,
        config: Config,
        runner: ZoomRunner,
        *,
        slot_wait_s: float = SLOT_WAIT_S,
        seen: SeenEvents | None = None,
    ):
        self.config = config
        self.runner = runner
        self.slot_wait_s = slot_wait_s
        # Per-dispatcher rather than the module-wide default in rtms, so two
        # servers in one process (and every test) keep separate memories.
        self.seen = seen or SeenEvents()
        self._threads: list[threading.Thread] = []

    # -- the front door ---------------------------------------------------

    def handle_request(
        self, headers: Any, body: bytes, *, now: float | None = None
    ) -> tuple[int, Any]:
        """One raw delivery in, one response out. Verification lives here.

        Deliberately the only way in: a caller cannot reach
        :meth:`handle_event_payload` with bytes off the wire without going
        through the signature check first, because the parsed payload only
        exists on the far side of it.
        """
        try:
            payload = verify_webhook(
                self.config, headers, body, now=now, seen=self.seen
            )
        except WebhookError as exc:
            if exc.reason == "replay":
                # A retry, not an attack. Answered 200 so Zoom stops retrying,
                # and emphatically not run a second time: that would be a
                # second paid pipeline run for one meeting.
                log.info("ignoring a repeated delivery: %s", exc)
                return exc.status, {"ok": True, "duplicate": True}
            log.warning("rejected a webhook delivery (%s): %s", exc.reason, exc)
            return exc.status, {"error": str(exc), "reason": exc.reason}
        return self.handle_event_payload(payload)

    def handle_event_payload(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """Map one *verified* webhook body to a response, scheduling any work."""
        event = str(payload.get("event", ""))

        if event == "endpoint.url_validation":
            # Answered inline: this is the one Zoom request that wants a
            # computed body rather than an acknowledgement.
            try:
                return 200, url_validation_reply(self.config, payload)
            except WebhookError as exc:
                log.warning("url_validation handshake failed: %s", exc)
                return exc.status, {"error": str(exc), "reason": exc.reason}

        if event != "meeting.rtms_started":
            # Every other event -- rtms_stopped, meeting.ended, participant
            # churn -- is acknowledged and ignored. There is nothing to do with
            # one that would not be guessing.
            return 200, {"ok": True, "ignored": event or "an unnamed event"}

        meeting_id = meeting_id_of(payload)
        if meeting_id and not self.config.allows(meeting_id):
            log.info("ignoring rtms for meeting %s: not in ZOOM_MEETINGS", meeting_id)
            return 200, {"ok": True, "ignored": "not in the meeting allowlist"}

        # Acknowledged now, worked on a thread: the run is minutes long and the
        # webhook deadline is seconds. The 200 promises a run, not a board.
        self._spawn(lambda: self._run_with_slot(payload))
        return 200, {"ok": True, "accepted": meeting_id or "meeting"}

    def summary_for(self, meeting_id: str) -> str:
        """What this process remembers about a meeting, or that it does not."""
        return self.runner.summary_for(meeting_id)

    # -- scheduling -------------------------------------------------------

    def _run_with_slot(self, payload: dict[str, Any]) -> None:
        if not self.runner.acquire_slot(timeout=self.slot_wait_s):
            log.warning(
                "no run slot for meeting %s after %.0fs; not started",
                meeting_id_of(payload),
                self.slot_wait_s,
            )
            return
        try:
            self.runner.handle_rtms_started(payload)
        except Exception:  # noqa: BLE001 - one run must not kill the worker
            log.exception("zoom run failed")
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

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "silkscreen-zoom"
        #: Applied to the socket by ``StreamRequestHandler.setup``.
        timeout = SOCKET_TIMEOUT_S

        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"error": "bad Content-Length"})
                return None
            if length > MAX_BODY_BYTES:
                # Not drained: this is before authentication, and reading a
                # declared gigabyte from an unverified client is exactly the
                # thread exhaustion this refuses to fund.
                self.close_connection = True
                self._send(413, {"error": "body too large"})
                return None
            return self.rfile.read(length) if length > 0 else b""

        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/healthz":
                self._send(200, {"ok": True, "service": "silkscreen-zoom"})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.split("?")[0] != "/zoom/events":
                self._send(404, {"error": "not found"})
                return
            body = self._read_body()
            if body is None:
                return
            code, response = dispatcher.handle_request(self.headers, body)
            self._send(code, response)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def make_server(
    config: Config, runner: ZoomRunner | None = None, *, port: int | None = None
) -> ThreadingHTTPServer:
    """Bind the surface. ``runner`` is the seam a test injects a fake through."""
    dispatcher = Dispatcher(config, runner or ZoomRunner(config))
    listen = port if port is not None else getattr(config, "port", DEFAULT_PORT)
    return ThreadingHTTPServer(("0.0.0.0", int(listen)), make_handler(dispatcher))


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
    log.info("listening on :%s (POST /zoom/events)", server.server_port)
    log.info(
        "speak mode %s; RTMS is receive-only, so audio out needs the headless "
        "Meeting SDK container in zoombot/bot/",
        config.speak_mode,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

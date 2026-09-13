"""The calling bot's local control surface — a skeleton, and it says so.

:class:`teamsbot.speak.CallingBotSpeaker` drives exactly two routes on this
process:

* ``GET  /healthz`` — is the container up?
* ``POST /say``     — ``{"meeting_id": "...", "text": "..."}``: say this in
  that meeting, out loud.

What is here is the *control plane*: the HTTP shape, the request validation and
a health check. What is **not** here is the media plane — joining a Teams call
as a participant, negotiating a media session and pushing audio frames into it.
That needs Microsoft's Graph Communications calling and media libraries
(``Microsoft.Graph.Communications.Calls.Media``), a tenant policy that permits a
bot to join meetings, and a deployment with a public callback endpoint and
media ports. None of that is vendored in this repository, and none of it has
ever been run against a live tenant from here. See ``README.md`` beside this
file.

So ``POST /say`` answers **501 Not Implemented**, naming what is missing. That
is a deliberate choice over three worse ones: returning 200 (which would let a
report claim the agent spoke in a meeting when no audio existed), returning
nothing (an unexplained timeout), or shipping a plausible-looking media stub
(code that pretends to work is the thing this repo refuses to write). A named
501 travels up through ``CallingBotSpeaker`` as ``SpeakError('bot_refused', …)``
and lands in the run report where a person will read it.

An operator who wires up a real media stack replaces :func:`say` — that is the
one function with a contract to keep, and the reason this file exists at all.

Run it::

    python -m teamsbot.bot.control          # or: docker run … (see Dockerfile)

**No authentication.** This surface is a loopback sidecar: it is expected to be
reachable only from the process that runs the bot, which is why
``teamsbot.speak.ensure_control_url`` refuses plaintext http to anything but
loopback. Exposing this port to a network would let anyone on it make the bot
speak in a meeting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("teamsbot.bot.control")

#: Matches ``teamsbot.speak.DEFAULT_CONTROL_URL``. Override with PORT.
DEFAULT_PORT = 8791

#: Bounded like every other body in this repo: this is a sentence, not a file.
MAX_BODY_BYTES = 64 * 1024

#: The one sentence an operator needs when the bot says nothing. It names the
#: missing piece rather than the symptom.
NOT_IMPLEMENTED = (
    "this container has no media backend: joining a Teams call and emitting "
    "audio needs Microsoft's Graph Communications calling/media libraries, an "
    "app registration with the calling permissions, and a tenant policy that "
    "allows the bot into meetings. None of that is vendored here. See "
    "teamsbot/bot/README.md. Nothing was said."
)


def say(meeting_id: str, text: str) -> None:
    """Say ``text`` out loud in ``meeting_id``. **Not implemented here.**

    Replace this with a call into a real media session to make the bot speak.
    Until then it raises, and the raise is reported — an agent that cannot
    speak must be loud about it, because the alternative is a report that reads
    as if it did.
    """
    raise NotImplementedError(NOT_IMPLEMENTED)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "silkscreen-teams-callingbot"
    timeout = 15.0

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path.split("?")[0] == "/healthz":
            # Honest health: the process is up, and it says in the same breath
            # that it cannot speak. A green health check that hides that would
            # be the most expensive kind of lie here.
            self._send(
                200,
                {
                    "ok": True,
                    "service": "silkscreen-teams-callingbot",
                    "can_speak": False,
                },
            )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path.split("?")[0] != "/say":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "bad Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "body was not JSON"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "expected a JSON object"})
            return
        meeting_id = str(payload.get("meeting_id", "")).strip()
        text = str(payload.get("text", "")).strip()
        if not meeting_id or not text:
            self._send(400, {"error": "meeting_id and text are both required"})
            return
        try:
            say(meeting_id, text)
        except NotImplementedError as exc:
            log.warning("refusing to pretend: %s", exc)
            self._send(501, {"error": str(exc), "said": False})
            return
        self._send(200, {"ok": True, "said": True})

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def main() -> int:  # pragma: no cover - not run by the test suite
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("calling-bot control surface on :%s (POST /say) — no media backend", port)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

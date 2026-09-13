"""The container's control surface, as a stub that refuses to pretend.

NOT RUN BY THE TEST SUITE. This file exists inside the image built from the
``Dockerfile`` beside it; nothing in ``zoombot/`` imports it, and no container
has been built from it in this repository.

It serves the three endpoints ``zoombot/speak.py``'s ``MeetingSdkSpeaker``
drives, so the protocol is written down in executable form, and it answers
every request to speak with ``{"spoken": false, "error": ...}`` because the
Zoom Meeting SDK participant is not implemented here. A stub that answered
``{"spoken": true}`` would be worse than no stub at all: the Python client
would report the line spoken aloud in a meeting where the room heard silence.

A real implementation replaces this with the participant built from
``zoom/meetingsdk-headless-linux-sample``, keeping the same endpoints and the
same rule -- ``spoken`` is true only after audio reached the SDK's raw audio
sender.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOT_IMPLEMENTED = (
    "the Zoom Meeting SDK participant is not implemented in this repository; "
    "see zoombot/bot/README.md"
)


class Handler(BaseHTTPRequestHandler):
    server_version = "zoombot-bot-stub/0"

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path == "/healthz":
            # ``joined: false`` is the truth: this stub never joins anything.
            self._reply(200, {"ok": True, "joined": False, "meeting_id": ""})
            return
        self._reply(404, {"error": "no such path"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path in {"/say", "/audio"}:
            self._reply(200, {"spoken": False, "error": NOT_IMPLEMENTED})
            return
        self._reply(404, {"error": "no such path"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Chat text would otherwise land in the container log.
        return


def main() -> None:
    port = int(os.environ.get("BOT_CONTROL_PORT", "8781"))
    # Loopback only: the surface is unauthenticated, and ``speak.py`` refuses
    # to drive a bot at any other host for the same reason.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()

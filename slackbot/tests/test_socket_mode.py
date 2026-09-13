"""Socket Mode against recorded frames, no network and no websocket library.

The ``events_api`` and ``hello`` frames below are copied verbatim from Slack's
own test server, ``slackapi/python-slack-sdk`` (``b9f4666``)
``tests/slack_sdk/socket_mode/mock_socket_mode_server.py``, so the parser is
checked against what Slack's maintainers recorded rather than a shape guessed
here.
"""

from __future__ import annotations

import json
import threading

import pytest

from slackbot.slack import HttpResponse, SlackError
from slackbot.socket_mode import (
    MAX_BACKOFF_S,
    SocketModeClient,
    SocketRequest,
    open_connection_url,
)

from .fakes import RecordingTransport

HELLO = """{"type":"hello","num_connections":2,"debug_info":{"host":"applink-111-xxx","build_number":10,"approximate_connection_time":18060},"connection_info":{"app_id":"A111"}}"""  # noqa: E501
APP_MENTION = """{"envelope_id":"cda4159a-72a5-4744-aba3-4d66eb52682b","payload":{"token":"verification-token","team_id":"T111","api_app_id":"A111","event":{"client_msg_id":"f0582a78-72db-4feb-b2f3-1e47d66365c8","type":"app_mention","text":"<@U111>","user":"U222","ts":"1610241741.000200","team":"T111","blocks":[{"type":"rich_text","block_id":"Sesm","elements":[{"type":"rich_text_section","elements":[{"type":"user","user_id":"U111"}]}]}],"channel":"C111","event_ts":"1610241741.000200"},"type":"event_callback","event_id":"Ev111","event_time":1610241741,"authorizations":[{"enterprise_id":null,"team_id":"T111","user_id":"U222","is_bot":true,"is_enterprise_install":false}],"is_ext_shared_channel":false,"event_context":"1-app_mention-T111-C111"},"type":"events_api","accepts_response_payload":false,"retry_attempt":0,"retry_reason":""}"""  # noqa: E501
DISCONNECT = """{"type":"disconnect","reason":"refresh_requested","debug_info":{"host":"applink-111"}}"""  # noqa: E501

WSS = {
    "apps.connections.open": {
        "ok": True,
        "url": "wss://wss-primary.slack.com/link/?ticket=abc",
    }
}


class ScriptedConnection:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    def send(self, text):
        self.sent.append(text)

    def recv(self):
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.closed = True


def test_open_uses_the_app_token_as_a_bearer_on_a_form_post():
    transport = RecordingTransport(WSS)
    url = open_connection_url("xapp-1-A111-abc", transport=transport)
    assert url.startswith("wss://")
    (request,) = transport.requests
    assert request.method == "POST"
    assert request.url == "https://slack.com/api/apps.connections.open"
    assert request.headers["Authorization"] == "Bearer xapp-1-A111-abc"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_a_bot_token_is_refused_before_any_request():
    transport = RecordingTransport(WSS)
    with pytest.raises(SlackError, match="xapp-"):
        open_connection_url("xoxb-123", transport=transport)
    assert transport.requests == []


def test_the_recorded_envelope_parses_field_for_field():
    request = SocketRequest.from_frame(json.loads(APP_MENTION))
    assert request is not None
    assert request.type == "events_api"
    assert request.envelope_id == "cda4159a-72a5-4744-aba3-4d66eb52682b"
    assert request.payload["event"]["type"] == "app_mention"
    assert request.retry_attempt == 0
    assert SocketRequest.from_frame(json.loads(HELLO)) is None


def test_every_envelope_is_acked_by_id_before_the_handler_runs():
    conn = ScriptedConnection([HELLO, APP_MENTION])
    order: list[str] = []

    def handler(request):
        # The ack is already on the wire when the handler sees the request.
        order.append("ack" if conn.sent else "no-ack")

    client = SocketModeClient(
        "xapp-1",
        handler,
        transport=RecordingTransport(WSS),
        connect=lambda url: conn,
        sleep=lambda s: None,
    )
    client.serve(threading.Event(), max_sessions=1)
    assert [json.loads(s) for s in conn.sent] == [
        {"envelope_id": "cda4159a-72a5-4744-aba3-4d66eb52682b"}
    ]
    assert order == ["ack"]
    assert conn.closed


def test_disconnect_reconnects_on_a_fresh_url_and_is_not_a_request():
    first = ScriptedConnection(
        [HELLO, DISCONNECT, APP_MENTION]
    )  # frame after is never read
    second = ScriptedConnection([HELLO, APP_MENTION])
    conns = [first, second]
    transport = RecordingTransport(WSS)
    seen = []
    client = SocketModeClient(
        "xapp-1",
        seen.append,
        transport=transport,
        connect=lambda url: conns.pop(0),
        sleep=lambda s: None,
    )
    client.serve(threading.Event(), max_sessions=2)
    assert len(seen) == 1 and first.sent == [] and len(second.sent) == 1
    assert sum("apps.connections.open" in r.url for r in transport.requests) == 2


def test_a_handler_that_raises_does_not_drop_the_socket():
    conn = ScriptedConnection(
        [HELLO, APP_MENTION, APP_MENTION.replace("cda4159a", "00000000")]
    )

    def boom(request):
        raise RuntimeError("bug")

    client = SocketModeClient(
        "xapp-1",
        boom,
        transport=RecordingTransport(WSS),
        connect=lambda url: conn,
        sleep=lambda s: None,
    )
    client.serve(threading.Event(), max_sessions=1)
    assert len(conn.sent) == 2


def test_connect_failures_back_off_exponentially_and_bad_tokens_are_fatal():
    sleeps: list[float] = []
    failing = RecordingTransport(
        {
            "apps.connections.open": HttpResponse(
                500, b'{"ok": false, "error": "internal_error"}'
            )
        }
    )
    client = SocketModeClient(
        "xapp-1",
        lambda r: None,
        transport=failing,
        connect=lambda url: ScriptedConnection([]),
        sleep=sleeps.append,
    )
    client.serve(threading.Event(), max_sessions=8)
    assert sleeps[:3] == [1.0, 2.0, 4.0]
    assert max(sleeps) <= MAX_BACKOFF_S

    revoked = RecordingTransport(
        {"apps.connections.open": {"ok": False, "error": "invalid_auth"}}
    )
    client = SocketModeClient(
        "xapp-1",
        lambda r: None,
        transport=revoked,
        connect=lambda url: ScriptedConnection([]),
        sleep=lambda s: None,
    )
    with pytest.raises(SlackError, match="invalid_auth"):
        client.serve(threading.Event())

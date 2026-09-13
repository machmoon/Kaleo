"""The Slack -> Ada bridge: filing an idea, and following it in the thread.

Two seams, both offline. Slack is :class:`RecordingTransport`. The engine is
either a scripted transport (for the long-running follow loop, where the test
owns every answer) or the *real* service on a loopback port
(``service/tests/test_app.py``'s fixture), because the inbox HTTP shape is the
contract between two processes and a fake of it would share this module's
assumptions about it.
"""

from __future__ import annotations

import json
from collections import deque

import pytest

from service import inbox as _inbox
from service.tests.test_app import server as _test_app_server  # noqa: F401
from slackbot import bridge as B
from slackbot.slack import HttpResponse, SlackClient
from slackbot.socket_mode import SocketRequest

from .fakes import RecordingTransport


@pytest.fixture
def server(_test_app_server):  # noqa: F811
    return _test_app_server


CONFIG = B.BridgeConfig(bot_token="xoxb-1", app_token="xapp-1")


def envelope(event, *, event_id="Ev1", retry=0):
    return SocketRequest(
        type="events_api",
        envelope_id="env-" + event_id,
        payload={
            "type": "event_callback",
            "team_id": "T1",
            "event_id": event_id,
            "event": event,
        },
        retry_attempt=retry,
    )


MENTION = {
    "type": "app_mention",
    "text": "<@UBOT> a 3.3V LDO board",
    "user": "U1",
    "channel": "C1",
    "ts": "100.1",
}
DM = {
    "type": "message",
    "channel_type": "im",
    "text": "a 555 blinker",
    "user": "U1",
    "channel": "D1",
    "ts": "200.2",
}


class ScriptedEngine:
    """Answers each engine route from a queue; the last answer repeats."""

    def __init__(self, routes):
        self.routes = {k: deque(v) for k, v in routes.items()}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        for fragment, answers in self.routes.items():
            if fragment in request.url:
                status, body = answers[0] if len(answers) == 1 else answers.popleft()
                if status == 0:
                    from slackbot.slack import SlackError

                    raise SlackError("network_error", "refused")
                return HttpResponse(status, json.dumps(body).encode())
        return HttpResponse(404, b'{"error": "no route"}')


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_bridge(engine_transport, *, config=CONFIG):
    slack = RecordingTransport()
    clock = FakeClock()
    ran = []
    bridge = B.Bridge(
        config,
        SlackClient("xoxb-1", transport=slack),
        B.EngineClient("http://127.0.0.1:8081", transport=engine_transport),
        spawn=lambda work: ran.append(work) or work(),
        sleep=clock.sleep,
        clock=clock,
    )
    return bridge, slack, ran


def said(slack):
    return [c["text"] for c in slack.calls("chat.postMessage")]


# ---------------------------------------------------------------- inbound


def test_config_needs_both_tokens_and_not_the_signing_secret_or_a_model_key():
    with pytest.raises(B.ConfigError) as exc:
        B.load_bridge_config({})
    assert "SLACK_BOT_TOKEN" in str(exc.value) and "SLACK_APP_TOKEN" in str(exc.value)
    config = B.load_bridge_config(
        {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1-x"}
    )
    assert config.engine_url == B.DEFAULT_ENGINE_URL
    assert "xapp" not in json.dumps(config.redacted())
    with pytest.raises(B.ConfigError, match="xapp-"):
        B.load_bridge_config({"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xoxb-2"})


def test_bots_edits_and_plain_channel_chatter_are_ignored():
    bridge, slack, ran = make_bridge(ScriptedEngine({}))
    bridge.handle(envelope({**MENTION, "bot_id": "B1"}, event_id="a"))
    bridge.handle(envelope({**DM, "subtype": "message_changed"}, event_id="b"))
    bridge.handle(envelope({**DM, "channel_type": "channel"}, event_id="c"))
    bridge.handle(SocketRequest(type="slash_commands", envelope_id="x", payload={}))
    assert ran == [] and slack.requests == []


def test_a_redelivered_message_is_filed_once():
    engine = ScriptedEngine({"/inbox": [(200, {"id": "idea_1", "state": "pending"})]})
    bridge, slack, ran = make_bridge(engine)
    bridge.handle(envelope(MENTION, event_id="Ev1"))
    bridge.handle(envelope(MENTION, event_id="Ev1", retry=1))
    # The same message as a DM-mention pair arrives under a different event id.
    bridge.handle(envelope({**MENTION, "type": "app_mention"}, event_id="Ev2"))
    assert len(ran) == 1


def test_users_outside_the_allow_list_are_told_and_nothing_is_filed():
    config = B.BridgeConfig("xoxb-1", "xapp-1", allowed_users=frozenset({"U9"}))
    engine = ScriptedEngine({})
    bridge, slack, _ = make_bridge(engine, config=config)
    bridge.handle(envelope(DM))
    assert engine.requests == []
    assert "allow list" in said(slack)[0]


def test_an_empty_mention_gets_help_not_a_run():
    engine = ScriptedEngine({})
    bridge, slack, _ = make_bridge(engine)
    bridge.handle(envelope({**MENTION, "text": "<@UBOT>"}))
    assert engine.requests == [] and "Tell me what to build" in said(slack)[0]


# ------------------------------------------------------- the hand-off, live


def test_a_mention_lands_in_the_real_service_inbox_threaded_and_keyed(
    server, monkeypatch
):
    monkeypatch.setattr(_inbox, "INBOX", _inbox.Inbox())
    slack = RecordingTransport()
    bridge = B.Bridge(
        CONFIG,
        SlackClient("xoxb-1", transport=slack),
        B.EngineClient(f"http://127.0.0.1:{server.server_port}"),
        spawn=lambda work: None,  # file it; the follow loop is tested below
    )
    monkeypatch.setattr(bridge, "watch", lambda *a, **k: None)
    idea_id = bridge.deliver(
        "a 3.3V LDO board",
        channel="C1",
        thread_ts="100.1",
        user="U1",
        key=B.message_key("T1", "C1", "100.1"),
    )
    assert idea_id
    idea = _inbox.INBOX.get(idea_id)
    assert idea.text == "a 3.3V LDO board"
    assert idea.reply_to == {"channel": "C1", "thread_ts": "100.1"}
    assert idea.key == "slack:T1:C1:100.1"
    (reply,) = slack.calls("chat.postMessage")
    assert reply["thread_ts"] == "100.1" and reply["text"].startswith("On it")


def test_an_engine_that_is_not_running_is_named_in_the_thread():
    bridge, slack, _ = make_bridge(ScriptedEngine({"/inbox": [(0, {})]}))
    assert bridge.deliver("x", channel="C1", thread_ts="1", user="U1", key="k") is None
    assert "silkscreen serve" in said(slack)[-1]


# ------------------------------------------------------------ following


def test_the_thread_hears_pickup_each_step_and_each_wait_once():
    engine = ScriptedEngine(
        {
            "/inbox/idea_1": [
                (200, {"state": "pending"}),
                (200, {"state": "accepted"}),
                (200, {"state": "started", "session": "sess1"}),
            ],
            "/steps/sess1": [
                (200, {"done": ["propose"], "next": ["place"], "background": []}),
                (200, {"done": ["propose"], "next": ["place"], "background": []}),
                (
                    200,
                    {
                        "done": ["place", "propose"],
                        "next": ["route", "case"],
                        "background": ["case"],
                    },
                ),
                (
                    200,
                    {
                        "done": ["place", "propose"],
                        "next": ["route", "case"],
                        "background": [],
                    },
                ),
                (
                    200,
                    {
                        "done": ["order", "place", "propose"],
                        "next": [],
                        "files": {"pcb": "x"},
                    },
                ),
            ],
        }
    )
    bridge, slack, _ = make_bridge(engine)
    bridge.watch("idea_1", channel="C1", thread_ts="100.1")
    assert said(slack) == [
        "Ada picked it up on the laptop and is starting the design.",
        "Design started on the laptop. You approve each step there; "
        "I'll post progress here.",
        "Done: propose.",
        "Waiting for you on the laptop — next: place.",
        "Done: place.",
        "Waiting for you on the laptop — next: route, case.",
        "Done: order.",
        "Every step has run on the laptop. Files: pcb.",
    ]
    assert all(c["thread_ts"] == "100.1" for c in slack.calls("chat.postMessage"))


def test_nobody_picking_it_up_is_said_once_and_expiry_ends_the_thread():
    answers = [(200, {"state": "pending"})] * 20 + [
        (
            200,
            {
                "state": "expired",
                "detail": "no Ada desktop accepted it within 30 minutes",
            },
        )
    ]
    bridge, slack, _ = make_bridge(ScriptedEngine({"/inbox/idea_1": answers}))
    bridge.watch("idea_1", channel="C1", thread_ts="1")
    texts = said(slack)
    assert sum("Still waiting" in t for t in texts) == 1
    assert (
        texts[-1]
        == "Ada didn't start this: no Ada desktop accepted it within 30 minutes."
    )


def test_a_restarted_engine_and_a_cancelled_run_both_end_in_words():
    bridge, slack, _ = make_bridge(ScriptedEngine({"/inbox/idea_1": [(404, {})]}))
    bridge.watch("idea_1", channel="C1", thread_ts="1")
    assert "restarted" in said(slack)[-1]

    engine = ScriptedEngine(
        {
            "/inbox/idea_1": [(200, {"state": "started", "session": "s"})],
            "/steps/s": [
                (200, {"done": ["propose"], "next": ["place"], "cancelled": True})
            ],
        }
    )
    bridge, slack, _ = make_bridge(engine)
    bridge.watch("idea_1", channel="C1", thread_ts="1")
    assert said(slack)[-1] == "The run was cancelled on the laptop."

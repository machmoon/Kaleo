"""The ``Speaker`` seam: who spoke, through what, and what a refusal looks like.

Offline: no network, no credentials, no container. Both transports are recorded
stand-ins, and the one test that exercises the real urllib transport drives it
through a fake opener rather than a socket.

The property under test throughout is the reason the seam exists: a speaker
that did not deliver anything must produce a **named** failure, never a quiet
return. "The agent replied" and "the agent spoke out loud in the meeting" are
different claims, and a report has to be able to tell them apart.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from teamsbot.config import Config
from teamsbot.graph import TeamsCallError, TeamsRefusedError
from teamsbot.speak import (
    DEFAULT_CONTROL_URL,
    MAX_MESSAGE_CHARS,
    CallingBotSpeaker,
    ChatSpeaker,
    NullSpeaker,
    SpeakError,
    UrllibBotTransport,
    ensure_control_url,
    is_audible,
    speaker_for,
)

CHAT_ID = "19:meeting_abc123@thread.v2"


def config(**overrides) -> Config:
    values = {
        "app_id": "11111111-2222-3333-4444-555555555555",
        "app_secret": "s3cr3t-value",
        "tenant_id": "66666666-7777-8888-9999-000000000000",
        "speak_mode": "chat",
    }
    values.update(overrides)
    return Config(**values)


class RecordedTransport:
    """Every request that was built, and canned answers for each.

    Recording the *request* is the point: the assertions below are about the
    URL, the headers and the body this package constructs, which is exactly
    what a live call would have sent.
    """

    def __init__(self, *answers: tuple[int, bytes]):
        self.answers = list(answers) or [(201, b"{}")]
        self.calls: list[tuple[str, dict, bytes]] = []

    def post(self, url, headers, body):
        self.calls.append((url, dict(headers), body))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def get(self, url, headers):  # pragma: no cover - ChatSpeaker never GETs
        raise AssertionError("ChatSpeaker made a GET")


class FakeClient:
    """A GraphClient stand-in: a token, and nothing that touches a network."""

    def __init__(self, token="app-token", raises=None):
        self.token = token
        self.raises = raises
        self.transport = None

    def access_token(self):
        if self.raises is not None:
            raise self.raises
        return self.token


def chat_speaker(transport, *, client=None, **config_kwargs) -> ChatSpeaker:
    return ChatSpeaker(
        config(**config_kwargs), transport=transport, client=client or FakeClient()
    )


# -- speaker_for ----------------------------------------------------------


def test_speaker_for_picks_one_speaker_per_mode():
    assert isinstance(speaker_for(config(speak_mode="off")), NullSpeaker)
    assert isinstance(
        speaker_for(config(speak_mode="chat"), transport=RecordedTransport()),
        ChatSpeaker,
    )
    sdk = speaker_for(
        config(speak_mode="sdk", bot_endpoint="https://bot.example/api/calls"),
        bot_transport=RecordedTransport(),
    )
    assert isinstance(sdk, CallingBotSpeaker)


def test_every_speaker_names_itself_and_no_two_names_collide():
    # The report prints this as ``spoke_via``. If two speakers shared a name,
    # "replied in the chat" and "spoke out loud" would be indistinguishable in
    # the one place a person actually reads.
    names = {
        NullSpeaker().name,
        ChatSpeaker(config(), transport=RecordedTransport(), client=FakeClient()).name,
        CallingBotSpeaker(transport=RecordedTransport()).name,
    }
    assert names == {"null", "chat", "calling-bot"}


def test_an_unknown_speak_mode_raises_rather_than_falling_back():
    # config.py rejects this at construction, so only a Config assembled
    # around the validator can get here -- and it must still not degrade
    # silently into saying nothing.
    broken = object.__new__(Config)
    object.__setattr__(broken, "speak_mode", "megaphone")
    with pytest.raises(SpeakError) as exc:
        speaker_for(broken)
    assert exc.value.code == "bad_speak_mode"


# -- ChatSpeaker ----------------------------------------------------------


def test_chat_speaker_posts_a_text_message_to_the_meeting_chat():
    transport = RecordedTransport((201, b'{"id":"1"}'))
    chat_speaker(transport).say(CHAT_ID, "Board is routed; 2 nets left as ratsnest.")

    url, headers, body = transport.calls[0]
    assert url == (
        "https://graph.microsoft.com/v1.0/chats/"
        "19%3Ameeting_abc123%40thread.v2/messages"
    )
    assert headers["Authorization"] == "Bearer app-token"
    payload = json.loads(body)
    # text, not html: a board summary carries angle brackets and net names, and
    # plain text cannot be mis-rendered or inject markup into a chat.
    assert payload["body"]["contentType"] == "text"
    assert "ratsnest" in payload["body"]["content"]


def test_chat_speaker_refuses_an_off_allowlist_host_before_sending_anything():
    transport = RecordedTransport()
    speaker = chat_speaker(transport, graph_base="https://graph.evil.example/v1.0")
    with pytest.raises(SpeakError) as exc:
        speaker.say(CHAT_ID, "hello")
    assert exc.value.code == "graph_error"
    assert "evil.example" in str(exc.value)
    # The refusal is at request-construction time, so it holds for every
    # transport including this fake: no byte, and no bearer token, was offered.
    assert transport.calls == []


def test_the_url_check_is_the_graph_allowlist_itself():
    speaker = chat_speaker(
        RecordedTransport(), graph_base="https://graph.evil.example/v1.0"
    )
    with pytest.raises(TeamsRefusedError):
        speaker.message_url(CHAT_ID)


def test_chat_speaker_refuses_a_redirect_rather_than_following_it():
    transport = RecordedTransport((302, b""))
    with pytest.raises(SpeakError) as exc:
        chat_speaker(transport).say(CHAT_ID, "hello")
    assert exc.value.code == "redirect_refused"
    # Not followed: one request was made, and the 3xx was not treated as
    # evidence that anything was posted.
    assert len(transport.calls) == 1


def test_chat_speaker_reports_graphs_own_error_message():
    body = json.dumps(
        {"error": {"code": "Forbidden", "message": "no application access policy"}}
    ).encode()
    with pytest.raises(SpeakError) as exc:
        chat_speaker(RecordedTransport((403, body))).say(CHAT_ID, "hello")
    assert exc.value.code == "graph_error"
    assert "no application access policy" in str(exc.value)


def test_a_token_failure_is_a_speak_failure_not_a_silence():
    speaker = chat_speaker(
        RecordedTransport(), client=FakeClient(raises=TeamsCallError("login refused"))
    )
    with pytest.raises(SpeakError) as exc:
        speaker.say(CHAT_ID, "hello")
    assert exc.value.code == "graph_error"


def test_chat_speaker_needs_somewhere_to_post():
    with pytest.raises(SpeakError) as exc:
        chat_speaker(RecordedTransport()).say("", "hello")
    assert exc.value.code == "no_chat"


# -- CallingBotSpeaker ----------------------------------------------------


def test_an_unreachable_container_is_a_named_failure():
    """The container is the *expected* absence, and must never be a no-op."""

    class DeadOpener:
        def open(self, request, timeout=None):
            raise urllib.error.URLError("Connection refused")

    transport = UrllibBotTransport()
    transport.opener = DeadOpener()
    with pytest.raises(SpeakError) as exc:
        CallingBotSpeaker(transport=transport).say(CHAT_ID, "hello")
    assert exc.value.code == "bot_unreachable"
    assert "8791" in str(exc.value)


def test_the_containers_501_is_reported_with_its_own_sentence():
    # teamsbot/bot/control.py answers 501 naming the missing media stack. That
    # sentence is what an operator needs, so it travels intact.
    body = json.dumps({"error": "no media backend: see README", "said": False}).encode()
    transport = RecordedTransport((501, body))
    with pytest.raises(SpeakError) as exc:
        CallingBotSpeaker(transport=transport).say(CHAT_ID, "hello")
    assert exc.value.code == "bot_refused"
    assert "no media backend" in str(exc.value)


def test_a_working_container_says_it_and_returns():
    transport = RecordedTransport((200, b'{"ok":true,"said":true}'))
    CallingBotSpeaker(transport=transport).say(CHAT_ID, "hello")
    url, _, body = transport.calls[0]
    assert url == f"{DEFAULT_CONTROL_URL}/say"
    assert json.loads(body) == {"meeting_id": CHAT_ID, "text": "hello"}


def test_a_redirect_from_the_container_is_not_a_delivered_utterance():
    with pytest.raises(SpeakError) as exc:
        CallingBotSpeaker(transport=RecordedTransport((307, b""))).say(CHAT_ID, "hi")
    assert exc.value.code == "redirect_refused"


def test_the_control_surface_may_not_be_plaintext_off_loopback():
    assert ensure_control_url("http://127.0.0.1:8791") == "http://127.0.0.1:8791"
    assert ensure_control_url("https://bot.internal:8791") == "https://bot.internal:8791"
    for bad in ("http://bot.internal:8791", "ftp://127.0.0.1", "http://10.0.0.5"):
        with pytest.raises(SpeakError) as exc:
            ensure_control_url(bad)
        assert exc.value.code == "bad_host"


def test_a_bad_control_url_is_refused_at_construction():
    with pytest.raises(SpeakError):
        CallingBotSpeaker("http://elsewhere.example:8791")


# -- NullSpeaker and shared rules ------------------------------------------


def test_null_speaker_keeps_the_receipts():
    speaker = NullSpeaker()
    speaker.say(CHAT_ID, "would have said this")
    assert [(u.meeting_id, u.text) for u in speaker.said] == [
        (CHAT_ID, "would have said this")
    ]
    assert speaker.name == "null"


@pytest.mark.parametrize(
    "speaker_factory",
    [
        lambda: NullSpeaker(),
        lambda: CallingBotSpeaker(transport=RecordedTransport()),
        lambda: chat_speaker(RecordedTransport()),
    ],
)
def test_no_speaker_sends_nothing_or_half_a_message(speaker_factory):
    speaker = speaker_factory()
    with pytest.raises(SpeakError) as empty:
        speaker.say(CHAT_ID, "   ")
    assert empty.value.code == "empty"
    with pytest.raises(SpeakError) as long:
        speaker.say(CHAT_ID, "x" * (MAX_MESSAGE_CHARS + 1))
    # Refused whole rather than truncated: a silently half-sent answer is worse
    # than a stated refusal.
    assert long.value.code == "too_long"


# ---------------------------------------------------------------------------
# "The agent replied" must never read as "the agent spoke out loud"
# ---------------------------------------------------------------------------


def test_a_200_without_an_affirmative_said_is_still_a_refusal():
    """The gap this closes: a container that answers 200 `{"said": false}`.

    Until 2026-09-08 CallingBotSpeaker checked only the HTTP status, so such a
    body put `spoke_via: "calling-bot"` on a report for a room that heard
    silence. zoombot's MeetingSdkSpeaker has always required the equivalent
    affirmative (`payload["spoken"]`).
    """
    for body in (
        b'{"ok": true, "said": false}',
        b'{"ok": true}',  # no field at all
        b"",  # not JSON
        b'"said"',  # JSON, but not an object
        b'{"said": "true"}',  # a string is not an affirmative
    ):
        transport = RecordedTransport((200, body))
        with pytest.raises(SpeakError) as exc:
            CallingBotSpeaker(transport=transport).say(CHAT_ID, "hello")
        assert exc.value.code == "bot_refused", body
        assert "did not report the line spoken" in str(exc.value), body


def test_only_the_calling_bot_is_audible_and_it_is_read_off_the_name():
    """AUDIBLE_SPEAKERS is a whitelist; a speaker cannot promote itself."""
    assert is_audible("calling-bot")
    assert not is_audible("chat")
    assert not is_audible("null")
    assert not is_audible("none")  # the runner's "nothing was said at all"
    assert not is_audible("")


def test_the_chat_speaker_is_not_audible_even_when_it_succeeds():
    """Posting into the meeting chat is delivery, not sound."""
    speaker = chat_speaker(RecordedTransport((201, b"{}")))
    speaker.say(CHAT_ID, "the board is drafted")
    assert not is_audible(speaker.name)


def test_nothing_in_this_tree_can_report_speaking_out_loud():
    """The end-to-end property, asserted rather than reasoned about.

    Every speaker that can succeed here is inaudible, and the only audible one
    cannot succeed, because the container behind it refuses. So no run this
    repo can produce may report audible speech — and if someone later builds
    the media backend, this test fails and asks to be rewritten deliberately.
    """
    from teamsbot.bot import control

    # The audible speaker's own backend refuses in words.
    with pytest.raises(NotImplementedError) as exc:
        control.say(CHAT_ID, "hello")
    assert "not" in str(exc.value).lower()

    # And the speakers that do work are not in the whitelist.
    assert not is_audible(chat_speaker(RecordedTransport()).name)
    assert not is_audible(NullSpeaker().name)

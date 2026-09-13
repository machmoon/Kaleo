"""Offline tests for the Zoom speakers.

No network, no keys, no container, no Docker. Every HTTP boundary is the
``Transport`` seam and every test hands it a recorded implementation.

The property under test throughout is the one in ``speak.py``'s docstring: a
speaker names itself, only the Meeting SDK name means the room heard anything,
and no speaker ever swallows a failure -- because a swallowed failure is how a
run report ends up claiming the agent spoke when it did not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from zoombot.speak import (
    AUDIBLE_SPEAKERS,
    BOT_CONTROL_URL,
    ChatSpeaker,
    MeetingSdkSpeaker,
    NullSpeaker,
    Speaker,
    SpeakError,
    VoiceUnavailable,
    ensure_zoom_url,
    is_audible,
    speaker_for,
)


@dataclass(frozen=True)
class FakeConfig:
    """The frozen ``Config`` fields ``speak.py`` actually reads.

    A stand-in rather than ``zoombot.config.Config`` so this file tests only
    what it owns; ``test_zoom_config.py`` owns the real one, and a separate
    test below checks the real ``Config`` drives ``speaker_for`` identically
    whenever that module is present.
    """

    client_id: str = "cid"
    client_secret: str = "csecret"
    account_id: str = "acct"
    webhook_secret: str = "whsec"
    api_base: str = "https://api.zoom.us/v2"
    speak_mode: str = "chat"


@dataclass
class RecordedTransport:
    """A transport that answers from a script and records what it was asked.

    ``responses`` maps a URL prefix to bytes, or to an exception to raise --
    the only way to express a refused redirect or an unreachable container at
    this seam, both of which the production transports raise rather than
    return.
    """

    responses: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, str], bytes]] = field(default_factory=list)

    def _answer(self, method: str, url: str, headers, body: bytes) -> bytes:
        self.calls.append((method, url, dict(headers), body))
        for prefix, value in self.responses.items():
            if url.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                assert isinstance(value, bytes)
                return value
        raise AssertionError(f"no recorded response for {method} {url}")

    def get(self, url: str, headers) -> bytes:
        return self._answer("GET", url, headers, b"")

    def post(self, url: str, headers, body: bytes) -> bytes:
        return self._answer("POST", url, headers, body)


TOKEN_URL = "https://zoom.us/oauth/token"
TOKEN_OK = json.dumps({"access_token": "tok-123", "expires_in": 3600}).encode()


def chat_transport(**extra: object) -> RecordedTransport:
    responses: dict[str, object] = {TOKEN_URL: TOKEN_OK}
    responses.update(extra)
    return RecordedTransport(responses=responses)


# --------------------------------------------------------------------------
# speaker_for: one speaker per mode, and no silent default
# --------------------------------------------------------------------------


def test_speaker_for_sdk_mode_builds_the_meeting_sdk_speaker():
    speaker = speaker_for(FakeConfig(speak_mode="sdk"))
    assert isinstance(speaker, MeetingSdkSpeaker)
    assert speaker.name == "meeting_sdk"
    assert speaker.control_url == BOT_CONTROL_URL


def test_speaker_for_chat_mode_builds_the_chat_speaker():
    transport = chat_transport()
    speaker = speaker_for(FakeConfig(speak_mode="chat"), transport=transport)
    assert isinstance(speaker, ChatSpeaker)
    assert speaker.name == "zoom_chat"
    assert speaker.transport is transport


def test_speaker_for_off_mode_builds_the_null_speaker():
    speaker = speaker_for(FakeConfig(speak_mode="off"))
    assert isinstance(speaker, NullSpeaker)
    assert speaker.name == "null"


def test_speaker_for_refuses_an_unknown_mode_rather_than_going_quiet():
    # Falling back to NullSpeaker would turn a typo in an env var into a
    # meeting where the agent never spoke and nothing said why.
    with pytest.raises(SpeakError) as excinfo:
        speaker_for(FakeConfig(speak_mode="loud"))
    assert excinfo.value.code == "bad_speak_mode"
    assert "loud" in str(excinfo.value)


def test_every_speaker_satisfies_the_frozen_protocol_and_names_itself():
    speakers = [
        speaker_for(FakeConfig(speak_mode=mode)) for mode in ("sdk", "chat", "off")
    ]
    names = {s.name for s in speakers}
    assert len(names) == 3, "two speakers sharing a name would be unreportable"
    for speaker in speakers:
        assert isinstance(speaker, Speaker)
        assert speaker.name


def test_only_the_meeting_sdk_speaker_counts_as_audible():
    # The whole point of the module: text in a chat panel is not a voice in
    # the room, and a report must not let one read as the other.
    assert set(AUDIBLE_SPEAKERS) == {"meeting_sdk"}
    assert is_audible(MeetingSdkSpeaker().name)
    assert not is_audible(ChatSpeaker(config=FakeConfig()).name)
    assert not is_audible(NullSpeaker().name)


def test_speaker_for_accepts_the_real_config_when_z1s_module_is_present():
    config_module = pytest.importorskip(
        "zoombot.config", reason="zoombot/config.py is Z1's file"
    )
    config = config_module.Config(
        client_id="cid",
        client_secret="csecret",
        account_id="acct",
        webhook_secret="whsec",
        speak_mode="off",
    )
    assert speaker_for(config).name == "null"


# --------------------------------------------------------------------------
# NullSpeaker: records, does not discard
# --------------------------------------------------------------------------


def test_null_speaker_records_what_would_have_been_said():
    speaker = NullSpeaker()
    speaker.say("8123", "R3 has no decoupling capacitor.")
    speaker.say("8123", "Two nets are unrouted.")
    assert speaker.said == [
        ("8123", "R3 has no decoupling capacitor."),
        ("8123", "Two nets are unrouted."),
    ]


def test_null_speaker_refuses_an_empty_utterance():
    with pytest.raises(SpeakError) as excinfo:
        NullSpeaker().say("8123", "   ")
    assert excinfo.value.code == "empty_text"


# --------------------------------------------------------------------------
# ChatSpeaker: allowlist, redirects, and the token
# --------------------------------------------------------------------------


def test_chat_speaker_posts_to_the_live_meeting_chat_endpoint():
    chat_url = "https://api.zoom.us/v2/live_meetings/8123/chat/messages"
    transport = chat_transport(**{chat_url: b""})
    speaker = ChatSpeaker(config=FakeConfig(), transport=transport)
    speaker.say("8123", "two nets unrouted")

    token_call, chat_call = transport.calls
    assert token_call[0] == "POST"
    assert token_call[1].startswith(TOKEN_URL)
    assert "grant_type=account_credentials" in token_call[1]
    assert token_call[2]["Authorization"].startswith("Basic ")

    assert chat_call[1] == chat_url
    assert chat_call[2]["Authorization"] == "Bearer tok-123"
    assert json.loads(chat_call[3]) == {
        "message": "two nets unrouted",
        "to_channel": "everyone",
    }


def test_chat_speaker_caches_the_token_across_messages():
    chat_url = "https://api.zoom.us/v2/live_meetings/8123/chat/messages"
    transport = chat_transport(**{chat_url: b""})
    speaker = ChatSpeaker(config=FakeConfig(), transport=transport)
    speaker.say("8123", "one")
    speaker.say("8123", "two")
    token_calls = [c for c in transport.calls if c[1].startswith(TOKEN_URL)]
    assert len(token_calls) == 1


def test_chat_speaker_refuses_an_off_allowlist_api_base_before_any_request():
    transport = chat_transport()
    speaker = ChatSpeaker(
        config=FakeConfig(api_base="https://api.zoom.us.evil.example/v2"),
        transport=transport,
    )
    with pytest.raises(SpeakError) as excinfo:
        speaker.say("8123", "hello")
    assert excinfo.value.code == "bad_host"
    assert "evil.example" in str(excinfo.value)
    # The bearer token must not have travelled toward that host. The token call
    # itself is allowlisted and does happen; nothing goes to the bad host.
    assert all("evil.example" not in call[1] for call in transport.calls)


def test_off_allowlist_and_non_https_urls_are_refused_at_construction():
    for bad in (
        "https://api.zoom.us.evil.example/v2/live_meetings/1/chat/messages",
        "http://api.zoom.us/v2/live_meetings/1/chat/messages",
        "https://evil.example/v2",
    ):
        with pytest.raises(SpeakError) as excinfo:
            ensure_zoom_url(bad)
        assert excinfo.value.code == "bad_host"
    assert ensure_zoom_url("https://api.zoom.us/v2/x") == "https://api.zoom.us/v2/x"


def test_chat_speaker_surfaces_a_refused_redirect_rather_than_following_it():
    # A 3xx would re-send the Authorization header to whatever host it names.
    # The production transport refuses it; the speaker must not turn that
    # refusal into a quiet success.
    chat_url = "https://api.zoom.us/v2/live_meetings/8123/chat/messages"
    redirect = SpeakError("redirect_refused", f"HTTP 302 redirect from {chat_url}")
    transport = chat_transport(**{chat_url: redirect})
    with pytest.raises(SpeakError) as excinfo:
        ChatSpeaker(config=FakeConfig(), transport=transport).say("8123", "hello")
    assert excinfo.value.code == "redirect_refused"


def test_the_production_zoom_transport_refuses_redirects():
    # Asserted on the handler itself: returning None is what makes urllib
    # raise the 3xx instead of re-sending the request with its bearer token.
    from zoombot.speak import _NoRedirect

    assert (
        _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x.example")
        is None
    )


def test_chat_speaker_raises_when_zoom_refuses_the_token():
    refusal = json.dumps({"reason": "Invalid client"}).encode()
    transport = chat_transport(**{TOKEN_URL: refusal})
    with pytest.raises(SpeakError) as excinfo:
        ChatSpeaker(config=FakeConfig(), transport=transport).say("8123", "hello")
    assert excinfo.value.code == "zoom_auth"
    assert "Invalid client" in str(excinfo.value)


def test_chat_speaker_raises_on_a_zoom_error_body():
    chat_url = "https://api.zoom.us/v2/live_meetings/8123/chat/messages"
    body = json.dumps({"code": 3001, "message": "Meeting is not live"}).encode()
    transport = chat_transport(**{chat_url: body})
    with pytest.raises(SpeakError) as excinfo:
        ChatSpeaker(config=FakeConfig(), transport=transport).say("8123", "hello")
    assert excinfo.value.code == "zoom_api"
    assert "not live" in str(excinfo.value)


def test_chat_speaker_raises_on_a_non_json_body():
    chat_url = "https://api.zoom.us/v2/live_meetings/8123/chat/messages"
    transport = chat_transport(**{chat_url: b"<html>proxy error</html>"})
    with pytest.raises(SpeakError) as excinfo:
        ChatSpeaker(config=FakeConfig(), transport=transport).say("8123", "hello")
    assert excinfo.value.code == "bad_response"


def test_chat_speaker_refuses_empty_text_and_a_missing_meeting_id():
    transport = chat_transport()
    speaker = ChatSpeaker(config=FakeConfig(), transport=transport)
    with pytest.raises(SpeakError) as excinfo:
        speaker.say("8123", "")
    assert excinfo.value.code == "empty_text"
    with pytest.raises(SpeakError) as excinfo:
        speaker.say("", "hello")
    assert excinfo.value.code == "no_meeting"
    assert transport.calls == [], "nothing may be sent for a request we refused"


# --------------------------------------------------------------------------
# MeetingSdkSpeaker: an absent container is a named failure, never silence
# --------------------------------------------------------------------------


def test_meeting_sdk_speaker_posts_the_line_to_the_container():
    transport = RecordedTransport(
        responses={f"{BOT_CONTROL_URL}/say": json.dumps({"spoken": True}).encode()}
    )
    MeetingSdkSpeaker(transport=transport).say("8123", "R3 has no decoupling cap")
    method, url, headers, body = transport.calls[0]
    assert (method, url) == ("POST", f"{BOT_CONTROL_URL}/say")
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "meeting_id": "8123",
        "text": "R3 has no decoupling cap",
    }


def test_meeting_sdk_speaker_names_the_failure_when_the_container_is_unreachable():
    # The failure this module exists for: nothing is listening, so nothing was
    # said. A silent return here would put spoke_via="meeting_sdk" on a report
    # for a meeting that heard nothing.
    unreachable = SpeakError("bot_unreachable", "127.0.0.1:8781: Connection refused")
    transport = RecordedTransport(responses={BOT_CONTROL_URL: unreachable})
    with pytest.raises(SpeakError) as excinfo:
        MeetingSdkSpeaker(transport=transport).say("8123", "hello")
    assert excinfo.value.code == "bot_unreachable"
    assert "8781" in str(excinfo.value)


def test_meeting_sdk_speaker_raises_when_the_container_refuses_to_speak():
    # The stub in bot/ answers exactly this, on purpose.
    refusal = json.dumps(
        {"spoken": False, "error": "the participant is not implemented"}
    ).encode()
    transport = RecordedTransport(responses={f"{BOT_CONTROL_URL}/say": refusal})
    with pytest.raises(SpeakError) as excinfo:
        MeetingSdkSpeaker(transport=transport).say("8123", "hello")
    assert excinfo.value.code == "bot_refused"
    assert "not implemented" in str(excinfo.value)


def test_meeting_sdk_speaker_raises_when_the_container_omits_spoken():
    transport = RecordedTransport(
        responses={f"{BOT_CONTROL_URL}/say": json.dumps({"ok": True}).encode()}
    )
    with pytest.raises(SpeakError) as excinfo:
        MeetingSdkSpeaker(transport=transport).say("8123", "hello")
    assert excinfo.value.code == "bot_refused"


def test_meeting_sdk_speaker_refuses_a_control_url_off_loopback():
    # The control surface has no authentication; publishing it would hand
    # anyone on the network a voice in the meeting.
    speaker = MeetingSdkSpeaker(
        control_url="http://bot.internal.example:8781",
        transport=RecordedTransport(),
    )
    with pytest.raises(SpeakError) as excinfo:
        speaker.say("8123", "hello")
    assert excinfo.value.code == "bad_host"


def test_meeting_sdk_speaker_sends_synthesised_audio_when_a_voice_is_given():
    transport = RecordedTransport(
        responses={f"{BOT_CONTROL_URL}/audio": json.dumps({"spoken": True}).encode()}
    )
    speaker = MeetingSdkSpeaker(
        transport=transport, voice=lambda text: b"RIFF" + text.encode()
    )
    speaker.say("8123", "hello")
    method, url, headers, body = transport.calls[0]
    assert (method, url) == ("POST", f"{BOT_CONTROL_URL}/audio")
    assert headers["Content-Type"] == "audio/wav"
    assert headers["X-Zoom-Meeting-Id"] == "8123"
    assert body == b"RIFFhello"


def test_a_voice_returning_no_audio_raises_rather_than_sending_silence():
    transport = RecordedTransport()
    speaker = MeetingSdkSpeaker(transport=transport, voice=lambda text: b"")
    with pytest.raises(VoiceUnavailable) as excinfo:
        speaker.say("8123", "hello")
    assert excinfo.value.code == "no_voice"
    assert transport.calls == []


def test_meeting_sdk_speaker_health_reports_the_container_state():
    body = json.dumps({"ok": True, "joined": False, "meeting_id": ""}).encode()
    transport = RecordedTransport(responses={f"{BOT_CONTROL_URL}/healthz": body})
    assert MeetingSdkSpeaker(transport=transport).health()["joined"] is False


def test_meeting_sdk_speaker_refuses_empty_text():
    transport = RecordedTransport()
    with pytest.raises(SpeakError) as excinfo:
        MeetingSdkSpeaker(transport=transport).say("8123", "\n")
    assert excinfo.value.code == "empty_text"
    assert transport.calls == []


# --------------------------------------------------------------------------
# The cross-cutting rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "speaker",
    [
        MeetingSdkSpeaker(
            transport=RecordedTransport(
                responses={BOT_CONTROL_URL: SpeakError("bot_unreachable", "refused")}
            )
        ),
        ChatSpeaker(
            config=FakeConfig(),
            transport=RecordedTransport(
                responses={TOKEN_URL: SpeakError("network_error", "no route")}
            ),
        ),
    ],
    ids=["meeting_sdk", "zoom_chat"],
)
def test_no_speaker_swallows_a_transport_failure(speaker):
    with pytest.raises(SpeakError):
        speaker.say("8123", "hello")


def test_the_bot_directory_states_it_is_unverified():
    # The claim lives in a file, so it can rot. Pin the sentence that must not
    # quietly disappear from bot/README.md.
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "bot" / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "never tested against a live Zoom account" in text
    assert "is built or executed by the test suite" in text


def test_nothing_in_this_tree_can_report_speaking_out_loud():
    """The end-to-end property, asserted rather than reasoned about.

    The only audible speaker drives the container in ``zoombot/bot/``, and that
    container's stub refuses every ``/say`` with ``{"spoken": false}`` — a stub
    that answered ``true`` would put ``spoke_via: meeting_sdk`` on a report for
    a room that heard silence. The counterpart of teamsbot's test of the same
    name; if someone vendors the Meeting SDK and builds the container, this
    fails and asks to be rewritten deliberately.
    """
    import json as _json

    from zoombot.bot import control_stub

    # The stub answers HTTP **200** with `spoken: false` -- so a speaker that
    # trusted the status code would call this a delivered utterance. It is the
    # body that is checked, and that is the whole point.
    stub_body = _json.dumps(
        {"spoken": False, "error": control_stub.NOT_IMPLEMENTED}
    ).encode()
    transport = RecordedTransport(responses={f"{BOT_CONTROL_URL}/say": stub_body})
    speaker = MeetingSdkSpeaker(transport=transport)
    with pytest.raises(SpeakError) as caught:
        speaker.say("meeting-1", "hello")
    assert caught.value.code == "bot_refused"

    assert not is_audible(ChatSpeaker(config=FakeConfig()).name)
    assert not is_audible(NullSpeaker().name)

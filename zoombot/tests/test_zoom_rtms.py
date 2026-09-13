"""Offline tests for the Zoom webhook verifier and the RTMS ingest.

Every one of these runs with no network, no keys and no WebSocket library: the
media stream is a recorded :class:`FakeConnection` handed in through the
``connect=`` seam, exactly as ``meetings/tests`` drive ``meet.py`` through a
recorded transport.

The signature and handshake tests deliberately compute their expected HMAC
inline, with ``hmac``/``hashlib`` directly, rather than calling the module's own
helper. A check written in terms of the code under test shares its blind spot --
the same reason ``engine/tests/test_kicad.py`` computes overlap independently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import pytest

from zoombot.config import Config
from zoombot.rtms import (
    MEDIA_TYPE_TRANSCRIPT,
    MSG_TYPE,
    STATUS_OK,
    TERMINAL_SESSION_STATES,
    TERMINAL_STREAM_STATES,
    EmptyStreamError,
    FrameError,
    HandshakeError,
    NotAuthorisedError,
    PayloadError,
    RtmsError,
    SeenEvents,
    StreamInterruptedError,
    TranscriptChunk,
    WebhookError,
    check_url,
    open_stream,
    url_validation_reply,
    verify_webhook,
)

SECRET = "webhook-secret-token"
NOW = 1_700_000_000.0


def make_config(**overrides) -> Config:
    values = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "account_id": "account-id",
        "webhook_secret": SECRET,
    }
    values.update(overrides)
    return Config(**values)


def sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    """Zoom's v0 scheme, computed here rather than borrowed from the module."""
    base = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def headers_for(body: bytes, *, at: float = NOW, secret: str = SECRET, **extra):
    timestamp = str(int(at))
    out = {
        "X-Zm-Request-Timestamp": timestamp,
        "X-Zm-Signature": sign(body, timestamp, secret),
        "x-zm-trackingid": "track-1",
    }
    out.update(extra)
    return out


def event_body(**overrides) -> bytes:
    payload = {
        "event": "meeting.rtms_started",
        "event_ts": 1_700_000_000_000,
        "payload": {
            "operator_id": "op",
            "object": {
                "meeting_uuid": "uuid-1",
                "rtms_stream_id": "stream-1",
                "server_urls": "wss://rtms-signal.zoom.us:443",
            },
        },
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


# -- webhook verification ---------------------------------------------------


def test_url_validation_matches_a_hand_computed_hmac():
    plain = "qgg8vlvZRS6UYooatFL8Aw"
    challenge = {"event": "endpoint.url_validation", "payload": {"plainToken": plain}}
    reply = url_validation_reply(make_config(), challenge)
    expected = hmac.new(
        SECRET.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert reply == {"plainToken": plain, "encryptedToken": expected}


def test_url_validation_without_a_plain_token_raises_rather_than_replying():
    with pytest.raises(WebhookError) as caught:
        url_validation_reply(make_config(), {"event": "endpoint.url_validation"})
    assert caught.value.reason == "no_plain_token"


def test_a_good_signature_parses_the_body():
    body = event_body()
    payload = verify_webhook(
        make_config(), headers_for(body), body, now=NOW, seen=SeenEvents()
    )
    assert payload["event"] == "meeting.rtms_started"


def test_a_wrong_signature_is_refused():
    body = event_body()
    headers = headers_for(body, secret="not-the-secret")
    with pytest.raises(WebhookError) as caught:
        verify_webhook(make_config(), headers, body, now=NOW, seen=SeenEvents())
    assert caught.value.reason == "bad_signature"
    assert caught.value.status == 401


def test_a_body_changed_after_signing_is_refused():
    body = event_body()
    headers = headers_for(body)
    with pytest.raises(WebhookError) as caught:
        verify_webhook(
            make_config(), headers, body + b" ", now=NOW, seen=SeenEvents()
        )
    assert caught.value.reason == "bad_signature"


def test_a_stale_timestamp_is_refused_even_with_a_valid_signature():
    body = event_body()
    headers = headers_for(body, at=NOW - 600)
    with pytest.raises(WebhookError) as caught:
        verify_webhook(make_config(), headers, body, now=NOW, seen=SeenEvents())
    assert caught.value.reason == "stale"


def test_a_millisecond_timestamp_is_read_as_milliseconds():
    body = event_body()
    timestamp = str(int(NOW * 1000))
    headers = {
        "x-zm-request-timestamp": timestamp,
        "x-zm-signature": sign(body, timestamp),
    }
    payload = verify_webhook(
        make_config(), headers, body, now=NOW, seen=SeenEvents()
    )
    assert payload["event"] == "meeting.rtms_started"


def test_missing_headers_are_refused_before_anything_is_parsed():
    with pytest.raises(WebhookError) as caught:
        verify_webhook(make_config(), {}, b"not json at all", now=NOW)
    assert caught.value.reason == "unsigned"


def test_a_signed_body_that_is_not_json_is_a_400_not_a_401():
    body = b"{oops"
    with pytest.raises(WebhookError) as caught:
        verify_webhook(
            make_config(), headers_for(body), body, now=NOW, seen=SeenEvents()
        )
    assert (caught.value.reason, caught.value.status) == ("not_json", 400)


def test_a_replayed_event_id_is_refused_the_second_time():
    body = event_body()
    headers = headers_for(body)
    seen = SeenEvents()
    verify_webhook(make_config(), headers, body, now=NOW, seen=seen)
    with pytest.raises(WebhookError) as caught:
        verify_webhook(make_config(), headers, body, now=NOW, seen=seen)
    assert caught.value.reason == "replay"


def test_a_replay_is_recognised_without_a_tracking_header():
    body = event_body()
    timestamp = str(int(NOW))
    headers = {
        "x-zm-request-timestamp": timestamp,
        "x-zm-signature": sign(body, timestamp),
    }
    seen = SeenEvents()
    verify_webhook(make_config(), headers, body, now=NOW, seen=seen)
    with pytest.raises(WebhookError) as caught:
        verify_webhook(make_config(), headers, body, now=NOW, seen=seen)
    assert caught.value.reason == "replay"


def test_a_repeated_url_validation_is_not_treated_as_a_replay():
    body = json.dumps(
        {"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}}
    ).encode("utf-8")
    headers = headers_for(body)
    seen = SeenEvents()
    for _ in range(2):
        payload = verify_webhook(make_config(), headers, body, now=NOW, seen=seen)
        assert payload["event"] == "endpoint.url_validation"


# -- URL discipline ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ws://rtms.zoom.us",  # plaintext
        "https://rtms.zoom.us",  # wrong scheme for a stream
        "wss://evil.example.com/rtms",  # off allowlist
        "wss://notzoom.us/rtms",  # suffix must be a real subdomain boundary
        "wss://api.zoom.us.evil.com/",  # allowlisted host as a prefix
    ],
)
def test_check_url_refuses_anything_that_is_not_a_zoom_wss_host(url):
    with pytest.raises(RtmsError):
        check_url(url, schemes=("wss",))


def test_check_url_accepts_a_dynamic_rtms_subdomain():
    assert check_url("wss://rtms-1234.zoom.us:443/x", schemes=("wss",))


# -- the media stream -------------------------------------------------------


class FakeConnection:
    """A recorded socket: a scripted list of frames, then closure."""

    def __init__(self, frames, *, repeat=None):
        self._frames = list(frames)
        #: A frame handed back forever once the script runs out -- a server-side
        #: bug that never ends the stream, which is what the loop cap is for.
        self._repeat = repeat
        self.sent: list[dict] = []
        self.closed = False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self):
        if not self._frames:
            if self._repeat is None:
                return None
            return json.dumps(self._repeat)
        frame = self._frames.pop(0)
        return frame if isinstance(frame, str) else json.dumps(frame)

    def close(self):
        self.closed = True


def signal_frames(media_url="wss://rtms-media.zoom.us:443", status=STATUS_OK):
    return [
        {
            "msg_type": MSG_TYPE["SIGNALING_HAND_SHAKE_RESP"],
            "status_code": status,
            "media_server": {"server_urls": {"all": media_url}},
        }
    ]


def transcript(text, *, user_name="", user_id=16778240, at=1000):
    content = {"user_id": user_id, "data": text, "timestamp": at}
    if user_name:
        content["user_name"] = user_name
    return {"msg_type": MSG_TYPE["MEDIA_DATA_TRANSCRIPT"], "content": content}


DATA_OK = {"msg_type": MSG_TYPE["DATA_HAND_SHAKE_RESP"], "status_code": STATUS_OK}
#: The wire's own terminal stream state: the integer 4. Zoom's sample
#: (signalingSocketMessageHandler.js) gates cleanup on `msg.state === 4`, and
#: `getRtmsStreamState` names 4 TERMINATED. This fixture said the *string*
#: "TERMINATED" until 2026-09-08 and the code agreed with it, so the two were
#: consistently wrong together -- which is exactly why the constants are now
#: cited to Zoom's source rather than to each other.
STREAM_END = {"msg_type": MSG_TYPE["STREAM_STATE_UPDATE"], "state": 4}
#: A stopped *session* (a different enum: 5 is STOPPED) ends the stream too.
SESSION_END = {"msg_type": MSG_TYPE["SESSION_STATE_UPDATE"], "state": 5}


def connector(signal, media):
    """A ``connect=`` that hands back the signalling socket first, then media."""
    sockets = [signal, media]

    def connect(url):
        assert url.startswith("wss://")
        return sockets.pop(0)

    return connect


def test_a_complete_stream_yields_chunks_with_zooms_own_labels():
    signal = FakeConnection(signal_frames())
    media = FakeConnection(
        [
            DATA_OK,
            transcript("we need a three point three volt rail", user_name="Ada"),
            {"msg_type": MSG_TYPE["KEEP_ALIVE_REQ"], "timestamp": 42},
            transcript("with a usb c input", at=2000),
            STREAM_END,
        ]
    )
    stream = open_stream(
        make_config(),
        json.loads(event_body()),
        connect=connector(signal, media),
    )
    chunks = list(stream)
    assert chunks == [
        TranscriptChunk("uuid-1", "Ada", "we need a three point three volt rail", 1000),
        TranscriptChunk("uuid-1", "16778240", "with a usb c input", 2000),
    ]
    # The handshakes went out signed, and the keep-alive was answered.
    expected = hmac.new(
        b"client-secret", b"client-id,uuid-1,stream-1", hashlib.sha256
    ).hexdigest()
    assert signal.sent[0]["signature"] == expected
    assert media.sent[0]["msg_type"] == MSG_TYPE["DATA_HAND_SHAKE_REQ"]
    assert media.sent[1]["msg_type"] == MSG_TYPE["KEEP_ALIVE_RESP"]
    assert signal.closed and media.closed


def test_a_refused_handshake_raises_before_any_chunk():
    signal = FakeConnection(signal_frames(status="STATUS_UNAUTHORIZED"))
    media = FakeConnection([])
    with pytest.raises(HandshakeError) as caught:
        open_stream(
            make_config(), json.loads(event_body()), connect=connector(signal, media)
        )
    assert "STATUS_UNAUTHORIZED" in str(caught.value)
    assert signal.closed


def test_a_meeting_outside_the_allowlist_is_refused_without_connecting():
    calls = []

    def connect(url):
        calls.append(url)
        raise AssertionError("must not connect")

    config = make_config(meeting_allowlist=("some-other-meeting",))
    with pytest.raises(NotAuthorisedError):
        open_stream(config, json.loads(event_body()), connect=connect)
    assert calls == []


def test_a_payload_missing_fields_names_all_of_them_at_once():
    payload = {"event": "meeting.rtms_started", "payload": {"object": {}}}
    with pytest.raises(PayloadError) as caught:
        open_stream(make_config(), payload, connect=lambda url: None)
    assert len(caught.value.problems) == 3


def test_a_truncated_frame_raises_rather_than_being_skipped():
    signal = FakeConnection(signal_frames())
    media = FakeConnection([DATA_OK, '{"msg_type": 17, "content": {"data": "hal'])
    stream = open_stream(
        make_config(), json.loads(event_body()), connect=connector(signal, media)
    )
    with pytest.raises(FrameError):
        list(stream)


def test_a_transcript_frame_with_no_content_object_is_an_error_not_silence():
    signal = FakeConnection(signal_frames())
    media = FakeConnection(
        [DATA_OK, {"msg_type": MSG_TYPE["MEDIA_DATA_TRANSCRIPT"]}, STREAM_END]
    )
    stream = open_stream(
        make_config(), json.loads(event_body()), connect=connector(signal, media)
    )
    with pytest.raises(FrameError):
        list(stream)


def test_a_stream_that_ends_early_is_distinguishable_from_one_that_ends():
    signal = FakeConnection(signal_frames())
    media = FakeConnection([DATA_OK, transcript("half a sentence")])
    stream = open_stream(
        make_config(), json.loads(event_body()), connect=connector(signal, media)
    )
    with pytest.raises(StreamInterruptedError) as caught:
        list(stream)
    assert caught.value.chunks == 1
    assert media.closed


def test_a_stream_with_no_transcript_at_all_raises_its_own_error():
    signal = FakeConnection(signal_frames())
    media = FakeConnection([DATA_OK, STREAM_END])
    stream = open_stream(
        make_config(), json.loads(event_body()), connect=connector(signal, media)
    )
    with pytest.raises(EmptyStreamError):
        list(stream)


def test_the_three_bad_outcomes_are_three_different_types():
    assert not issubclass(EmptyStreamError, StreamInterruptedError)
    assert not issubclass(StreamInterruptedError, NotAuthorisedError)
    assert not issubclass(EmptyStreamError, NotAuthorisedError)


def test_a_stream_that_never_stops_hits_the_loop_cap():
    """A server-side bug: keep-alives forever, never a terminal state."""
    signal = FakeConnection(signal_frames())
    media = FakeConnection(
        [DATA_OK], repeat={"msg_type": MSG_TYPE["KEEP_ALIVE_REQ"], "timestamp": 1}
    )
    stream = open_stream(
        make_config(),
        json.loads(event_body()),
        connect=connector(signal, media),
        max_frames=25,
    )
    with pytest.raises(RtmsError) as caught:
        list(stream)
    assert "more than 25 frames" in str(caught.value)
    assert media.closed


def test_a_signalling_socket_that_never_answers_hits_its_own_cap():
    signal = FakeConnection(
        [], repeat={"msg_type": MSG_TYPE["KEEP_ALIVE_REQ"], "timestamp": 1}
    )
    with pytest.raises(HandshakeError) as caught:
        open_stream(
            make_config(),
            json.loads(event_body()),
            connect=connector(signal, FakeConnection([])),
        )
    assert "without a handshake response" in str(caught.value)


def test_the_media_url_must_also_be_a_zoom_host():
    signal = FakeConnection(signal_frames(media_url="wss://evil.example.com/media"))
    media = FakeConnection([DATA_OK])
    with pytest.raises(RtmsError):
        open_stream(
            make_config(), json.loads(event_body()), connect=connector(signal, media)
        )


def test_the_module_imports_without_pulling_in_a_websocket_library():
    """The lazy import is the point: nothing here may need a WS package.

    Run in a subprocess rather than by reloading the module in place.
    ``importlib.reload`` re-executes the module body, which rebuilds every
    class in it -- so ``zoombot.rtms.PayloadError`` afterwards is a *different*
    class object from the one this test file imported at the top, and any
    ``pytest.raises(PayloadError)`` in a test that happens to run later fails
    with the exception it was looking for printed in the traceback. That cost
    an hour on 2026-09-08. A fresh interpreter answers the same question
    (does importing this module pull in ``websocket``?) without reaching into
    the one the rest of the suite is using.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import zoombot.rtms; "
        "sys.exit(1 if 'websocket' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0


# ---------------------------------------------------------------------------
# The wire protocol, pinned to Zoom's own published sample
#
# Every assertion below was read out of `github.com/zoom/rtms-samples` (MIT)
# on 2026-09-08 and names the file it came from. These are not tests of our
# behaviour -- they are tests that our *constants* still say what Zoom's
# reference implementation says, so that a well-meaning edit to the table in
# rtms.py fails here rather than six months later against a live account.
#
# They exist because the previous values were self-consistent with the test
# fixtures and wrong about the wire, which is the one failure a normal
# round-trip test cannot catch.
# ---------------------------------------------------------------------------


def test_message_types_match_zooms_reference_implementation():
    """`zoom/rtms-samples`, library/javascript/rtmsManager/*.js.

    The numbering from EVENT_SUBSCRIPTION on was wrong until 2026-09-08:
    STREAM_STATE_UPDATE is 8, not 5.
    """
    assert MSG_TYPE["SIGNALING_HAND_SHAKE_REQ"] == 1  # signalingSocket.js
    assert MSG_TYPE["SIGNALING_HAND_SHAKE_RESP"] == 2  # ...MessageHandler case 2
    assert MSG_TYPE["DATA_HAND_SHAKE_REQ"] == 3  # mediaSocket.js
    assert MSG_TYPE["DATA_HAND_SHAKE_RESP"] == 4  # mediaSocketMessageHandler case 4
    assert MSG_TYPE["EVENT_SUBSCRIPTION"] == 5  # `{ msg_type: 5, events: [...] }`
    assert MSG_TYPE["EVENT_UPDATE"] == 6  # `case 6: // Events`
    assert MSG_TYPE["CLIENT_READY_ACK"] == 7  # `{ msg_type: 7, rtms_stream_id }`
    assert MSG_TYPE["STREAM_STATE_UPDATE"] == 8  # `case 8: // Stream State changed`
    assert MSG_TYPE["SESSION_STATE_UPDATE"] == 9  # `case 9: // Session State Changed`
    assert MSG_TYPE["KEEP_ALIVE_REQ"] == 12  # `case 12:` replies msg_type 13
    assert MSG_TYPE["KEEP_ALIVE_RESP"] == 13
    assert MSG_TYPE["MEDIA_DATA_TRANSCRIPT"] == 17  # `case 17: // TRANSCRIPT`


def test_status_ok_is_the_integer_zero_not_a_string():
    """`if (msg.status_code === 0)` -- signaling/mediaSocketMessageHandler.js.

    This was the string "STATUS_OK". Against a real account every handshake
    would have been read as a refusal, and the run would have reported
    "never authorised" for a stream Zoom had accepted.
    """
    assert STATUS_OK == 0
    assert not isinstance(STATUS_OK, str)


def test_the_transcript_media_type_is_a_bitmask_flag():
    """mediaSocket.js: `TYPE_FLAGS = { audio: 1, ..., transcript: 8, chat: 16 }`."""
    assert MEDIA_TYPE_TRANSCRIPT == 8
    assert MEDIA_TYPE_TRANSCRIPT == 1 << 3


def test_terminal_states_are_integers_and_the_two_enums_stay_apart():
    """getRtmsStreamState / getRtmsSessionState, rtmsEventLookupHelper.js.

    Stream 4 is TERMINATED; session 4 is RESUMED. One shared set would end a
    meeting the moment it came back from a pause.
    """
    assert set(TERMINAL_STREAM_STATES) == {4}
    assert set(TERMINAL_SESSION_STATES) == {5}
    assert not TERMINAL_STREAM_STATES & TERMINAL_SESSION_STATES
    assert all(isinstance(state, int) for state in TERMINAL_STREAM_STATES)
    # TERMINATING (3) is deliberately not terminal: the sample keeps reading.
    assert 3 not in TERMINAL_STREAM_STATES


def test_a_terminating_stream_is_not_treated_as_the_end():
    """State 3 means "told to terminate", and frames may still follow."""
    signal = FakeConnection(signal_frames())
    media = FakeConnection(
        [
            DATA_OK,
            {"msg_type": MSG_TYPE["STREAM_STATE_UPDATE"], "state": 3},
            transcript("after the terminating notice"),
            STREAM_END,
        ]
    )
    stream = open_stream(
        make_config(), json.loads(event_body()), connect=connector(signal, media)
    )
    assert [chunk.text for chunk in stream] == ["after the terminating notice"]


def test_a_stopped_session_ends_the_stream_cleanly():
    """`if (msg.state === 5 && conn)` -- signalingSocketMessageHandler.js.

    Without the SESSION_STATE_UPDATE branch this ran on to the socket close
    and raised StreamInterruptedError -- "half a meeting" said about a meeting
    that ended normally.
    """
    signal = FakeConnection(signal_frames())
    media = FakeConnection([DATA_OK, transcript("all done"), SESSION_END])
    stream = open_stream(
        make_config(), json.loads(event_body()), connect=connector(signal, media)
    )
    assert [chunk.text for chunk in stream] == ["all done"]


def test_a_handshake_with_an_unreadable_status_is_a_refusal():
    """"I could not read the status" must never pass as "the status was OK"."""
    signal = FakeConnection(
        [{"msg_type": MSG_TYPE["SIGNALING_HAND_SHAKE_RESP"], "reason": "who knows"}]
    )
    with pytest.raises(HandshakeError) as caught:
        open_stream(
            make_config(),
            json.loads(event_body()),
            connect=connector(signal, FakeConnection([DATA_OK])),
        )
    assert "unreadable status_code" in str(caught.value)


# -- the rtms_started envelope ---------------------------------------------


def flat_event_body(**object_overrides) -> bytes:
    """The shape Zoom's sample actually reads: fields flat on `payload`.

    RTMSManager.js:

        this.on('meeting.rtms_started', (payload) => {
          const { meeting_uuid, rtms_stream_id, server_urls, event_ts } = payload;

    -- no `.object` step anywhere in the chain.
    """
    fields = {
        "meeting_uuid": "uuid-1",
        "rtms_stream_id": "stream-1",
        "server_urls": "wss://rtms-signal.zoom.us:443",
    }
    fields.update(object_overrides)
    return json.dumps(
        {
            "event": "meeting.rtms_started",
            "event_ts": 1_700_000_000_000,
            "payload": {"operator_id": "op", **fields},
        }
    ).encode("utf-8")


def test_a_flat_rtms_started_payload_opens_a_stream():
    """The shape Zoom actually sends. This raised PayloadError until 2026-09-08."""
    signal = FakeConnection(signal_frames())
    media = FakeConnection([DATA_OK, transcript("hello"), STREAM_END])
    stream = open_stream(
        make_config(),
        json.loads(flat_event_body()),
        connect=connector(signal, media),
    )
    assert [chunk.text for chunk in stream] == ["hello"]
    assert stream.session.meeting_uuid == "uuid-1"


def test_two_different_flat_events_are_not_confused_for_a_replay():
    """The replay guard must key on the fields, wherever they live.

    While `event_key` read `payload.object` against a flat payload, every field
    came back empty and *every* rtms_started digested to the same key: the
    first meeting was accepted and every later one refused as a replay. A
    replay guard that rejects real events fails as silence.
    """
    from zoombot.rtms import SeenEvents, event_key

    first = json.loads(flat_event_body(meeting_uuid="uuid-a", rtms_stream_id="s-a"))
    second = json.loads(flat_event_body(meeting_uuid="uuid-b", rtms_stream_id="s-b"))
    assert event_key({}, first) != event_key({}, second)

    seen = SeenEvents()
    assert seen.add_if_new(event_key({}, first))
    assert seen.add_if_new(event_key({}, second))
    assert not seen.add_if_new(event_key({}, first))


def test_the_nested_envelope_still_works():
    """Zoom's other event families do nest under `.object`; both are read."""
    signal = FakeConnection(signal_frames())
    media = FakeConnection([DATA_OK, transcript("nested"), STREAM_END])
    stream = open_stream(
        make_config(), json.loads(event_body()), connect=connector(signal, media)
    )
    assert [chunk.text for chunk in stream] == ["nested"]


def test_a_flat_payload_missing_every_field_names_all_of_them():
    payload = {"event": "meeting.rtms_started", "payload": {"operator_id": "op"}}
    with pytest.raises(PayloadError) as caught:
        open_stream(make_config(), payload, connect=lambda url: None)
    assert len(caught.value.problems) == 3


# -- the media URL out of the signalling response ---------------------------


def test_a_transcript_specific_media_url_is_preferred_over_all():
    """rtmsEntityHelper.js::getPreferredMediaUrl takes the requested type first.

    We subscribe to transcript alone; taking `all` when a transcript socket was
    offered asks Zoom for audio and video frames we then drop on the floor.
    """
    frame = {
        "msg_type": MSG_TYPE["SIGNALING_HAND_SHAKE_RESP"],
        "status_code": STATUS_OK,
        "media_server": {
            "server_urls": {
                "all": "wss://rtms-all.zoom.us:443",
                "transcript": "wss://rtms-transcript.zoom.us:443",
            }
        },
    }
    signal = FakeConnection([frame])
    media = FakeConnection([DATA_OK, transcript("x"), STREAM_END])
    urls = []

    def connect(url):
        urls.append(url)
        return signal if len(urls) == 1 else media

    list(open_stream(make_config(), json.loads(event_body()), connect=connect))
    assert urls[1] == "wss://rtms-transcript.zoom.us:443"


def test_the_webhook_timestamp_is_read_as_seconds():
    """zoomWebhookSignature.js compares it against `Date.now() / 1000`.

    A 10-digit value is seconds; the 13-digit `event_ts` *inside* the body is
    milliseconds. Two timestamps, two units, one delivery.
    """
    from zoombot.rtms import _webhook_timestamp

    assert _webhook_timestamp("1700000000") == 1_700_000_000.0


def test_a_millisecond_timestamp_is_still_read_but_says_so_out_loud(caplog):
    """The fallback is kept; it is no longer silent.

    If Zoom ever does send milliseconds, a live run must be able to learn it
    from the log rather than from a mysteriously never-stale webhook.
    """
    from zoombot.rtms import _webhook_timestamp

    with caplog.at_level(logging.WARNING, logger="zoombot.rtms"):
        assert _webhook_timestamp("1700000000000") == 1_700_000_000.0
    assert "too large to be epoch seconds" in caplog.text

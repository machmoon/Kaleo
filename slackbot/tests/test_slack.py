"""Signature verification, and the request construction the client does.

The signature tests matter most: this is the only thing standing between the
public internet and a bot that spends money on model calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from slackbot.slack import (
    HttpResponse,
    SlackClient,
    SlackError,
    verify_signature,
)
from slackbot.tests.fakes import RecordingTransport

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
NOW = 1_700_000_000.0


def sign(body: bytes, timestamp: str = "1700000000", secret: str = SECRET) -> str:
    base = b"v0:%s:%s" % (timestamp.encode(), body)
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_a_correct_signature_verifies():
    body = b"token=x&text=hello"
    assert verify_signature(
        SECRET, timestamp="1700000000", signature=sign(body), body=body, now=NOW
    )


def test_a_tampered_body_fails():
    body = b"token=x&text=hello"
    signature = sign(body)
    assert not verify_signature(
        SECRET,
        timestamp="1700000000",
        signature=signature,
        body=b"token=x&text=goodbye",
        now=NOW,
    )


def test_an_old_but_correctly_signed_request_fails():
    """A captured request replayed six minutes later is still a valid HMAC."""
    body = b"token=x"
    assert not verify_signature(
        SECRET,
        timestamp="1700000000",
        signature=sign(body),
        body=body,
        now=NOW + 400,
    )


def test_a_future_timestamp_fails():
    body = b"token=x"
    assert not verify_signature(
        SECRET, timestamp="1700000000", signature=sign(body), body=body, now=NOW - 400
    )


def test_wrong_secret_fails():
    body = b"token=x"
    assert not verify_signature(
        "other-secret",
        timestamp="1700000000",
        signature=sign(body),
        body=body,
        now=NOW,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timestamp": "", "signature": "v0=abc"},
        {"timestamp": "1700000000", "signature": ""},
        {"timestamp": "not-a-number", "signature": "v0=abc"},
        {"timestamp": "1700000000", "signature": "garbage"},
    ],
)
def test_missing_or_malformed_headers_fail_closed(kwargs):
    assert not verify_signature(SECRET, body=b"x", now=NOW, **kwargs)


def test_empty_signing_secret_fails_closed():
    """A bot started without a secret must reject everything, not accept it."""
    body = b"x"
    assert not verify_signature(
        "", timestamp="1700000000", signature=sign(body, secret=""), body=body, now=NOW
    )


# -- client ---------------------------------------------------------------


def test_post_message_sends_a_bearer_token_and_returns_the_ts():
    transport = RecordingTransport()
    client = SlackClient("xoxb-secret", transport=transport)
    ts = client.post_message(
        "C1", "hello", thread_ts="1.2", blocks=[{"type": "divider"}]
    )

    assert ts == "1700000000.000100"
    request = transport.requests[0]
    assert request.url.endswith("chat.postMessage")
    assert request.headers["Authorization"] == "Bearer xoxb-secret"
    body = json.loads(request.body)
    assert body["channel"] == "C1"
    assert body["thread_ts"] == "1.2"
    assert body["text"] == "hello"


def test_none_valued_fields_are_dropped():
    """Slack rejects `thread_ts: null`; a top-level post has no thread."""
    transport = RecordingTransport()
    SlackClient("t", transport=transport).post_message("C1", "hi")
    assert "thread_ts" not in json.loads(transport.requests[0].body)


def test_an_api_error_raises_with_slacks_own_code():
    transport = RecordingTransport(
        {"chat.postMessage": {"ok": False, "error": "not_in_channel"}}
    )
    with pytest.raises(SlackError) as excinfo:
        SlackClient("t", transport=transport, max_retries=0).post_message("C1", "hi")
    assert excinfo.value.code == "not_in_channel"


def test_rate_limits_are_retried_then_surface():
    slept: list[float] = []
    transport = RecordingTransport(
        {
            "chat.postMessage": HttpResponse(
                429, b'{"ok": false, "error": "ratelimited"}'
            )
        }
    )
    client = SlackClient(
        "t", transport=transport, sleep=slept.append, max_retries=2
    )
    with pytest.raises(SlackError) as excinfo:
        client.post_message("C1", "hi")
    assert excinfo.value.code == "ratelimited"
    assert len(slept) == 2


def test_a_failed_reaction_is_swallowed():
    """Losing an emoji must never lose a run."""
    transport = RecordingTransport(
        {"reactions.add": {"ok": False, "error": "already_reacted"}}
    )
    client = SlackClient("t", transport=transport, max_retries=0)
    client.add_reaction("C1", "1.2", "eyes")


def test_upload_is_the_three_step_external_flow():
    transport = RecordingTransport(
        {
            "getUploadURLExternal": {
                "ok": True,
                "upload_url": "https://files.slack.test/upload/abc",
                "file_id": "F123",
            }
        }
    )
    client = SlackClient("t", transport=transport)
    file_id = client.upload_file(
        "C1", "board.kicad_pcb", b"(kicad_pcb)", thread_ts="1.2", initial_comment="here"
    )

    assert file_id == "F123"
    urls = [r.url for r in transport.requests]
    assert "files.getUploadURLExternal" in urls[0]
    assert "length=11" in urls[0]
    assert urls[1] == "https://files.slack.test/upload/abc"
    assert transport.requests[1].body == b"(kicad_pcb)"
    assert "files.completeUploadExternal" in urls[2]

    complete = json.loads(transport.requests[2].body)
    assert complete["files"] == [{"id": "F123", "title": "board.kicad_pcb"}]
    assert complete["channel_id"] == "C1"
    assert complete["thread_ts"] == "1.2"


def test_upload_refuses_an_empty_file():
    with pytest.raises(SlackError):
        SlackClient("t", transport=RecordingTransport()).upload_file("C1", "x.txt", b"")


def _upload_transport():
    return RecordingTransport(
        {
            "getUploadURLExternal": {
                "ok": True,
                "upload_url": "https://files.slack.test/u",
                "file_id": "F1",
            }
        }
    )


def test_the_bytes_stored_are_the_bytes_given_and_the_declared_length():
    """The upload step is checked against Slack's own client, not against us.

    ``slack_sdk/web/internal_utils.py::_upload_file_via_v2_url`` POSTs the raw
    file with ``Request(method="POST", url=url, data=data, headers={})`` -- no
    multipart envelope, no headers -- and ``files.getUploadURLExternal`` was
    already told ``length``, documented as "Size in bytes of the file being
    uploaded". So the body Slack stores must be byte-identical to the content
    *and* exactly that many bytes.

    This is the assertion the old test could not make: it checked
    ``content in body``, which a multipart envelope satisfies too. The
    envelope is why it was written that way, and why a board file uploaded
    from here would have arrived with boundary lines wrapped around it.
    """
    content = b"\x00(kicad_pcb version 20240108)\xff\r\n--boundary--\r\n"
    transport = _upload_transport()
    SlackClient("t", transport=transport).upload_file("C1", "board.kicad_pcb", content)

    reserve, upload, _complete = transport.requests
    assert f"length={len(content)}" in reserve.url
    assert upload.body == content
    assert len(upload.body) == len(content)
    assert upload.headers == {}


def test_the_two_documented_http_methods_are_used():
    """Both are POST.

    ``files.getUploadURLExternal`` is documented as "POST requests accepting
    application/x-www-form-urlencoded or application/json", and Slack's own
    client sends it as ``api_call("files.getUploadURLExternal", params=...)``
    -- a POST with the arguments in the query string. This was a GET, which is
    the kind of thing that works until Slack tightens it.
    """
    transport = _upload_transport()
    SlackClient("t", transport=transport).upload_file("C1", "b.kicad_pcb", b"data")
    assert [r.method for r in transport.requests] == ["POST", "POST", "POST"]


def test_a_hostile_filename_never_reaches_a_header():
    """With the envelope gone the filename only travels url-encoded in the
    query string and as a JSON ``title``; neither can become a header line."""
    transport = _upload_transport()
    SlackClient("t", transport=transport).upload_file(
        "C1", 'evil"\r\nX-Injected: yes\r\n.txt', b"data"
    )
    reserve, upload, complete = transport.requests
    assert "\r\n" not in reserve.url and "X-Injected: yes" not in reserve.url
    assert upload.body == b"data"
    assert json.loads(complete.body)["files"][0]["title"].startswith('evil"')

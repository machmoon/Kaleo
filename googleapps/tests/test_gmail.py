"""Gmail: the MIME message survives its own encoding, and the guards hold."""

from __future__ import annotations

import base64
import email
import email.message
import email.policy
import json

import pytest

from googleapps import gmail
from googleapps.auth import AuthError
from googleapps.tests.fakes import RecordingTransport
from googleapps.transport import GoogleError, HttpResponse

BOARD_BYTES = b"(kicad_pcb (version 20240108) (generator silkscreen))\n"


def board_file(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes(BOARD_BYTES)
    return path


def sent_message(transport):
    """Decode what reached the API back into a parsed MIME message."""
    body = json.loads(transport.requests[-1].body)
    raw = base64.urlsafe_b64decode(body["raw"].encode("ascii"))
    return email.message_from_bytes(raw, policy=email.policy.default)


def test_the_send_hits_the_documented_endpoint_with_a_bearer_token(tmp_path):
    transport = RecordingTransport()
    message_id = gmail.send_run_email(
        "ya29.tok",
        to=["team@example.com"],
        subject="silkscreen: board ready",
        body="2 parts",
        board_path=board_file(tmp_path),
        transport=transport,
    )
    assert message_id == "msg-1"
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    )
    assert request.headers["Authorization"] == "Bearer ya29.tok"


def test_the_raw_field_round_trips_through_base64url(tmp_path):
    """The repo's round-trip discipline, applied to MIME: the attachment
    bytes recovered from the ``raw`` field equal the file that went in."""
    transport = RecordingTransport()
    gmail.send_run_email(
        "t",
        to=["a@example.com", "b@example.com"],
        subject="the subject line",
        body="the summary",
        board_path=board_file(tmp_path),
        transport=transport,
    )
    message = sent_message(transport)
    assert message["To"] == "a@example.com, b@example.com"
    assert message["Subject"] == "the subject line"
    parts = list(message.iter_attachments())
    assert len(parts) == 1
    assert parts[0].get_filename() == "board.kicad_pcb"
    assert parts[0].get_content() == BOARD_BYTES
    assert message.get_body(("plain",)).get_content().strip() == "the summary"


def test_the_raw_field_is_base64url_not_plain_base64(tmp_path):
    # 0xfb 0xff forces '-' and '_' into the url-safe alphabet ('+' and '/'
    # in plain base64), so a wrong encoder cannot pass this.
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes(b"\xfb\xff" * 12)
    transport = RecordingTransport()
    gmail.send_run_email(
        "t", to=["a@example.com"], subject="s", body="b",
        board_path=path, transport=transport,
    )
    raw = json.loads(transport.requests[0].body)["raw"]
    assert "+" not in raw and "/" not in raw


def test_an_oversized_attachment_is_refused_locally(tmp_path):
    path = tmp_path / "board.kicad_pcb"
    path.write_bytes(b"x" * (gmail.MAX_ATTACHMENT_BYTES + 1))
    transport = RecordingTransport()
    with pytest.raises(GoogleError, match="attachment_too_large"):
        gmail.send_run_email(
            "t", to=["a@example.com"], subject="s", body="b",
            board_path=path, transport=transport,
        )
    assert transport.requests == []  # refused before any bytes left


def test_no_recipients_is_an_error(tmp_path):
    with pytest.raises(GoogleError, match="recipient"):
        gmail.send_run_email(
            "t", to=[], subject="s", body="b",
            board_path=board_file(tmp_path), transport=RecordingTransport(),
        )


def test_a_401_tells_the_user_to_reauthenticate(tmp_path):
    transport = RecordingTransport(
        {"gmail.googleapis.com": HttpResponse(401, b"{}")}
    )
    with pytest.raises(AuthError, match="python -m googleapps auth"):
        gmail.send_run_email(
            "t", to=["a@example.com"], subject="s", body="b",
            board_path=board_file(tmp_path), transport=transport,
        )


def test_the_largest_allowed_attachment_fits_the_message_and_the_request(tmp_path):
    """Measure what actually leaves, not what the constant's comment claims.

    Two limits, and the old version of this test only checked the first: the
    finished MIME message against Gmail's 25 MB sending cap, and the JSON
    request body -- the thing Google's frontend measures -- against
    :data:`gmail.MAX_REQUEST_BYTES`. The attachment is base64'd into the
    message and the message is base64url'd again into ``raw``, so the second
    number is roughly 1.83x the file while the first is roughly 1.37x. A guard
    checked only against the first is a guard against the wrong limit.
    """
    message = gmail.build_message(
        to=["a@example.com"], subject="s", body="b",
        attachment_name="board.kicad_pcb",
        attachment=b"\xfb" * gmail.MAX_ATTACHMENT_BYTES,
    )
    assert len(message.as_bytes()) < gmail.GMAIL_MESSAGE_CAP_BYTES
    assert len(gmail.encode_request(message)) <= gmail.MAX_REQUEST_BYTES


def test_the_encoded_request_is_measured_not_assumed():
    """``WIRE_INFLATION`` is a claim about base64; check it against base64.

    Computed independently of the module: 4/3 for the MIME base64 plus CRLF
    every 76 characters, then 4/3 again for ``raw``. The constant must be an
    over-estimate, because it is used to warn a person how big their request
    would be and an under-estimate would understate the problem.
    """
    payload = b"\x00\xfb\xff" * 400_000  # 1.2 MB, no compressible structure
    message = gmail.build_message(
        to=["a@example.com"], subject="s", body="b",
        attachment_name="board.kicad_pcb", attachment=payload,
    )
    measured = len(gmail.encode_request(message)) / len(payload)
    theoretical = (4 / 3) * (78 / 76) * (4 / 3)
    assert measured == pytest.approx(theoretical, rel=0.02)
    assert measured <= gmail.WIRE_INFLATION


def test_a_message_too_big_for_the_request_is_refused_before_it_is_sent():
    """The refusal names the documented fix rather than leaving a 400 behind.

    Google's REST reference calls ``SEND_URL`` the metadata URI and publishes
    no size for it; the only documented ceiling (35 MiB, ``mediaUpload.maxSize``
    in the discovery document) belongs to the ``/upload`` URI. So an oversized
    message must be refused here, and the refusal must say which endpoint it is
    talking about and where a larger message is supposed to go.
    """
    # Built with the stdlib rather than build_message, so the file-level
    # estimate is not in the way: this is the check on the finished artefact.
    message = email.message.EmailMessage()
    message["To"] = "a@example.com"
    message["Subject"] = "s"
    message.set_content("b")
    message.add_attachment(
        b"\xfb" * gmail.MAX_REQUEST_BYTES,
        maintype="application",
        subtype="octet-stream",
        filename="board.kicad_pcb",
    )
    with pytest.raises(GoogleError, match="request_too_large") as caught:
        gmail.encode_request(message)
    assert gmail.UPLOAD_URL in str(caught.value)


@pytest.mark.parametrize(
    "recipient",
    [
        "a@example.com\nBcc: x@evil.example",  # header injection
        "a@example.com, b@example.com",  # two in one flag
        "not an address",
    ],
)
def test_a_recipient_that_could_break_the_header_is_refused_locally(
    tmp_path, recipient
):
    transport = RecordingTransport()
    with pytest.raises(GoogleError, match="bad_address"):
        gmail.send_run_email(
            "t", to=[recipient], subject="s", body="b",
            board_path=board_file(tmp_path), transport=transport,
        )
    assert transport.requests == []


def test_google_s_own_error_message_is_surfaced(tmp_path):
    transport = RecordingTransport(
        {"gmail.googleapis.com": HttpResponse(
            400, b'{"error": {"code": 400, "message": "Recipient address required"}}')}
    )
    with pytest.raises(GoogleError, match="Recipient address required"):
        gmail.send_run_email(
            "t", to=["a@example.com"], subject="s", body="b",
            board_path=board_file(tmp_path), transport=transport,
        )

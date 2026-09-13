"""Sending the run's results by Gmail, board file attached.

One endpoint: ``users/me/messages/send`` with a base64url-encoded RFC 2822
message in the ``raw`` field. The message is built with the stdlib ``email``
package -- multipart/mixed, a plain-text summary part, and the emitted
``.kicad_pcb`` as an application/octet-stream attachment -- so the tests can
decode ``raw`` back through the same stdlib and compare the attachment bytes
to what went in.

**The arithmetic here was wrong until 2026-09-08, and wrong in the direction
that produces a remote refusal nobody can read.** The old comment counted one
base64 pass -- "15 MB of attachment is about 20 MB on the wire" -- but there
are *two*. The attachment is base64'd into the MIME message (4/3, plus CRLF
folding every 76 characters, so about 1.37x), and then the whole message is
base64url'd again into the JSON ``raw`` field (another 4/3). A 15 MiB file
is therefore about 20.5 MiB of message and about **27.4 MiB of HTTP request
body**, not 20. The unit test that "checked" the guard measured
``message.as_bytes()`` and so shared the mistake exactly: it never looked at
what was POSTed.

Why that matters, from Google's own published surface rather than from
guesswork. The Gmail discovery document
(``https://gmail.googleapis.com/$discovery/rest?version=v1``) marks
``gmail.users.messages.send`` ``supportsMediaUpload: true`` with
``mediaUpload.maxSize: "36700160"`` (35 MiB) and ``accept: ["message/*"]``,
reachable only at ``/upload/gmail/v1/...``; the REST reference calls the URL
this module uses the "metadata" URI and publishes **no** size for it. So the
one number that is documented does not apply to the endpoint being used, and
the endpoint being used has no documented number at all -- which is exactly a
"state it and fail loudly" case rather than a "pick a number and hope" one.
:data:`MAX_REQUEST_BYTES` is set to the smallest limit Google is observed to
enforce on a metadata request, refused locally with the two-stage arithmetic
spelled out, so an operator gets a sentence instead of a 400 whose body is a
proxy's HTML.

A clear local refusal beats a remote 413 that names nothing.
"""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from pathlib import Path

from .addresses import validate_addresses
from .auth import RERUN_HINT, AuthError
from .transport import (
    GoogleError,
    HttpRequest,
    Transport,
    ensure_google_url,
    error_detail,
)

__all__ = [
    "GMAIL_MESSAGE_CAP_BYTES",
    "MAX_ATTACHMENT_BYTES",
    "MAX_REQUEST_BYTES",
    "MEDIA_UPLOAD_MAX_BYTES",
    "SEND_URL",
    "UPLOAD_URL",
    "build_message",
    "encode_request",
    "send_run_email",
]

#: The "metadata" URI, in the REST reference's own words -- the one that takes
#: a ``Message`` with a base64url ``raw`` field in a JSON body.
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
#: The media URI for the same method, named here because it is the documented
#: fix an operator is pointed at when a message is too big for the one above.
#: Nothing sends to it yet: it would be a second, differently-shaped request
#: this package has never run against Google, and an unverified fallback is
#: how a silent degrade gets built. Refusing and naming it is the honest half.
UPLOAD_URL = (
    "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"
    "?uploadType=media"
)

#: Gmail's sending limit on the finished RFC 5322 message. 25 MB is Google's
#: published figure for Gmail attachments; it bounds the message, not the
#: request. **Not machine-checkable from the discovery document** -- it is a
#: product limit rather than an API one -- so it is a documented number this
#: package believes, and the failure if it is wrong is a refusal, not a send.
GMAIL_MESSAGE_CAP_BYTES = 25 * 1024 * 1024

#: **CONFIRMED** from the Gmail discovery document,
#: ``resources.users.resources.messages.methods.send.mediaUpload.maxSize`` =
#: ``"36700160"``. This is the cap on the ``/upload`` URI only, and is recorded
#: here so the refusal below can say what the documented ceiling actually is
#: and where it applies.
MEDIA_UPLOAD_MAX_BYTES = 36_700_160

#: **UNDOCUMENTED, and therefore refused locally rather than discovered
#: remotely.** Google publishes no request-body size for the metadata URI. What
#: is observed in the field is a 400 whose message is "Request payload size
#: exceeds the limit: 10485760 bytes" -- 10 MiB -- and that number is not in
#: any Gmail reference page, so it can change without notice. This module
#: therefore refuses at it, in words, naming :data:`UPLOAD_URL` as the
#: documented path for anything larger. If a live run ever shows the real limit
#: is different, it is one constant here.
MAX_REQUEST_BYTES = 10 * 1024 * 1024

#: How much bigger the HTTP body is than the file, both base64 passes and MIME
#: line folding included: (4/3 x 76/78) for the MIME encoding, then 4/3 again
#: for ``raw``. Measured, not asserted -- see
#: ``test_the_encoded_request_is_measured_not_assumed``.
WIRE_INFLATION = 1.83

#: The largest *file* whose finished message stays under Gmail's sending cap
#: **and** whose JSON request stays under :data:`MAX_REQUEST_BYTES`. The second
#: bound is the binding one, and it is the one the old constant ignored.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def build_message(
    *,
    to: list[str],
    subject: str,
    body: str,
    attachment_name: str,
    attachment: bytes,
) -> EmailMessage:
    """The MIME message, before encoding. ``From`` is left to Gmail, which
    stamps the authenticated user's own address and refuses forgeries."""
    if not to:
        raise GoogleError("bad_request", "an email needs at least one recipient")
    to = validate_addresses(to, what="the recipient list")
    if len(attachment) > MAX_ATTACHMENT_BYTES:
        raise GoogleError(
            "attachment_too_large",
            f"{attachment_name} is {len(attachment) / 1_048_576:.1f} MB. It is "
            "base64-encoded twice on the way to Gmail -- once into the MIME "
            "message and again into the JSON `raw` field -- so it would be "
            f"about {len(attachment) * WIRE_INFLATION / 1_048_576:.1f} MB of "
            f"request body, over the {MAX_REQUEST_BYTES // 1_048_576} MB this "
            "integration will send to the metadata endpoint "
            f"({SEND_URL}). Anything larger needs the media upload URI "
            f"({UPLOAD_URL}), which this package does not implement",
        )
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(
        attachment,
        maintype="application",
        subtype="octet-stream",
        filename=attachment_name,
    )
    return message


def encode_request(message: EmailMessage) -> bytes:
    """The exact JSON body that goes on the wire, or a refusal.

    Both size checks live here, on the finished artefacts, rather than on the
    file the guard in :func:`build_message` estimates from: the text part and
    the headers contribute too, and an estimate that is checked only against
    itself is how the old 15 MB constant survived.
    """
    mime = message.as_bytes()
    if len(mime) > GMAIL_MESSAGE_CAP_BYTES:
        raise GoogleError(
            "message_too_large",
            f"the finished message is {len(mime) / 1_048_576:.1f} MB, over "
            f"Gmail's {GMAIL_MESSAGE_CAP_BYTES // 1_048_576} MB sending cap",
        )
    raw = base64.urlsafe_b64encode(mime).decode("ascii")
    body = json.dumps({"raw": raw}).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise GoogleError(
            "request_too_large",
            f"the JSON request would be {len(body) / 1_048_576:.1f} MB "
            f"({len(mime) / 1_048_576:.1f} MB of message, base64url'd again "
            f"into `raw`), over the {MAX_REQUEST_BYTES // 1_048_576} MB this "
            f"integration will send to {SEND_URL}; the documented path for a "
            f"message this size is the media upload URI ({UPLOAD_URL}), which "
            "this package does not implement",
        )
    return body


def send_run_email(
    token: str,
    *,
    to: list[str],
    subject: str,
    body: str,
    board_path: Path,
    transport: Transport,
) -> str:
    """Send the summary with the board attached; returns Gmail's message id."""
    message = build_message(
        to=to,
        subject=subject,
        body=body,
        attachment_name=board_path.name,
        attachment=board_path.read_bytes(),
    )
    body = encode_request(message)
    response = transport(
        HttpRequest(
            "POST",
            ensure_google_url(SEND_URL),
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body,
        )
    )
    if response.status == 401:
        raise AuthError(f"Gmail rejected the access token; {RERUN_HINT}")
    if response.status >= 300:
        raise GoogleError(
            f"http_{response.status}", error_detail(response, "Gmail refused the send")
        )
    return str(response.json().get("id", ""))

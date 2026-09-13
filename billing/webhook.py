"""Stripe webhook signature verification, before the body is ever parsed.

This is ``slackbot/slack.py``'s rule applied to a second vendor, and for a
sharper reason: a forged Slack event wastes a model call, while a forged
``checkout.session.completed`` grants compute nobody paid for. The signature
is checked against the **raw bytes** -- parsing first and re-serialising would
verify a different string than the one Stripe signed.

Stripe's header looks like::

    Stripe-Signature: t=1614556800,v1=5257a8...,v1=1c0a3d...

The signed payload is ``f"{t}.{raw_body}"``, HMAC-SHA256 under the endpoint's
signing secret, hex-encoded. Several ``v1`` values can appear at once while a
secret is being rolled; any one matching is a pass.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from .errors import WebhookSignatureError

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SignatureHeader",
    "parse_signature_header",
    "verify_signature",
]

#: Stripe's own recommendation. An older timestamp is a replay, not a slow
#: network -- the same five minutes ``slackbot`` allows Slack.
DEFAULT_TOLERANCE_SECONDS = 300

#: Canonical shapes only. Anything else is refused before it reaches int() or
#: hmac.compare_digest, both of which have non-uniform failure modes.
_TIMESTAMP_RE = re.compile(r"[0-9]{1,12}")
_HEX_RE = re.compile(r"[0-9a-f]{64}")

#: Stripe sends one v1, or two while a signing secret is being rolled. A
#: header carrying thousands is not a rollover, it is someone making us run
#: thousands of constant-time comparisons per request.
MAX_SIGNATURES = 8


@dataclass(frozen=True)
class SignatureHeader:
    timestamp: int
    signatures: tuple[str, ...]


def parse_signature_header(header: str) -> SignatureHeader:
    """Split the ``Stripe-Signature`` header into its parts."""
    if not header:
        raise WebhookSignatureError("no Stripe-Signature header")
    timestamp: int | None = None
    sigs: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            # A plain int() accepts "+1788732011", "1_788_732_011", full-width
            # unicode digits and 10^400 -- the last raising OverflowError out
            # of the comparison below rather than the one uniform error this
            # module promises. Canonical shapes only.
            if not _TIMESTAMP_RE.fullmatch(value):
                raise WebhookSignatureError("unparseable timestamp in Stripe-Signature")
            timestamp = int(value)
        elif key == "v1":
            if not _HEX_RE.fullmatch(value):
                raise WebhookSignatureError("malformed v1 value in Stripe-Signature")
            sigs.append(value)
    if timestamp is None or not sigs:
        raise WebhookSignatureError("Stripe-Signature is missing t or v1")
    if len(sigs) > MAX_SIGNATURES:
        raise WebhookSignatureError("Stripe-Signature carries too many v1 values")
    return SignatureHeader(timestamp=timestamp, signatures=tuple(sigs))


def verify_signature(
    payload: bytes,
    header: str,
    secret: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Raise :class:`WebhookSignatureError` unless ``payload`` is genuine.

    Returns ``None`` on success rather than ``True``: a caller that writes
    ``if verify(...)`` and forgets the ``if`` gets a pass either way, and a
    caller that forgets to call this at all is the bug this shape cannot fix
    but a bare boolean actively invites.
    """
    if not secret:
        raise WebhookSignatureError("no webhook signing secret configured")
    if not isinstance(payload, (bytes, bytearray)):
        raise WebhookSignatureError(
            "payload must be the raw request bytes, not a parsed object"
        )

    parsed = parse_signature_header(header)
    current = time.time() if now is None else now
    if abs(current - parsed.timestamp) > tolerance_seconds:
        raise WebhookSignatureError(
            "Stripe-Signature timestamp is outside the tolerance window"
        )

    signed = b"%d.%s" % (parsed.timestamp, bytes(payload))
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    for candidate in parsed.signatures:
        if hmac.compare_digest(expected, candidate):
            return
    raise WebhookSignatureError("no Stripe-Signature v1 value matched")


def sign_payload(payload: bytes, secret: str, *, timestamp: int) -> str:
    """Build a header the way Stripe would. For tests and local replay only."""
    signed = b"%d.%s" % (timestamp, payload)
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"

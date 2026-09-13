"""Signature verification, which runs before the body is ever parsed."""

from __future__ import annotations

import time

import pytest

from billing.errors import WebhookSignatureError
from billing.webhook import sign_payload, verify_signature

SECRET = "whsec_testsecret"
BODY = b'{"id":"evt_1","type":"checkout.session.completed"}'


def test_a_genuine_signature_passes():
    now = int(time.time())
    verify_signature(BODY, sign_payload(BODY, SECRET, timestamp=now), SECRET)


def test_a_tampered_body_fails():
    now = int(time.time())
    header = sign_payload(BODY, SECRET, timestamp=now)
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY.replace(b"evt_1", b"evt_2"), header, SECRET)


def test_a_stale_timestamp_is_a_replay():
    old = int(time.time()) - 4000
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY, sign_payload(BODY, SECRET, timestamp=old), SECRET)


def test_a_future_timestamp_is_also_refused():
    ahead = int(time.time()) + 4000
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY, sign_payload(BODY, SECRET, timestamp=ahead), SECRET)


def test_multiple_v1_values_pass_if_any_matches():
    """Stripe sends several while a signing secret is being rolled."""
    now = int(time.time())
    good = sign_payload(BODY, SECRET, timestamp=now).split("v1=")[1]
    header = f"t={now},v1={'d' * 64},v1={good}"
    verify_signature(BODY, header, SECRET)


@pytest.mark.parametrize(
    "header", ["", "t=123", "v1=abc", "nonsense", "t=notanint,v1=abc"]
)
def test_malformed_headers_raise_one_undifferentiated_error(header):
    """One error for every shape: distinguishing them tells an attacker which
    half of the check failed."""
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY, header, SECRET)


def test_an_empty_secret_never_passes():
    now = int(time.time())
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY, sign_payload(BODY, "", timestamp=now), "")


def test_a_parsed_dict_is_refused_not_re_encoded():
    """Verifying a re-serialised object checks a different string than the one
    Stripe signed, so the type is rejected outright."""
    now = int(time.time())
    with pytest.raises(WebhookSignatureError):
        header = sign_payload(BODY, SECRET, timestamp=now)
        verify_signature({"id": "evt_1"}, header, SECRET)


@pytest.mark.parametrize(
    "value",
    ["+1788732011", "1_788_732_011", "１７８８７３２０１１", str(10**40), "0x123"],
)
def test_non_canonical_timestamps_raise_the_uniform_error(value):
    """A bare int() accepts all of these; the last one raised OverflowError
    out of the comparison instead of the one error this module promises."""
    header = f"t={value},v1={'a' * 64}"
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY, header, SECRET)


def test_a_malformed_v1_value_raises_the_uniform_error():
    now = int(time.time())
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY, f"t={now},v1=not-hex", SECRET)


# --------------------------------------------------------------------- fuzz
def test_no_header_shape_escapes_as_anything_but_the_uniform_error():
    """The header is fully attacker-controlled and reaches int(), a dict-free
    split, and hmac.compare_digest -- each with its own failure mode.

    The module promises exactly one error type for every rejection. Anything
    else escapes as a 500 from the HTTP route, which both leaks that something
    unusual happened and makes Stripe retry a request it should not.
    """
    import random

    rng = random.Random(1234)
    alphabet = list("t=,v0123456789abcdefzZ +-_.\t\n\x00é１２３𝟙") + ["v1=", "t=", "=="]
    for _ in range(4000):
        header = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        try:
            verify_signature(BODY, header, SECRET)
        except WebhookSignatureError:
            pass
        except Exception as exc:  # noqa: BLE001 - that is the point of the test
            raise AssertionError(
                f"{type(exc).__name__} escaped for header {header!r}"
            ) from exc


def test_a_giant_header_is_refused_rather_than_chewed_on():
    huge = "t=1788732011," + ",".join(f"v1={'a' * 64}" for _ in range(5000))
    with pytest.raises(WebhookSignatureError):
        verify_signature(BODY, huge, SECRET)


def test_the_payload_is_never_coerced():
    """str, memoryview, None -- anything but real bytes must be refused, or a
    caller could verify a re-encoded body that Stripe never signed."""
    now = int(time.time())
    header = sign_payload(BODY, SECRET, timestamp=now)
    for payload in [BODY.decode(), None, 42, ["evt"], {"id": "evt"}]:
        with pytest.raises(WebhookSignatureError):
            verify_signature(payload, header, SECRET)

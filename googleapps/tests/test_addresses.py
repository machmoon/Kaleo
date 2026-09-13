"""Address hygiene: what may reach a MIME header or an attendee list."""

from __future__ import annotations

import pytest

from googleapps.addresses import validate_addresses
from googleapps.transport import GoogleError


@pytest.mark.parametrize(
    "address",
    [
        "lead@example.com",
        "first.last+tag@sub.example.co.uk",
        "  padded@example.com  ",  # stripped, not refused
        "james_oneil@example.com",
    ],
)
def test_plain_addresses_pass(address):
    assert validate_addresses([address], what="--email") == [address.strip()]


@pytest.mark.parametrize(
    "address",
    [
        "",
        "not-an-address",
        "two@@example.com",
        "no-domain@",
        "no-dot@example",
        "a@example.com, b@example.com",  # two in one flag
        "a@example.com\nBcc: x@evil.example",  # header injection
        "a@example.com\r\nSubject: hi",
        '"quoted"@example.com',
        "Lead <lead@example.com>",  # display-name form; the header would fold it
        "a b@example.com",
    ],
)
def test_anything_that_could_break_a_header_is_refused(address):
    with pytest.raises(GoogleError, match="bad_address"):
        validate_addresses([address], what="--email")


def test_every_bad_address_is_named_at_once():
    with pytest.raises(GoogleError) as excinfo:
        validate_addresses(["ok@example.com", "bad one", "worse"], what="--attendee")
    message = str(excinfo.value)
    assert "--attendee" in message
    assert "'bad one'" in message and "'worse'" in message
    assert "ok@example.com" not in message

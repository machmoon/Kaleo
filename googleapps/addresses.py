"""Email-address hygiene for everything that becomes a header or an invite.

Two reasons to check before a paid run rather than let Google answer:

- ``To`` is a MIME header. An address carrying a newline, a comma or a
  quote would fold, split or inject a second header, and the stdlib
  ``email`` package does not refuse it -- it faithfully writes what it was
  given. A recipient list is the one place user input reaches a header.
- ``--email`` and ``--attendee`` are typed by hand, and a typo discovered
  after the pipeline has spent its model calls is the wrong time.

This is deliberately not RFC 5322 in full; it is the subset that keeps a
header well-formed and refuses the things a shell can smuggle in.
"""

from __future__ import annotations

import re

from .transport import GoogleError

__all__ = ["validate_addresses"]

#: One ``@``, a dotted domain, and none of the characters that would break
#: a header list: whitespace, separators, brackets, quotes, backslash.
_FORBIDDEN = r"\s@,;:<>\"'()\[\]\\"
_ADDRESS = re.compile(
    rf"^[^{_FORBIDDEN}]+@[^{_FORBIDDEN}.][^{_FORBIDDEN}]*\.[^{_FORBIDDEN}]+$"
)


def validate_addresses(addresses: list[str], *, what: str) -> list[str]:
    """The addresses, stripped, or a refusal naming every bad one at once.

    ``what`` names the flag or field in the message (``--email``), so the
    user knows which list to fix.
    """
    cleaned = [address.strip() for address in addresses]
    bad = [address for address in cleaned if not _ADDRESS.match(address)]
    if bad:
        raise GoogleError(
            "bad_address",
            f"{what} needs plain email addresses (one per flag); refused: "
            + ", ".join(repr(address) for address in bad),
        )
    return cleaned

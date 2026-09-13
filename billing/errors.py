"""Every failure this package can produce, named.

The convention is ``engine/silkscreen/spice/errors.py``'s: nothing returns a
quiet zero or a falsy default. A billing path that fails silently either
charges someone twice or hands out compute nobody paid for, so every failure
raises something specific enough to act on.
"""

from __future__ import annotations

__all__ = [
    "BillingError",
    "ConfigError",
    "LedgerError",
    "ReplayedEvent",
    "SpendCapExceeded",
    "StripeError",
    "WebhookSignatureError",
]


class BillingError(Exception):
    """Base for everything in this package."""


class ConfigError(BillingError):
    """Configuration is missing or malformed. Names what, never the value."""


class StripeError(BillingError):
    """Stripe answered with an error, or the transport could not reach it."""

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class WebhookSignatureError(BillingError):
    """The Stripe-Signature header is absent, malformed, stale, or wrong.

    Deliberately one error for all four cases: an endpoint that distinguishes
    them in its response tells an attacker which half of the check failed.
    """


class LedgerError(BillingError):
    """The ledger was asked to do something that would corrupt the balance."""


class ReplayedEvent(BillingError):
    """This Stripe event id has already been applied.

    Not a bug -- Stripe delivers at least once, so this is the normal path for
    a retry. Callers answer 200 and move on. It is an exception rather than a
    bool because the one thing that must never happen is a caller forgetting
    to check and crediting twice.
    """

    def __init__(self, event_id: str) -> None:
        super().__init__(f"event {event_id} was already applied")
        self.event_id = event_id


class SpendCapExceeded(BillingError):
    """An auto-reload would breach the account's own monthly ceiling."""

    def __init__(
        self, *, would_spend_cents: int, cap_cents: int, spent_cents: int
    ) -> None:
        super().__init__(
            f"auto-reload of {would_spend_cents}c would exceed the "
            f"{cap_cents}c monthly cap ({spent_cents}c already spent)"
        )
        self.would_spend_cents = would_spend_cents
        self.cap_cents = cap_cents
        self.spent_cents = spent_cents

"""Who is paying, as a seam rather than a decision.

This repo has no user, session, or auth concept today, and inventing one
inside the billing package would be the wrong place to put it. So an account
is an opaque string behind a Protocol -- the same move ``agents/`` makes with
``Model`` and ``googleapps/`` makes with ``Transport``.

Everything downstream (ledger, webhooks, reserve/commit, spend caps) is built
and tested against this, so whichever identity lands later -- an email from
the Checkout customer, a device-bound licence key, real auth -- plugs in here
and no billing code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .errors import ConfigError

__all__ = [
    "AccountId",
    "AccountResolver",
    "SingleAccountResolver",
    "StripeCustomerResolver",
]

#: Conservative on purpose: an account id reaches Stripe metadata and comes
#: back through a webhook, so it must survive a round trip with no escaping.
_ACCOUNT_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")


@dataclass(frozen=True, order=True)
class AccountId:
    value: str

    def __post_init__(self) -> None:
        if not _ACCOUNT_RE.match(self.value):
            raise ConfigError(
                "account id must be 1-128 chars of [A-Za-z0-9_.:-]; "
                f"got {len(self.value)} chars"
            )

    def __str__(self) -> str:
        return self.value


class AccountResolver(Protocol):
    """Turn whatever identifies a caller into an :class:`AccountId`."""

    def resolve(self, *, subject: str | None) -> AccountId: ...


class SingleAccountResolver:
    """One account for the whole install. The honest default for today.

    Kaleo is a desktop app with no login, so "the person at this machine" is
    the only subject that exists. This makes that explicit instead of
    pretending there is multi-tenancy.
    """

    def __init__(self, account: str = "local") -> None:
        self._account = AccountId(account)

    def resolve(self, *, subject: str | None = None) -> AccountId:
        return self._account


class StripeCustomerResolver:
    """Account id *is* the Stripe customer id (``cus_...``).

    Available the moment Checkout completes, so it needs no auth system --
    but it only identifies someone who has already paid once, which is why it
    is not the default.
    """

    def resolve(self, *, subject: str | None) -> AccountId:
        if not subject:
            raise ConfigError("no Stripe customer id to resolve an account from")
        if not subject.startswith("cus_"):
            raise ConfigError("expected a Stripe customer id starting with 'cus_'")
        return AccountId(subject)

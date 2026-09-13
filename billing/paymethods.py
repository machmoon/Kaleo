"""Who to charge, when nobody is at the keyboard.

Overage is billed *after* the compute was delivered, off-session, which means
``charge_overage`` needs two Stripe ids that only exist because the customer
was once present: a ``cus_...`` and a ``pm_...``. Until now nothing in the
package kept them, so the settlement path was complete except for the one
fact that makes it runnable.

Three decisions worth stating:

* **This stores tokens, not card details.** ``pm_...`` is a Stripe handle. It
  cannot be turned back into a PAN, it is scoped to the account that issued
  it, and it is useless to anyone without the API key. That is the whole point
  of the SetupIntent flow — the app never sees, transmits or stores a number.
  It is still account-linking data, so the file is the ledger's, mode 0600 by
  the directory it lives in, and nothing here is ever logged.

* **One pair per account, replaced not accumulated.** A customer who updates
  their card should be charged on the new one; keeping a history would mean
  choosing between them at settlement time, and the wrong choice is a decline
  on compute already delivered.

* **``mode == "setup"`` is the event that carries it**, and
  ``fulfillment.py`` currently *ignores* setup-mode sessions by design — they
  grant no credit. Ignoring the credit is right; ignoring the payment method
  was the gap. Recording is therefore separate from granting and happens for
  both modes.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from .accounts import AccountId
from .errors import ConfigError

__all__ = [
    "MemoryPaymentMethods",
    "PaymentMethodStore",
    "SavedMethod",
    "SqlitePaymentMethods",
    "method_from_session",
]

#: Stripe object ids. Checked because these are read out of a webhook body and
#: then interpolated into an API path and a charge; a value that is not an id
#: is either a bug upstream or an attempt at one.
_CUSTOMER_RE = re.compile(r"\Acus_[A-Za-z0-9]{1,64}\Z")
_METHOD_RE = re.compile(r"\Apm_[A-Za-z0-9]{1,64}\Z")


@dataclass(frozen=True)
class SavedMethod:
    customer: str
    payment_method: str
    #: Excluded from equality on purpose: two records naming the same card
    #: *are* the same card, and comparing the write time instead makes every
    #: "is this still the card we saved" check depend on a clock.
    updated_at: float = field(default=0.0, compare=False)

    def __post_init__(self) -> None:
        if not _CUSTOMER_RE.match(self.customer):
            raise ConfigError("a Stripe customer id looks like cus_...")
        if not _METHOD_RE.match(self.payment_method):
            raise ConfigError("a Stripe payment method id looks like pm_...")

    def __repr__(self) -> str:
        """Truncated. This object lands in the frame of any settlement error,
        and an account-linking id has no business in a traceback."""
        return (
            f"SavedMethod({self.customer[:8]}…, {self.payment_method[:7]}…)"
        )


class PaymentMethodStore(Protocol):
    def remember(self, account: AccountId, method: SavedMethod) -> None: ...
    def lookup(self, account: AccountId) -> SavedMethod | None: ...
    def forget(self, account: AccountId) -> bool: ...


class MemoryPaymentMethods:
    """The offline stand-in, per ``MemoryLedger``."""

    def __init__(self) -> None:
        self._saved: dict[str, SavedMethod] = {}

    def remember(self, account: AccountId, method: SavedMethod) -> None:
        self._saved[str(account)] = method

    def lookup(self, account: AccountId) -> SavedMethod | None:
        return self._saved.get(str(account))

    def forget(self, account: AccountId) -> bool:
        return self._saved.pop(str(account), None) is not None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_methods (
    account        TEXT PRIMARY KEY,
    customer       TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    updated_at     REAL NOT NULL DEFAULT 0
);
"""


class SqlitePaymentMethods:
    """Durable, on the ledger's own connection.

    Sharing the ledger's connection and lock is deliberate: recording the
    payment method and granting the credit both come out of one webhook, and
    they should not be able to half-commit. Two connections to one file would
    also mean two write locks and a ``database is locked`` under exactly the
    concurrency this is meant to survive.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        lock: threading.RLock | None = None,
        *,
        clock=time.time,
    ) -> None:
        self._db = db
        self._lock = lock or threading.RLock()
        self._clock = clock
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def remember(self, account: AccountId, method: SavedMethod) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO payment_methods (account, customer, payment_method, "
                "updated_at) VALUES (?,?,?,?) ON CONFLICT(account) DO UPDATE SET "
                "customer=excluded.customer, "
                "payment_method=excluded.payment_method, "
                "updated_at=excluded.updated_at",
                (
                    str(account),
                    method.customer,
                    method.payment_method,
                    method.updated_at or self._clock(),
                ),
            )
            self._db.commit()

    def lookup(self, account: AccountId) -> SavedMethod | None:
        with self._lock:
            row = self._db.execute(
                "SELECT customer, payment_method, updated_at FROM payment_methods "
                "WHERE account = ?",
                (str(account),),
            ).fetchone()
        if row is None:
            return None
        return SavedMethod(
            customer=row[0], payment_method=row[1], updated_at=row[2]
        )

    def forget(self, account: AccountId) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM payment_methods WHERE account = ?", (str(account),)
            )
            self._db.commit()
            return cursor.rowcount > 0


def method_from_session(session: dict) -> SavedMethod | None:
    """Pull the pair out of a Checkout Session, or return ``None``.

    ``None`` rather than an exception on every miss, because most sessions
    legitimately have neither: a one-off purchase without ``customer_creation``
    has no customer, and a session whose payment method was not saved has no
    ``pm_``. Only a *malformed* id raises, via :class:`SavedMethod`.

    Stripe expands these two differently depending on the call, so both the
    string form (``"cus_x"``) and the expanded object (``{"id": "cus_x"}``)
    are accepted. Reading only one shape is a bug that shows up as "overage
    never settles" long after the webhook that dropped it.
    """
    customer = _id_of(session.get("customer"))
    method = _id_of(session.get("payment_method")) or _id_of(
        (session.get("setup_intent") or {}).get("payment_method")
        if isinstance(session.get("setup_intent"), dict)
        else None
    )
    if not method:
        intent = session.get("payment_intent")
        if isinstance(intent, dict):
            method = _id_of(intent.get("payment_method"))
    if not customer or not method:
        return None
    return SavedMethod(customer=customer, payment_method=method)


def _id_of(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        found = value.get("id")
        return found if isinstance(found, str) and found else None
    return None

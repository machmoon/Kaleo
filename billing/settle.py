"""Running a settlement end to end.

Every piece of this existed and none of them were joined: ``begin_settlement``
froze a quantity, ``charge_overage`` could bill a saved card, ``settle_overage``
recorded the result — but nothing knew *which* card, because nothing kept the
``cus_``/``pm_`` pair. :mod:`billing.paymethods` closes that, and this module
is the twenty lines that make the path runnable.

The ordering is the whole content of this file, and each step is where a
specific incident would otherwise happen:

1. **Freeze the quantity first.** ``begin_settlement`` fixes what the charge is
   for. Re-reading the balance after the charge — the obvious way to write
   this — means a run finishing during the round trip has its compute written
   off unbillable, or a top-up landing during it leaves a *successful* charge
   with no ledger entry.
2. **Key idempotency on the settlement, not the clock.** A retried settlement
   without a stable key bills the same overage twice, and nobody is present to
   notice. ``settle_id`` is that key.
3. **A declined or unauthenticated charge abandons the intent, it does not
   consume it.** The compute was delivered; refusing to try again would be
   writing it off, and cutting the account off mid-run would burn the model
   calls already spent and deliver nothing.
4. **``authentication_required`` is not a retry.** The bank wants the customer
   present. Retrying off-session fails identically forever; the answer is to
   ask them, which is what the returned outcome says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .accounts import AccountId
from .config import BillingConfig
from .errors import ReplayedEvent, StripeError
from .stripe_api import charge_overage
from .transport import Transport, urllib_transport

__all__ = ["SettlementResult", "settle_account"]


@dataclass(frozen=True)
class SettlementResult:
    #: ``settled`` | ``nothing_owed`` | ``no_payment_method`` |
    #: ``needs_customer`` | ``declined`` | ``replayed``
    action: str
    account: AccountId
    mkcu: int = 0
    cents: int = 0
    charge_id: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.action in {"settled", "nothing_owed", "replayed"}


def settle_account(
    ledger: Any,
    config: BillingConfig,
    account: AccountId,
    *,
    transport: Transport = urllib_transport,
) -> SettlementResult:
    """Charge this account's outstanding overage on its saved card."""
    # Checked before `begin_settlement`, which raises when nothing is owed.
    # A settlement sweep runs over every account, and most of them owe
    # nothing; crashing on the healthy ones would stop the sweep before it
    # reached the account that actually needed charging.
    if ledger.overage_mkcu(account) <= 0:
        return SettlementResult(action="nothing_owed", account=account)

    intent = ledger.begin_settlement(account)
    if intent.mkcu <= 0 or intent.cents <= 0:
        ledger.abandon_settlement(intent)
        return SettlementResult(action="nothing_owed", account=account)

    saved = ledger.payment_methods.lookup(account)
    if saved is None:
        # Not an error and not a cutoff: the compute is delivered and still
        # owed. The account needs a card on file, which is a SetupIntent
        # session, and the run that earned this must not be punished for it.
        ledger.abandon_settlement(intent)
        return SettlementResult(
            action="no_payment_method",
            account=account,
            mkcu=intent.mkcu,
            cents=intent.cents,
            detail="no saved card; send them through a setup session",
        )

    try:
        charge = charge_overage(
            config,
            account,
            amount_cents=intent.cents,
            customer=saved.customer,
            payment_method=saved.payment_method,
            transport=transport,
            # Keyed on the settlement, not the clock. See rule 2 above.
            idempotency_key=intent.settle_id,
        )
    except StripeError as exc:
        ledger.abandon_settlement(intent)
        needs_customer = exc.code == "authentication_required"
        return SettlementResult(
            action="needs_customer" if needs_customer else "declined",
            account=account,
            mkcu=intent.mkcu,
            cents=intent.cents,
            detail=exc.args[0] if exc.args else str(exc),
        )

    charge_id = str(charge.get("id") or "")
    if not charge_id:
        # A 2xx with no id means money may have moved and we cannot name the
        # movement. Abandoning is the safe half: the overage stays owed and a
        # later settlement retries under the same idempotency key, so Stripe
        # returns the original charge rather than making a second one.
        ledger.abandon_settlement(intent)
        return SettlementResult(
            action="declined",
            account=account,
            mkcu=intent.mkcu,
            cents=intent.cents,
            detail="Stripe returned no charge id",
        )

    try:
        entry = ledger.settle_overage(
            intent, charge_id=charge_id, cents_paid=intent.cents
        )
    except ReplayedEvent:
        # This charge was already recorded — the idempotency key did its job
        # on a retry. Normal, not an error.
        return SettlementResult(
            action="replayed",
            account=account,
            mkcu=intent.mkcu,
            cents=intent.cents,
            charge_id=charge_id,
        )

    return SettlementResult(
        action="settled",
        account=account,
        mkcu=entry.delta_mkcu,
        cents=intent.cents,
        charge_id=charge_id,
    )

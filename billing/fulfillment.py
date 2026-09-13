"""Turning a signed Stripe event into compute, correctly, exactly once.

The rule Stripe states plainly and that integrations get wrong: **fulfil from
the event handler, never from the success page.** A customer can pay and then
lose their connection before the redirect loads; anything that only runs on
the landing page silently drops that order.

The subtler half, which is the actual bug most integrations ship:
``checkout.session.completed`` is **not** proof of payment. With a
delayed-notification method the completed event arrives while the session is
still ``unpaid``. Fulfilling on it alone grants compute for payments that
later fail, *and* never fulfils the ones that eventually succeed -- because
those arrive as ``checkout.session.async_payment_succeeded``. So this module
handles both and gates on ``payment_status``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .accounts import AccountId
from .errors import ConfigError, LedgerError, ReplayedEvent
from .ledger import MemoryLedger
from .paymethods import method_from_session

__all__ = ["FULFILLABLE_EVENTS", "Outcome", "handle_event"]

#: The only event types that may grant compute, plus the failure we record.
FULFILLABLE_EVENTS = frozenset(
    {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
)
#: Off-session charges -- overage settlement and auto-reload -- arrive as
#: PaymentIntent events, not Checkout events. Without these the entire overage
#: path had no way to be confirmed asynchronously.
OFF_SESSION_EVENTS = frozenset(
    {"payment_intent.succeeded", "payment_intent.payment_failed"}
)
FAILURE_EVENTS = frozenset({"checkout.session.async_payment_failed"})
#: Money going back out. These must reverse compute, not merely be noted.
REVERSAL_EVENTS = frozenset(
    {"charge.refunded", "charge.dispute.created", "charge.dispute.closed"}
)


@dataclass(frozen=True)
class Outcome:
    """What the handler did. ``granted_mkcu`` is 0 for every non-grant path."""

    #: Every value this can take. A caller switching on it needs the real set:
    #: granted, replayed, pending, failed, ignored, reversed, unlinkable_reversal.
    action: str
    granted_mkcu: int = 0
    account: AccountId | None = None
    detail: str = ""


def _remember_method(ledger: Any, session: dict[str, Any]) -> None:
    """Save the pair off-session settlement needs, if this event carries it.

    Never raises into the webhook path. A malformed id is worth ignoring, not
    worth a non-2xx: Stripe would retry the same event for days, and no amount
    of retrying will make a bad id good -- meanwhile the grant that shares the
    event would be blocked behind it.
    """
    store = getattr(ledger, "payment_methods", None)
    if store is None:
        return
    metadata = session.get("metadata") or {}
    account_raw = metadata.get("kaleo_account") or session.get("client_reference_id")
    if not account_raw:
        return
    try:
        method = method_from_session(session)
        if method is not None:
            store.remember(AccountId(account_raw), method)
    except (ConfigError, ValueError):
        return


def _session_of(event: dict[str, Any]) -> dict[str, Any]:
    obj = event.get("data", {}).get("object", {})
    return obj if isinstance(obj, dict) else {}


def handle_event(event: dict[str, Any], ledger: MemoryLedger) -> Outcome:
    """Apply one **already signature-verified** event to the ledger.

    Never call this with an unverified body. Verification lives in
    ``webhook.verify_signature`` and is a separate step on purpose: this
    function's job is the state machine, and folding the two together makes it
    possible to test one while accidentally skipping the other.
    """
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.startswith("evt_"):
        raise LedgerError("event has no usable Stripe event id")

    kind = event.get("type", "")
    if kind in OFF_SESSION_EVENTS:
        return _off_session(event, kind)
    if kind in REVERSAL_EVENTS:
        return _reverse(event, kind, ledger)
    if kind not in FULFILLABLE_EVENTS and kind not in FAILURE_EVENTS:
        return Outcome(action="ignored", detail=f"unhandled type {kind!r}")

    session = _session_of(event)

    # Record the cus_/pm_ pair BEFORE any of the mode and status gates below.
    # A setup-mode session is exactly the event that carries a saved card, and
    # the gate a few lines down ignores it -- correctly, since it grants no
    # credit. Ignoring the credit was right; ignoring the payment method was
    # the gap that left overage settlement unrunnable.
    _remember_method(ledger, session)

    if kind in FAILURE_EVENTS:
        return Outcome(action="failed", detail=f"{kind}; no compute granted")

    # Gate on MODE before anything else. A setup-mode session (saving a card
    # for overage) is a legitimate, signed, expected event that carries no
    # line items and no credit metadata. Letting it fall through to the
    # payment path raised LedgerError, which any HTTP route maps to a non-2xx,
    # which makes Stripe retry the same event for up to three days and can
    # get the endpoint disabled -- taking real purchases down with it.
    mode = session.get("mode")
    if mode is not None and mode != "payment":
        return Outcome(action="ignored", detail=f"mode {mode!r} is not a purchase")

    payment_status = session.get("payment_status")
    if payment_status == "unpaid":
        # The delayed-notification path. Do nothing and wait for
        # async_payment_succeeded; do NOT mark the event applied, or the
        # later success event for the same session still works but this one
        # would have consumed the id.
        return Outcome(action="pending", detail="session completed but still unpaid")
    if payment_status not in {"paid", "no_payment_required"}:
        return Outcome(
            action="ignored", detail=f"unexpected payment_status {payment_status!r}"
        )

    metadata = session.get("metadata") or {}
    account_raw = metadata.get("kaleo_account") or session.get("client_reference_id")
    # A signed-but-unusable event is never worth raising over: raising makes
    # the route answer non-2xx, and no amount of retrying will add the
    # metadata. Ignore it loudly instead.
    if not account_raw:
        return Outcome(action="ignored", detail=f"{event_id} carries no kaleo_account")
    try:
        credit_mkcu = int(metadata.get("kaleo_credit_mkcu", 0))
    except (TypeError, ValueError):
        return Outcome(action="ignored", detail=f"{event_id} has a bad credit amount")
    if credit_mkcu <= 0:
        return Outcome(action="ignored", detail=f"{event_id} would grant {credit_mkcu}")

    try:
        account = AccountId(account_raw)
    except ConfigError as exc:
        # Signed, paid, and unusable -- the same class as every other case in
        # this function, and it has to answer the same way. `AccountId`
        # refuses anything outside its charset, and `client_reference_id` is
        # settable from a Payment Link or the Dashboard, so an email address
        # there is entirely ordinary. Letting this escape made it a 500, and
        # a non-2xx is what Stripe retries for days before disabling the
        # endpoint: the module docstring's own named failure mode, reached by
        # the one branch that did not guard.
        return Outcome(
            action="ignored",
            detail=f"{event_id} carries an unusable kaleo_account: {exc}",
        )
    amount_cents = session.get("amount_total") or 0

    # Dedupe on the SESSION, not the delivery. One paid session legitimately
    # emits more than one event with different ids -- completed and
    # async_payment_succeeded both name it -- and an event-id key grants the
    # pack once per event. The session is the unit of fulfillment.
    session_id = session.get("id")
    if not isinstance(session_id, str) or not session_id.startswith("cs_"):
        return Outcome(action="ignored", detail=f"{event_id} names no Checkout Session")
    pi = session.get("payment_intent")
    charge_ref = pi if isinstance(pi, str) else None

    try:
        ledger.grant(
            account,
            credit_mkcu,
            reason=f"{kind} {session_id} via {event_id}",
            event_id=f"cs:{session_id}",
            charge_id=charge_ref,
            cents_paid=int(amount_cents),
        )
    except ReplayedEvent:
        # Stripe retried. This is the normal path, not an error: answer 200 or
        # Stripe keeps retrying for days.
        return Outcome(action="replayed", account=account, detail=event_id)

    return Outcome(action="granted", granted_mkcu=credit_mkcu, account=account)


def _off_session(event: dict[str, Any], kind: str) -> Outcome:
    """Report an off-session charge; never grant from it.

    The grant for a settlement is written by ``settle_overage`` at the moment
    the charge returns, against a frozen quantity. Granting again here would
    double it, so this path exists to *surface* the outcome -- especially
    ``authentication_required``, where the bank wants the customer and the
    compute has already been delivered.
    """
    obj = event.get("data", {}).get("object", {})
    obj = obj if isinstance(obj, dict) else {}
    if obj.get("metadata", {}).get("kaleo_kind") != "overage":
        return Outcome(action="ignored", detail=f"{kind}; not a Hardy overage charge")
    if kind == "payment_intent.succeeded":
        return Outcome(
            action="ignored", detail=f"overage charge {obj.get('id')} succeeded"
        )
    err = obj.get("last_payment_error") or {}
    return Outcome(
        action="failed",
        detail=f"overage charge {obj.get('id')} failed: {err.get('code') or 'unknown'}",
    )


def _reverse(event: dict[str, Any], kind: str, ledger: MemoryLedger) -> Outcome:
    """A refund or chargeback: take the compute back, not just note it.

    Without this, buy-spend-refund is free compute: the money returns, the
    balance stays where it is, and nothing is ever owed. The grant is found
    by the charge / payment_intent id recorded on it at fulfillment time --
    which is why ``Entry.charge_id`` exists.
    """
    # Idempotency first, business validation second -- the same ordering rule
    # settle_overage needed. A redelivered reversal is a replay, and answering
    # anything else makes the caller report the wrong thing about real money.
    event_id = event.get("id")
    if isinstance(event_id, str) and ledger.was_applied(f"rev:{event_id}"):
        return Outcome(action="replayed", detail=str(event_id))

    obj = event.get("data", {}).get("object", {})
    if not isinstance(obj, dict):
        return Outcome(action="ignored", detail="reversal carries no object")
    if kind == "charge.dispute.closed" and obj.get("status") == "won":
        return Outcome(action="ignored", detail="dispute won; nothing to reverse")

    charge_ref = obj.get("payment_intent") or obj.get("charge") or obj.get("id")
    if not isinstance(charge_ref, str):
        return Outcome(action="ignored", detail="reversal names no charge")

    grants = [
        e
        for e in ledger.entries()
        if e.charge_id == charge_ref and e.kind == "grant" and e.delta_mkcu > 0
    ]
    if not grants:
        # Real and worth surfacing: money went back for something this ledger
        # cannot link. Reported, never silently swallowed.
        return Outcome(
            action="unlinkable_reversal", detail=f"no grant for {charge_ref}"
        )

    granted = sum(e.delta_mkcu for e in grants)
    paid = sum(e.cents_paid for e in grants) or 1
    refunded_cents = obj.get("amount_refunded")
    if not isinstance(refunded_cents, int) or refunded_cents <= 0:
        refunded_cents = paid
    # Proportional, and rounded UP against the account: a partial refund must
    # not leave a sliver of unpaid compute behind.
    revoke = min(granted, -(-granted * min(refunded_cents, paid) // paid))

    # Already-reversed compute must not be revoked twice. A charge can be
    # partially refunded more than once, and can be refunded AND disputed, so
    # the key is per EVENT while the amount is capped by what is left.
    already = sum(
        -e.delta_mkcu
        for e in ledger.entries()
        if e.charge_id == charge_ref and e.kind == "adjustment" and e.delta_mkcu < 0
    )
    revoke = max(0, min(revoke, granted - already))
    if revoke == 0:
        return Outcome(action="ignored", detail=f"{charge_ref} already fully reversed")

    account = grants[0].account
    try:
        ledger.adjust(
            account,
            -revoke,
            reason=f"{kind} {charge_ref}",
            charge_id=charge_ref,
            event_id=f"rev:{event_id}",
        )
    except ReplayedEvent:
        return Outcome(action="replayed", account=account, detail=str(event_id))
    return Outcome(action="reversed", granted_mkcu=-revoke, account=account)

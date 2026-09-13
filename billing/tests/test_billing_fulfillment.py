"""The state machine that turns a signed event into compute, exactly once.

Every test here is a real incident that integrations ship.
"""

from __future__ import annotations

import pytest

from billing.accounts import AccountId
from billing.fulfillment import handle_event
from billing.ledger import MemoryLedger


def event(
    event_id="evt_1",
    kind="checkout.session.completed",
    payment_status="paid",
    account="local",
    credit="8888",
    amount_total=2000,
):
    return {
        "id": event_id,
        "type": kind,
        "data": {
            "object": {
                "id": "cs_test_1",
                "mode": "payment",
                "payment_status": payment_status,
                "amount_total": amount_total,
                "client_reference_id": account,
                "metadata": {"kaleo_account": account, "kaleo_credit_mkcu": credit},
            }
        },
    }


def test_a_paid_session_grants_compute():
    ledger = MemoryLedger()
    out = handle_event(event(), ledger)
    assert out.action == "granted"
    assert out.granted_mkcu == 8888
    assert ledger.balance(AccountId("local")).available_mkcu == 8888


def test_a_redelivered_event_does_not_grant_twice():
    """Stripe delivers at least once and retries any non-2xx for days."""
    ledger = MemoryLedger()
    handle_event(event(), ledger)
    out = handle_event(event(), ledger)
    assert out.action == "replayed"
    assert ledger.balance(AccountId("local")).available_mkcu == 8888


def test_completed_but_unpaid_grants_nothing():
    """The delayed-notification trap: `completed` is not proof of payment."""
    ledger = MemoryLedger()
    out = handle_event(event(payment_status="unpaid"), ledger)
    assert out.action == "pending"
    assert ledger.balance(AccountId("local")).available_mkcu == 0


def test_the_later_async_success_still_fulfils_that_session():
    """And the pending event must not have consumed the id, or this is lost."""
    ledger = MemoryLedger()
    handle_event(event(event_id="evt_a", payment_status="unpaid"), ledger)
    out = handle_event(
        event(event_id="evt_b", kind="checkout.session.async_payment_succeeded"), ledger
    )
    assert out.action == "granted"
    assert ledger.balance(AccountId("local")).available_mkcu == 8888


def test_async_failure_grants_nothing():
    ledger = MemoryLedger()
    out = handle_event(event(kind="checkout.session.async_payment_failed"), ledger)
    assert out.action == "failed"
    assert ledger.balance(AccountId("local")).available_mkcu == 0


def test_unrelated_event_types_are_ignored_not_crashed():
    ledger = MemoryLedger()
    assert handle_event(event(kind="customer.created"), ledger).action == "ignored"


@pytest.mark.parametrize("credit", ["0", "-100", "8888.5", "banana"])
def test_a_malformed_credit_is_ignored_not_raised(credit):
    """Ignored, deliberately, not raised.

    Raising makes the HTTP route answer non-2xx, and Stripe then retries the
    same event for up to three days -- for an event no amount of retrying can
    fix. Repeated failures can get the endpoint disabled, which takes real
    purchases down with it.
    """
    ledger = MemoryLedger()
    out = handle_event(event(credit=credit), ledger)
    assert out.action == "ignored"
    assert ledger.entries() == []


def test_an_event_without_an_account_is_ignored():
    ledger = MemoryLedger()
    bad = event()
    bad["data"]["object"]["metadata"] = {"kaleo_credit_mkcu": "8888"}
    bad["data"]["object"]["client_reference_id"] = None
    assert handle_event(bad, ledger).action == "ignored"


def test_a_setup_mode_session_is_ignored_not_treated_as_a_purchase():
    """Saving a card for overage is a legitimate signed event with no line
    items. Falling through to the payment path poisoned the endpoint."""
    ledger = MemoryLedger()
    setup = {
        "id": "evt_setup",
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_9", "mode": "setup",
                            "payment_status": "no_payment_required"}},
    }
    out = handle_event(setup, ledger)
    assert out.action == "ignored" and ledger.entries() == []


def test_two_events_for_one_session_grant_the_pack_once():
    """Stripe emits completed AND async_payment_succeeded for the same paid
    session, with different event ids. Deduping on the event id granted twice
    for one payment."""
    ledger = MemoryLedger()
    first = event(event_id="evt_1")
    second = event(event_id="evt_2", kind="checkout.session.async_payment_succeeded")
    assert handle_event(first, ledger).action == "granted"
    assert handle_event(second, ledger).action == "replayed"
    assert ledger.balance(AccountId("local")).available_mkcu == 8888


def test_a_refund_takes_the_compute_back():
    """Otherwise buy-spend-refund is free compute."""
    ledger = MemoryLedger()
    ev = event()
    ev["data"]["object"]["payment_intent"] = "pi_1"
    handle_event(ev, ledger)
    ledger.reserve(AccountId("local"), 8000, run_id="r1")
    ledger.commit("r1", 8000)
    refund = {
        "id": "evt_refund",
        "type": "charge.refunded",
        "data": {"object": {"id": "ch_1", "payment_intent": "pi_1",
                            "amount_refunded": 2000}},
    }
    out = handle_event(refund, ledger)
    assert out.action == "reversed"
    # They spent 8000 of a refunded 8888 pack, so they now owe for what they used.
    assert ledger.overage_mkcu(AccountId("local")) == 8000


def test_an_unlinkable_refund_is_surfaced_not_swallowed():
    ledger = MemoryLedger()
    refund = {
        "id": "evt_r",
        "type": "charge.refunded",
        "data": {"object": {"id": "ch_x", "payment_intent": "pi_unknown"}},
    }
    assert handle_event(refund, ledger).action == "unlinkable_reversal"


def test_a_dispute_won_reverses_nothing():
    ledger = MemoryLedger()
    ev = {"id": "evt_d", "type": "charge.dispute.closed",
          "data": {"object": {"id": "dp_1", "status": "won"}}}
    assert handle_event(ev, ledger).action == "ignored"


def test_the_grant_amount_comes_from_the_signed_event_only():
    """Not from anything a client sent. Doubling metadata doubles the grant --
    which is exactly why metadata is written server-side at session creation
    and the event is signature-verified before it reaches here."""
    ledger = MemoryLedger()
    handle_event(event(credit="17776"), ledger)
    assert ledger.balance(AccountId("local")).available_mkcu == 17776


# ------------------------------------------- findings from the OSS research
def test_a_charge_refunded_twice_does_not_crash_or_over_revoke():
    """A charge can be partially refunded more than once, and can be refunded
    AND disputed. The second reversal used to hit ReplayedEvent inside
    adjust() and escape uncaught."""
    ledger = MemoryLedger()
    ev = event()
    ev["data"]["object"]["payment_intent"] = "pi_1"
    handle_event(ev, ledger)

    def refund(event_id, amount):
        return {
            "id": event_id,
            "type": "charge.refunded",
            "data": {"object": {"id": "ch_1", "payment_intent": "pi_1",
                                "amount_refunded": amount}},
        }

    first = handle_event(refund("evt_r1", 1000), ledger)
    second = handle_event(refund("evt_r2", 1000), ledger)
    assert first.action == "reversed" and second.action == "reversed"
    total_revoked = -sum(
        e.delta_mkcu for e in ledger.entries() if e.kind == "adjustment"
    )
    # Never more than was granted, however many reversals arrive.
    assert 0 < total_revoked <= 8888


def test_the_same_reversal_event_twice_is_a_replay_not_a_crash():
    ledger = MemoryLedger()
    ev = event()
    ev["data"]["object"]["payment_intent"] = "pi_1"
    handle_event(ev, ledger)
    refund = {
        "id": "evt_r",
        "type": "charge.refunded",
        "data": {"object": {"id": "ch_1", "payment_intent": "pi_1",
                            "amount_refunded": 2000}},
    }
    assert handle_event(refund, ledger).action == "reversed"
    assert handle_event(refund, ledger).action == "replayed"


def test_an_off_session_overage_charge_is_reported_never_granted():
    """The grant is written by settle_overage against a frozen quantity;
    granting again here would double it."""
    ledger = MemoryLedger()
    ok = {
        "id": "evt_pi",
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_9", "metadata": {"kaleo_kind": "overage"}}},
    }
    out = handle_event(ok, ledger)
    assert out.action == "ignored" and ledger.entries() == []


def test_an_off_session_failure_surfaces_the_reason():
    ledger = MemoryLedger()
    bad = {
        "id": "evt_pf",
        "type": "payment_intent.payment_failed",
        "data": {"object": {"id": "pi_9", "metadata": {"kaleo_kind": "overage"},
                            "last_payment_error": {"code": "authentication_required"}}},
    }
    out = handle_event(bad, ledger)
    assert out.action == "failed"
    assert "authentication_required" in out.detail


def test_an_unrelated_payment_intent_is_ignored():
    ledger = MemoryLedger()
    other = {
        "id": "evt_x",
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_other", "metadata": {}}},
    }
    assert handle_event(other, ledger).action == "ignored"


def test_an_unusable_account_id_is_ignored_rather_than_raising():
    """A paid session that 500s is a multi-day Stripe retry storm.

    ``AccountId`` refuses anything outside ``[A-Za-z0-9_.:-]{1,128}``, and
    ``client_reference_id`` can be set from a Payment Link or the Dashboard,
    so an email address arriving there is ordinary rather than hostile.
    Escaping as a ``ConfigError`` made the webhook answer 500; Stripe retries
    a non-2xx for days and then disables the endpoint -- the failure this
    module's docstring names. Every other "signed but unusable" branch answers
    ``ignored``, and this was the one that did not.
    """
    ledger = MemoryLedger()
    event = {
        "id": "evt_bad_account",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_unusable",
                "payment_status": "paid",
                "amount_total": 2000,
                "metadata": {
                    "kaleo_account": "user@example.com",
                    "kaleo_credit_mkcu": "1000",
                },
            }
        },
    }
    outcome = handle_event(event, ledger=ledger)
    assert outcome.action == "ignored"
    assert "unusable kaleo_account" in outcome.detail

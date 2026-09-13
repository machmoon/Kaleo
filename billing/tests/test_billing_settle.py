"""Overage settlement, end to end and offline.

The pair that makes this runnable — a ``cus_`` and a ``pm_`` — had nowhere to
live until ``paymethods.py``, so this path existed in pieces and had never
been driven from one end to the other. These tests drive it, and most of them
are about what happens when the charge does *not* succeed, because that is
where money and delivered compute can quietly diverge.
"""

from __future__ import annotations

import json

import pytest

from billing.accounts import AccountId
from billing.errors import ConfigError, StripeError
from billing.fulfillment import handle_event
from billing.ledger import MemoryLedger, OveragePolicy
from billing.paymethods import (
    MemoryPaymentMethods,
    SavedMethod,
    method_from_session,
)
from billing.settle import settle_account
from billing.sqlite_ledger import SqliteLedger
from billing.transport import HttpResponse

ACCOUNT = AccountId("desk")
SAVED = SavedMethod(customer="cus_TESTONLY", payment_method="pm_TESTONLY")


def owing(mkcu: int = 3000) -> MemoryLedger:
    """A ledger with delivered-but-unpaid compute on it."""
    ledger = MemoryLedger()
    ledger.reserve(ACCOUNT, mkcu, run_id="r", overage=OveragePolicy())
    ledger.commit("r", mkcu)
    return ledger


class Stripe:
    def __init__(self, *answers):
        self.answers = list(answers) or [(200, {"id": "pi_TESTONLY"})]
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, payload = (
            self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        )
        return HttpResponse(status=status, body=json.dumps(payload).encode())


# -- reading the pair off a session ----------------------------------------


def test_a_setup_session_yields_the_pair():
    session = {
        "customer": "cus_A",
        "setup_intent": {"payment_method": "pm_B"},
    }
    assert method_from_session(session) == SavedMethod("cus_A", "pm_B")


def test_both_the_string_and_the_expanded_object_are_read():
    """Stripe expands these differently per call. Reading only one shape shows
    up as "overage never settles", long after the webhook that dropped it."""
    assert method_from_session(
        {"customer": {"id": "cus_A"}, "payment_method": {"id": "pm_B"}}
    ) == SavedMethod("cus_A", "pm_B")


def test_a_payment_intent_can_carry_the_method():
    assert method_from_session(
        {"customer": "cus_A", "payment_intent": {"payment_method": "pm_B"}}
    ) == SavedMethod("cus_A", "pm_B")


def test_a_session_with_neither_is_none_rather_than_an_error():
    # Most sessions legitimately have neither; raising would make the webhook
    # answer non-2xx and Stripe retry for days over nothing.
    assert method_from_session({}) is None
    assert method_from_session({"customer": "cus_A"}) is None


def test_a_malformed_id_is_refused_rather_than_stored():
    with pytest.raises(ConfigError):
        SavedMethod(customer="not-a-customer", payment_method="pm_B")
    with pytest.raises(ConfigError):
        SavedMethod(customer="cus_A", payment_method="4242424242424242")


def test_the_repr_does_not_spell_out_the_ids():
    # This object lands in the frame of any settlement error.
    text = repr(SAVED)
    assert "pm_TESTONLY" not in text
    assert "cus_TESTONLY" not in text


# -- the webhook records it ------------------------------------------------


def test_a_setup_session_saves_the_card_even_though_it_grants_nothing():
    """The gap this closes: `handle_event` ignores setup-mode sessions, which
    is right for credit and was wrong for the payment method — so the card
    the customer had just saved was thrown away."""
    ledger = MemoryLedger()
    outcome = handle_event(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "mode": "setup",
                    "customer": "cus_A",
                    "setup_intent": {"payment_method": "pm_B"},
                    "metadata": {"kaleo_account": "desk"},
                }
            },
        },
        ledger,
    )
    assert outcome.action == "ignored"  # still grants nothing
    assert ledger.payment_methods.lookup(ACCOUNT) == SavedMethod("cus_A", "pm_B")


def test_a_bad_id_in_a_signed_event_is_ignored_not_raised():
    """Raising makes the route answer non-2xx, Stripe retries for days, and
    the grant sharing that event is blocked behind a bad id no retry fixes."""
    ledger = MemoryLedger()
    outcome = handle_event(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": 2000,
                    "customer": "cus_A",
                    "payment_method": "pm_../../evil",
                    "metadata": {"kaleo_account": "desk", "kaleo_credit_mkcu": "800"},
                }
            },
        },
        ledger,
    )
    assert outcome.action == "granted"  # the purchase still lands
    assert ledger.payment_methods.lookup(ACCOUNT) is None


def test_a_replaced_card_wins_rather_than_accumulating():
    # Charging the old card after an update is a decline on compute already
    # delivered.
    store = MemoryPaymentMethods()
    store.remember(ACCOUNT, SavedMethod("cus_A", "pm_OLD"))
    store.remember(ACCOUNT, SavedMethod("cus_A", "pm_NEW"))
    assert store.lookup(ACCOUNT).payment_method == "pm_NEW"


# -- settling --------------------------------------------------------------


def test_a_settlement_charges_the_saved_card_and_records_it(config):
    ledger = owing(3000)
    ledger.payment_methods.remember(ACCOUNT, SAVED)
    stripe = Stripe((200, {"id": "pi_1"}))

    result = settle_account(ledger, config, ACCOUNT, transport=stripe)

    assert result.action == "settled"
    assert result.charge_id == "pi_1"
    assert ledger.overage_mkcu(ACCOUNT) == 0
    form = dict(
        pair.split("=", 1)
        for pair in (stripe.requests[-1].body or b"").decode().split("&")
    )
    assert form["customer"] == "cus_TESTONLY"
    assert form["payment_method"] == "pm_TESTONLY"
    assert form["off_session"] == "true"
    assert form["confirm"] == "true"


def test_the_idempotency_key_is_the_settlement_not_the_clock(config):
    """A retried settlement without a stable key bills the same overage twice,
    and nobody is present to notice."""
    ledger = owing(3000)
    ledger.payment_methods.remember(ACCOUNT, SAVED)
    stripe = Stripe((200, {"id": "pi_1"}))
    settle_account(ledger, config, ACCOUNT, transport=stripe)
    key = stripe.requests[-1].headers["Idempotency-Key"]
    assert key.startswith("stl_")


def test_no_saved_card_leaves_the_debt_owed_rather_than_cutting_off(config):
    """The compute is delivered. Writing it off or cutting the account off
    both punish a run for a missing card."""
    ledger = owing(3000)
    before = ledger.overage_mkcu(ACCOUNT)
    stripe = Stripe()

    result = settle_account(ledger, config, ACCOUNT, transport=stripe)

    assert result.action == "no_payment_method"
    assert result.ok is False
    assert ledger.overage_mkcu(ACCOUNT) == before
    assert stripe.requests == []  # nothing was charged


def test_a_decline_abandons_the_intent_so_it_can_be_retried(config):
    ledger = owing(3000)
    ledger.payment_methods.remember(ACCOUNT, SAVED)
    stripe = Stripe((402, {"error": {"code": "card_declined", "message": "no"}}))

    result = settle_account(ledger, config, ACCOUNT, transport=stripe)

    assert result.action == "declined"
    assert ledger.overage_mkcu(ACCOUNT) == 3000
    # And the same debt can be settled later, on a working card.
    ledger.payment_methods.remember(ACCOUNT, SavedMethod("cus_A", "pm_GOOD"))
    again = settle_account(
        ledger, config, ACCOUNT, transport=Stripe((200, {"id": "pi_2"}))
    )
    assert again.action == "settled"


def test_a_bank_asking_for_the_customer_is_not_a_retry(config):
    """Retrying off-session fails identically forever; the answer is to ask
    them, so the outcome has to say which of the two it is."""
    ledger = owing(3000)
    ledger.payment_methods.remember(ACCOUNT, SAVED)
    stripe = Stripe(
        (402, {"error": {"code": "authentication_required", "message": "3DS"}})
    )
    result = settle_account(ledger, config, ACCOUNT, transport=stripe)
    assert result.action == "needs_customer"
    assert ledger.overage_mkcu(ACCOUNT) == 3000


def test_a_success_with_no_charge_id_keeps_the_debt_and_the_key(config):
    """Money may have moved and we cannot name the movement. Leaving it owed
    means the retry reuses the idempotency key, so Stripe returns the original
    charge instead of making a second one."""
    ledger = owing(3000)
    ledger.payment_methods.remember(ACCOUNT, SAVED)
    result = settle_account(ledger, config, ACCOUNT, transport=Stripe((200, {})))
    assert result.action == "declined"
    assert ledger.overage_mkcu(ACCOUNT) == 3000


def test_nothing_owed_does_not_call_stripe(config):
    ledger = MemoryLedger()
    ledger.grant(ACCOUNT, 5000, reason="pack")
    stripe = Stripe()
    result = settle_account(ledger, config, ACCOUNT, transport=stripe)
    assert result.action == "nothing_owed"
    assert result.ok is True
    assert stripe.requests == []


def test_the_whole_path_works_on_the_durable_ledger(tmp_path, config):
    """The card is saved by one webhook and used by a settlement after a
    restart — which is the only way this ever happens in practice."""
    path = tmp_path / "ledger.sqlite3"
    ledger = SqliteLedger(path)
    handle_event(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "mode": "setup",
                    "customer": "cus_A",
                    "setup_intent": {"payment_method": "pm_B"},
                    "metadata": {"kaleo_account": "desk"},
                }
            },
        },
        ledger,
    )
    ledger.reserve(ACCOUNT, 3000, run_id="r", overage=OveragePolicy())
    ledger.commit("r", 3000)
    ledger.close()

    restarted = SqliteLedger(path)
    assert restarted.payment_methods.lookup(ACCOUNT) == SavedMethod("cus_A", "pm_B")
    result = settle_account(
        restarted, config, ACCOUNT, transport=Stripe((200, {"id": "pi_9"}))
    )
    assert result.action == "settled"
    assert restarted.overage_mkcu(ACCOUNT) == 0
    restarted.close()


def test_forgetting_a_card_is_reported(tmp_path):
    ledger = SqliteLedger(tmp_path / "l.sqlite3")
    ledger.payment_methods.remember(ACCOUNT, SAVED)
    assert ledger.payment_methods.forget(ACCOUNT) is True
    assert ledger.payment_methods.forget(ACCOUNT) is False
    assert ledger.payment_methods.lookup(ACCOUNT) is None
    ledger.close()


def test_an_unknown_stripe_error_is_a_decline_not_a_crash(config):
    ledger = owing(3000)
    ledger.payment_methods.remember(ACCOUNT, SAVED)

    def broken(request):
        raise StripeError("could not reach Stripe: down")

    result = settle_account(ledger, config, ACCOUNT, transport=broken)
    assert result.action == "declined"
    assert ledger.overage_mkcu(ACCOUNT) == 3000

"""Ledger invariants: append-only, no overdraw, holds before debits."""

from __future__ import annotations

import pytest

from billing.accounts import AccountId
from billing.errors import LedgerError, ReplayedEvent, SpendCapExceeded
from billing.ledger import MemoryLedger, TopUpPolicy

ACC = AccountId("local")


def funded(amount=8888):
    ledger = MemoryLedger()
    ledger.grant(ACC, amount, reason="pack", event_id="evt_seed", cents_paid=2000)
    return ledger


def test_balance_is_a_fold_not_a_stored_number():
    ledger = funded()
    total = sum(e.delta_mkcu for e in ledger.entries(ACC))
    assert ledger.balance(ACC).available_mkcu == total


def test_a_reservation_reduces_what_is_spendable_before_any_debit():
    ledger = funded()
    ledger.reserve(ACC, 2000, run_id="r1")
    assert ledger.balance(ACC).available_mkcu == 8888  # nothing debited yet
    assert ledger.balance(ACC).spendable_mkcu == 6888


def test_two_concurrent_runs_cannot_jointly_overdraw():
    """The reason holds exist. Without them both checks pass and the account
    goes negative once both commit."""
    ledger = funded(3000)
    ledger.reserve(ACC, 2000, run_id="r1")
    with pytest.raises(LedgerError):
        ledger.reserve(ACC, 2000, run_id="r2")


def test_a_failed_run_costs_nothing():
    ledger = funded()
    ledger.reserve(ACC, 2000, run_id="r1")
    ledger.release("r1")
    assert ledger.balance(ACC).spendable_mkcu == 8888


def test_commit_charges_actual_not_the_estimate():
    ledger = funded()
    ledger.reserve(ACC, 2000, run_id="r1")
    ledger.commit("r1", 1500)
    assert ledger.balance(ACC).available_mkcu == 8888 - 1500
    assert ledger.balance(ACC).held_mkcu == 0


def test_committing_or_releasing_twice_raises():
    ledger = funded()
    ledger.reserve(ACC, 2000, run_id="r1")
    ledger.commit("r1", 1000)
    with pytest.raises(LedgerError):
        ledger.commit("r1", 1000)
    with pytest.raises(LedgerError):
        ledger.release("r1")


def test_the_same_run_cannot_hold_twice():
    ledger = funded()
    ledger.reserve(ACC, 100, run_id="r1")
    with pytest.raises(LedgerError):
        ledger.reserve(ACC, 100, run_id="r1")


def test_a_replayed_grant_raises_rather_than_crediting():
    ledger = funded()
    with pytest.raises(ReplayedEvent):
        ledger.grant(ACC, 8888, reason="retry", event_id="evt_seed")


def test_grants_without_an_event_id_are_allowed_but_not_deduped():
    """Manual adjustments have no Stripe event; they are deliberately exempt."""
    ledger = funded()
    ledger.grant(ACC, 100, reason="support credit")
    ledger.grant(ACC, 100, reason="support credit")
    assert ledger.balance(ACC).available_mkcu == 9088


def test_autoreload_fires_only_below_the_threshold():
    ledger = funded(400)
    policy = TopUpPolicy(enabled=True, threshold_mkcu=500)
    assert ledger.needs_topup(ACC, policy)
    ledger.grant(ACC, 1000, reason="top up")
    assert not ledger.needs_topup(ACC, policy)


def test_autoreload_is_off_unless_asked_for():
    ledger = funded(0) if False else MemoryLedger()
    assert not ledger.needs_topup(ACC, TopUpPolicy())


def test_the_monthly_cap_stops_a_runaway_agent():
    """This bills an agent. A retry loop is the normal failure mode, and
    auto-reload without a ceiling turns it into an unbounded card charge."""
    ledger = funded()  # already paid 2000c
    with pytest.raises(SpendCapExceeded):
        ledger.check_topup_allowed(ACC, TopUpPolicy(enabled=True, topup_cents=2000,
                                                    monthly_cap_cents=3000))


def test_accounts_are_isolated():
    ledger = funded()
    other = AccountId("someone-else")
    assert ledger.balance(other).available_mkcu == 0
    with pytest.raises(LedgerError):
        ledger.reserve(other, 1, run_id="r9")


# ---------------------------------------------------------------- overage
from billing.ledger import OveragePolicy  # noqa: E402


def test_a_run_finishes_past_zero_instead_of_being_cut_off():
    """Cutting off mid-run burns the model calls and delivers nothing."""
    ledger = funded(1000)
    ledger.reserve(ACC, 4000, run_id="r1", overage=OveragePolicy())
    ledger.commit("r1", 4000)
    assert ledger.balance(ACC).available_mkcu == -3000
    assert ledger.overage_mkcu(ACC) == 3000


def test_the_credit_line_is_finite():
    """'No cutoff' without a limit is unlimited free compute for anyone."""
    ledger = funded(1000)
    policy = OveragePolicy(limit_mkcu=5000)
    ledger.reserve(ACC, 6000, run_id="r1", overage=policy)
    ledger.commit("r1", 6000)
    with pytest.raises(LedgerError):
        ledger.reserve(ACC, 1, run_id="r2", overage=policy)


def test_overage_is_off_unless_a_policy_is_passed():
    ledger = funded(1000)
    with pytest.raises(LedgerError):
        ledger.reserve(ACC, 4000, run_id="r1")


def in_overage(amount=4000, funded_with=1000):
    ledger = funded(funded_with)
    ledger.reserve(ACC, amount, run_id="r1", overage=OveragePolicy())
    ledger.commit("r1", amount)
    return ledger


def test_settlement_returns_the_balance_to_zero():
    ledger = in_overage()
    intent = ledger.begin_settlement(ACC)
    ledger.settle_overage(intent, charge_id="ch_1", cents_paid=intent.cents)
    assert ledger.balance(ACC).available_mkcu == 0
    assert ledger.overage_mkcu(ACC) == 0


def test_a_retried_settlement_cannot_zero_the_balance_twice():
    """Otherwise the second one grants the difference as free compute."""
    ledger = in_overage()
    intent = ledger.begin_settlement(ACC)
    ledger.settle_overage(intent, charge_id="ch_1", cents_paid=intent.cents)
    with pytest.raises(ReplayedEvent):
        ledger.settle_overage(intent, charge_id="ch_1", cents_paid=intent.cents)


def test_settling_with_nothing_owed_raises():
    ledger = funded()
    with pytest.raises(LedgerError):
        ledger.begin_settlement(ACC)


def test_usage_during_the_charge_window_stays_owed():
    """THE bug this two-phase shape exists to prevent.

    The single-phase version re-read the balance when the money came back, so
    a run finishing during the Stripe round trip had its compute written off
    unbillable -- silently, with no entry left that remembered it.
    """
    ledger = in_overage()                       # owes 3000
    intent = ledger.begin_settlement(ACC)       # charge built for exactly 3000
    assert intent.mkcu == 3000
    ledger.reserve(ACC, 1000, run_id="r2", overage=OveragePolicy())
    ledger.commit("r2", 1000)                   # 1000 more, mid-flight
    entry = ledger.settle_overage(intent, charge_id="ch_1", cents_paid=intent.cents)
    assert entry.delta_mkcu == 3000             # exactly what was paid for
    assert ledger.overage_mkcu(ACC) == 1000     # the rest is still owed


def test_a_purchase_during_the_charge_window_never_voids_the_payment():
    """The mirror case: a top-up landing mid-flight used to make a SUCCESSFUL
    charge unrecordable -- money taken, no ledger entry at all."""
    ledger = in_overage()
    intent = ledger.begin_settlement(ACC)
    ledger.grant(ACC, 8888, reason="pack", event_id="cs:cs_2", cents_paid=2000)
    entry = ledger.settle_overage(intent, charge_id="ch_1", cents_paid=intent.cents)
    assert entry.delta_mkcu == intent.mkcu
    assert ledger.was_applied("ch_1")


def test_an_abandoned_settlement_leaves_the_debt_outstanding():
    ledger = in_overage()
    intent = ledger.begin_settlement(ACC)
    ledger.abandon_settlement(intent)
    assert ledger.overage_mkcu(ACC) == 3000


def test_the_cap_is_monthly_not_lifetime():
    """A lifetime cap disables auto-reload permanently after a few weeks of
    normal paid use, and the customer's runs then start failing."""
    import time as _t

    old = MemoryLedger(clock=lambda: _t.time() - 70 * 86400)
    old.grant(ACC, 8888, reason="old pack", event_id="evt_old", cents_paid=9999)
    assert old.spent_cents_this_period(ACC, now=_t.time()) == 0


def test_an_orphan_hold_can_be_found_and_released():
    """A crash between reserve and commit otherwise strands the hold forever."""
    import time as _t

    now = _t.time()
    ledger = MemoryLedger(clock=lambda: now - 7200)
    ledger.grant(ACC, 8888, reason="pack", event_id="evt_seed")
    ledger.reserve(ACC, 2000, run_id="dead")
    assert len(ledger.holds(ACC)) == 1
    ledger._clock = lambda: now
    stale = ledger.expire_stale_holds(older_than_seconds=3600)
    assert len(stale) == 1
    assert ledger.balance(ACC).spendable_mkcu == 8888


def test_settlement_fires_before_the_limit_is_reached():
    """Small and frequent beats one large charge at the credit line."""
    ledger = funded(1000)
    policy = OveragePolicy(limit_mkcu=5000, settle_at_mkcu=2000)
    ledger.reserve(ACC, 3500, run_id="r1", overage=policy)
    ledger.commit("r1", 3500)
    assert ledger.overage_mkcu(ACC) == 2500
    assert ledger.needs_settlement(ACC, policy)


def test_cancelling_a_run_late_is_not_free():
    """release() used to refund the hold unconditionally, which made
    cancelling after the model calls were spent strictly cheaper than
    finishing. LiteLLM ships the opposite rule for the same reason."""
    ledger = funded()
    ledger.reserve(ACC, 4000, run_id="r1")
    entry = ledger.release("r1", consumed_mkcu=3000, reason="user cancelled")
    assert entry is not None and entry.delta_mkcu == -3000
    assert entry.reserved_mkcu == 4000       # estimate-vs-actual survives
    assert ledger.balance(ACC).available_mkcu == 8888 - 3000


def test_a_run_that_did_nothing_is_still_free():
    ledger = funded()
    ledger.reserve(ACC, 4000, run_id="r1")
    assert ledger.release("r1") is None
    assert ledger.balance(ACC).available_mkcu == 8888


def test_commit_records_what_was_reserved():
    """The one number that says whether the estimator is safe used to be
    destroyed at commit."""
    ledger = funded()
    ledger.reserve(ACC, 4000, run_id="r1")
    entry = ledger.commit("r1", 1500)
    assert entry.reserved_mkcu == 4000 and entry.delta_mkcu == -1500


def test_a_charged_back_account_loses_its_credit_line():
    """Otherwise the same money leaves twice: the issuer claws the payment
    back and the account then draws a full credit line of fresh compute."""
    ledger = funded()
    ledger.adjust(ACC, -9000, reason="chargeback", event_id="rev:evt_1")
    with pytest.raises(LedgerError):
        ledger.reserve(ACC, 100, run_id="r1", overage=OveragePolicy())


def test_idempotency_keys_are_namespaced_by_kind():
    """One flat set held event ids, session ids and charge ids together.
    Formance shipped exactly that and migrated off it."""
    ledger = funded()
    assert ledger.applied_kind("evt_seed") == "grant"
    ledger.adjust(ACC, -10, reason="fix", event_id="rev:evt_9")
    assert ledger.applied_kind("rev:evt_9") == "adjustment"


def test_a_stale_hold_is_swept_when_the_next_run_reserves():
    """expire_stale_holds() existed and nothing ever called it."""
    import time as _t

    now = _t.time()
    ledger = MemoryLedger(clock=lambda: now - 7200)
    ledger.grant(ACC, 8888, reason="pack", event_id="evt_seed")
    ledger.reserve(ACC, 4000, run_id="dead")
    ledger._clock = lambda: now
    ledger.reserve(ACC, 1000, run_id="alive")
    assert [h.run_id for h in ledger.holds(ACC)] == ["alive"]

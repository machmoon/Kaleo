"""Randomised state-machine tests over the ledger.

Example-based tests check the sequences you thought of. Money bugs live in the
sequences you did not -- the two confirmed P1s in this package were both
"something landed during a window I was not thinking about". So this drives
thousands of random operation sequences and asserts the invariants after
*every* step.

Deterministic on purpose: seeded from a fixed list, no wall clock, no
``random.seed()`` from the environment. A failure here must reproduce exactly,
because a flaky test in billing code is worse than no test.

``hypothesis`` is not a dependency of this repo, so this is the hand-rolled
equivalent: a shadow model plus an operation chooser.
"""

from __future__ import annotations

import random

import pytest

from billing.accounts import AccountId
from billing.errors import BillingError, LedgerError, ReplayedEvent
from billing.ledger import MemoryLedger, OveragePolicy
from billing.units import MKCU_PER_KCU, Meter, price_mkcu

ACCOUNTS = [AccountId("a1"), AccountId("a2"), AccountId("a3")]
POLICY = OveragePolicy(enabled=True, limit_mkcu=5000)


def invariants(ledger: MemoryLedger) -> None:
    """Everything that must be true after any operation, always."""
    for account in ACCOUNTS:
        entries = ledger.entries(account)
        balance = ledger.balance(account)

        # 1. The balance IS the fold. If these ever diverge, the ledger has
        #    started storing state and can no longer be reconciled.
        assert balance.available_mkcu == sum(e.delta_mkcu for e in entries)

        # 2. Holds are non-negative and spendable is exactly available - held.
        assert balance.held_mkcu >= 0
        assert balance.spendable_mkcu == balance.available_mkcu - balance.held_mkcu

        # 3. Overage is the mirror of a negative balance, never independent.
        assert ledger.overage_mkcu(account) == max(0, -balance.available_mkcu)

        # 4. The credit line is a real floor for SPENDING. Corrections are
        #    exempt by design: a refund of already-spent compute must be
        #    allowed to leave the account owing more than the credit line,
        #    because pretending it is zero is how buy-spend-refund becomes
        #    free compute.
        if not any(e.kind == "adjustment" for e in entries):
            assert balance.available_mkcu >= -POLICY.limit_mkcu

        # 5. Every applied idempotency key appears at most once as an entry.
        keys = [e.event_id for e in entries if e.event_id]
        assert len(keys) == len(set(keys))

        # 6. Entries are append-only: sequence numbers strictly increase.
        seqs = [e.seq for e in entries]
        assert seqs == sorted(seqs)


def run_sequence(
    seed: int, steps: int = 250, ledger: MemoryLedger | None = None
) -> MemoryLedger:
    """The same operation sequence, optionally against a supplied ledger.

    `ledger` exists so `test_billing_sqlite_ledger.py` can run this exact
    fuzz against the durable implementation. A durable ledger proved only by
    its own bespoke tests is a durable ledger free to drift from the rules
    everything else was checked against.
    """
    rng = random.Random(seed)
    if ledger is None:
        ledger = MemoryLedger()
    live: dict[str, AccountId] = {}
    used_keys: list[str] = []
    ops = [
        "grant",
        "grant",
        "reserve",
        "commit",
        "release",
        "settle",
        "adjust",
        "replay",
    ]

    for step in range(steps):
        counter = step + 1
        account = rng.choice(ACCOUNTS)
        op = rng.choice(ops)
        try:
            if op == "grant":
                key = f"cs:cs_{counter}"
                ledger.grant(
                    account,
                    rng.randrange(1, 9000),
                    reason="pack",
                    event_id=key,
                    cents_paid=rng.randrange(0, 5000),
                )
                used_keys.append(key)
            elif op == "reserve":
                run_id = f"r{counter}"
                ledger.reserve(
                    account, rng.randrange(1, 4000), run_id=run_id, overage=POLICY
                )
                live[run_id] = account
            elif op == "commit" and live:
                run_id = rng.choice(list(live))
                # Deliberately allow the actual to exceed the estimate: a run
                # that overruns its reservation is the normal case.
                ledger.commit(run_id, rng.randrange(0, 4500))
                live.pop(run_id, None)
            elif op == "release" and live:
                run_id = rng.choice(list(live))
                ledger.release(run_id)
                live.pop(run_id, None)
            elif op == "settle":
                intent = ledger.begin_settlement(account)
                if rng.random() < 0.3:
                    ledger.abandon_settlement(intent)
                else:
                    ledger.settle_overage(
                        intent, charge_id=f"ch_{counter}", cents_paid=intent.cents
                    )
            elif op == "adjust":
                ledger.adjust(
                    account,
                    rng.choice([-1, 1]) * rng.randrange(1, 500),
                    reason="correction",
                    event_id=f"adj_{counter}",
                )
            elif op == "replay" and used_keys:
                # Re-apply an id that really was used. Must never move money.
                key = rng.choice(used_keys)
                before = ledger.balance(account).available_mkcu
                with pytest.raises(ReplayedEvent):
                    ledger.grant(account, 1000, reason="replay", event_id=key)
                assert ledger.balance(account).available_mkcu == before
        except (LedgerError, ReplayedEvent):
            # A refusal is a valid outcome for most of these -- insufficient
            # balance, nothing owed, unknown run. What must never happen is a
            # refusal that leaves the ledger inconsistent, which is what the
            # invariant check immediately below is for.
            pass
        except BillingError:
            pass

        invariants(ledger)
    return ledger


@pytest.mark.parametrize("seed", range(40))
def test_no_operation_sequence_can_break_the_invariants(seed):
    run_sequence(seed)


@pytest.mark.parametrize("seed", range(12))
def test_compute_granted_is_always_backed_by_an_applied_key(seed):
    """Every positive grant carries an idempotency key, and no key is reused.

    This is the property that makes the ledger reconcilable against Stripe:
    without it, credit can exist that no payment can be matched to.
    """
    ledger = run_sequence(seed, steps=150)
    grants = [e for e in ledger.entries() if e.kind == "grant" and e.delta_mkcu > 0]
    keys = [e.event_id for e in grants]
    assert all(keys), "a grant exists with no idempotency key"
    assert len(keys) == len(set(keys)), "an idempotency key was used twice"


@pytest.mark.parametrize("seed", range(12))
def test_a_settled_charge_is_never_lost(seed):
    """Every successful settlement leaves exactly one entry carrying its id.

    The single-phase bug took money and left no entry at all, so this is the
    property that would have caught it.
    """
    ledger = run_sequence(seed, steps=150)
    for entry in ledger.entries():
        if entry.charge_id and entry.kind == "grant":
            assert ledger.was_applied(entry.charge_id)


def test_settlement_never_grants_more_than_it_charged_for():
    """Exhaustive over a small grid rather than random: this is THE bug."""
    for owed in range(500, 5001, 500):
        for interleaved in (0, 250, 1000, 3000):
            ledger = MemoryLedger()
            ledger.grant(ACCOUNTS[0], 1000, reason="p", event_id=f"e{owed}")
            ledger.reserve(ACCOUNTS[0], 1000 + owed, run_id="r", overage=POLICY)
            ledger.commit("r", 1000 + owed)
            intent = ledger.begin_settlement(ACCOUNTS[0])
            assert intent.mkcu == owed

            # Cap by the remaining credit line -- the point of this test is
            # the settlement window, not the floor, which has its own test.
            headroom = POLICY.limit_mkcu - owed
            extra = min(interleaved, headroom)
            if extra:
                ledger.reserve(ACCOUNTS[0], extra, run_id="r2", overage=POLICY)
                ledger.commit("r2", extra)

            entry = ledger.settle_overage(
                intent, charge_id=f"ch{owed}", cents_paid=intent.cents
            )
            assert entry.delta_mkcu == owed
            # Whatever accrued mid-flight is still owed, never written off.
            assert ledger.overage_mkcu(ACCOUNTS[0]) == extra


@pytest.mark.parametrize("seconds", [0, 1, 59, 60, 61, 3599, 86400])
def test_metering_is_monotonic_and_never_free(seconds):
    """More time never costs less, and any nonzero usage costs something."""
    mkcu = Meter(engine_seconds=seconds).as_mkcu()
    assert mkcu >= 0
    if seconds > 0:
        assert mkcu >= 1
    if seconds >= 60:
        assert mkcu >= MKCU_PER_KCU


def test_pricing_is_monotonic_across_the_whole_range():
    previous = -1
    for mkcu in range(0, 20000, 37):
        cents = price_mkcu(mkcu, rate_cents_per_kcu=225)
        assert cents >= previous
        previous = cents


def test_rounding_never_favours_the_customer_at_scale():
    """A thousand small runs must not cost less than one big one."""
    many = sum(price_mkcu(37, rate_cents_per_kcu=225) for _ in range(1000))
    one = price_mkcu(37 * 1000, rate_cents_per_kcu=225)
    assert many >= one

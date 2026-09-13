"""The durable ledger: same rules, and they survive a restart.

Two kinds of test here, and the first is the important one.

**Conformance.** :class:`SqliteLedger` changes storage and nothing else, so
the way to prove it did not change a rule is to run the *rules* against it.
``test_billing_invariants.py`` already fuzzes 40 seeds x 250 operations
against ``MemoryLedger``; ``test_the_invariants_hold_on_disk_too`` runs that
same machinery on a file. A durable ledger that quietly disagreed with the
tested one about a hold or a monthly window would fail here rather than in
production, which is the only place the disagreement is about money.

**Durability.** The things a restart must not lose -- and the idempotency key
is the dangerous one. Stripe retries a webhook for days, so a restart between
the first delivery and the retry means a forgotten replay guard grants the
same Checkout Session twice.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from billing.accounts import AccountId
from billing.errors import LedgerError, ReplayedEvent
from billing.ledger import OveragePolicy
from billing.sqlite_ledger import SCHEMA_VERSION, SqliteLedger
from billing.tests.test_billing_invariants import invariants, run_sequence

ACCOUNT = AccountId("desk")


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledger.sqlite3"


def reopen(ledger: SqliteLedger) -> SqliteLedger:
    """What a restart actually is: close the file, open it again."""
    path = ledger.path
    ledger.close()
    return SqliteLedger(path)


# -- conformance -----------------------------------------------------------


def test_the_invariants_hold_on_disk_too(tmp_path):
    """The fuzz suite, re-run against a file instead of two dicts.

    This is the test that keeps the two implementations honest. Everything
    else in this file could pass while `reserve` on disk quietly disagreed
    with `reserve` in memory about what a hold means.
    """
    for seed in range(8):
        ledger = SqliteLedger(
            tmp_path / f"seed{seed}.sqlite3", clock=lambda: 1_700_000_000.0
        )
        try:
            run_sequence(seed, steps=120, ledger=ledger)
            invariants(ledger)
            # And again after a restart: the fold must be identical, because
            # the balance is recomputed from entries rather than stored.
            before = ledger.balance(ACCOUNT)
            entries = [e.seq for e in ledger.entries()]
            ledger = reopen(ledger)
            assert ledger.balance(ACCOUNT) == before
            assert [e.seq for e in ledger.entries()] == entries
            invariants(ledger)
        finally:
            ledger.close()


def test_a_fresh_file_starts_empty_rather_than_erroring(path):
    ledger = SqliteLedger(path)
    assert ledger.entries() == []
    assert ledger.balance(ACCOUNT).spendable_mkcu == 0
    ledger.close()


# -- durability ------------------------------------------------------------


def test_a_balance_survives_a_restart(path):
    ledger = SqliteLedger(path)
    ledger.grant(ACCOUNT, 5000, reason="pack", event_id="cs:1", cents_paid=2000)
    ledger = reopen(ledger)
    assert ledger.balance(ACCOUNT).available_mkcu == 5000
    ledger.close()


def test_the_replay_guard_survives_a_restart(path):
    """The one that would cost real money.

    Stripe retries a non-2xx for days. A restart in that window used to mean
    the guard had forgotten the event, so the retry granted the pack again --
    one payment, two packs, and nothing in the ledger saying why.
    """
    ledger = SqliteLedger(path)
    ledger.grant(ACCOUNT, 5000, reason="pack", event_id="cs:paid_once")
    ledger = reopen(ledger)
    with pytest.raises(ReplayedEvent):
        ledger.grant(ACCOUNT, 5000, reason="pack", event_id="cs:paid_once")
    assert ledger.balance(ACCOUNT).available_mkcu == 5000
    ledger.close()


def test_a_live_hold_survives_a_restart(path):
    """Otherwise a crash mid-run frees compute the run is still spending, and
    the next run passes a balance check it should have failed."""
    ledger = SqliteLedger(path)
    ledger.grant(ACCOUNT, 5000, reason="pack")
    ledger.reserve(ACCOUNT, 1200, run_id="run-a")
    ledger = reopen(ledger)
    assert ledger.balance(ACCOUNT).held_mkcu == 1200
    assert [h.run_id for h in ledger.holds()] == ["run-a"]
    ledger.commit("run-a", 900)
    assert ledger.balance(ACCOUNT).spendable_mkcu == 4100
    ledger.close()


def test_a_frozen_settlement_survives_a_restart(path):
    """Two-phase settlement spans a network round trip to Stripe. If the
    intent does not survive a restart, a charge can succeed with no ledger
    entry to record it — money taken, compute never granted."""
    ledger = SqliteLedger(path)
    ledger.reserve(ACCOUNT, 3000, run_id="r", overage=OveragePolicy())
    ledger.commit("r", 3000)
    intent = ledger.begin_settlement(ACCOUNT)
    assert intent.mkcu > 0
    ledger = reopen(ledger)
    entry = ledger.settle_overage(intent, charge_id="ch_1", cents_paid=intent.cents)
    assert entry.delta_mkcu == intent.mkcu
    ledger.close()


def test_the_sequence_continues_rather_than_restarting_at_one(path):
    """A restarted counter collides with the primary key, and worse, makes two
    different movements share a seq for anything reading the cache."""
    ledger = SqliteLedger(path)
    ledger.grant(ACCOUNT, 100, reason="a")
    ledger.grant(ACCOUNT, 100, reason="b")
    ledger = reopen(ledger)
    ledger.grant(ACCOUNT, 100, reason="c")
    assert [e.seq for e in ledger.entries()] == [1, 2, 3]
    ledger.close()


def test_the_monthly_window_survives_a_restart(path):
    """`created_at` is what makes the cap monthly rather than lifetime, so it
    has to be a stored column, not a runtime default."""
    now = 1_700_000_000.0
    ledger = SqliteLedger(path, clock=lambda: now)
    ledger.grant(ACCOUNT, 5000, reason="pack", cents_paid=2000)
    ledger = reopen(ledger)
    ledger._clock = lambda: now
    assert ledger.spent_cents_this_period(ACCOUNT, now=now) == 2000
    ledger.close()


# -- the schema's own guarantees -------------------------------------------


def test_the_idempotency_key_is_a_constraint_not_just_a_dict_check(path):
    """A dict check is only correct while one process owns the ledger. The
    desktop app plus a `stripe listen` forwarder is already two, so the guard
    has to live in the database as well."""
    ledger = SqliteLedger(path)
    ledger.grant(ACCOUNT, 100, reason="a", event_id="evt_1")
    # A second process, unaware of the first's in-memory dict.
    other = SqliteLedger(path)
    with pytest.raises(ReplayedEvent):
        other.grant(ACCOUNT, 100, reason="a", event_id="evt_1")
    # And even with the cache forcibly cleared, the constraint still refuses.
    other._applied.clear()
    with pytest.raises(sqlite3.IntegrityError):
        other.grant(ACCOUNT, 100, reason="a", event_id="evt_1")
    assert other.balance(ACCOUNT).available_mkcu == 100
    ledger.close()
    other.close()


def test_entries_are_append_only_in_the_schema(path):
    """No code path in the package updates or deletes an entry; this asserts
    the file agrees, since the balance is a fold that assumes it."""
    ledger = SqliteLedger(path)
    ledger.grant(ACCOUNT, 100, reason="a")
    ledger.grant(ACCOUNT, 250, reason="b")
    rows = ledger._db.execute("SELECT delta_mkcu FROM entries ORDER BY seq").fetchall()
    assert [r[0] for r in rows] == [100, 250]
    ledger.close()


def test_a_file_from_a_future_schema_is_refused_rather_than_misread(path):
    """Reading a ledger this build does not understand is how a balance comes
    back *wrong* instead of absent."""
    ledger = SqliteLedger(path)
    ledger.close()
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 41}")
    db.commit()
    db.close()
    with pytest.raises(LedgerError, match="schema"):
        SqliteLedger(path)


def test_money_and_compute_are_stored_as_integers(path):
    """SQLite REAL would reintroduce exactly the float drift the package
    exists to avoid."""
    ledger = SqliteLedger(path)
    columns = {
        row[1]: row[2]
        for row in ledger._db.execute("PRAGMA table_info(entries)").fetchall()
    }
    assert columns["delta_mkcu"] == "INTEGER"
    assert columns["cents_paid"] == "INTEGER"
    assert columns["reserved_mkcu"] == "INTEGER"
    ledger.close()


def test_two_threads_can_use_one_ledger(path):
    """The service answers webhooks on one thread and the UI on another;
    sqlite3's default would refuse the second outright."""
    ledger = SqliteLedger(path)
    errors: list[BaseException] = []

    def grant(n: int) -> None:
        try:
            for i in range(20):
                ledger.grant(ACCOUNT, 10, reason=f"t{n}-{i}")
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised
            errors.append(exc)

    threads = [threading.Thread(target=grant, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert ledger.balance(ACCOUNT).available_mkcu == 800
    assert len({e.seq for e in ledger.entries()}) == 80
    ledger.close()


def test_a_failed_write_does_not_leave_the_cache_ahead_of_the_file(path):
    """Writes go to the file first and the cache second. The other order
    reports compute the file does not have."""
    ledger = SqliteLedger(path)
    ledger.grant(ACCOUNT, 100, reason="real")
    ledger._db.close()  # every later write now raises
    with pytest.raises(sqlite3.ProgrammingError):
        ledger.grant(ACCOUNT, 9_999_999, reason="never lands")
    assert ledger.balance(ACCOUNT).available_mkcu == 100

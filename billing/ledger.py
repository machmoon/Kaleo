"""The credit ledger: append-only, integer, and idempotent by construction.

Three rules, each of which exists because breaking it is a real incident:

1. **Append-only.** The balance is a fold over entries, never a stored mutable
   number. A stored balance that drifts cannot be reconstructed; a fold can
   always be recomputed and diffed against Stripe.

2. **Idempotent against Stripe event ids.** Stripe delivers at least once and
   retries on any non-2xx. ``slackbot/app.py`` already remembers ``event_id``
   because a Slack retry means a second paid run; here a retry means a second
   grant of compute. Applying an event that was already applied raises
   :class:`ReplayedEvent` rather than crediting again.

3. **Reserve, then commit.** This is the overlay's own rule -- *a sentence
   never spends money* -- expressed in the ledger. ``reserve`` holds an
   estimate while a run is in flight, ``commit`` converts the hold into the
   real metered cost, ``release`` returns it if the run failed. Debiting only
   at the end lets two concurrent runs each pass a balance check and jointly
   overdraw; holding first makes that impossible.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .accounts import AccountId
from .errors import LedgerError, ReplayedEvent, SpendCapExceeded
from .paymethods import MemoryPaymentMethods
from .units import price_mkcu

__all__ = [
    "Balance",
    "SettlementIntent",
    "Entry",
    "MemoryLedger",
    "OveragePolicy",
    "Reservation",
    "TOPUP_DEFAULTS",
    "TopUpPolicy",
]


@dataclass(frozen=True)
class Entry:
    """One immutable movement. ``delta_mkcu`` positive credits the account."""

    seq: int
    account: AccountId
    kind: str  # grant | debit | hold | release | adjustment
    delta_mkcu: int
    reason: str
    event_id: str | None = None
    cents_paid: int = 0
    run_id: str | None = None
    #: Unix seconds. An append-only ledger with no time cannot answer "this
    #: month", which is what a monthly cap is defined in terms of.
    created_at: float = 0.0
    #: What the run had reserved, kept on the debit so estimate-vs-actual can
    #: be measured after the fact. Without it the only number that says
    #: whether the estimator is safe is destroyed at commit time.
    reserved_mkcu: int = 0
    #: The Stripe charge or payment_intent this entry came from, so a refund
    #: can find the grant it needs to reverse. Without it a refund is
    #: unlinkable and the compute can never be clawed back.
    charge_id: str | None = None


@dataclass(frozen=True)
class Balance:
    available_mkcu: int
    held_mkcu: int

    @property
    def spendable_mkcu(self) -> int:
        return self.available_mkcu - self.held_mkcu


@dataclass(frozen=True)
class Reservation:
    run_id: str
    account: AccountId
    held_mkcu: int
    created_at: float = 0.0


@dataclass(frozen=True)
class SettlementIntent:
    """A quantity of overage frozen for one charge. See ``begin_settlement``."""

    settle_id: str
    account: AccountId
    mkcu: int
    cents: int


@dataclass(frozen=True)
class OveragePolicy:
    """Keep serving past a zero balance, and bill for it afterwards.

    A hard cutoff mid-run is the worst possible moment to stop: the engine has
    already spent the model calls and the solver time, so refusing to finish
    burns the cost and delivers nothing. Overage lets the run finish and
    charges for what it used.

    ``limit_mkcu`` is a real credit line, not a formality -- it is how much
    unpaid compute the account may accrue before it *is* cut off. Without it
    "no cutoff" means an anonymous account can mine unlimited compute.
    """

    enabled: bool = True
    limit_mkcu: int = 5000
    #: Settle automatically once unpaid usage passes this, rather than waiting
    #: for the limit. Keeps the eventual charge small and frequent.
    settle_at_mkcu: int = 2000


@dataclass(frozen=True)
class TopUpPolicy:
    """When to auto-reload, and the ceiling that makes it safe.

    The cap is not a nicety. This bills an *agent*: a loop that retries a
    failing run is the normal failure mode, and auto-reload without a ceiling
    turns that loop into an unbounded charge on a card. Devin's own model has
    the same exposure. The cap is per calendar month and is checked before
    every reload.
    """

    enabled: bool = False
    threshold_mkcu: int = 500
    topup_cents: int = 2000
    monthly_cap_cents: int = 10000


TOPUP_DEFAULTS = TopUpPolicy()

#: A run holding longer than this is assumed dead. Longer than any real run,
#: short enough that a crashed process does not strand the balance for a day.
STALE_HOLD_SECONDS = 3600.0


def _month_start(now: float) -> float:
    """Unix seconds at 00:00 UTC on the first of ``now``'s month."""
    dt = datetime.fromtimestamp(now, tz=UTC)
    return datetime(dt.year, dt.month, 1, tzinfo=UTC).timestamp()


class MemoryLedger:
    """In-process ledger. The offline stand-in, per ``MemoryFactStore``.

    This class owns every *rule*; it owns storage only through the small set
    of hooks in the "storage seam" section below. That is what makes
    :class:`billing.sqlite_ledger.SqliteLedger` a subclass of forty lines
    rather than a second copy of the rules: a durable ledger that reimplements
    reserve/commit/release is a durable ledger that will drift from this one,
    and the two will disagree about money.
    """

    def __init__(
        self,
        *,
        rate_cents_per_kcu: int = 225,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._entries: list[Entry] = []
        # Namespaced, not flat. Formance shipped a single untyped idempotency
        # column and migrated to (ledger, key) precisely because a charge id
        # and a session id colliding is silent and unrecoverable.
        self._applied: dict[str, str] = {}
        self._holds: dict[str, Reservation] = {}
        self._settlements: dict[str, SettlementIntent] = {}
        self._pending_reloads: dict[str, tuple[AccountId, int]] = {}
        #: The ``cus_``/``pm_`` pair for off-session settlement. In-memory
        #: here; :class:`~billing.sqlite_ledger.SqliteLedger` replaces it with
        #: a durable one. Attached to the ledger rather than passed around
        #: separately because every caller that settles already holds one.
        self.payment_methods = MemoryPaymentMethods()
        self._seq = itertools.count(1)
        self.rate_cents_per_kcu = rate_cents_per_kcu
        #: Injectable so a test can drive the calendar-month window.
        self._clock = clock or time.time

    # ------------------------------------------------------- storage seam
    #
    # Every mutation of persistent state goes through one of these. A
    # subclass overrides them to write through to a real store and overrides
    # nothing else; if a rule below ever mutates a dict directly again, the
    # durable ledger silently stops persisting that fact.

    def _next_seq(self) -> int:
        return next(self._seq)

    def _append(self, entry: Entry) -> Entry:
        self._entries.append(entry)
        return entry

    def _mark_applied(self, key: str, kind: str) -> None:
        self._applied[key] = kind

    def _put_hold(self, hold: Reservation) -> None:
        self._holds[hold.run_id] = hold

    def _drop_hold(self, run_id: str) -> Reservation | None:
        return self._holds.pop(run_id, None)

    def _put_settlement(self, intent: SettlementIntent) -> None:
        self._settlements[intent.settle_id] = intent

    def _drop_settlement(self, settle_id: str) -> None:
        self._settlements.pop(settle_id, None)

    # ---------------------------------------------------------------- reads
    def entries(self, account: AccountId | None = None) -> list[Entry]:
        if account is None:
            return list(self._entries)
        return [e for e in self._entries if e.account == account]

    def balance(self, account: AccountId) -> Balance:
        available = sum(e.delta_mkcu for e in self._entries if e.account == account)
        held = sum(r.held_mkcu for r in self._holds.values() if r.account == account)
        return Balance(available_mkcu=available, held_mkcu=held)

    def spent_cents_this_period(
        self, account: AccountId, *, now: float | None = None
    ) -> int:
        """Cents paid to Stripe in the current calendar month.

        Windowed, not lifetime. Summing every entry ever made the monthly cap
        a lifetime cap: a paying customer crosses it in weeks, auto-reload
        then switches off permanently, and their runs start failing on an
        empty balance. That is blocked revenue, not a safety feature.
        """
        current = self._clock() if now is None else now
        start = _month_start(current)
        return sum(
            e.cents_paid
            for e in self._entries
            if e.account == account and e.created_at >= start
        )

    def was_applied(self, event_id: str) -> bool:
        return event_id in self._applied

    def applied_kind(self, event_id: str) -> str | None:
        """Which namespace claimed this key. Useful when reconciling."""
        return self._applied.get(event_id)

    # --------------------------------------------------------------- writes
    def grant(
        self,
        account: AccountId,
        amount_mkcu: int,
        *,
        reason: str,
        event_id: str | None = None,
        cents_paid: int = 0,
        charge_id: str | None = None,
    ) -> Entry:
        """Credit compute. ``event_id`` makes it idempotent against Stripe."""
        if amount_mkcu <= 0:
            raise LedgerError("a grant must be positive")
        if event_id is not None:
            if event_id in self._applied:
                raise ReplayedEvent(event_id)
            self._mark_applied(event_id, "grant")
        return self._append(
            Entry(
                seq=self._next_seq(),
                account=account,
                kind="grant",
                delta_mkcu=amount_mkcu,
                reason=reason,
                event_id=event_id,
                cents_paid=cents_paid,
                created_at=self._clock(),
                charge_id=charge_id,
            )
        )

    def adjust(
        self,
        account: AccountId,
        delta_mkcu: int,
        *,
        reason: str,
        charge_id: str | None = None,
        event_id: str | None = None,
    ) -> Entry:
        """A correction: a refund clawback, a support credit, a write-off.

        Deliberately allowed to drive the balance negative. A refund of spent
        compute *should* leave the account owing -- pretending it is zero is
        how buy-spend-refund becomes free compute.
        """
        if delta_mkcu == 0:
            raise LedgerError("an adjustment must be non-zero")
        if event_id is not None:
            if event_id in self._applied:
                raise ReplayedEvent(event_id)
            self._mark_applied(event_id, "adjustment")
        return self._append(
            Entry(
                seq=self._next_seq(),
                account=account,
                kind="adjustment",
                delta_mkcu=delta_mkcu,
                reason=reason,
                event_id=event_id,
                charge_id=charge_id,
                created_at=self._clock(),
            )
        )

    def reserve(
        self,
        account: AccountId,
        estimate_mkcu: int,
        *,
        run_id: str,
        overage: OveragePolicy | None = None,
    ) -> Reservation:
        """Hold an estimate before a run starts.

        With an overage policy the account may draw below zero, down to
        ``-limit_mkcu``. Past that it is refused: an unbounded credit line to
        an unauthenticated account is free compute for anyone.
        """
        if estimate_mkcu <= 0:
            raise LedgerError("a reservation must be positive")
        if run_id in self._holds:
            raise LedgerError(f"run {run_id} already holds a reservation")
        # Opportunistic sweep, Helicone's pattern: a stale hold from a run
        # whose process died would otherwise be immortal, and nothing else
        # calls the sweeper. Cheap because holds are few.
        self.expire_stale_holds(older_than_seconds=STALE_HOLD_SECONDS)
        floor = -overage.limit_mkcu if (overage and overage.enabled) else 0
        spendable = self.balance(account).spendable_mkcu
        # A chargeback means the card issuer took the money back. Letting
        # that account then draw the full credit line of fresh unpaid compute
        # is the same money leaving twice.
        if floor and self._has_reversal(account):
            floor = 0
        if spendable - estimate_mkcu < floor:
            raise LedgerError(
                f"insufficient balance: {spendable} mKCU spendable, "
                f"{estimate_mkcu} mKCU needed"
                + (
                    f", overage limit {overage.limit_mkcu} mKCU reached"
                    if floor
                    else ""
                )
            )
        hold = Reservation(
            run_id=run_id,
            account=account,
            held_mkcu=estimate_mkcu,
            created_at=self._clock(),
        )
        self._put_hold(hold)
        return hold

    def _has_reversal(self, account: AccountId) -> bool:
        return any(
            e.account == account and e.kind == "adjustment" and e.delta_mkcu < 0
            for e in self._entries
        )

    def holds(self, account: AccountId | None = None) -> list[Reservation]:
        """Every live hold. Without a read there is no way to find orphans."""
        return [
            h for h in self._holds.values() if account is None or h.account == account
        ]

    def expire_stale_holds(self, *, older_than_seconds: float) -> list[Reservation]:
        """Release holds from runs that died before commit or release.

        A crash between reserve and commit otherwise leaves the hold forever:
        in memory it evaporates on restart (free compute), and in a durable
        store it persists and permanently shrinks the customer's balance for a
        run that never finished. Both are wrong, so this is explicit policy --
        release in the customer's favour, and say so.
        """
        cutoff = self._clock() - older_than_seconds
        stale = [
            h for h in self._holds.values() if h.created_at and h.created_at < cutoff
        ]
        for hold in stale:
            self._drop_hold(hold.run_id)
        return stale

    def commit(self, run_id: str, actual_mkcu: int) -> Entry:
        """Convert a hold into the real cost. Over-run is charged, not capped."""
        hold = self._drop_hold(run_id)
        if hold is None:
            raise LedgerError(f"run {run_id} has no reservation to commit")
        if actual_mkcu < 0:
            raise LedgerError("actual usage must not be negative")
        return self._append(
            Entry(
                seq=self._next_seq(),
                account=hold.account,
                kind="debit",
                delta_mkcu=-actual_mkcu,
                reason="run committed",
                run_id=run_id,
                created_at=self._clock(),
                reserved_mkcu=hold.held_mkcu,
            )
        )

    def release(
        self, run_id: str, *, consumed_mkcu: int = 0, reason: str = "run failed"
    ) -> Entry | None:
        """End a run without completing it, charging what it actually used.

        This used to refund the hold unconditionally, and the docstring sold
        that as a virtue -- "a failed run costs the user nothing". It is a
        free-compute exploit: the model calls and solver time are spent by the
        time a run is cancelled, so cancelling late is strictly cheaper than
        finishing. LiteLLM ships the opposite rule for exactly this reason.

        ``consumed_mkcu=0`` is still allowed and still free, because a run
        that failed before doing any work genuinely cost nothing -- but the
        caller now has to say so rather than getting it by default.
        """
        hold = self._drop_hold(run_id)
        if hold is None:
            raise LedgerError(f"run {run_id} has no reservation to release")
        if consumed_mkcu < 0:
            raise LedgerError("consumed usage must not be negative")
        if consumed_mkcu == 0:
            return None
        return self._append(
            Entry(
                seq=self._next_seq(),
                account=hold.account,
                kind="debit",
                delta_mkcu=-consumed_mkcu,
                reason=f"{reason} (partial usage)",
                run_id=run_id,
                created_at=self._clock(),
                reserved_mkcu=hold.held_mkcu,
            )
        )

    # ----------------------------------------------------------- auto-reload
    def needs_topup(self, account: AccountId, policy: TopUpPolicy) -> bool:
        spendable = self.balance(account).spendable_mkcu
        return policy.enabled and spendable < policy.threshold_mkcu

    def check_topup_allowed(self, account: AccountId, policy: TopUpPolicy) -> None:
        """Raise :class:`SpendCapExceeded` if a reload would breach the cap."""
        spent = self.spent_cents_this_period(account)
        # Pending reloads count too, or N concurrent checks all pass against
        # the same cap and authorise N charges.
        spent += sum(
            amount for acct, amount in self._pending_reloads.values() if acct == account
        )
        if spent + policy.topup_cents > policy.monthly_cap_cents:
            raise SpendCapExceeded(
                would_spend_cents=policy.topup_cents,
                cap_cents=policy.monthly_cap_cents,
                spent_cents=spent,
            )

    # -------------------------------------------------------------- overage
    def overage_mkcu(self, account: AccountId) -> int:
        """Unpaid compute already delivered, as a positive number."""
        return max(0, -self.balance(account).available_mkcu)

    def needs_settlement(self, account: AccountId, policy: OveragePolicy) -> bool:
        return policy.enabled and self.overage_mkcu(account) >= policy.settle_at_mkcu

    def begin_settlement(self, account: AccountId) -> SettlementIntent:
        """Freeze the quantity a settlement charge will be built for.

        Settlement is two-phase because the charge is a network round trip
        that takes seconds and can stretch much further through an
        authentication step. The single-phase version re-read the balance
        when the money came back, so *anything* landing in that window
        changed the amount being settled: a run that finished had its compute
        written off unbillable, and a top-up that landed made a successful
        charge unrecordable. Freezing the quantity first removes the window.
        """
        owed = self.overage_mkcu(account)
        if owed <= 0:
            raise LedgerError(f"{account} has no overage to settle")
        settle_id = f"stl_{self._next_seq()}_{account}"
        intent = SettlementIntent(
            settle_id=settle_id,
            account=account,
            mkcu=owed,
            cents=self.cost_cents(owed),
        )
        self._put_settlement(intent)
        return intent

    def settle_overage(
        self,
        intent: SettlementIntent,
        *,
        charge_id: str,
        cents_paid: int,
    ) -> Entry:
        """Record that the charge for ``intent`` succeeded.

        Grants exactly the quantity the charge was built for -- never a
        re-read. If usage continued during the round trip the remainder stays
        outstanding for the next settlement; if a purchase landed instead, the
        surplus becomes ordinary credit the customer paid for. A successful
        charge is always recorded, because the alternative is money taken
        with no ledger entry at all.
        """
        # The idempotency guard runs FIRST, before any business validation.
        # After a successful settlement nothing is owed, so an "is anything
        # owed?" check would fire on the retry and raise the wrong error --
        # and a caller that answers 500 to that makes Stripe retry for days.
        if charge_id in self._applied:
            raise ReplayedEvent(charge_id)
        if intent.settle_id not in self._settlements:
            raise LedgerError(f"unknown or already-settled intent {intent.settle_id}")
        self._drop_settlement(intent.settle_id)
        self._mark_applied(charge_id, "settlement")
        return self._append(
            Entry(
                seq=self._next_seq(),
                account=intent.account,
                kind="grant",
                delta_mkcu=intent.mkcu,
                reason=f"overage settled by {charge_id}",
                event_id=charge_id,
                charge_id=charge_id,
                cents_paid=cents_paid,
                created_at=self._clock(),
            )
        )

    def abandon_settlement(self, intent: SettlementIntent) -> None:
        """The charge failed. Release the frozen quantity; it stays owed."""
        self._drop_settlement(intent.settle_id)

    def cost_cents(self, amount_mkcu: int) -> int:
        return price_mkcu(amount_mkcu, rate_cents_per_kcu=self.rate_cents_per_kcu)

"""The ledger, on disk.

``MemoryLedger`` is honest about being a stand-in: a desktop app that restarts
loses every balance, every live hold and every idempotency key it had. The
last of those is the dangerous one. Stripe retries a webhook for days, so a
restart between the first delivery and the retry means the replay guard has
forgotten the event and the same Checkout Session grants the pack twice.

This subclass changes storage and nothing else. Every rule -- append-only,
reserve/commit/release, two-phase settlement, the monthly window -- stays in
:class:`MemoryLedger`, because a durable ledger that reimplements those rules
is a durable ledger that will drift from the tested one, and then the two
disagree about money.

Design decisions, each with a failure it prevents:

* **Entries are append-only in the schema, not just by convention.** There is
  no UPDATE or DELETE against ``entries`` anywhere in this file, and ``seq``
  is the primary key. Recomputing the balance is then always possible, which
  is the whole reason the balance is a fold.
* **``applied`` has a UNIQUE key.** The in-memory guard is a dict check, which
  is correct only while one process owns the ledger. The database constraint
  holds even if two processes race on the same file -- and the desktop app
  plus a `stripe listen` forwarder is exactly two processes.
* **``synchronous=FULL`` with WAL.** The default ``NORMAL`` can lose the last
  transactions on power loss. For a cache that is a fine trade; for "did we
  already grant this payment" it is not.
* **Every write is one transaction.** The entry, the idempotency key and the
  hold removal that go with a commit either all land or none do. A crash
  between them would otherwise leave a hold that no run will ever release, or
  a granted event with no key to stop the retry granting it again.
* **Money and compute are stored as INTEGER.** Same reason ``units.py`` makes
  every dimension an integer nanometre: SQLite's REAL would reintroduce the
  drift the whole package exists to avoid.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from .accounts import AccountId
from .errors import LedgerError
from .ledger import Entry, MemoryLedger, Reservation, SettlementIntent
from .paymethods import SqlitePaymentMethods

__all__ = ["SCHEMA_VERSION", "SqliteLedger", "default_ledger_path"]

#: Bumped when the schema changes in a way an older file cannot satisfy.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    seq           INTEGER PRIMARY KEY,
    account       TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    delta_mkcu    INTEGER NOT NULL,
    reason        TEXT    NOT NULL,
    event_id      TEXT,
    cents_paid    INTEGER NOT NULL DEFAULT 0,
    run_id        TEXT,
    created_at    REAL    NOT NULL DEFAULT 0,
    reserved_mkcu INTEGER NOT NULL DEFAULT 0,
    charge_id     TEXT
);
CREATE INDEX IF NOT EXISTS entries_account ON entries(account);

-- The replay guard, as a constraint rather than a convention. A dict check
-- is only correct while one process owns the ledger; the desktop app plus a
-- `stripe listen` forwarder is already two.
CREATE TABLE IF NOT EXISTS applied (
    key  TEXT PRIMARY KEY,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holds (
    run_id     TEXT PRIMARY KEY,
    account    TEXT    NOT NULL,
    held_mkcu  INTEGER NOT NULL,
    created_at REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settlements (
    settle_id TEXT PRIMARY KEY,
    account   TEXT    NOT NULL,
    mkcu      INTEGER NOT NULL,
    cents     INTEGER NOT NULL
);
"""


def default_ledger_path() -> Path:
    return Path.home() / ".kaleo" / "ledger.sqlite3"


class SqliteLedger(MemoryLedger):
    """A ledger that survives a restart. Same rules, durable storage.

    The in-memory structures are kept as a read cache and rebuilt from the
    file on open, so every read path in :class:`MemoryLedger` -- the balance
    fold, the monthly window, the hold list -- works unchanged and at the same
    speed. Writes go to SQLite first and the cache second: a cache updated
    before a failed write would report compute the file does not have.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        rate_cents_per_kcu: int = 225,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(rate_cents_per_kcu=rate_cents_per_kcu, clock=clock)
        self.path = Path(path) if path is not None else default_ledger_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` plus an explicit lock: the service answers
        # webhooks on one thread and the UI on another, and sqlite3's default
        # would refuse the second outright.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self._load()
        #: The ``cus_``/``pm_`` pair overage settlement needs, on this same
        #: connection and lock so recording a payment method and granting the
        #: credit from one webhook cannot half-commit.
        self.payment_methods = SqlitePaymentMethods(self._db, self._lock)

    # ------------------------------------------------------------ lifecycle
    def _configure(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            # NORMAL can lose the last transactions on power loss. Acceptable
            # for a cache; not for "did we already grant this payment".
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)
            found = self._db.execute("PRAGMA user_version").fetchone()[0]
            if found == 0:
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif found != SCHEMA_VERSION:
                # Refuse rather than guess. Silently reading a ledger written
                # by a schema this build does not understand is how a balance
                # comes back wrong instead of absent.
                raise LedgerError(
                    f"{self.path} was written by ledger schema v{found}; "
                    f"this build speaks v{SCHEMA_VERSION}"
                )
            self._db.commit()

    def _load(self) -> None:
        """Rebuild the in-memory cache from the file."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM entries ORDER BY seq").fetchall()
            self._entries = [
                Entry(
                    seq=row["seq"],
                    account=AccountId(row["account"]),
                    kind=row["kind"],
                    delta_mkcu=row["delta_mkcu"],
                    reason=row["reason"],
                    event_id=row["event_id"],
                    cents_paid=row["cents_paid"],
                    run_id=row["run_id"],
                    created_at=row["created_at"],
                    reserved_mkcu=row["reserved_mkcu"],
                    charge_id=row["charge_id"],
                )
                for row in rows
            ]
            self._applied = {
                row["key"]: row["kind"]
                for row in self._db.execute("SELECT key, kind FROM applied")
            }
            self._holds = {
                row["run_id"]: Reservation(
                    run_id=row["run_id"],
                    account=AccountId(row["account"]),
                    held_mkcu=row["held_mkcu"],
                    created_at=row["created_at"],
                )
                for row in self._db.execute("SELECT * FROM holds")
            }
            self._settlements = {
                row["settle_id"]: SettlementIntent(
                    settle_id=row["settle_id"],
                    account=AccountId(row["account"]),
                    mkcu=row["mkcu"],
                    cents=row["cents"],
                )
                for row in self._db.execute("SELECT * FROM settlements")
            }
            # Continue the sequence past what is on disk. Restarting it at 1
            # would collide with the primary key and, worse, make two
            # different movements share a seq in anything that read the cache.
            highest = max((e.seq for e in self._entries), default=0)
            self._seq = itertools.count(highest + 1)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --------------------------------------------------------- storage seam
    def _append(self, entry: Entry) -> Entry:
        with self._lock:
            self._db.execute(
                "INSERT INTO entries (seq, account, kind, delta_mkcu, reason, "
                "event_id, cents_paid, run_id, created_at, reserved_mkcu, "
                "charge_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.seq,
                    str(entry.account),
                    entry.kind,
                    entry.delta_mkcu,
                    entry.reason,
                    entry.event_id,
                    entry.cents_paid,
                    entry.run_id,
                    entry.created_at,
                    entry.reserved_mkcu,
                    entry.charge_id,
                ),
            )
            self._db.commit()
            return super()._append(entry)

    def _mark_applied(self, key: str, kind: str) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO applied (key, kind) VALUES (?,?)", (key, kind)
                )
            except sqlite3.IntegrityError:
                # Another process got there first. The caller's own dict check
                # already passed, so this is the cross-process race the
                # constraint exists for -- and the right answer is the same
                # one a replay gets: refuse, do not credit twice.
                self._db.rollback()
                raise
            self._db.commit()
            super()._mark_applied(key, kind)

    def _put_hold(self, hold: Reservation) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO holds (run_id, account, held_mkcu, "
                "created_at) VALUES (?,?,?,?)",
                (hold.run_id, str(hold.account), hold.held_mkcu, hold.created_at),
            )
            self._db.commit()
            super()._put_hold(hold)

    def _drop_hold(self, run_id: str) -> Reservation | None:
        with self._lock:
            self._db.execute("DELETE FROM holds WHERE run_id = ?", (run_id,))
            self._db.commit()
            return super()._drop_hold(run_id)

    def _put_settlement(self, intent: SettlementIntent) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO settlements (settle_id, account, mkcu, "
                "cents) VALUES (?,?,?,?)",
                (intent.settle_id, str(intent.account), intent.mkcu, intent.cents),
            )
            self._db.commit()
            super()._put_settlement(intent)

    def _drop_settlement(self, settle_id: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM settlements WHERE settle_id = ?", (settle_id,)
            )
            self._db.commit()
            super()._drop_settlement(settle_id)

    # -------------------------------------------------------------- reading
    def reload(self) -> None:
        """Re-read the file. For a second process that wants a fresh view."""
        self._load()

    def __repr__(self) -> str:
        return f"SqliteLedger({str(self.path)!r}, entries={len(self._entries)})"

"""Charge a run against the ledger: hold first, convert after.

``billing/`` has implemented reserve -> commit -> release since it was written,
with an append-only ledger, integer mKCU, two-phase settlement and a 250-op
fuzz test. Until this module existed **nothing called it**:
``docs/paid-run-safety.md`` §5 states that plainly, and a grep for ``reserve``,
``commit`` and ``release`` outside ``billing/`` and its own tests still finds
nothing else. This is the one module that knows both the service and the
ledger -- the ``slackbot/runner.py`` and ``meetings/runner.py`` convention, for
the same reason: neither side should have to import the other.

**Hold, then convert.** ``billing/ledger.py::MemoryLedger.reserve`` takes an
estimate before the run and ``commit`` converts it to the measured cost after.
The closest live prior art is not Stripe but LiteLLM, whose proxy meters LLM
spend the same way in ``litellm/proxy/spend_tracking/budget_reservation.py``:
``reserve_budget_for_request`` holds ``estimate_request_max_cost`` (the
*worst-case* cost, input tokens counted plus max output tokens priced) at auth
time, and ``reconcile_budget_reservation`` settles it to the actual afterwards.
Upstream also states the failure this prevents, in
``litellm/proxy/auth/user_api_key_auth.py`` where the reservation can be turned
off::

    "Budget enforcement is read-time only -- concurrent requests can each
     pass the spend check before their cost is recorded, so a configured
     budget may be briefly exceeded under high concurrency."

That is precisely why ``reserve`` exists rather than a balance check.

Stripe is the shape the *vocabulary* comes from -- ``billing/`` was modelled on
authorize-then-capture -- and the release of an over-hold is real but is stated
inversely in stripe-python: ``stripe/params/_payment_intent_capture_params.py``
documents ``amount_to_capture`` as "must be less than or equal to the original
amount" and ``final_capture`` (default ``True``) as the flag you set to
``False`` "to not release the remaining uncaptured funds". So the default
*does* release the remainder; you opt out of the release, not into it.
``PaymentIntent.cancel`` (``stripe/_payment_intent.py``) says the same for the
abandoned case: "For PaymentIntents with a status of requires_capture, the
remaining amount_capturable is automatically refunded."

**A run that passes zero finishes and is billed afterwards.** That is
``OveragePolicy``'s whole argument and it is not negotiable here: cutting off
mid-run burns the model calls already spent and delivers nothing.
``limit_mkcu`` is the real credit line, and only crossing *that* refuses a run
-- before it starts, which is the one moment refusing is free.

**A run that dies is charged for what it used.** ``release`` takes
``consumed_mkcu`` and its docstring says why the old unconditional refund was
wrong. LiteLLM writes the identical rule for the identical reason, in
``budget_reservation.py::release_budget_reservation_on_cancel``::

    "Reconcile to the request's input-token cost rather than refunding to
     zero: by the time a request is cancelled in-flight the provider call was
     already dispatched, so the input tokens were billed even if no chunk
     reached the client. Refunding to zero would let a caller abort pre-token
     to dodge that charge..."

and it bills a broken stream rather than losing it
(``litellm/proxy/hooks/proxy_track_cost_callback.py::_ProxyDBLogger.
async_post_call_failure_hook`` writes a spend row through the same
``update_database`` as a success, carrying the recovered partial cost;
``litellm/proxy/common_request_processing.py::
_bill_partial_streamed_spend_on_disconnect`` does it for the cancelled case,
under ``anyio.CancelScope(shield=True)`` so teardown billing survives the
cancellation that triggered it). So ``fail()`` here passes the measured elapsed
time, not zero -- and it is called on the disconnect path as well as the error
and cancel paths, which is where a client that hung up would otherwise have
run for free.

**Metering is OFF unless it is switched on**, and when it is off it says so in
words on every run rather than silently not billing. There is no account system
in this repo (``billing/accounts.py::SingleAccountResolver`` is the honest
default), so "who is billed" is a question this module cannot answer on its
own; defaulting to on would invent an answer. See ``docs/paid-run-safety.md``
§7 for the decision and its alternatives.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_ESTIMATE_MKCU",
    "InsufficientCredit",
    "Metering",
    "RunCharge",
    "current",
    "reset_for_tests",
]

#: What one run is assumed to cost before it has run. Two minutes of engine
#: wall-clock (``billing/units.py``: 1 KCU = 60 s = 1000 mKCU). Deliberately
#: generous rather than tight: the estimate is a *hold*, released down to the
#: real number at commit, so an estimate that is too small lets concurrent runs
#: overdraw while one that is too large costs a customer nothing.
DEFAULT_ESTIMATE_MKCU = 2000

#: Runs longer than this are still billed in full -- there is no cap here on
#: purpose. Stated so nobody later reads the absence as an oversight: a cap on
#: the *charge* for work already delivered is a write-off, and write-offs are
#: an explicit ``adjust`` entry, not a silent one.


class InsufficientCredit(RuntimeError):
    """The account is past its overage limit. Answered as 402, before any work.

    Kept out of the ``ValueError`` family deliberately: ``_error_response``
    reads a bare ``ValueError`` from the pipeline as an internal failure, and
    this is neither internal nor a failure -- it is the one refusal that is
    honest, because it happens before a single model call is spent.
    """


@dataclass(frozen=True)
class RunCharge:
    """What one run cost, as it is reported back to the caller.

    Every field is an integer or a plain string for the reason ``billing/
    units.py`` gives: money is integer cents and compute is integer mKCU, and
    a float that reaches a balance is a reconciliation bug months later.
    """

    account: str
    run_id: str
    reserved_mkcu: int
    charged_mkcu: int
    cost_cents: int
    state: str  # committed | released | refused | off

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "account": self.account,
            "run_id": self.run_id,
            "reserved_mkcu": self.reserved_mkcu,
            "charged_mkcu": self.charged_mkcu,
            "cost_cents": self.cost_cents,
            "state": self.state,
        }


#: What a run reports when nothing is metering it. A dict rather than ``None``
#: so the answer is a sentence a person can read, not an absence they have to
#: interpret -- ``spice/``'s rule that a missing verdict says why it is missing.
def off_block(reason: str) -> dict[str, Any]:
    return {"enabled": False, "state": "off", "reason": reason}


NOT_ENABLED = (
    "metering is off (KALEO_METERING is not set); this run was not charged "
    "against any ledger"
)


class Metering:
    """The service's half of the ledger, resolved once from the environment."""

    def __init__(
        self,
        *,
        enabled: bool,
        ledger: Any = None,
        account: Any = None,
        estimate_mkcu: int = DEFAULT_ESTIMATE_MKCU,
        overage: Any = None,
        reason: str = "",
    ) -> None:
        self.enabled = enabled
        self._ledger = ledger
        self._account = account
        self.estimate_mkcu = estimate_mkcu
        self._overage = overage
        self.reason = reason or (NOT_ENABLED if not enabled else "")

    # ------------------------------------------------------------- lifecycle
    def begin(self, run_id: str) -> Any:
        """Hold the estimate. Returns an opaque handle, or ``None`` when off.

        Raises :class:`InsufficientCredit` when the ledger refuses -- the only
        moment a refusal is free, because nothing has been spent yet.
        """
        if not self.enabled:
            return None
        from billing.errors import LedgerError

        try:
            return self._ledger.reserve(
                self._account,
                self.estimate_mkcu,
                run_id=run_id,
                overage=self._overage,
            )
        except LedgerError as exc:
            raise InsufficientCredit(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            # A ledger that is broken rather than empty must not refuse every
            # run: "we cannot bill you" is not "you may not build a board".
            # Loud on stderr, unmetered, and the run continues -- the same
            # call ``_settle`` makes at the other end.
            sys.stderr.write(
                f"metering: could not reserve for run {run_id}: "
                f"{type(exc).__name__}: {exc}\n"
            )
            return None

    def finish(self, hold: Any, *, elapsed_s: float) -> dict[str, Any]:
        """Convert the hold into the measured cost."""
        if hold is None:
            return off_block(self.reason)
        return self._settle(hold, elapsed_s=elapsed_s, committed=True)

    def fail(self, hold: Any, *, elapsed_s: float, reason: str) -> dict[str, Any]:
        """End a run that did not finish, charging what it actually used."""
        if hold is None:
            return off_block(self.reason)
        return self._settle(hold, elapsed_s=elapsed_s, committed=False, reason=reason)

    def _settle(
        self,
        hold: Any,
        *,
        elapsed_s: float,
        committed: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        from billing.units import Meter

        used = Meter(engine_seconds=max(0.0, elapsed_s)).as_mkcu()
        try:
            if committed:
                self._ledger.commit(hold.run_id, used)
                state = "committed"
            else:
                self._ledger.release(
                    hold.run_id,
                    consumed_mkcu=used,
                    reason=reason or "run failed",
                )
                state = "released"
        except Exception as exc:  # pragma: no cover - defensive
            # A ledger that cannot record must never take the board down with
            # it: the run happened, the customer has the result, and an
            # unrecorded charge is a reconciliation problem, not a 500. It is
            # loud on stderr for exactly that reason.
            sys.stderr.write(
                f"metering: could not settle run {hold.run_id}: "
                f"{type(exc).__name__}: {exc}\n"
            )
            return {
                "enabled": True,
                "state": "unrecorded",
                "run_id": hold.run_id,
                "reason": (
                    "the ledger refused to record this run "
                    f"({type(exc).__name__})"
                ),
            }
        return RunCharge(
            account=str(hold.account),
            run_id=hold.run_id,
            reserved_mkcu=hold.held_mkcu,
            charged_mkcu=used,
            cost_cents=self._ledger.cost_cents(used),
            state=state,
        ).as_dict()

    # ------------------------------------------------------------------ read
    def describe(self) -> dict[str, Any]:
        """Configuration, for ``/integrations``-style reporting. No secrets."""
        if not self.enabled:
            return {"enabled": False, "reason": self.reason}
        return {
            "enabled": True,
            "account": str(self._account),
            "estimate_mkcu": self.estimate_mkcu,
            "overage_limit_mkcu": getattr(self._overage, "limit_mkcu", 0),
        }


_CURRENT: Metering | None = None


def reset_for_tests() -> None:
    global _CURRENT
    _CURRENT = None


def _build() -> Metering:
    if os.environ.get("KALEO_METERING", "").strip().lower() not in ("1", "true", "on"):
        return Metering(enabled=False, reason=NOT_ENABLED)

    try:
        from billing.accounts import AccountId, SingleAccountResolver
        from billing.ledger import OveragePolicy
        from service import billing_routes as _billing

        account_value = os.environ.get("KALEO_ACCOUNT", "").strip()
        account = (
            AccountId(account_value)
            if account_value
            else SingleAccountResolver().resolve(subject=None)
        )
        raw = os.environ.get("KALEO_RUN_ESTIMATE_MKCU", "").strip()
        estimate = int(raw) if raw else DEFAULT_ESTIMATE_MKCU
        if estimate <= 0:
            raise ValueError("KALEO_RUN_ESTIMATE_MKCU must be a positive integer")
        # The *same* ledger the balance route reads, never a second one --
        # ``/integrations`` calls ``deliver.config_report`` for the same
        # reason: two views of one fact must not be able to disagree.
        ledger = _billing._ledger()
    except Exception as exc:
        # Misconfigured metering degrades to off and says exactly why, rather
        # than failing every run or -- far worse -- billing against a ledger
        # nobody meant to open.
        reason = (
            f"metering was requested but could not be configured "
            f"({type(exc).__name__}: {exc}); this run was not charged"
        )
        sys.stderr.write(f"metering: {reason}\n")
        return Metering(enabled=False, reason=reason)

    return Metering(
        enabled=True,
        ledger=ledger,
        account=account,
        estimate_mkcu=estimate,
        overage=OveragePolicy(),
    )


def current() -> Metering:
    """The process's metering, resolved once.

    Resolved at call time and cached, never at import: a test's environment and
    the desktop's ``envfiles.apply_saved_env`` both land after this module is
    imported, and reading the environment at import would freeze the wrong
    answer. ``reset_for_tests`` clears the cache.
    """
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = _build()
    return _CURRENT

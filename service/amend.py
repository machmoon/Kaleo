"""Typing at the strip while a run is in flight: notes, and cancel.

The engineer watches a run and wants to say something to it -- "make that
5 V", "give the case a vent", "stop, I meant something else". This module is
the service-side half of that, and its whole design is a refusal to promise
more than the engine can do.

**What a note cannot do.** A run is staged (read, plan, propose, place,
placement repair, schematic, route, review) and every stage downstream of
``propose`` consumes a *validated* ``CircuitSpec``, not English. Amending the
spec after placement would leave the solver's reserved boxes describing parts
the netlist no longer has -- the exact "the solver reserved a box that doesn't
match the part actually written" bug class ``kicad.py`` exists to prevent. So
a note is never applied to a stage that has already run, and it is never
silently re-interpreted into a spec. It has exactly two honest destinations:

* **restart** -- the note joins the intent and a *new* run is started from it.
  Costs a fresh plan and propose (model calls) plus a fresh solve, whose
  budget is the effort level's (5 s at the default ``fast``, 45 s at
  ``thorough``) rather than a fixed 20 s.
  :func:`amended_intent` builds the text; the client POSTs it to ``/steps``.
  Whether that happens now or after this run finishes is the client's choice,
  and is the only difference between "cancel and redo" and "queue a follow-up".
* **a later step that genuinely takes free text** -- :data:`CONSUMING_STEPS`.
  Today that is ``case`` and nothing else, because ``case`` is the only step
  whose payload has a free-text field (``enclosure_style``). ``place``,
  ``route`` and ``order`` are deterministic and take no text at all;
  ``review`` and ``sourcing`` read the board, not a sentence.

Nothing here classifies a note with a model. Guessing which of the two doors a
sentence wants is a model call per typed sentence, on a free-tier key, to
produce an answer that is wrong often enough to place text on the wrong stage.
The caller says which door it wants (``"step"`` in the request) or leaves it
unattached, and the response names both doors and their costs so a UI can ask.

**Cancel** is the other half, and it is deliberately modest about what it
achieves. It closes the session to further steps -- which is the real saving,
since every remaining step is a model call or a solve that now never happens --
and it makes the event sink raise :class:`RunCancelled`, which the engine's
"a callback exception abandons the run" contract turns into an abort of
whatever stage is running. What it does **not** do is reach inside work already
started: the CP-SAT solve, an HTTP request in flight to the model provider, and
the background case and sourcing threads all run to their next event boundary
before they notice. :func:`cancel` reports that rather than hiding it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "RunCancelled",
    "Amendment",
    "CONSUMING_STEPS",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "note",
    "cancel",
    "amended_intent",
    "take_for",
    "check_cancelled",
    "block",
    "is_lifecycle_path",
    "handle_post",
]


class RunCancelled(RuntimeError):
    """Raised from a cancelled session's event sink, to abandon the stage.

    Deliberately a ``RuntimeError`` rather than a ``ValueError``: several
    stages catch ``ValueError`` to keep a bad model answer from failing the
    run (``sourcing_stage`` is the clearest), and a cancel swallowed into a
    warning would leave the run going after the engineer stopped it.
    """


#: The steps whose payload has a free-text field a note can honestly become.
#: ``case`` takes ``enclosure_style``. Nothing else does -- ``place``,
#: ``route`` and ``order`` make no model call at all, and ``review`` and
#: ``sourcing`` are handed the board rather than a sentence. Adding a name
#: here is a claim that the step reads text, and the step must then call
#: :func:`take_for`.
CONSUMING_STEPS = ("case",)

#: Per session. A strip that dictates for a minute must not grow the session
#: without bound; the cap refuses loudly rather than dropping the oldest note,
#: because silently discarding something the engineer typed is the failure
#: this whole module exists to avoid.
MAX_NOTES = 20

MAX_NOTE_CHARS = 500

#: What every response says about the stages a note cannot reach. One
#: sentence, so a client renders it verbatim instead of inventing its own.
NOT_CONSUMED = (
    "no stage other than the ones named re-reads the request: place, route, "
    "review, sourcing and order work from the validated circuit and the "
    "placed board, so a note cannot change what they do. To change the "
    "circuit itself, start a new run from the amended intent."
)

RESTART_COST = (
    "a new run: plan and propose are model calls, and placement is another "
    "full solver budget"
)

CANNOT_STOP = (
    "work already started is not interrupted: the placement solve, any model "
    "request in flight, and the background case and parts lookups each run to "
    "their next reporting point before they stop. Their results are discarded."
)


@dataclass
class Amendment:
    """One thing the engineer typed while the run was going.

    ``stage`` is what the run had reached when it arrived, kept because "make
    it 5 V" typed before ``place`` and the same words typed after ``route``
    are different requests with different costs, and a transcript that loses
    the ordering cannot tell them apart afterwards.
    """

    id: int
    text: str
    at: float
    stage: str
    step: str | None
    status: str = "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "at": round(self.at, 3),
            "stage": self.stage,
            "step": self.step,
            "status": self.status,
        }


def _notes(session: Any) -> list[Amendment]:
    return session.amendments


def _lock(session: Any) -> threading.Lock:
    """The short lock guarding the note list.

    Never :attr:`Session.lock`, which a running step holds for its whole
    body: taking that here would make a note typed during a long placement
    wait for the placement, which is precisely the moment the feature is for.
    """
    return session.notes_lock


def note(session: Any, text: str, *, step: str | None = None) -> dict[str, Any]:
    """Record what was typed, and say honestly what can become of it."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("'text' is required")
    text = text.strip()
    if len(text) > MAX_NOTE_CHARS:
        raise ValueError(
            f"'text' must be at most {MAX_NOTE_CHARS} characters "
            f"(got {len(text)})"
        )
    if step is not None:
        if not isinstance(step, str):
            raise ValueError("'step' must be a string or null")
        step = step.strip()
        if step not in CONSUMING_STEPS:
            raise ValueError(
                f"'step' must be null or one of {', '.join(CONSUMING_STEPS)}; "
                f"{step!r} does not read free text. Leave it out to keep the "
                "note for a new run."
            )
        if step in session.done:
            raise ValueError(
                f"step {step!r} has already run for this session; its note "
                "would never be read. Start a new run from the amended intent."
            )

    with _lock(session):
        notes = _notes(session)
        if len(notes) >= MAX_NOTES:
            raise ValueError(
                f"this session already holds {MAX_NOTES} notes; start a new "
                "run from the amended intent rather than adding to it"
            )
        entry = Amendment(
            id=len(notes) + 1,
            text=text,
            at=time.time(),
            stage=session.stage,
            step=step,
            status="pending",
        )
        notes.append(entry)
        pending = [a for a in notes if a.status == "pending"]

    if step is not None:
        applies = {
            "at_step": step,
            "when": f"when you press {step!r}, which redesigns it for this note",
        }
        headline = (
            f"noted -- it will be used when you press {step!r}. It does not "
            "change the circuit or the placement."
        )
    else:
        applies = {"at_step": None, "when": None}
        headline = (
            "noted -- nothing in this run will read it. Start a new run from "
            "the amended intent to act on it."
        )

    return {
        "session": session.id,
        "amendment": entry.as_dict(),
        "pending": len(pending),
        "applies": applies,
        "restart": {
            "available": True,
            "intent": amended_intent(session),
            "cost": RESTART_COST,
            "how": "POST /steps with this intent",
        },
        "consuming_steps": list(CONSUMING_STEPS),
        "not_consumed": NOT_CONSUMED,
        "headline": headline,
        "cancelled": session.cancelled.is_set(),
    }


def amended_intent(session: Any) -> str:
    """The intent a restart would use: the original plus every note, in order.

    Every note is included, applied or not, because a restart is a new run
    from scratch: a note the ``case`` step already consumed still described
    what the engineer wanted, and dropping it would silently lose it from the
    board that replaces this one.
    """
    with _lock(session):
        lines = [a.text for a in _notes(session)]
    if not lines:
        return session.intent
    return session.intent + "\n\n" + "\n".join(f"Also: {line}" for line in lines)


def take_for(session: Any, step: str) -> list[Amendment]:
    """The pending notes addressed to ``step``, marked applied.

    Called by the step itself, inside its own runner, so a note is marked
    applied only when the step that reads it actually runs. A step not in
    :data:`CONSUMING_STEPS` gets nothing, by construction rather than by
    convention -- the empty list is the honest answer for a step that has no
    field to put text in.
    """
    if step not in CONSUMING_STEPS:
        return []
    with _lock(session):
        taken = [
            a for a in _notes(session) if a.status == "pending" and a.step == step
        ]
        for entry in taken:
            entry.status = "applied"
    return taken


def check_cancelled(session: Any) -> None:
    """Raise :class:`RunCancelled` if this session has been cancelled.

    Called from the step event sink, so the raise travels out through the
    engine's existing contract that a callback exception abandons the run.
    That is the only interruption seam the pipeline has, and it fires at
    event boundaries: see :func:`cancel` for what that does and does not buy.
    """
    if session.cancelled.is_set():
        raise RunCancelled(f"run {session.id} was cancelled")


def cancel(session: Any) -> dict[str, Any]:
    """Stop the run, and report exactly what that stopped.

    Idempotent: cancelling an already-cancelled session answers the same
    shape rather than an error, so a client that fires the button twice (or
    retries a request whose response it never saw) does not have to
    distinguish the cases.
    """
    already = session.cancelled.is_set()
    session.cancelled.set()
    # ``Session.lock`` is held for the whole body of a running step, so its
    # state is a true answer to "is a step running right now" -- and a
    # cheaper one than any flag this module could keep in step with it.
    in_flight = session.lock.locked()
    still_running = list(session.background)
    from . import steps as _steps

    refused = [s for s in _steps.STEPS if s not in session.done]
    return {
        "session": session.id,
        "cancelled": True,
        "already_cancelled": already,
        "stage": session.stage,
        "refused_steps": refused,
        "in_flight": in_flight,
        "aborts_at": (
            "the next event the running step reports"
            if in_flight
            else "nothing was running"
        ),
        "still_running": still_running,
        "not_stoppable": CANNOT_STOP,
        "restart": {
            "available": True,
            "intent": amended_intent(session),
            "cost": RESTART_COST,
            "how": "POST /steps with this intent",
        },
        "headline": (
            "cancelled -- no further step will run. "
            + (
                "Work already under way finishes and is discarded."
                if in_flight or still_running
                else "Nothing was under way."
            )
        ),
    }


def block(session: Any) -> list[dict[str, Any]]:
    """The amendments, for the step envelope and ``GET /steps/<id>``."""
    with _lock(session):
        return [a.as_dict() for a in _notes(session)]


# ---------------------------------------------------------------- routing


def _parse(path: str) -> tuple[str, str] | None:
    parts = [p for p in path.split("?")[0].split("/") if p]
    if len(parts) != 3 or parts[0] != "steps":
        return None
    if parts[2] not in ("amend", "cancel"):
        return None
    return parts[1], parts[2]


def is_lifecycle_path(path: str) -> bool:
    """True for ``/steps/<id>/amend`` and ``/steps/<id>/cancel``.

    Read by ``app.py`` *before* it builds a model, the way
    ``deliver.is_deliver_path`` is: neither route spends a model call, and a
    cancel that could not be pressed because no API key was configured would
    be a cancel button that fails exactly when a run is going wrong.
    """
    return _parse(path) is not None


def handle_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse(path)
    if parsed is None:  # pragma: no cover -- guarded by is_lifecycle_path
        from .steps import StepNotFound

        raise StepNotFound(f"no route {path}")
    session_id, action = parsed
    from . import steps as _steps

    session = _steps._get(session_id)
    if action == "cancel":
        return cancel(session)
    if session.cancelled.is_set():
        raise _steps.StepOrderError(
            f"run {session_id} was cancelled; start a new run from the "
            "amended intent"
        )
    return note(session, payload.get("text"), step=payload.get("step"))

"""A streamed run, given a name so it can be polled and cancelled.

``POST /generate/stream`` sends its 200 and its ``run.accepted`` frame and
*then* starts the pipeline (``service/app.py::_generate_stream``). Until this
module existed, no identifier for that run ever reached the client, so a reader
that lost the connection could neither rejoin it, poll it, nor cancel it: the
spend was real, invisible and unrecoverable. ``docs/paid-run-safety.md`` §6
records that gap; this closes it.

The shape is copied, not invented.

* **The caller may name the run; the server names it only when the caller did
  not.** LiteLLM's proxy does exactly this, in
  ``litellm/proxy/common_request_processing.py``,
  ``ProxyBaseLLMRequestProcessing.common_processing_pre_call_logic``::

      self.data["litellm_call_id"] = request.headers.get(
          "x-litellm-call-id", str(uuid.uuid4())
      )

  and returns it on the same header name outbound
  (``ProxyBaseLLMRequestProcessing.get_custom_headers``: ``headers: Final =
  {"x-litellm-call-id": call_id, ...}``). Temporal goes further and makes the
  caller *always* own it -- ``temporalio/client/_client.py::Client.start_workflow``
  takes ``id: str`` as a mandatory keyword-only argument. Both are the same
  point against Celery, which mints server-side
  (``celery/app/base.py::Celery.send_task``: ``task_id = task_id or uuid()``):
  a client that chose the id knows the run's name *before* the request is
  sent, so a response that never arrives is still addressable. That is the
  whole failure this module exists for, so the header is accepted inbound as
  well as sent outbound.

* **The id is the cancellation handle, and cancelling is a separate request.**
  Celery revokes by id through ``celery/app/control.py::Control.revoke``, and
  revocation is a *flag* the worker consults rather than a kill (actually
  stopping in-flight work needs ``terminate=True`` and a signal). LiteLLM's
  ``/v1/responses/{response_id}/cancel``
  (``litellm/proxy/response_api_endpoints/endpoints.py::cancel_response``) is
  the HTTP shape. A flag is what this module does too, because the only safe
  way to stop this pipeline is the one it already has.

* **Cancellation reuses the mechanism the pipeline already has** -- a callback
  that raises abandons the run (``agents/pipeline.py``'s ``on_event`` contract,
  which ``_generate_stream``'s ``emit`` already relies on to stop a run whose
  client hung up). :meth:`RunRecord.check` is called from inside that callback
  and raises :class:`RunCancelled`. No second mechanism, no thread killing.
  This repo already had one of these: ``service/amend.py::check_cancelled``
  does exactly this for a *step* session, raising from the step event sink.
  So this module is that design applied to a streamed run, down to the answer
  vocabulary (``already_cancelled``, ``in_flight``, ``aborts_at``), because a
  second convention for the same fact is how two cancels come to disagree.

What this deliberately does **not** do is replay the stream. Frames are not
buffered and the result is not kept: a board is megabytes and holding one per
run turns a bounded registry into a memory leak. Celery has the same boundary
-- ``AsyncResult.get`` needs a result *backend*, and with none configured you
can learn a task's state and nothing else. So ``GET /runs/<id>`` answers "did
the run I paid for finish, and what did it cost", not "give me the board".
``/steps`` is the surface that keeps artifacts.
"""

from __future__ import annotations

import collections
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MAX_RUNS",
    "MAX_RUN_ID_CHARS",
    "RunCancelled",
    "RunIdInUse",
    "RunNotFound",
    "RunRecord",
    "cancel",
    "new_run",
    "reset_runs",
    "snapshot",
    "snapshot_all",
]

#: How many runs stay addressable. Sized like ``steps.MAX_SESSIONS``: a
#: registry that outlives every client that could ask about it protects
#: nothing, and this process has no store to spill into.
MAX_RUNS = 32


#: A caller-supplied id must survive a URL path, a log line and a bug report
#: unescaped, so the alphabet is ``billing/accounts.py::_ACCOUNT_RE``'s, for
#: that module's stated reason. The length ceiling is the chat route's 128.
MAX_RUN_ID_CHARS = 128
_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")


class RunNotFound(LookupError):
    """No such run id here. A 404, and the message says why it may be gone."""


class RunIdInUse(RuntimeError):
    """A caller named a run that already exists here.

    Answered as a 409, which is what ``steps.start_once`` answers an
    ``Idempotency-Key`` still in flight. Deliberately **not** a replay: this
    route holds no result to hand back (see the module docstring), so
    pretending a second request was the first one would report someone else's
    run as this caller's. Refusing names the collision instead.
    """


class RunCancelled(RuntimeError):
    """Raised inside the event callback so the pipeline abandons the run.

    Deliberately not a ``ValueError``: ``service/app.py::_error_response``
    treats a bare ``ValueError`` out of the pipeline as an internal failure,
    and a cancellation the caller asked for is neither internal nor a failure.
    """


@dataclass
class RunRecord:
    """One in-flight or finished run. Mutated under :attr:`lock`."""

    id: str
    route: str
    created: float
    #: ``running`` | ``done`` | ``failed`` | ``cancelled``. Four words, kept
    #: distinct for the reason ``spice/`` keeps its outcomes distinct: a client
    #: that cannot tell "cancelled" from "failed" cannot tell whether its own
    #: press stopped the run.
    state: str = "running"
    updated: float = 0.0
    events: int = 0
    #: The last ``stage.start``/``stage.done`` stage seen, so a poll can say
    #: where the run had got to rather than only that it was alive.
    stage: str | None = None
    #: Set by :func:`cancel`; read by :meth:`check` from inside the callback.
    cancel_requested: bool = False
    #: Why it ended, in words, when it did not end well. Never a traceback:
    #: this is served to a caller, and ``_error_response`` already owns the
    #: rule that internal text does not leave the process.
    detail: str | None = None
    #: Attributable engine wall-clock. Filled in at :meth:`finish`.
    elapsed_s: float = 0.0
    #: Whatever ``service.metering`` recorded for this run, or ``None`` when
    #: metering is off. Kept on the record so one poll answers both questions
    #: a disconnected client has: did it finish, and what did it cost.
    metering: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note(self, event: dict[str, Any]) -> None:
        """Record one pipeline event. Cheap on purpose -- this runs per frame."""
        name = str(event.get("event", ""))
        with self.lock:
            self.events += 1
            self.updated = time.time()
            if name in ("stage.start", "stage.done"):
                stage = event.get("stage")
                if isinstance(stage, str):
                    self.stage = stage

    def check(self) -> None:
        """Raise if a cancel landed. Called from the run's event callback.

        Only while the run is still running. The terminal frames -- the
        ``run.cancelled`` frame most of all -- go out through the same ``emit``
        and would otherwise be refused by the very flag they are reporting.
        """
        with self.lock:
            requested = self.cancel_requested and self.state == "running"
        if requested:
            raise RunCancelled(
                f"run {self.id} was cancelled by POST /runs/{self.id}/cancel"
            )

    def finish(self, state: str, *, detail: str | None = None) -> None:
        with self.lock:
            # A cancel that landed after the run already ended does not get to
            # rewrite history: whichever terminal state was reached first is
            # the one that happened.
            if self.state != "running":
                return
            self.state = state
            self.detail = detail
            self.updated = time.time()
            self.elapsed_s = max(0.0, self.updated - self.created)

    def as_dict(self) -> dict[str, Any]:
        with self.lock:
            body: dict[str, Any] = {
                "run_id": self.id,
                "route": self.route,
                "state": self.state,
                "events": self.events,
                "stage": self.stage,
                "started_at": self.created,
                "updated_at": self.updated or self.created,
                "elapsed_s": round(
                    self.elapsed_s or max(0.0, time.time() - self.created), 3
                ),
                "cancel_requested": self.cancel_requested,
                "detail": self.detail,
                "metering": self.metering,
            }
        # Stated on every poll rather than left to be discovered: a client that
        # expects its board back from here would otherwise wait forever.
        body["result_available"] = False
        body["note"] = (
            "This is the run's state, not its output. The stream carries the "
            "board; a run whose stream was lost has to be started again."
        )
        return body


_RUNS: collections.OrderedDict[str, RunRecord] = collections.OrderedDict()
_LOCK = threading.Lock()


def reset_runs() -> None:
    """Forget every run (tests, and ``reset_sessions``' convention)."""
    with _LOCK:
        _RUNS.clear()


def new_run(route: str, *, run_id: str = "") -> RunRecord:
    """Register a run before any work starts, under its id.

    ``run_id`` is the caller's own if it sent one -- LiteLLM's
    ``request.headers.get("x-litellm-call-id", str(uuid.uuid4()))`` exactly --
    and otherwise minted here: ``run_`` prefixed and uuid-shaped, the way every
    id in this repo that reaches a caller is (``_error_response``'s
    ``error_id``, the chat route's ``session_id``). The prefix is Stripe's
    convention and is here for the same reason: an id pasted into a bug report
    should say what kind of thing it is.

    A caller-supplied id that is already registered raises
    :class:`RunIdInUse`. It is validated rather than trusted: it is echoed into
    a header, a JSON body and a URL path.
    """
    if run_id and not _RUN_ID_RE.match(run_id):
        raise ValueError(
            f"'X-Kaleo-Run-Id' must be 1-{MAX_RUN_ID_CHARS} characters of "
            "[A-Za-z0-9_.:-]"
        )
    record = RunRecord(
        id=run_id or f"run_{uuid.uuid4().hex}", route=route, created=time.time()
    )
    with _LOCK:
        if record.id in _RUNS:
            raise RunIdInUse(
                f"run {record.id!r} is already registered here; a run id names "
                "one run and is not reusable"
            )
        # Evict finished runs before live ones: a poll for a run still burning
        # money is worth more than one for a run that ended.
        while len(_RUNS) >= MAX_RUNS:
            victim = next(
                (rid for rid, r in _RUNS.items() if r.state != "running"),
                next(iter(_RUNS)),
            )
            del _RUNS[victim]
        _RUNS[record.id] = record
    return record


def get(run_id: str) -> RunRecord:
    with _LOCK:
        record = _RUNS.get(run_id)
    if record is None:
        raise RunNotFound(
            f"no run {run_id!r} (it may have finished long enough ago to be "
            "forgotten, or the service restarted -- runs are held in this "
            "process only)"
        )
    return record


def snapshot(run_id: str) -> dict[str, Any]:
    return get(run_id).as_dict()


def snapshot_all() -> dict[str, Any]:
    with _LOCK:
        records = list(_RUNS.values())
    return {"runs": [r.as_dict() for r in records]}


def cancel(run_id: str) -> dict[str, Any]:
    """Ask a run to stop. Idempotent, and honest about what it can promise.

    Celery's ``Control.revoke`` sets a flag the worker consults and returns
    immediately; it does not promise the task stopped. The same is true here
    and more sharply: the flag is only read when the pipeline next emits an
    event, so a run inside a long solve or a long model call keeps going until
    it next speaks. What the answer therefore says is *requested*, not
    *stopped*, and the state a subsequent poll reports is the truth.

    Cancelling a run that already ended changes nothing and is not an error --
    the caller learned the outcome late, which is exactly the case this route
    exists for.
    """
    record = get(run_id)
    with record.lock:
        already_requested = record.cancel_requested
        finished = record.state != "running"
        record.cancel_requested = True
    body = record.as_dict()
    # Same vocabulary as ``service/amend.py::cancel``, which does this for a
    # step session: one product, one word for each fact.
    body["cancelled"] = True
    body["already_cancelled"] = already_requested
    body["in_flight"] = not finished
    body["aborts_at"] = (
        "the next event the pipeline reports" if not finished else "nothing was running"
    )
    body["not_stoppable"] = (
        "A model call or a solve already under way runs to completion and is "
        "still billed; only the run stops."
    )
    body["headline"] = (
        "cancelled -- the run stops at its next pipeline event"
        if not finished
        else f"already {record.state}; nothing was stopped"
    )
    return body

"""From a live Zoom meeting to a drafted board, and back into the room.

This is the only module that knows both Zoom and silkscreen, the shape
``slackbot/runner.py`` and ``meetings/runner.py`` both take: everything below
it speaks Zoom and nothing else, and :func:`silkscreen.agents.generate_pcb` has
never heard of a meeting. ``generate_pcb`` is imported *inside* the function
that needs it, so this package carries no import-time dependency on the engine
-- the webhook surface starts on a machine where the solver is not installed,
and says so when a run is attempted rather than failing to import.

**The gates are the point, and they are carried over unchanged.** A meeting is
a noisy source: people think out loud, change their minds, and say "we could
just use a 5 volt rail" without meaning "build that". So a transcript never
reaches the pipeline directly. It goes through
:func:`~zoombot.agent.requests_from_chunks`, which drops anything the model
cannot quote from what was actually said, and then through the confidence floor
here. What survives is *drafted*.

**Nothing is ever ordered.** No module in this package imports an ordering
path, and a board a meeting merely implied is exactly the wrong thing to spend
money on. ``slackbot/order.py`` already settled that a human confirms first.

**``considered`` is deliberately separate from ``runs``.** A report that listed
only what it built would make a skipped request invisible, and "it ignored what
I asked for" is the failure people actually hit.

**``spoke_via`` names the speaker that actually delivered something**, and stays
``"none"`` when nothing did -- the configured speaker is ``speaker_name``, and
the two differ exactly when a send failed. "The agent
replied" must never be readable as "the agent spoke out loud in the meeting"
when the configured speaker was :class:`~zoombot.speak.ChatSpeaker` (a chat
message) or :class:`~zoombot.speak.NullSpeaker` (nothing left the process).
Audio out needs a real participant -- the headless Meeting SDK container in
``bot/`` -- which has never been run against a live Zoom account here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meetings.intent import BoardRequest, IntentError

from .agent import requests_from_chunks
from .config import Config
from .rtms import MediaStream, TranscriptChunk, open_stream
from .speak import Speaker, speaker_for

__all__ = [
    "ZoomRun",
    "ZoomReport",
    "ZoomRunner",
    "RunStore",
    "run_meeting",
    "DEFAULT_CONFIDENCE_FLOOR",
    "NO_RUN_REMEMBERED",
    "meeting_id_of",
]

log = logging.getLogger("zoombot.runner")

#: Below this a request is recorded but not built. Same value and same reason
#: as ``meetings.runner``: a missed request costs someone re-asking, a false
#: one costs a board nobody wanted -- and here the speaker would announce it in
#: the room, which makes a false positive louder still.
DEFAULT_CONFIDENCE_FLOOR = 0.6

#: What the surface says about a meeting it has no memory of. Runs live in
#: memory only, so a restart forgets them; saying so is the whole point, since
#: the alternative is answering confidently about a different board.
NO_RUN_REMEMBERED = (
    "I don't have a finished run for this meeting. Runs are remembered in "
    "memory only, so a restart forgets them -- ask again and I'll start a "
    "fresh one."
)


@dataclass
class ZoomRun:
    """One board request found in one meeting, and what became of it."""

    request: BoardRequest
    meeting_id: str
    #: The pipeline's result, or None when it was not built.
    result: Any = None
    #: Why it was not built, when it was not. Empty on success.
    skipped: str = ""
    error: str = ""

    @property
    def built(self) -> bool:
        return self.result is not None


@dataclass
class ZoomReport:
    """Everything one meeting produced, including everything it declined to do."""

    meeting_id: str = ""
    considered: list[BoardRequest] = field(default_factory=list)
    runs: list[ZoomRun] = field(default_factory=list)
    #: The :class:`~zoombot.speak.Speaker` this run was configured with, read
    #: off the speaker object rather than inferred from the configuration, so
    #: a fallback to ``NullSpeaker`` cannot be reported as speech.
    speaker_name: str = "none"
    #: The speaker that actually delivered something, and ``"none"`` until one
    #: does. Deliberately not the configured speaker: a chat POST that 500'd
    #: leaves ``speaker_name="zoom_chat"`` and ``spoke_via="none"``, because a
    #: reader checking this field must not read a failed send as speech. This
    #: is the same split ``teamsbot`` uses.
    spoke_via: str = "none"
    warnings: list[str] = field(default_factory=list)
    #: What actually reached the meeting, in order -- appended only after the
    #: speaker returned. An attempt that failed is in ``warnings`` with its
    #: reason instead, so this list cannot overstate what was heard.
    said: list[str] = field(default_factory=list)
    finished_at: float = field(default_factory=time.time)

    @property
    def built(self) -> list[ZoomRun]:
        return [run for run in self.runs if run.built]

    def summary(self) -> str:
        """The one line a human reads. A failure must never read as silence.

        `HandshakeError`, `StreamInterruptedError` and `EmptyStreamError` are
        three distinguishable facts on purpose -- and then this line collapsed
        all of them, plus a failed attempt to speak, into "no board request in
        this meeting", the sentence for a meeting that simply went quietly.
        """
        if self.warnings:
            reason = self.warnings[0]
            if not self.considered:
                return f"{self.meeting_id}: nothing could be read -- {reason}"
            return (
                f"{self.meeting_id}: {len(self.considered)} request(s), "
                f"{len(self.built)} built, "
                f"{len(self.runs) - len(self.built)} skipped "
                f"(spoke via {self.spoke_via}; "
                f"{len(self.warnings)} warning(s): {reason})"
            )
        if not self.considered:
            return f"{self.meeting_id}: no board request in this meeting"
        return (
            f"{self.meeting_id}: {len(self.considered)} request(s), "
            f"{len(self.built)} built, "
            f"{len(self.runs) - len(self.built)} skipped "
            f"(spoke via {self.spoke_via})"
        )


def _say(report: ZoomReport, speaker: Speaker, meeting_id: str, text: str) -> None:
    """Say one thing in the meeting, recording it either way.

    A speaker failure is never allowed to take the run with it: the board is
    the product, and losing it because a chat POST 500'd would be the tail
    wagging the dog. It is recorded as a warning, which is the difference
    between "said nothing" and "tried to speak and could not".
    """
    try:
        speaker.say(meeting_id, text)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        log.warning("speaker %s failed: %s", getattr(speaker, "name", "?"), exc)
        report.warnings.append(
            f"could not say this in the meeting via {report.speaker_name}: "
            f"{type(exc).__name__}: {exc}"
        )
        return
    report.said.append(text)
    report.spoke_via = report.speaker_name


def _drain(stream: MediaStream, report: ZoomReport) -> list[TranscriptChunk]:
    """Read the whole stream, keeping whatever arrived before a failure.

    A media socket that drops halfway through a meeting has still delivered
    real speech, and throwing it away would turn a partial meeting into a
    silent one. The failure is named in ``warnings`` so a short transcript is
    never mistaken for a short meeting.
    """
    chunks: list[TranscriptChunk] = []
    try:
        for chunk in stream:
            chunks.append(chunk)
    except Exception as exc:  # noqa: BLE001 - any transport failure
        report.warnings.append(
            f"the media stream ended early ({type(exc).__name__}: {exc}); "
            f"{len(chunks)} chunk(s) had already arrived"
        )
    return chunks


def run_meeting(
    config: Config,
    model,
    stream: MediaStream,
    *,
    speaker: Speaker | None = None,
    generate: Callable[..., Any] | None = None,
    meeting_id: str = "",
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    max_requests: int = 3,
    **generate_kwargs,
) -> ZoomReport:
    """Listen to one meeting, draft what it asked for, and say what happened.

    ``generate`` defaults to :func:`silkscreen.agents.generate_pcb`, injected
    rather than imported at module scope so a test can drive the whole path
    without the engine's solver.

    A failure in one request does not abandon the others: a meeting that
    mentions two boards should not lose the second because the first named an
    unsupported package. Each failure is recorded on its own run, and said out
    loud with its reason -- an agent that goes quiet is indistinguishable from
    one that is still working.
    """
    speaker = speaker or speaker_for(config)
    report = ZoomReport(
        meeting_id=meeting_id, speaker_name=getattr(speaker, "name", "?")
    )

    chunks = _drain(stream, report)
    if not report.meeting_id:
        report.meeting_id = chunks[0].meeting_id if chunks else ""

    if report.meeting_id and not config.allows(report.meeting_id):
        # Checked here as well as at the webhook: a stream can be opened from
        # more than one place, and the allowlist is what stops this joining a
        # meeting nobody enabled it for.
        report.warnings.append(
            f"meeting {report.meeting_id} is not in ZOOM_MEETINGS; nothing was "
            f"read and no model call was made"
        )
        return report

    if not chunks:
        # Distinct from "no request found": there was nothing to read at all,
        # which usually means RTMS was not enabled for this meeting.
        report.warnings.append(
            "no transcript arrived; RTMS only streams when the meeting's host "
            "has real-time media streams enabled for this app"
        )
        return report

    try:
        requests = requests_from_chunks(
            model, chunks, max_requests=max_requests, warnings=report.warnings
        )
    except IntentError as exc:
        # The model claimed requests and could not back any of them up. That is
        # one meeting's problem, not the caller's: report it, say it, move on.
        report.warnings.append(f"could not read a request out of this meeting: {exc}")
        _say(
            report,
            speaker,
            report.meeting_id,
            "I heard something that sounded like a board request but couldn't "
            "match it to anything actually said, so I haven't built anything.",
        )
        return report

    report.considered = list(requests)
    if not requests:
        return report

    if generate is None:
        from silkscreen.agents import generate_pcb as generate  # noqa: PLC0415

    cap = max(1, int(config.max_runs_per_meeting))
    started = 0
    for request in requests:
        run = ZoomRun(request=request, meeting_id=report.meeting_id)
        if request.confidence < confidence_floor:
            run.skipped = (
                f"confidence {request.confidence:.2f} is below the "
                f"{confidence_floor:.2f} floor; recorded, not built"
            )
            report.runs.append(run)
            _say(
                report,
                speaker,
                report.meeting_id,
                f"I noted “{request.intent}” but wasn't confident enough that "
                f"it was a real request, so I haven't built it. Say it again "
                f"plainly if you want it.",
            )
            continue
        if started >= cap:
            # Say it rather than trimming quietly: a backlog that looks like an
            # empty queue is how "it ignored what I asked for" happens.
            run.skipped = (
                f"stopped at max_runs_per_meeting={cap}; recorded, not built"
            )
            report.runs.append(run)
            _say(
                report,
                speaker,
                report.meeting_id,
                f"That's more than {cap} board(s) from one meeting, so I've "
                f"noted “{request.intent}” without building it.",
            )
            continue

        started += 1
        try:
            run.result = generate(model, request.intent, **generate_kwargs)
        except Exception as exc:  # noqa: BLE001 - see docstring
            # Deliberately broad: the pipeline raises several unrelated types
            # (ProposalError, UnsupportedPackage, ValueError, ModelError), and
            # one bad request must not sink the rest of the meeting.
            run.error = f"{type(exc).__name__}: {exc}"
            _say(
                report,
                speaker,
                report.meeting_id,
                f"I tried to draft “{request.intent}” and it failed: "
                f"{run.error}. Nothing was ordered.",
            )
        else:
            _say(
                report,
                speaker,
                report.meeting_id,
                f"I've drafted a board for “{request.intent}”. It's a draft in "
                f"KiCad for someone to review -- nothing has been ordered.",
            )
        report.runs.append(run)

    return report


class RunStore:
    """Per-meeting memory of finished reports, bounded so it cannot grow.

    In memory only, exactly as ``slackbot.runner.RunStore`` is: a restart loses
    it, and the surface then says :data:`NO_RUN_REMEMBERED` rather than
    answering about whichever board happened to be last -- which is the failure
    a persisted store would have to be designed to avoid anyway.
    """

    def __init__(self, limit: int = 200):
        self._limit = limit
        self._lock = threading.Lock()
        self._reports: dict[str, ZoomReport] = {}

    def put(self, meeting_id: str, report: ZoomReport) -> None:
        if not meeting_id:
            return
        with self._lock:
            self._reports[meeting_id] = report
            while len(self._reports) > self._limit:
                oldest = min(
                    self._reports, key=lambda key: self._reports[key].finished_at
                )
                del self._reports[oldest]

    def get(self, meeting_id: str) -> ZoomReport | None:
        with self._lock:
            return self._reports.get(meeting_id)


class ZoomRunner:
    """What the HTTP surface calls: one RTMS notification in, one report out.

    Separate from :func:`run_meeting` so the pure half -- a stream and a model
    in, a report out -- stays testable with no config plumbing, and so the
    webhook has one object to hold the store, the speaker and the concurrency
    bound.
    """

    def __init__(
        self,
        config: Config,
        *,
        model_factory: Callable[[], Any] | None = None,
        speaker: Speaker | None = None,
        connect: Callable[..., Any] | None = None,
        store: RunStore | None = None,
        max_concurrent: int = 1,
        **run_kwargs: Any,
    ):
        self.config = config
        self.store = store or RunStore()
        self._model_factory = model_factory
        self._speaker = speaker
        self._connect = connect
        self._run_kwargs = run_kwargs
        # A design run costs real model calls and a CP-SAT solve; the semaphore
        # is what stops five simultaneous meetings starting five at once.
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))

    # -- the work ---------------------------------------------------------

    def handle_rtms_started(self, payload: dict[str, Any]) -> ZoomReport:
        """Open the stream this notification describes and run the meeting."""
        meeting_id = meeting_id_of(payload)
        speaker = self._speaker or speaker_for(self.config)
        report = ZoomReport(
            meeting_id=meeting_id, speaker_name=getattr(speaker, "name", "?")
        )
        try:
            model = self._model()
        except Exception as exc:  # noqa: BLE001 - a missing key is not a crash
            report.warnings.append(
                f"could not build the model ({type(exc).__name__}: {exc}); "
                f"nothing was read"
            )
            self.store.put(meeting_id, report)
            return report
        try:
            stream = open_stream(self.config, payload, connect=self._connect)
        except Exception as exc:  # noqa: BLE001 - any transport failure
            report.warnings.append(
                f"could not open the media stream ({type(exc).__name__}: {exc})"
            )
            self.store.put(meeting_id, report)
            return report

        report = run_meeting(
            self.config,
            model,
            stream,
            speaker=speaker,
            meeting_id=meeting_id,
            **self._run_kwargs,
        )
        self.store.put(report.meeting_id or meeting_id, report)
        return report

    def report_for(self, meeting_id: str) -> ZoomReport | None:
        return self.store.get(meeting_id)

    def summary_for(self, meeting_id: str) -> str:
        """A sentence about this meeting, or the honest admission of amnesia."""
        report = self.store.get(meeting_id)
        return report.summary() if report is not None else NO_RUN_REMEMBERED

    def _model(self) -> Any:
        if self._model_factory is not None:
            return self._model_factory()
        from silkscreen.agents.model import GeminiModel  # noqa: PLC0415

        return GeminiModel()

    # -- concurrency ------------------------------------------------------

    def acquire_slot(self, timeout: float = 0.0) -> bool:
        return self._slots.acquire(blocking=timeout > 0, timeout=timeout or None)

    def release_slot(self) -> None:
        self._slots.release()


def meeting_id_of(payload: dict[str, Any]) -> str:
    """The meeting this notification is about.

    Zoom nests the identifiers under ``payload.object``; the meeting UUID is
    the stable one (a meeting *number* is reused by every recurrence), so it is
    preferred and the number is the fallback.
    """
    obj = payload.get("payload") or {}
    obj = obj.get("object") if isinstance(obj, dict) else None
    if not isinstance(obj, dict):
        obj = {}
    for key in ("meeting_uuid", "uuid", "id", "meeting_id"):
        value = obj.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""

"""From a Teams meeting to a drafted board.

The only module in this package that knows both Teams and silkscreen -- the
shape ``slackbot/runner.py`` and ``meetings/runner.py`` already take. Below it,
:mod:`teamsbot.graph` speaks HTTP to Microsoft Graph and has never heard of a
netlist; above it, the pipeline has never heard of a meeting.
:func:`silkscreen.agents.generate_pcb` is imported **inside** the call rather
than at module scope, so importing this package costs nothing and pulls in no
solver: the webhook process should be able to answer a Teams handshake without
OR-Tools resident.

**Nothing is ever ordered.** A meeting is a place where people think out loud.
Everything here is drafted, reported with the sentence that caused it, and left
for a human to confirm -- ``slackbot/order.py`` already established that money
is spent only after an explicit human step, and a board that a meeting merely
implied is precisely the wrong thing to spend it on.

**Four gates, all inherited and none optional:**

1. A request whose quote is not in the transcript is dropped
   (:mod:`meetings.intent`, applied per window by :mod:`teamsbot.agent`).
2. A request below :data:`DEFAULT_CONFIDENCE_FLOOR` is *recorded* in
   ``considered`` and not built. "We could just use a 5 volt rail" is not an
   order.
3. ``max_runs_per_meeting`` caps how many paid runs one meeting can bill for,
   and the requests past the cap are reported as capped rather than vanishing.
4. A meeting still in progress is skipped entirely. Half a meeting is half a
   requirement, and the other half often contradicts the first.

**``considered`` is deliberately separate from ``runs``.** A report that listed
only what it built would make a skipped request invisible, and "it ignored what
I asked for" is the failure people actually hit and cannot debug.

**``spoke_via`` names the speaker that actually delivered the message**, so
"the agent replied in the meeting chat" can never be read as "the agent spoke
out loud". A speaker that raised leaves ``spoke_via`` at ``"none"`` and puts
the failure in ``warnings``; a claim that something was said is only ever made
when it was.

Unverified against a live Microsoft 365 tenant: every test in this package runs
against a recorded transport and a scripted model.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from meetings.intent import BoardRequest, IntentError

from .agent import (
    DEFAULT_WINDOW_POLICY,
    Chunk,
    WindowPolicy,
    requests_from_chunks,
    requests_from_transcript,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from silkscreen.agents.model import Model

    from .app import Incoming
    from .config import Config

__all__ = [
    "TeamsRun",
    "TeamsReport",
    "Speaker",
    "TranscriptSource",
    "run_meeting",
    "run_transcript",
    "handle_incoming",
    "DEFAULT_CONFIDENCE_FLOOR",
    "DEFAULT_MAX_RUNS_PER_MEETING",
]

#: Below this a request is recorded but not built. Identical to
#: ``meetings.runner.DEFAULT_CONFIDENCE_FLOOR`` and deliberately restated
#: rather than imported: the two front ends must be free to tune independently
#: (live speech is noisier than a proofread transcript), and a shared constant
#: would make one of them silently follow the other's tuning.
DEFAULT_CONFIDENCE_FLOOR = 0.6

#: Used only when the config object does not carry one. Every run is a *paid*
#: pipeline run, so the fallback is small and always present rather than
#: unbounded.
DEFAULT_MAX_RUNS_PER_MEETING = 2


class Speaker(Protocol):
    """The frozen ``teamsbot.speak.Speaker`` seam, restated structurally.

    Restated rather than imported so this module can be driven by a recording
    stand-in with no transport at all -- the same reason the pipeline talks to
    ``Model`` and not to ``GeminiModel``. ``teamsbot.speak.ChatSpeaker``,
    ``CallingBotSpeaker`` and ``NullSpeaker`` all satisfy it.
    """

    name: str

    def say(self, meeting_id: str, text: str) -> None:
        ...


class TranscriptSource(Protocol):
    """The half of :class:`teamsbot.graph.GraphClient` this module needs.

    ``user_id`` is Graph's, not ours: ``callTranscripts`` hang off
    ``users/{id}/onlineMeetings/{id}``, so reading one always needs the
    organiser's id as well as the meeting's.
    """

    def transcript_text(self, user_id: str, meeting: Any) -> str:
        ...


@dataclass
class TeamsRun:
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
class TeamsReport:
    """Everything one meeting produced, including everything it declined to do."""

    meeting_id: str = ""
    transcript_chars: int = 0
    considered: list[BoardRequest] = field(default_factory=list)
    runs: list[TeamsRun] = field(default_factory=list)
    #: The speaker that actually delivered the summary; ``"none"`` when
    #: nothing was said, whatever the reason.
    spoke_via: str = "none"
    warnings: list[str] = field(default_factory=list)

    @property
    def built(self) -> list[TeamsRun]:
        return [run for run in self.runs if run.built]

    @property
    def skipped(self) -> list[TeamsRun]:
        return [run for run in self.runs if not run.built]

    def summary(self) -> str:
        """One line; it hides neither a declined request nor a failure.

        The four transcript outcomes are four types precisely so that "the
        meeting was silent" and "the transcript could not be read" stay
        apart -- and then this line reported both as "no board request in
        this meeting".
        """
        if self.warnings:
            reason = self.warnings[0]
            if not self.considered:
                return f"{self.meeting_id}: nothing could be read -- {reason}"
            return (
                f"{self.meeting_id}: {len(self.considered)} request(s), "
                f"{len(self.built)} built, {len(self.skipped)} not built "
                f"({len(self.warnings)} warning(s): {reason})"
            )
        if not self.considered:
            return f"{self.meeting_id}: no board request in this meeting"
        return (
            f"{self.meeting_id}: {len(self.considered)} request(s), "
            f"{len(self.built)} built, {len(self.skipped)} not built"
        )

    def spoken_text(self) -> str:
        """What the agent says back into the meeting.

        It names the skipped requests as well as the built ones, for the same
        reason ``considered`` exists: the room should hear that something was
        heard and declined, rather than silence it cannot tell from deafness.
        """
        lines = [self.summary()]
        for run in self.runs:
            if run.built:
                lines.append(f"drafted: {run.request.intent}")
            elif run.error:
                lines.append(f"failed: {run.request.intent} ({run.error})")
            else:
                lines.append(f"not built: {run.request.intent} -- {run.skipped}")
        lines.append("Nothing has been ordered.")
        return "\n".join(lines)


def _meeting_id(meeting: Any) -> str:
    """The id of whatever the caller passed, be it an object or a string."""
    if isinstance(meeting, str):
        return meeting
    for attribute in ("id", "meeting_id", "name"):
        value = getattr(meeting, attribute, None)
        if isinstance(value, str) and value:
            return value
    return str(meeting)


def _in_progress(meeting: Any, now: datetime) -> bool:
    """Is this meeting still running at ``now``?

    :meth:`teamsbot.graph.Meeting.ongoing_at` is the authority when the caller
    passed a real meeting -- it already decides that an unknown end time counts
    as ongoing, which is the safe direction: defaulting to "finished" is how a
    paid run happens on half a meeting. An ``in_progress`` flag is honoured for
    a caller holding a lighter object, and a bare meeting id carries no end
    time at all, so it is taken at the caller's word.
    """
    ongoing_at = getattr(meeting, "ongoing_at", None)
    if callable(ongoing_at):
        return bool(ongoing_at(now))
    flag = getattr(meeting, "in_progress", None)
    if isinstance(flag, bool):
        return flag
    return False


def _allowed(config: Any, meeting_id: str) -> bool:
    allows = getattr(config, "allows", None)
    return True if allows is None else bool(allows(meeting_id))


def _max_runs(config: Any) -> int:
    value = getattr(config, "max_runs_per_meeting", DEFAULT_MAX_RUNS_PER_MEETING)
    return int(value) if isinstance(value, int) and value > 0 else 1


def _build(
    config: Any,
    model: Model,
    meeting_id: str,
    requests: list[BoardRequest],
    report: TeamsReport,
    *,
    generate: Callable[..., Any] | None,
    confidence_floor: float,
    generate_kwargs: dict[str, Any],
) -> None:
    """Turn verified requests into runs, honouring the floor and the cap."""
    report.considered = list(requests)
    if not requests:
        return

    if generate is None:
        from silkscreen.agents import generate_pcb as generate  # noqa: PLC0415

    cap = _max_runs(config)
    built = 0
    for request in requests:
        run = TeamsRun(request=request, meeting_id=meeting_id)
        if request.confidence < confidence_floor:
            run.skipped = (
                f"confidence {request.confidence:.2f} is below the "
                f"{confidence_floor:.2f} floor; recorded, not built"
            )
            report.runs.append(run)
            continue
        if built >= cap:
            # Reported, not trimmed. A request that silently disappears at the
            # cap is indistinguishable from one the agent never heard.
            run.skipped = (
                f"max_runs_per_meeting={cap} reached; recorded, not built"
            )
            report.runs.append(run)
            continue
        try:
            run.result = generate(model, request.intent, **generate_kwargs)
            built += 1
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad, exactly as ``meetings/runner.py`` is: the
            # pipeline raises several unrelated types (ProposalError,
            # UnsupportedPackage, ValueError) and one bad request must not sink
            # the rest of the meeting. The cap is not consumed by a failure.
            run.error = f"{type(exc).__name__}: {exc}"
        report.runs.append(run)


def _speak(
    report: TeamsReport, speaker: Speaker | None, *, target: str = ""
) -> None:
    """Say the summary back, and record only what was actually said.

    ``target`` is where to say it -- the chat conversation id when there is
    one, since a meeting id is not addressable by
    :class:`teamsbot.speak.ChatSpeaker`. ``spoke_via`` stays ``"none"`` unless
    delivery returned without raising. A :class:`~teamsbot.speak.NullSpeaker`
    run reports ``"null"`` instead, which is a different fact worth keeping
    apart: speaking was switched off, rather than attempted and lost.
    """
    if not report.considered:
        # Nobody asked for anything. Announcing that into a live meeting is
        # noise, and silence here is not ambiguous: there is no request whose
        # fate could be misread.
        return
    if speaker is None:
        report.warnings.append(
            "no speaker configured; the meeting was not told what was drafted"
        )
        return
    name = getattr(speaker, "name", type(speaker).__name__)
    try:
        speaker.say(target or report.meeting_id, report.spoken_text())
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"{name} speaker failed: {type(exc).__name__}: {exc}")
        return
    report.spoke_via = name


def run_meeting(
    config: Any,
    model: Model,
    chunks: Iterable[Chunk],
    *,
    meeting_id: str = "",
    speaker: Speaker | None = None,
    speak_to: str = "",
    generate: Callable[..., Any] | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    max_requests: int = 3,
    policy: WindowPolicy = DEFAULT_WINDOW_POLICY,
    **generate_kwargs: Any,
) -> TeamsReport:
    """The live path: a stream of transcript fragments in, a report out.

    ``chunks`` is anything iterable of :class:`~teamsbot.agent.Chunk` -- the
    media stream in production, a recorded list in tests. ``meeting_id``
    defaults to the id the chunks themselves carry, so a caller holding only
    the stream does not have to repeat it.

    ``generate`` defaults to :func:`silkscreen.agents.generate_pcb`, injected
    rather than imported at module scope so a test can drive the whole path
    without the solver.
    """
    materialised = list(chunks)
    if not meeting_id:
        for chunk in materialised:
            candidate = getattr(chunk, "meeting_id", "")
            if candidate:
                meeting_id = candidate
                break
    report = TeamsReport(meeting_id=meeting_id)

    if not _allowed(config, meeting_id):
        report.warnings.append(
            f"meeting {meeting_id!r} is not in TEAMS_MEETINGS; nothing was read"
        )
        return report

    report.transcript_chars = sum(len(chunk.text or "") for chunk in materialised)
    if not materialised:
        # Distinct from "no request found": the stream delivered nothing at
        # all, which usually means the calling bot never received media.
        report.warnings.append(
            "the stream produced no transcript fragments; the calling bot may "
            "not have been admitted to the meeting"
        )
        return report

    try:
        requests = requests_from_chunks(
            model,
            materialised,
            max_requests=max_requests,
            policy=policy,
            on_warning=report.warnings.append,
        )
    except IntentError as exc:
        # One meeting's problem, not the poll's: propagating it would abandon
        # every other meeting a caller is processing.
        report.warnings.append(f"could not read a request out of this meeting: {exc}")
        _speak(report, speaker, target=speak_to)
        return report

    _build(
        config,
        model,
        meeting_id,
        requests,
        report,
        generate=generate,
        confidence_floor=confidence_floor,
        generate_kwargs=generate_kwargs,
    )
    _speak(report, speaker, target=speak_to)
    return report


def run_transcript(
    config: Any,
    model: Model,
    client: TranscriptSource,
    meeting: Any,
    *,
    user_id: str = "",
    speaker: Speaker | None = None,
    speak_to: str = "",
    generate: Callable[..., Any] | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    max_requests: int = 3,
    now: datetime | None = None,
    **generate_kwargs: Any,
) -> TeamsReport:
    """The post-hoc path: one finished meeting's Graph ``callTranscript``.

    ``meeting`` may be the :class:`teamsbot.graph.Meeting` the client yields or
    a bare id; ``user_id`` is the organiser Graph hangs the transcript off.
    A meeting still in progress is skipped and said to be skipped -- reading
    half a transcript would draft a board from a requirement the second half
    revises.

    Three outcomes that must never be confused, and are not: **no transcript
    exists** (the read raised -- transcription was probably off, or the bot
    lacks the permission), **an empty transcript** (the resource is there and
    says nothing), and **no request in it** (a complete, correct answer).
    """
    meeting_id = _meeting_id(meeting)
    report = TeamsReport(meeting_id=meeting_id)

    if not _allowed(config, meeting_id):
        report.warnings.append(
            f"meeting {meeting_id!r} is not in TEAMS_MEETINGS; nothing was read"
        )
        return report

    if _in_progress(meeting, now or datetime.now(UTC)):
        report.warnings.append(
            "the meeting is still in progress; skipped until it ends, because "
            "half a meeting is half a requirement"
        )
        return report

    try:
        transcript = client.transcript_text(user_id, meeting)
    except Exception as exc:  # noqa: BLE001
        # The type is named in the message rather than caught narrowly: this
        # module must not import Graph's error class to stay testable without
        # the transport, and every failure here means the same thing to the
        # caller -- there is no transcript to read, which is not the same as a
        # meeting that asked for nothing.
        # ``graph.NoTranscriptError`` and ``TranscriptionDisabledError`` already
        # explain themselves in their message, so the distinction they draw
        # (nothing yet, versus never) survives into the warning verbatim.
        report.warnings.append(
            f"no transcript could be read for this meeting: "
            f"{type(exc).__name__}: {exc}"
        )
        return report

    report.transcript_chars = len(transcript or "")
    if not (transcript or "").strip():
        report.warnings.append(
            "the meeting has a transcript resource but no text in it; Teams "
            "only records one when transcription was turned on"
        )
        return report

    try:
        requests = requests_from_transcript(
            model, transcript, max_requests=max_requests
        )
    except IntentError as exc:
        report.warnings.append(f"could not read a request out of this meeting: {exc}")
        _speak(report, speaker, target=speak_to)
        return report

    _build(
        config,
        model,
        meeting_id,
        requests,
        report,
        generate=generate,
        confidence_floor=confidence_floor,
        generate_kwargs=generate_kwargs,
    )
    _speak(report, speaker, target=speak_to)
    return report


#: What a follow-up ("review", "status") gets from here. Answering it would
#: mean reaching into the dispatcher's run memory, which lives in
#: :mod:`teamsbot.app` and is deliberately in-process; saying so is better than
#: answering about whatever board happens to be lying around.
FOLLOW_UP_NOTE = (
    "I can only report on the run you are replying to. Ask for a board and "
    "I'll draft it; the last run's summary is in this meeting's thread."
)

#: The standing rule, said out loud when someone asks for it. There is no
#: ordering path in this package at all -- ``slackbot/order.py`` prepares an
#: order and stops, and nothing here even does that.
NO_ORDER_NOTE = (
    "Nothing is ever ordered from a meeting. I draft boards and report what "
    "they need; a person places any order, deliberately, somewhere else."
)


def _live_model() -> Model:
    """The default model, built only once something is actually going to run."""
    from silkscreen.agents.model import default_model  # noqa: PLC0415

    return default_model()


def handle_incoming(
    config: Config,
    incoming: Incoming,
    *,
    speaker: Speaker | None = None,
    model: Model | None = None,
    client: TranscriptSource | None = None,
    user_id: str = "",
    generate: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> TeamsReport:
    """The entry point :class:`teamsbot.app.Dispatcher` hands accepted work to.

    One authenticated notification in, one report out. The dispatcher has
    already proved the request authentic, new, in scope and slotted; every
    judgement about *what it asked for* is made here and nowhere else.

    ``model`` defaults to a live Gemini model, imported inside the call for the
    same reason ``generate`` is: this module must stay importable, and the HTTP
    surface must stay startable, on a machine with no engine and no key. It is
    built at the point of use rather than up front, so a notification that is
    refused here -- an ``order``, a follow-up, a call with no Graph client --
    needs neither a key nor a model call to be declined.
    """
    target = incoming.conversation_id or incoming.meeting_id
    follow_up = incoming.follow_up

    if follow_up == "order":
        report = TeamsReport(meeting_id=incoming.meeting_id)
        report.warnings.append(NO_ORDER_NOTE)
        _say_flat(report, speaker, target, NO_ORDER_NOTE)
        return report

    if follow_up:
        report = TeamsReport(meeting_id=incoming.meeting_id)
        report.warnings.append(f"follow-up {follow_up!r}: {FOLLOW_UP_NOTE}")
        _say_flat(report, speaker, target, FOLLOW_UP_NOTE)
        return report

    if incoming.kind == "call":
        if client is None or not user_id:
            # Not a quiet no-op: the call path needs a Graph client and the
            # organiser's user id, and a notification that silently does
            # nothing is indistinguishable from one that was never delivered.
            report = TeamsReport(meeting_id=incoming.meeting_id)
            report.warnings.append(
                "a call notification needs a GraphClient and the organiser's "
                "user id to read the transcript; neither was supplied, so "
                "nothing was read"
            )
            return report
        return run_transcript(
            config,
            model or _live_model(),
            client,
            incoming.meeting_id,
            user_id=user_id,
            speaker=speaker,
            speak_to=target,
            generate=generate,
            **kwargs,
        )

    # A chat message. The text is one utterance, so it is one chunk: the quote
    # gate then asks the model to quote the sentence the person actually typed,
    # which is exactly the check that belongs here.
    chunks = [
        _Utterance(
            meeting_id=incoming.meeting_id,
            speaker=incoming.sender,
            text=incoming.text,
            at_ms=0,
        )
    ]
    return run_meeting(
        config,
        model or _live_model(),
        chunks,
        meeting_id=incoming.meeting_id,
        speaker=speaker,
        speak_to=target,
        generate=generate,
        **kwargs,
    )


@dataclass(frozen=True)
class _Utterance:
    """One chat message as a :class:`~teamsbot.agent.Chunk`."""

    meeting_id: str
    speaker: str
    text: str
    at_ms: int


def _say_flat(
    report: TeamsReport, speaker: Speaker | None, target: str, text: str
) -> None:
    """Say one fixed line, recording ``spoke_via`` only if it was delivered."""
    if speaker is None:
        report.warnings.append("no speaker configured; nothing was said back")
        return
    name = getattr(speaker, "name", type(speaker).__name__)
    try:
        speaker.say(target, text)
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"{name} speaker failed: {type(exc).__name__}: {exc}")
        return
    report.spoke_via = name

"""From a finished meeting to a drafted board.

This is the only module that knows both Meet and silkscreen, the same shape
``slackbot/runner.py`` takes: the client below it speaks HTTP and nothing else,
the pipeline above it has never heard of a meeting.

**Two gates, and they are the point.** A meeting is a lossy, noisy source --
people think out loud, change their minds, and say "we could just use a 5 volt
rail" without meaning "build that". So a transcript never reaches the pipeline
directly. It goes through :func:`~meetings.intent.extract_requests`, which
drops anything it cannot quote from the transcript, and then through the
confidence floor here. What survives is *drafted*, reported back with the
sentence that caused it, and never ordered: :mod:`slackbot.order` already
established that a human confirms before money is spent, and a board that a
meeting merely implied is exactly the wrong thing to spend money on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import MeetConfig
from .intent import BoardRequest, IntentError, extract_requests
from .meet import Conference, MeetClient, MeetError

__all__ = ["MeetingRun", "MeetingReport", "run_meeting", "poll_once"]

#: Below this the request is recorded but not built. A meeting is full of
#: hypotheticals, and a paid pipeline run per idle musing is both expensive and
#: noisy. Tuned to be conservative: a missed request costs someone re-asking,
#: a false one costs a board nobody wanted.
DEFAULT_CONFIDENCE_FLOOR = 0.6


@dataclass
class MeetingRun:
    """One board request found in one meeting, and what became of it."""

    request: BoardRequest
    conference: str
    #: The pipeline's result, or None when it was not built.
    result: Any = None
    #: Why it was not built, when it was not. Empty on success.
    skipped: str = ""
    error: str = ""

    @property
    def built(self) -> bool:
        return self.result is not None


@dataclass
class MeetingReport:
    """Everything one poll did, including everything it declined to do.

    ``considered`` is deliberately separate from ``runs``: a report that only
    listed what it built would make a skipped request invisible, and "the bot
    silently ignored what I asked for" is the failure people actually hit.
    """

    conference: str = ""
    transcript_chars: int = 0
    considered: list[BoardRequest] = field(default_factory=list)
    runs: list[MeetingRun] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def built(self) -> list[MeetingRun]:
        return [r for r in self.runs if r.built]

    def summary(self) -> str:
        """The one line a human reads. A failure must never read as silence.

        This branched on ``considered`` alone and dropped ``warnings``
        entirely, so a meeting whose transcript could not be read at all --
        403, transcription never enabled, the stream cut halfway -- produced
        the identical sentence as a meeting where nobody asked for a board:
        "no board request in this meeting". The rest of this module keeps
        those two facts carefully apart (that is what ``considered`` exists
        for) and then the summary collapsed them.
        """
        if self.warnings:
            reason = self.warnings[0]
            if not self.considered:
                return f"{self.conference}: nothing could be read -- {reason}"
            return (
                f"{self.conference}: {len(self.considered)} request(s), "
                f"{len(self.built)} built, "
                f"{len(self.runs) - len(self.built)} skipped "
                f"({len(self.warnings)} warning(s): {reason})"
            )
        if not self.considered:
            return f"{self.conference}: no board request in this meeting"
        return (
            f"{self.conference}: {len(self.considered)} request(s), "
            f"{len(self.built)} built, "
            f"{len(self.runs) - len(self.built)} skipped"
        )


def run_meeting(
    client: MeetClient,
    model,
    conference: Conference | str,
    *,
    generate: Callable[..., Any] | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    max_requests: int = 3,
    **generate_kwargs,
) -> MeetingReport:
    """Read one conference's transcript and draft a board for what it asked for.

    ``generate`` defaults to :func:`silkscreen.agents.generate_pcb`, injected
    rather than imported at module scope so a test can drive the whole path
    without the engine's solver -- and so this module keeps no import-time
    dependency on the engine at all.

    A failure in one request does not abandon the others: a meeting that
    mentions two boards should not lose the second because the first had an
    unsupported package. Each failure is recorded on its own run.
    """
    name = conference.name if isinstance(conference, Conference) else conference
    report = MeetingReport(conference=name)

    try:
        transcript = client.transcript_text(name)
    except MeetError as exc:
        report.warnings.append(f"could not read the transcript: {exc}")
        return report

    report.transcript_chars = len(transcript)
    if not transcript.strip():
        # Distinct from "no request found": transcription was probably off.
        report.warnings.append(
            "the conference has no transcript; Meet only records one when the "
            "organiser turns transcription on"
        )
        return report

    try:
        requests = extract_requests(model, transcript, max_requests=max_requests)
    except IntentError as exc:
        # extract_requests raises when the model claimed board requests but
        # every quote it gave was absent from the transcript. That is a real
        # signal and must be reported -- but it is one meeting's problem, and
        # letting it propagate would abandon every other conference in the
        # poll. Record it and move on, the same way a failed pipeline run for
        # one request does not sink the others below.
        report.warnings.append(f"could not read a request out of this meeting: {exc}")
        return report
    report.considered = list(requests)

    if generate is None:
        from silkscreen.agents import generate_pcb as generate  # noqa: PLC0415

    for request in requests:
        run = MeetingRun(request=request, conference=name)
        if request.confidence < confidence_floor:
            run.skipped = (
                f"confidence {request.confidence:.2f} is below the "
                f"{confidence_floor:.2f} floor; recorded, not built"
            )
            report.runs.append(run)
            continue
        try:
            run.result = generate(model, request.intent, **generate_kwargs)
        except Exception as exc:
            # Deliberately broad: the pipeline raises several unrelated types
            # (ProposalError, UnsupportedPackage, ValueError), and one bad
            # request must not sink the rest of the meeting.
            run.error = f"{type(exc).__name__}: {exc}"
        report.runs.append(run)

    return report


def poll_once(
    client: MeetClient,
    model,
    *,
    seen: set[str] | None = None,
    now: datetime | None = None,
    config: MeetConfig | None = None,
    **kwargs,
) -> list[MeetingReport]:
    """Process every in-scope conference that finished recently.

    ``seen`` is the caller's memory of conferences already handled, mutated in
    place. Without it a poll loop re-runs every meeting in the window on every
    tick -- each one a paid pipeline run. The caller owns it because durable
    storage is the caller's problem; an in-memory set is honest for one
    process and obviously wrong for two.
    """
    config = config or client.config
    seen = seen if seen is not None else set()
    reports: list[MeetingReport] = []

    for conference in client.recent_conferences(now=now):
        if conference.name in seen:
            continue
        if len(reports) >= config.max_runs_per_poll:
            # Say so rather than trimming quietly: the caller needs to know
            # there is more waiting, or a backlog looks like an empty queue.
            reports.append(
                MeetingReport(
                    conference=conference.name,
                    warnings=[
                        f"stopped at max_runs_per_poll="
                        f"{config.max_runs_per_poll}; more conferences are "
                        f"waiting and will be picked up next poll"
                    ],
                )
            )
            break
        seen.add(conference.name)
        reports.append(run_meeting(client, model, conference, **kwargs))

    return reports

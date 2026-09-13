"""Runner tests.

The bug class this file guards is **the silently dropped meeting**. Every
failure mode of a poll loop that turns transcripts into board runs is invisible
from the outside: a request below the confidence floor that never appears in
the report reads as "the bot ignored me"; one bad request that aborts the loop
loses the second board someone asked for and reports success on neither; a
truncated poll that says nothing reads as an empty queue; and an exception out
of the extractor kills every remaining conference in the same tick. So nothing
here asserts "a run happened" -- every test asserts that the *declined* half is
still named in the report.

Nothing in this file opens a socket or runs the real pipeline. The Meet client
is driven through :class:`~meetings.tests.fakes.FakeTransport`, the model is a
:class:`~silkscreen.agents.model.ScriptedModel`, and ``generate`` is always
injected as a fake callable -- an un-injected ``generate`` would import the
engine and run CP-SAT, which is both slow and exactly the thing these tests are
meant to prove is *not* reached.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from silkscreen.agents.model import ScriptedModel

from meetings.config import MeetConfig
from meetings.meet import MeetClient
from meetings.runner import MeetingReport, MeetingRun, poll_once, run_meeting
from meetings.tests.fakes import FakeTransport, page

TOKEN = "ya29.test-token"
BASE = "https://meet.googleapis.com/v2"

CONF = "conferenceRecords/abc"
OTHER = "conferenceRecords/def"

#: One utterance per line, keyed by the participant resource name the client
#: turns into a speaker label. Both quotes below are copied out of these.
SAID = [
    ("alice", "we need a little 3.3 volt regulator board for the sensor rig"),
    ("bob", "and a small usb-c breakout board to go with it would be handy"),
]

REGULATOR_QUOTE = SAID[0][1]
BREAKOUT_QUOTE = SAID[1][1]

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
RECENT = "2026-08-31T10:00:00.000000000Z"
STALE = "2026-08-25T10:00:00.000000000Z"


# -- fixtures and builders ------------------------------------------------


def config(**overrides) -> MeetConfig:
    fields = {"access_token": TOKEN, "api_base": BASE}
    fields.update(overrides)
    return MeetConfig(**fields)


def conference_record(
    name: str = CONF,
    *,
    space: str = "spaces/aaa",
    end_time: str | None = RECENT,
) -> dict:
    record = {
        "name": name,
        "space": space,
        "startTime": "2026-08-31T09:00:00.000000000Z",
    }
    if end_time is not None:
        record["endTime"] = end_time
    return record


def transcript_routes(said: list[tuple[str, str]]) -> dict:
    """Script the two calls ``transcript_text`` makes for any conference.

    The keys are matched as substrings, so one pair of routes serves every
    conference record a test lists -- which is what the poll tests want.
    """
    return {
        "/transcripts": page(
            "transcripts", [{"name": f"{CONF}/transcripts/t1", "state": "ENDED"}]
        ),
        "/entries": page(
            "transcriptEntries",
            [
                {
                    "name": f"{CONF}/transcripts/t1/entries/{index}",
                    "participant": f"{CONF}/participants/{speaker}",
                    "text": text,
                }
                for index, (speaker, text) in enumerate(said, start=1)
            ],
        ),
    }


def client(routes: dict, **config_overrides) -> MeetClient:
    return MeetClient(config(**config_overrides), FakeTransport(routes))


def model_saying(*requests: dict) -> ScriptedModel:
    """A model that answers the extraction prompt with exactly ``requests``."""
    return ScriptedModel(responses=[json.dumps({"requests": list(requests)})])


def asked_for(intent: str, quote: str, confidence: float, speaker: str = "alice"):
    return {
        "intent": intent,
        "quote": quote,
        "speaker": speaker,
        "confidence": confidence,
    }


class RecordingGenerate:
    """A stand-in for ``generate_pcb`` that records and never computes."""

    def __init__(self, raises_for: str = ""):
        self.calls: list[tuple] = []
        self.raises_for = raises_for

    def __call__(self, model, intent, **kwargs):
        self.calls.append((model, intent, kwargs))
        if self.raises_for and self.raises_for in intent:
            raise ValueError("unsupported package QFN-DFN-9000")
        return {"intent": intent}


# -- run_meeting: the built half ------------------------------------------


def test_a_request_above_the_confidence_floor_is_built_from_its_own_intent():
    generate = RecordingGenerate()
    report = run_meeting(
        client(transcript_routes(SAID)),
        model_saying(asked_for("a 3.3V LDO board", REGULATOR_QUOTE, 0.9)),
        CONF,
        generate=generate,
    )

    assert [run.built for run in report.runs] == [True]
    # The pipeline must receive the *rewritten* intent, not the raw quote:
    # the quote is evidence for a human, the intent is the pipeline's input.
    assert [intent for _, intent, _ in generate.calls] == ["a 3.3V LDO board"]
    assert report.runs[0].skipped == ""
    assert report.runs[0].error == ""
    assert report.transcript_chars > 0


def test_generate_keyword_arguments_reach_the_pipeline_untouched():
    generate = RecordingGenerate()
    run_meeting(
        client(transcript_routes(SAID)),
        model_saying(asked_for("a 3.3V LDO board", REGULATOR_QUOTE, 0.9)),
        CONF,
        generate=generate,
        output="/tmp/board.kicad_pcb",
    )

    assert generate.calls[0][2] == {"output": "/tmp/board.kicad_pcb"}


# -- run_meeting: the declined half ---------------------------------------


def test_a_request_below_the_confidence_floor_is_recorded_rather_than_dropped():
    generate = RecordingGenerate()
    report = run_meeting(
        client(transcript_routes(SAID)),
        model_saying(asked_for("a 3.3V LDO board", REGULATOR_QUOTE, 0.2)),
        CONF,
        generate=generate,
    )

    assert generate.calls == []
    # The guarantee: a skipped request is visible in both lists. Appearing in
    # `considered` alone would still let a caller that reads `runs` conclude
    # the meeting asked for nothing.
    assert len(report.considered) == 1
    assert len(report.runs) == 1
    assert report.runs[0].built is False
    assert report.runs[0].skipped, "a skipped run must say why it was skipped"
    # The floor and the confidence both belong in the message: "skipped" alone
    # cannot be argued with, and re-asking is the user's only recovery.
    assert "0.20" in report.runs[0].skipped
    assert "0.60" in report.runs[0].skipped


def test_one_request_that_raises_does_not_sink_the_rest_of_the_meeting():
    generate = RecordingGenerate(raises_for="LDO")
    report = run_meeting(
        client(transcript_routes(SAID)),
        model_saying(
            asked_for("a 3.3V LDO board", REGULATOR_QUOTE, 0.9),
            asked_for("a USB-C breakout", BREAKOUT_QUOTE, 0.8, speaker="bob"),
        ),
        CONF,
        generate=generate,
    )

    # Requests come back highest-confidence first, so the LDO is run[0].
    assert len(report.runs) == 2
    assert report.runs[0].error.startswith("ValueError:")
    assert report.runs[0].built is False
    # The whole point: the second board still got drafted.
    assert report.runs[1].built is True
    assert report.runs[1].error == ""
    assert report.built == [report.runs[1]]


def test_a_conference_with_no_transcript_says_transcription_was_off():
    generate = RecordingGenerate()
    report = run_meeting(
        client(transcript_routes([("alice", "   ")])),
        ScriptedModel(),
        CONF,
        generate=generate,
    )

    assert report.warnings, "an empty transcript must not be a silent no-op"
    assert "transcription" in " ".join(report.warnings)
    # Distinct from "found no request": nothing was even looked at, so the
    # model was never called and `considered` is empty for a different reason.
    assert report.considered == []
    assert report.runs == []
    assert generate.calls == []


def test_a_meet_error_reading_the_transcript_becomes_a_warning():
    failing = client(
        {
            "/transcripts": (403, {"error": {"message": "insufficient scope"}}),
        }
    )

    # No raise: one unreadable conference must not abort the poll that
    # contains it, and the reason has to survive into the report.
    report = run_meeting(failing, ScriptedModel(), CONF, generate=RecordingGenerate())

    assert report.considered == []
    assert len(report.warnings) == 1
    assert "403" in report.warnings[0]
    assert "insufficient scope" in report.warnings[0]


def test_a_model_that_invented_every_quote_is_reported_not_raised():
    """``extract_requests`` raises ``IntentError`` when nothing verifies.

    That is the right contract for the extractor -- "the model claimed three
    requests and could back up none" is not "this meeting asked for nothing".
    It is the wrong thing to let out of ``run_meeting``, because the caller is
    ``poll_once``, and one meeting with a confabulating answer would take
    every later conference in the same tick down with it.
    """
    generate = RecordingGenerate()
    report = run_meeting(
        client(transcript_routes(SAID)),
        model_saying(asked_for("a 5V rail board", "nobody ever said this line", 0.9)),
        CONF,
        generate=generate,
    )

    assert isinstance(report, MeetingReport)
    assert report.runs == []
    assert generate.calls == []
    assert report.warnings, "an unverifiable extraction must surface as a warning"


# -- poll_once ------------------------------------------------------------


def poll_client(*records: dict, **config_overrides) -> MeetClient:
    routes = transcript_routes([("alice", "   ")])
    routes["conferenceRecords"] = page("conferenceRecords", list(records))
    return client(routes, **config_overrides)


def test_poll_once_skips_a_conference_already_in_seen():
    seen = {CONF}
    reports = poll_once(
        poll_client(conference_record(CONF), conference_record(OTHER)),
        ScriptedModel(),
        seen=seen,
        now=NOW,
        generate=RecordingGenerate(),
    )

    # Re-running a handled meeting is a paid pipeline run per poll tick, so
    # "already seen" has to win before anything else happens to it.
    assert [report.conference for report in reports] == [OTHER]


def test_poll_once_adds_newly_processed_conferences_to_seen_in_place():
    seen: set[str] = set()
    poll_once(
        poll_client(conference_record(CONF), conference_record(OTHER)),
        ScriptedModel(),
        seen=seen,
        now=NOW,
        generate=RecordingGenerate(),
    )

    # Mutated in place, not returned: the caller owns the memory across ticks.
    assert seen == {CONF, OTHER}


def test_poll_once_warns_rather_than_truncating_quietly_at_the_cap():
    third = "conferenceRecords/ghi"
    reports = poll_once(
        poll_client(
            conference_record(CONF),
            conference_record(OTHER),
            conference_record(third),
            max_runs_per_poll=1,
        ),
        ScriptedModel(),
        now=NOW,
        generate=RecordingGenerate(),
    )

    assert [report.conference for report in reports] == [CONF, OTHER]
    # The cap report carries no runs -- it exists only to say the queue is not
    # empty. Silent truncation is indistinguishable from "nothing else today".
    cap = reports[-1]
    assert cap.runs == []
    assert len(cap.warnings) == 1
    assert "max_runs_per_poll=1" in cap.warnings[0]
    assert "waiting" in cap.warnings[0]


def test_poll_once_leaves_a_capped_conference_unseen_so_it_is_retried():
    seen: set[str] = set()
    poll_once(
        poll_client(
            conference_record(CONF),
            conference_record(OTHER),
            max_runs_per_poll=1,
        ),
        ScriptedModel(),
        seen=seen,
        now=NOW,
        generate=RecordingGenerate(),
    )

    # Marking the deferred conference seen would lose it forever, which is the
    # failure the warning above is trying to prevent.
    assert seen == {CONF}


def test_poll_once_skips_an_ongoing_conference():
    reports = poll_once(
        poll_client(
            conference_record(CONF, end_time=None),
            conference_record(OTHER),
        ),
        ScriptedModel(),
        now=NOW,
        generate=RecordingGenerate(),
    )

    # A live meeting has no final transcript; half a requirement is worse
    # than none.
    assert [report.conference for report in reports] == [OTHER]


def test_poll_once_skips_a_conference_older_than_the_age_window():
    reports = poll_once(
        poll_client(
            conference_record(CONF, end_time=STALE),
            conference_record(OTHER, end_time=RECENT),
        ),
        ScriptedModel(),
        # Explicit `now`, never the wall clock: a window test that drifts with
        # the calendar is a test that passes today and fails in a month.
        now=NOW,
        generate=RecordingGenerate(),
    )

    assert [report.conference for report in reports] == [OTHER]


# -- MeetingReport ---------------------------------------------------------


def built_run(intent: str = "a board") -> MeetingRun:
    request = _request(intent, 0.9)
    return MeetingRun(request=request, conference=CONF, result={"ok": True})


def skipped_run(intent: str = "a board") -> MeetingRun:
    request = _request(intent, 0.1)
    return MeetingRun(request=request, conference=CONF, skipped="below the floor")


def _request(intent: str, confidence: float):
    from meetings.intent import BoardRequest

    return BoardRequest(intent=intent, quote=REGULATOR_QUOTE, confidence=confidence)


def test_a_report_with_no_requests_says_no_request_rather_than_zero_built():
    report = MeetingReport(conference=CONF)

    assert report.built == []
    # "0 built" would read as a failed meeting; "no board request" is the
    # accurate and much less alarming statement of the same fact.
    assert report.summary() == f"{CONF}: no board request in this meeting"


def test_a_report_where_everything_was_built_counts_them_all():
    report = MeetingReport(
        conference=CONF,
        considered=[_request("a", 0.9), _request("b", 0.8)],
        runs=[built_run("a"), built_run("b")],
    )

    assert len(report.built) == 2
    assert report.summary() == f"{CONF}: 2 request(s), 2 built, 0 skipped"


def test_a_mixed_report_separates_what_was_built_from_what_was_not():
    report = MeetingReport(
        conference=CONF,
        considered=[_request("a", 0.9), _request("b", 0.1)],
        runs=[built_run("a"), skipped_run("b")],
    )

    assert [run.request.intent for run in report.built] == ["a"]
    assert report.summary() == f"{CONF}: 2 request(s), 1 built, 1 skipped"


def test_a_run_that_errored_counts_as_not_built():
    report = MeetingReport(
        conference=CONF,
        considered=[_request("a", 0.9)],
        runs=[MeetingRun(request=_request("a", 0.9), conference=CONF, error="boom")],
    )

    # `built` is defined by a result, not by the absence of a `skipped` note:
    # a failed run and a declined one are both "not built" to a reader.
    assert report.built == []
    assert report.summary() == f"{CONF}: 1 request(s), 0 built, 1 skipped"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_a_meeting_that_could_not_be_read_does_not_read_as_a_quiet_meeting():
    """"Nothing could be read" and "nobody asked" are different facts.

    The summary is the one line a human sees. It used to branch on
    `considered` alone and drop `warnings`, so a 403 from Meet, a meeting
    with transcription switched off and a meeting where nobody asked for a
    board all produced "no board request in this meeting".
    """
    from meetings.runner import MeetingReport

    quiet = MeetingReport(conference="conferences/MU-1")
    assert quiet.summary() == "conferences/MU-1: no board request in this meeting"

    broken = MeetingReport(conference="conferences/MU-1")
    broken.warnings.append(
        "could not read the transcript: 403 from Meet: transcription access denied"
    )
    assert broken.summary() != quiet.summary()
    assert "403" in broken.summary()
    assert "no board request" not in broken.summary()

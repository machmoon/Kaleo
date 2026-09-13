"""The gates between a live meeting and a paid pipeline run.

Offline: the model is a :class:`~silkscreen.agents.model.ScriptedModel`, the
media stream is a recorded list of chunks, ``generate`` is injected, and the
speaker is one of the real ones from :mod:`zoombot.speak` (the null one, or a
deliberately failing stub). Nothing here opens a socket, needs a key, or
touches the engine.

What is actually being pinned: a request below the floor is *recorded and not
built*, the run cap holds, one failing request does not abandon the others,
``spoke_via`` names the speaker that was really used, and nothing is ever
ordered.
"""

from __future__ import annotations

import json
from pathlib import Path

from silkscreen.agents.model import ScriptedModel

from zoombot.config import Config
from zoombot.rtms import TranscriptChunk
from zoombot.runner import (
    NO_RUN_REMEMBERED,
    RunStore,
    ZoomReport,
    ZoomRunner,
    meeting_id_of,
    run_meeting,
)
from zoombot.speak import NullSpeaker, SpeakError, speaker_for

ASK = "we need a little 3.3 volt regulator board for the sensor rig"
HUB = "and we also need a small usb hub board for the bench next week"


def make_config(**overrides) -> Config:
    values = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "account_id": "account-id",
        "webhook_secret": "webhook-secret",
        "speak_mode": "off",
    }
    values.update(overrides)
    return Config(**values)


class FakeStream:
    """A recorded :class:`~zoombot.rtms.MediaStream`."""

    def __init__(
        self,
        lines: list[str],
        *,
        meeting_id: str = "uuid-1",
        fail: Exception | None = None,
    ):
        self.chunks = [
            TranscriptChunk(
                meeting_id=meeting_id, speaker="carol", text=text, at_ms=i * 1000
            )
            for i, text in enumerate(lines)
        ]
        self._fail = fail

    def __iter__(self):
        yield from self.chunks
        if self._fail is not None:
            raise self._fail


def answer(*requests) -> str:
    return json.dumps({"requests": list(requests)})


def request(quote: str, *, intent: str, conf: float = 0.9) -> dict:
    return {"intent": intent, "quote": quote, "speaker": "carol", "confidence": conf}


class Generated:
    """Stand-in for a ``PipelineResult``; only its identity matters here."""

    def __init__(self, intent: str):
        self.intent = intent


def generator(*, fails: set[str] = frozenset()):
    calls: list[str] = []

    def generate(model, intent, **kwargs):
        calls.append(intent)
        if intent in fails:
            raise ValueError(f"no footprint rule for {intent}")
        return Generated(intent)

    generate.calls = calls  # type: ignore[attr-defined]
    return generate


# -- the confidence floor ---------------------------------------------------


def test_a_low_confidence_request_is_recorded_and_not_built():
    """People think out loud; "we could just use a 5 volt rail" is not an order."""
    model = ScriptedModel(responses=[answer(request(ASK, intent="rail", conf=0.2))])
    generate = generator()
    speaker = NullSpeaker()
    report = run_meeting(
        make_config(), model, FakeStream([ASK]), speaker=speaker, generate=generate
    )
    assert [r.intent for r in report.considered] == ["rail"]
    assert report.runs[0].built is False
    assert "below the" in report.runs[0].skipped
    assert generate.calls == []
    # And it said so, rather than going quiet on a request someone made.
    assert any("wasn't confident" in text for _, text in speaker.said)


def test_considered_is_separate_from_runs_so_a_skip_stays_visible():
    model = ScriptedModel(responses=[answer(request(ASK, intent="rail", conf=0.1))])
    report = run_meeting(
        make_config(),
        model,
        FakeStream([ASK]),
        speaker=NullSpeaker(),
        generate=generator(),
    )
    assert len(report.considered) == 1
    assert report.built == []
    assert "1 request(s), 0 built, 1 skipped" in report.summary()


# -- the run cap ------------------------------------------------------------


def test_the_run_cap_stops_paid_runs_and_says_so():
    model = ScriptedModel(
        responses=[
            answer(
                request(ASK, intent="regulator", conf=0.95),
                request(HUB, intent="usb hub", conf=0.9),
            )
        ]
    )
    generate = generator()
    speaker = NullSpeaker()
    report = run_meeting(
        make_config(max_runs_per_meeting=1),
        model,
        FakeStream([ASK, HUB]),
        speaker=speaker,
        generate=generate,
    )
    assert generate.calls == ["regulator"]
    assert len(report.considered) == 2
    assert [r.built for r in report.runs] == [True, False]
    assert "max_runs_per_meeting=1" in report.runs[1].skipped
    assert any("noted" in text for _, text in speaker.said)


# -- isolation --------------------------------------------------------------


def test_one_failing_request_does_not_abandon_the_others():
    model = ScriptedModel(
        responses=[
            answer(
                request(ASK, intent="regulator", conf=0.95),
                request(HUB, intent="usb hub", conf=0.9),
            )
        ]
    )
    generate = generator(fails={"regulator"})
    report = run_meeting(
        make_config(max_runs_per_meeting=5),
        model,
        FakeStream([ASK, HUB]),
        speaker=NullSpeaker(),
        generate=generate,
    )
    assert generate.calls == ["regulator", "usb hub"]
    assert report.runs[0].error.startswith("ValueError:")
    assert report.runs[1].built is True


def test_a_speaker_failure_does_not_lose_the_board():
    class Broken:
        name = "meeting_sdk"

        def say(self, meeting_id, text):
            raise SpeakError("no_bot", "the container is not running")

    model = ScriptedModel(responses=[answer(request(ASK, intent="regulator"))])
    report = run_meeting(
        make_config(speak_mode="sdk"),
        model,
        FakeStream([ASK]),
        speaker=Broken(),
        generate=generator(),
    )
    assert report.built and report.built[0].result is not None
    assert any("could not say this in the meeting" in w for w in report.warnings)
    # The configured speaker is still named -- that is what was tried -- but
    # nothing reached the meeting, so spoke_via stays "none" and `said` is
    # empty. A reader checking only spoke_via must not see a failed send as
    # speech.
    assert report.speaker_name == "meeting_sdk"
    assert report.spoke_via == "none"
    assert report.said == []


# -- honesty about the speaker ----------------------------------------------


def test_speaker_name_is_the_configured_speaker_read_off_the_object():
    modes = (("off", "null"), ("chat", "zoom_chat"), ("sdk", "meeting_sdk"))
    for mode, expected in modes:
        config = make_config(speak_mode=mode)
        speaker = speaker_for(config, transport=_silent_transport())
        report = run_meeting(
            config,
            ScriptedModel(responses=[answer(request(ASK, intent="regulator"))]),
            FakeStream([ASK]),
            speaker=speaker,
            generate=generator(),
        )
        assert report.speaker_name == expected, mode


def test_spoke_via_names_only_a_speaker_that_delivered():
    """The silent transport is why this test exists.

    It answers ``{}``, so ``ChatSpeaker`` raises "no access_token" -- and the
    older version of this test still asserted ``spoke_via == "zoom_chat"``,
    which is precisely the misreading the field is meant to prevent: a send
    that failed, reported as speech. ``spoke_via`` now moves only after the
    speaker returns.
    """
    config = make_config(speak_mode="chat")
    speaker = speaker_for(config, transport=_silent_transport())
    report = run_meeting(
        config,
        ScriptedModel(responses=[answer(request(ASK, intent="regulator"))]),
        FakeStream([ASK]),
        speaker=speaker,
        generate=generator(),
    )
    assert report.speaker_name == "zoom_chat"
    assert report.spoke_via == "none"
    assert any("could not say this in the meeting" in w for w in report.warnings)

    # And the working case: NullSpeaker never raises, so it did deliver -- to
    # a process, not a room, which is what the name says.
    quiet = run_meeting(
        make_config(speak_mode="off"),
        ScriptedModel(responses=[answer(request(ASK, intent="regulator"))]),
        FakeStream([ASK]),
        speaker=NullSpeaker(),
        generate=generator(),
    )
    assert quiet.spoke_via == "null" and quiet.said


def _silent_transport():
    """A transport that answers every call with an empty JSON object."""

    class T:
        def get(self, url, headers):
            return b"{}"

        def post(self, url, headers, body):
            return b"{}"

    return T()


def test_what_was_said_is_recorded_even_when_the_speaker_is_off():
    speaker = NullSpeaker()
    report = run_meeting(
        make_config(),
        ScriptedModel(responses=[answer(request(ASK, intent="regulator"))]),
        FakeStream([ASK]),
        speaker=speaker,
        generate=generator(),
    )
    assert report.said and report.said == [text for _, text in speaker.said]
    assert "nothing has been ordered" in report.said[0].lower()


# -- nothing to work with ---------------------------------------------------


def test_an_empty_stream_says_rtms_may_be_off_rather_than_finding_nothing():
    model = ScriptedModel(responses=[])
    report = run_meeting(
        make_config(),
        model,
        FakeStream([]),
        speaker=NullSpeaker(),
        generate=generator(),
        meeting_id="uuid-1",
    )
    assert report.considered == []
    assert model.calls == []
    assert any("no transcript arrived" in w for w in report.warnings)


def test_a_stream_that_ends_early_keeps_what_arrived_and_names_the_failure():
    stream = FakeStream([ASK], fail=RuntimeError("socket closed"))
    report = run_meeting(
        make_config(),
        ScriptedModel(responses=[answer(request(ASK, intent="regulator"))]),
        stream,
        speaker=NullSpeaker(),
        generate=generator(),
    )
    assert report.built
    assert any("ended early" in w for w in report.warnings)


def test_a_meeting_outside_the_allowlist_costs_no_model_call():
    model = ScriptedModel(responses=[])
    report = run_meeting(
        make_config(meeting_allowlist=("uuid-other",)),
        model,
        FakeStream([ASK]),
        speaker=NullSpeaker(),
        generate=generator(),
    )
    assert model.calls == []
    assert any("not in ZOOM_MEETINGS" in w for w in report.warnings)


def test_an_unquotable_answer_is_this_meetings_problem_not_a_crash():
    invented = answer(request("a line nobody said here", intent="x"))
    model = ScriptedModel(responses=[invented])
    speaker = NullSpeaker()
    report = run_meeting(
        make_config(), model, FakeStream([ASK]), speaker=speaker, generate=generator()
    )
    assert report.considered == []
    assert any("could not read a request" in w for w in report.warnings)
    assert any("haven't built anything" in text for _, text in speaker.said)


# -- run memory -------------------------------------------------------------


def test_a_meeting_with_no_remembered_run_says_so():
    runner = ZoomRunner(make_config(), model_factory=ScriptedModel)
    assert runner.report_for("uuid-1") is None
    assert runner.summary_for("uuid-1") == NO_RUN_REMEMBERED


def test_the_store_is_bounded_and_keyed_by_meeting():
    store = RunStore(limit=2)
    for i in range(3):
        store.put(f"m{i}", ZoomReport(meeting_id=f"m{i}", finished_at=float(i)))
    assert store.get("m0") is None
    assert store.get("m2") is not None


def test_the_meeting_id_comes_from_the_event_payload():
    payload = {"payload": {"object": {"meeting_uuid": "uuid-9", "id": "123"}}}
    assert meeting_id_of(payload) == "uuid-9"
    assert meeting_id_of({}) == ""


def test_a_model_that_cannot_be_built_is_a_warning_not_a_crash():
    def explode():
        raise RuntimeError("GOOGLE_API_KEY is not set")

    runner = ZoomRunner(make_config(), model_factory=explode)
    report = runner.handle_rtms_started(
        {"payload": {"object": {"meeting_uuid": "uuid-1"}}}
    )
    assert report.considered == []
    assert any("could not build the model" in w for w in report.warnings)
    # And it is remembered, so the surface can say what happened.
    assert runner.report_for("uuid-1") is report


# -- nothing is ever ordered ------------------------------------------------


def test_no_ordering_path_exists_in_this_package():
    """Enforced by inspection, the way ``slackbot/tests/test_order.py`` is.

    A meeting is the weakest possible authorisation for spending money, so the
    absence of an ordering path is a property of the package rather than a
    habit of its callers.
    """
    import zoombot

    for path in Path(zoombot.__file__).resolve().parent.glob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert not [line for line in lines if "prepare_order" in line], path.name
        imports = [
            line.strip()
            for line in lines
            if line.strip().startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "order" in line], path.name
        assert not [line for line in imports if "slackbot" in line], path.name

"""The runner's gates: the floor, the cap, failures, and honest speech.

Offline throughout: a :class:`ScriptedModel`, a recorded transcript source, a
recorded speaker, and a ``generate`` stub in place of the pipeline. Nothing
here imports the solver and nothing opens a socket.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from silkscreen.agents.model import ScriptedModel

from teamsbot.agent import WindowPolicy
from teamsbot.graph import Meeting, NoTranscriptError
from teamsbot.runner import (
    DEFAULT_CONFIDENCE_FLOOR,
    NO_ORDER_NOTE,
    TeamsReport,
    handle_incoming,
    run_meeting,
    run_transcript,
)

MEETING = "19:meeting_abc@thread.v2"
REGULATOR = "we need a little 3.3 volt regulator board for the sensor rig"
BLINKER = "please design a 555 blinker board with a barrel jack input"
CAN_BOARD = "and a CAN transceiver breakout would help the bench setup"


# --- offline stand-ins ----------------------------------------------------


@dataclass(frozen=True)
class FakeChunk:
    meeting_id: str
    speaker: str
    text: str
    at_ms: int


@dataclass(frozen=True)
class StubConfig:
    """The half of ``teamsbot.config.Config`` the runner reads.

    Written locally so this suite passes with or without ``config.py`` on
    disk; the real class is exercised by ``test_teams_config.py`` and by the
    integration test at the bottom of this file.
    """

    max_runs_per_meeting: int = 2
    meeting_allowlist: tuple[str, ...] = ()

    def allows(self, meeting_id: str) -> bool:
        return not self.meeting_allowlist or meeting_id in self.meeting_allowlist


@dataclass
class RecordingSpeaker:
    """A speaker that records instead of talking, per the frozen Protocol."""

    name: str = "chat"
    said: list[tuple[str, str]] = field(default_factory=list)
    fail_with: Exception | None = None

    def say(self, meeting_id: str, text: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.said.append((meeting_id, text))


@dataclass
class RecordedTranscripts:
    """The ``transcript_text`` half of ``graph.GraphClient``, from a script.

    Same signature as the real one, ``(user_id, meeting)``, so a mismatch with
    :class:`teamsbot.graph.GraphClient` shows up here rather than in a tenant.
    """

    texts: dict[str, str] = field(default_factory=dict)
    raises: dict[str, Exception] = field(default_factory=dict)
    asked: list[tuple[str, str]] = field(default_factory=list)

    def transcript_text(self, user_id: str, meeting) -> str:
        meeting_id = getattr(meeting, "id", meeting)
        self.asked.append((user_id, meeting_id))
        if meeting_id in self.raises:
            raise self.raises[meeting_id]
        return self.texts[meeting_id]


ORGANISER = "amira@example.com"


def ended(meeting_id: str = MEETING) -> Meeting:
    """A real ``graph.Meeting`` that finished an hour ago."""
    return Meeting(id=meeting_id, end_time="2026-09-06T09:00:00Z")


def ongoing(meeting_id: str = MEETING) -> Meeting:
    """A real ``graph.Meeting`` that is still running."""
    return Meeting(id=meeting_id, end_time="2026-09-06T23:00:00Z")


NOW = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def answer(*requests: dict) -> str:
    return json.dumps({"requests": list(requests)})


def request(intent: str, quote: str, confidence: float = 0.9) -> dict:
    return {"intent": intent, "quote": quote, "speaker": "amira",
            "confidence": confidence}


def chunks(*pairs: tuple[str, int]) -> list[FakeChunk]:
    return [
        FakeChunk(meeting_id=MEETING, speaker="amira", text=text, at_ms=at)
        for text, at in pairs
    ]


def recording_generate(results=None):
    """A stand-in pipeline that records its intents and returns a truthy result."""
    calls: list[str] = []

    def generate(model, intent, **kwargs):
        calls.append(intent)
        if results is not None:
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return {"intent": intent}

    generate.calls = calls
    return generate


TIGHT = WindowPolicy(span_ms=10_000, overlap_ms=1_000, silence_ms=30_000,
                     max_chars=2_000)


# --- the live path --------------------------------------------------------


def test_a_verified_request_is_drafted_and_reported():
    model = ScriptedModel(
        by_marker={"TRANSCRIPT": answer(request("A 3.3V regulator board", REGULATOR))}
    )
    generate = recording_generate()
    speaker = RecordingSpeaker(name="chat")
    report = run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0)),
        speaker=speaker, generate=generate, policy=TIGHT,
    )
    assert generate.calls == ["A 3.3V regulator board"]
    assert len(report.built) == 1
    assert report.meeting_id == MEETING  # taken from the chunks themselves
    assert report.spoke_via == "chat"
    assert speaker.said and speaker.said[0][0] == MEETING


def test_a_quote_the_meeting_does_not_contain_is_dropped_before_any_run():
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("A 3.3V regulator board", REGULATOR),
                request("A CAN transceiver board", "nobody uttered this sentence"),
            )
        }
    )
    generate = recording_generate()
    report = run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0)),
        generate=generate, policy=TIGHT,
    )
    assert generate.calls == ["A 3.3V regulator board"]
    assert [r.intent for r in report.considered] == ["A 3.3V regulator board"]


def test_below_the_floor_is_recorded_but_never_built():
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("A 3.3V regulator board", REGULATOR, confidence=0.2)
            )
        }
    )
    generate = recording_generate()
    report = run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0)),
        generate=generate, policy=TIGHT,
    )
    assert generate.calls == []
    # Recorded, visibly: considered holds it and the run says why.
    assert len(report.considered) == 1
    assert len(report.runs) == 1
    assert not report.runs[0].built
    assert "below the" in report.runs[0].skipped
    assert f"{DEFAULT_CONFIDENCE_FLOOR:.2f}" in report.runs[0].skipped
    assert "1 built" not in report.summary()


def test_the_run_cap_stops_at_the_configured_number_and_says_so():
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("Board one", REGULATOR, confidence=0.95),
                request("Board two", BLINKER, confidence=0.9),
                request("Board three", CAN_BOARD, confidence=0.85),
            )
        }
    )
    generate = recording_generate()
    report = run_meeting(
        StubConfig(max_runs_per_meeting=2),
        model,
        chunks((REGULATOR, 0), (BLINKER, 100), (CAN_BOARD, 200)),
        generate=generate,
        max_requests=3,
        policy=TIGHT,
    )
    assert generate.calls == ["Board one", "Board two"]
    assert len(report.built) == 2
    capped = [r for r in report.runs if "max_runs_per_meeting" in r.skipped]
    assert len(capped) == 1
    assert capped[0].request.intent == "Board three"
    # The capped request is still visible as considered -- not silently trimmed.
    assert len(report.considered) == 3


def test_one_failing_request_does_not_abandon_the_others():
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("Board one", REGULATOR, confidence=0.95),
                request("Board two", BLINKER, confidence=0.9),
            )
        }
    )
    generate = recording_generate(
        results=[ValueError("unsupported package"), {"ok": True}]
    )
    report = run_meeting(
        StubConfig(max_runs_per_meeting=3),
        model,
        chunks((REGULATOR, 0), (BLINKER, 100)),
        generate=generate,
        max_requests=2,
        policy=TIGHT,
    )
    assert generate.calls == ["Board one", "Board two"]
    assert len(report.built) == 1
    failed = [r for r in report.runs if r.error]
    assert len(failed) == 1
    assert failed[0].error == "ValueError: unsupported package"


def test_an_empty_stream_is_a_named_warning_not_a_silent_success():
    report = run_meeting(
        StubConfig(), ScriptedModel(), [], meeting_id=MEETING,
        generate=recording_generate(),
    )
    assert report.runs == []
    assert report.warnings
    assert "no transcript fragments" in report.warnings[0]


def test_a_meeting_outside_the_allowlist_is_never_read():
    model = ScriptedModel()  # would raise ModelError if it were called
    report = run_meeting(
        StubConfig(meeting_allowlist=("19:other",)),
        model,
        chunks((REGULATOR, 0)),
        generate=recording_generate(),
    )
    assert model.calls == []
    assert "not in TEAMS_MEETINGS" in report.warnings[0]


def test_an_unreadable_model_answer_is_one_meeting_s_problem():
    model = ScriptedModel(by_marker={"TRANSCRIPT": "sorry, I can't do that"})
    report = run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0)),
        generate=recording_generate(), policy=TIGHT,
    )
    assert report.considered == []
    assert any("could not read" in w for w in report.warnings)


# --- spoke_via ------------------------------------------------------------


def test_spoke_via_names_the_speaker_that_actually_spoke():
    model = ScriptedModel(
        by_marker={"TRANSCRIPT": answer(request("A regulator board", REGULATOR))}
    )
    for name in ("chat", "sdk", "null"):
        speaker = RecordingSpeaker(name=name)
        report = run_meeting(
            StubConfig(), ScriptedModel(by_marker=dict(model.by_marker)),
            chunks((REGULATOR, 0)),
            speaker=speaker, generate=recording_generate(), policy=TIGHT,
        )
        assert report.spoke_via == name


def test_a_speaker_that_failed_never_claims_to_have_spoken():
    model = ScriptedModel(
        by_marker={"TRANSCRIPT": answer(request("A regulator board", REGULATOR))}
    )
    speaker = RecordingSpeaker(name="chat", fail_with=RuntimeError("403 forbidden"))
    report = run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0)),
        speaker=speaker, generate=recording_generate(), policy=TIGHT,
    )
    assert report.spoke_via == "none"
    assert any("chat speaker failed" in w for w in report.warnings)
    assert len(report.built) == 1  # the board still exists


def test_no_speaker_is_stated_rather_than_implied():
    model = ScriptedModel(
        by_marker={"TRANSCRIPT": answer(request("A regulator board", REGULATOR))}
    )
    report = run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0)),
        generate=recording_generate(), policy=TIGHT,
    )
    assert report.spoke_via == "none"
    assert any("no speaker configured" in w for w in report.warnings)


def test_what_is_spoken_names_the_requests_that_were_not_built():
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("Board one", REGULATOR, confidence=0.95),
                request("Board two", BLINKER, confidence=0.1),
            )
        }
    )
    speaker = RecordingSpeaker()
    run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0), (BLINKER, 100)),
        speaker=speaker, generate=recording_generate(), max_requests=2,
        policy=TIGHT,
    )
    spoken = speaker.said[0][1]
    assert "Board one" in spoken
    assert "Board two" in spoken  # the skipped one is audible too
    assert "Nothing has been ordered." in spoken


def test_a_meeting_with_no_request_says_nothing_into_the_room():
    model = ScriptedModel(by_marker={"TRANSCRIPT": json.dumps({"requests": []})})
    speaker = RecordingSpeaker()
    report = run_meeting(
        StubConfig(), model, chunks(("we should get lunch at some point", 0)),
        speaker=speaker, generate=recording_generate(), policy=TIGHT,
    )
    assert speaker.said == []
    assert report.spoke_via == "none"
    assert "no board request" in report.summary()


# --- the post-hoc path ----------------------------------------------------


def test_an_in_progress_meeting_is_skipped_before_anything_is_read():
    client = RecordedTranscripts(texts={MEETING: f"amira: {REGULATOR}"})
    model = ScriptedModel()
    report = run_transcript(
        StubConfig(), model, client, ongoing(),
        user_id=ORGANISER, generate=recording_generate(), now=NOW,
    )
    assert client.asked == []
    assert model.calls == []
    assert "still in progress" in report.warnings[0]


def test_a_meeting_with_no_end_time_at_all_counts_as_still_running():
    # graph.Meeting.ongoing_at treats an unknown end as ongoing; the runner
    # must not second-guess it, because the other direction spends money on
    # half a meeting.
    client = RecordedTranscripts(texts={MEETING: f"amira: {REGULATOR}"})
    report = run_transcript(
        StubConfig(), ScriptedModel(), client, Meeting(id=MEETING),
        user_id=ORGANISER, generate=recording_generate(), now=NOW,
    )
    assert client.asked == []
    assert "still in progress" in report.warnings[0]


def test_a_transcript_that_does_not_exist_is_reported_as_such():
    client = RecordedTranscripts(
        raises={
            MEETING: NoTranscriptError(
                f"meeting {MEETING} has no transcript resource"
            )
        }
    )
    report = run_transcript(
        StubConfig(), ScriptedModel(), client, ended(),
        user_id=ORGANISER, generate=recording_generate(), now=NOW,
    )
    assert report.considered == []
    assert report.runs == []
    # Distinct from an empty meeting, and the failure names itself.
    assert "no transcript could be read" in report.warnings[0]
    assert "NoTranscriptError" in report.warnings[0]
    assert "no transcript resource" in report.warnings[0]


def test_an_empty_transcript_is_distinguished_from_a_missing_one():
    client = RecordedTranscripts(texts={MEETING: "   \n  "})
    report = run_transcript(
        StubConfig(), ScriptedModel(), client, ended(),
        user_id=ORGANISER, generate=recording_generate(), now=NOW,
    )
    assert "no text in it" in report.warnings[0]
    assert "no transcript could be read" not in report.warnings[0]


def test_the_post_hoc_path_builds_what_the_transcript_supports():
    client = RecordedTranscripts(
        texts={MEETING: f"amira: {REGULATOR}\nbo: sounds good"}
    )
    model = ScriptedModel(
        responses=[answer(request("A 3.3V regulator board", REGULATOR))]
    )
    generate = recording_generate()
    speaker = RecordingSpeaker(name="chat")
    report = run_transcript(
        StubConfig(), model, client, ended(),
        user_id=ORGANISER, speaker=speaker, generate=generate, now=NOW,
    )
    assert client.asked == [(ORGANISER, MEETING)]
    assert generate.calls == ["A 3.3V regulator board"]
    assert report.transcript_chars > 0
    assert report.spoke_via == "chat"


def test_the_post_hoc_path_honours_the_floor_and_the_cap_too():
    client = RecordedTranscripts(
        texts={MEETING: f"amira: {REGULATOR}\namira: {BLINKER}\namira: {CAN_BOARD}"}
    )
    model = ScriptedModel(
        responses=[
            answer(
                request("Board one", REGULATOR, confidence=0.95),
                request("Board two", BLINKER, confidence=0.9),
                request("Board three", CAN_BOARD, confidence=0.05),
            )
        ]
    )
    generate = recording_generate()
    report = run_transcript(
        StubConfig(max_runs_per_meeting=1), model, client, ended(),
        user_id=ORGANISER, generate=generate, max_requests=3, now=NOW,
    )
    assert generate.calls == ["Board one"]
    assert len(report.considered) == 3
    reasons = [r.skipped for r in report.runs if not r.built]
    assert any("max_runs_per_meeting" in reason for reason in reasons)
    assert any("below the" in reason for reason in reasons)


# --- the standing rule ----------------------------------------------------


def test_nothing_in_this_package_can_order_a_board():
    """No ordering module is reachable from the runner, by import.

    ``slackbot/tests/test_order.py`` enforces the same property the same way:
    a rule about money is worth an import-level check, not a comment.
    """
    source = Path(__file__).resolve().parents[1] / "runner.py"
    tree = ast.parse(source.read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    assert not any("order" in name for name in imported), imported
    assert "order" not in {f.__name__ for f in ()}


def test_the_report_is_honest_when_it_has_nothing():
    report = TeamsReport(meeting_id=MEETING)
    assert "no board request" in report.summary()
    assert report.spoke_via == "none"


def test_the_real_config_drives_the_runner_when_it_exists():
    config_module = pytest.importorskip("teamsbot.config")
    load_config = getattr(config_module, "load_config", None)
    if load_config is None:  # pragma: no cover - config.py names it differently
        pytest.skip("teamsbot.config has no load_config")
    config = load_config(
        {
            "TEAMS_APP_ID": "app-id",
            "TEAMS_APP_SECRET": "app-secret",
            "TEAMS_TENANT_ID": "tenant-id",
            "TEAMS_BOT_ENDPOINT": "https://bot.example.com/api/calls",
            "TEAMS_MAX_RUNS_PER_MEETING": "1",
        }
    )
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("Board one", REGULATOR, confidence=0.95),
                request("Board two", BLINKER, confidence=0.9),
            )
        }
    )
    generate = recording_generate()
    report = run_meeting(
        config, model, chunks((REGULATOR, 0), (BLINKER, 100)),
        generate=generate, max_requests=2, policy=TIGHT,
    )
    assert generate.calls == ["Board one"]
    assert len(report.considered) == 2


# --- handle_incoming: what the dispatcher actually calls -------------------


def incoming(kind="message", text="", **kw):
    """One ``teamsbot.app.Incoming``, built with the real class."""
    from teamsbot.app import Incoming

    return Incoming(
        kind=kind,
        notification_id=kw.pop("notification_id", "n1"),
        meeting_id=kw.pop("meeting_id", MEETING),
        conversation_id=kw.pop("conversation_id", "19:conv"),
        text=text,
        sender=kw.pop("sender", "amira"),
        **kw,
    )


def test_a_chat_message_is_read_through_the_same_quote_gate():
    model = ScriptedModel(
        by_marker={"TRANSCRIPT": answer(request("A 3.3V regulator board", REGULATOR))}
    )
    generate = recording_generate()
    speaker = RecordingSpeaker(name="chat")
    report = handle_incoming(
        StubConfig(), incoming(text=REGULATOR),
        speaker=speaker, model=model, generate=generate,
    )
    assert generate.calls == ["A 3.3V regulator board"]
    assert report.spoke_via == "chat"
    # Said into the conversation, not the meeting id: ChatSpeaker addresses a
    # chat, and a meeting id is not a chat id.
    assert speaker.said[0][0] == "19:conv"


def test_a_message_the_model_cannot_quote_builds_nothing():
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("A board nobody asked for", "a line that was never typed")
            )
        }
    )
    generate = recording_generate()
    report = handle_incoming(
        StubConfig(), incoming(text=REGULATOR), model=model, generate=generate,
    )
    assert generate.calls == []
    assert report.considered == []
    assert any("could not read" in w for w in report.warnings)


def test_asking_to_order_is_refused_out_loud_and_builds_nothing():
    speaker = RecordingSpeaker(name="chat")
    generate = recording_generate()
    model = ScriptedModel()  # would raise if it were consulted
    report = handle_incoming(
        StubConfig(), incoming(text="order the boards please"),
        speaker=speaker, model=model, generate=generate,
    )
    assert generate.calls == []
    assert model.calls == []
    assert report.runs == []
    assert speaker.said[0][1] == NO_ORDER_NOTE
    assert "Nothing is ever ordered" in report.warnings[0]


def test_a_follow_up_says_what_it_can_and_cannot_answer():
    speaker = RecordingSpeaker(name="chat")
    model = ScriptedModel()
    report = handle_incoming(
        StubConfig(), incoming(text="status of the last board"),
        speaker=speaker, model=model,
    )
    assert model.calls == []
    assert report.spoke_via == "chat"
    assert "follow-up 'status'" in report.warnings[0]


def test_a_call_notification_without_a_client_says_so_rather_than_doing_nothing():
    report = handle_incoming(
        StubConfig(), incoming(kind="call", change_type="created"),
        model=ScriptedModel(), generate=recording_generate(),
    )
    assert report.runs == []
    assert "needs a GraphClient" in report.warnings[0]


def test_a_call_notification_with_a_client_reads_the_transcript():
    client = RecordedTranscripts(texts={MEETING: f"amira: {REGULATOR}"})
    model = ScriptedModel(
        responses=[answer(request("A 3.3V regulator board", REGULATOR))]
    )
    generate = recording_generate()
    report = handle_incoming(
        StubConfig(), incoming(kind="call"),
        model=model, client=client, user_id=ORGANISER, generate=generate,
    )
    assert client.asked == [(ORGANISER, MEETING)]
    assert generate.calls == ["A 3.3V regulator board"]
    assert len(report.built) == 1


def test_the_app_s_default_runner_seam_exists_under_the_name_it_imports():
    # teamsbot/app.py does ``from .runner import handle_incoming`` lazily; a
    # rename here would only surface on a live notification.
    import inspect

    from teamsbot import runner

    signature = inspect.signature(runner.handle_incoming)
    assert list(signature.parameters)[:2] == ["config", "incoming"]
    assert "speaker" in signature.parameters


def test_the_real_speakers_satisfy_the_runner_s_protocol():
    from teamsbot.speak import NullSpeaker

    model = ScriptedModel(
        by_marker={"TRANSCRIPT": answer(request("A regulator board", REGULATOR))}
    )
    speaker = NullSpeaker()
    report = run_meeting(
        StubConfig(), model, chunks((REGULATOR, 0)),
        speaker=speaker, generate=recording_generate(), policy=TIGHT,
    )
    # "null", not the runner's "none" sentinel: speaking was switched off,
    # which is a different fact from a delivery that was attempted and lost.
    assert report.spoke_via == "null"
    assert speaker.said and REGULATOR not in speaker.said[0].text

"""Windowing and the inherited extraction gates, entirely offline.

Every model here is a :class:`ScriptedModel`; nothing opens a socket. The
windowing tests use two-second spans rather than the production ninety, which
is the whole reason :class:`WindowPolicy` is a value and not four literals in a
loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from silkscreen.agents.model import ScriptedModel

from meetings.intent import IntentError
from teamsbot.agent import (
    DEFAULT_WINDOW_POLICY,
    WindowPolicy,
    dedupe,
    requests_from_chunks,
    requests_from_transcript,
    transcript_from_chunks,
    windows,
)


@dataclass(frozen=True)
class FakeChunk:
    """Shaped exactly like ``teamsbot.graph.TranscriptChunk``."""

    meeting_id: str
    speaker: str
    text: str
    at_ms: int


def chunk(text: str, at_ms: int, speaker: str = "amira") -> FakeChunk:
    return FakeChunk(meeting_id="19:meeting_abc", speaker=speaker, text=text,
                     at_ms=at_ms)


def answer(*requests: dict) -> str:
    return json.dumps({"requests": list(requests)})


def request(intent: str, quote: str, confidence: float = 0.9,
            speaker: str = "amira") -> dict:
    return {
        "intent": intent,
        "quote": quote,
        "speaker": speaker,
        "confidence": confidence,
    }


REGULATOR = "we need a little 3.3 volt regulator board for the sensor rig"
BLINKER = "please design a 555 blinker board with a barrel jack input"


# --- transcript rendering -------------------------------------------------


def test_consecutive_fragments_from_one_speaker_join_into_one_line():
    text = transcript_from_chunks(
        [
            chunk("okay so for the sensor rig", 0),
            chunk("we need a little regulator board", 1_000),
            chunk("sounds good to me honestly", 2_000, speaker="bo"),
        ]
    )
    assert text == (
        "amira: okay so for the sensor rig we need a little regulator board\n"
        "bo: sounds good to me honestly"
    )


def test_empty_fragments_never_become_blank_lines():
    assert transcript_from_chunks([chunk("   ", 0), chunk("hello there", 10)]) == (
        "amira: hello there"
    )


# --- windowing ------------------------------------------------------------


def test_a_short_burst_is_one_window():
    panes = windows(
        [chunk("one two three", 0), chunk("four five six", 500)],
        policy=WindowPolicy(span_ms=2_000, overlap_ms=500, silence_ms=1_000,
                            max_chars=200),
    )
    assert len(panes) == 1
    assert panes[0].start_ms == 0
    assert panes[0].end_ms == 500


def test_span_closes_a_window_and_the_overlap_carries_the_boundary_forward():
    policy = WindowPolicy(span_ms=2_000, overlap_ms=1_000, silence_ms=5_000,
                          max_chars=500)
    panes = windows(
        [chunk(f"line {n}", n * 1_000) for n in range(6)],
        policy=policy,
    )
    assert len(panes) > 1
    # The point of the overlap: no fragment falls between two windows, and the
    # last fragment of one window reappears in the next.
    seen = {c.at_ms for pane in panes for c in pane.chunks}
    assert seen == {0, 1_000, 2_000, 3_000, 4_000, 5_000}
    assert any(
        panes[0].chunks[-1].at_ms in {c.at_ms for c in panes[1].chunks}
        for _ in [0]
    )


def test_silence_closes_a_window_early():
    policy = WindowPolicy(span_ms=60_000, overlap_ms=1_000, silence_ms=5_000,
                          max_chars=500)
    panes = windows(
        [chunk("first topic entirely", 0), chunk("unrelated second topic", 20_000)],
        policy=policy,
    )
    assert len(panes) == 2
    assert [pane.start_ms for pane in panes] == [0, 20_000]


def test_max_chars_closes_a_window_but_never_drops_a_long_fragment():
    policy = WindowPolicy(span_ms=60_000, overlap_ms=1_000, silence_ms=30_000,
                          max_chars=20)
    long_text = "x" * 200
    panes = windows([chunk(long_text, 0), chunk("short", 100)], policy=policy)
    kept = [c.text for pane in panes for c in pane.chunks]
    assert long_text in kept
    assert "short" in kept


def test_out_of_order_fragments_are_sorted_before_windowing():
    panes = windows(
        [chunk("second", 1_000), chunk("first", 0)],
        policy=WindowPolicy(span_ms=10_000, overlap_ms=100, silence_ms=5_000,
                            max_chars=500),
    )
    assert [c.text for c in panes[0].chunks] == ["first", "second"]


def test_no_fragments_is_no_windows():
    assert windows([]) == []


def test_an_overlap_wider_than_the_span_is_refused_at_construction():
    with pytest.raises(ValueError, match="never advances"):
        WindowPolicy(span_ms=1_000, overlap_ms=1_000)


def test_windowing_terminates_on_a_long_dense_stream():
    # The regression this guards: an overlap that resets the cursor to where it
    # started would loop forever rather than fail a test.
    panes = windows(
        [chunk("talking", n * 100) for n in range(400)],
        policy=WindowPolicy(span_ms=1_000, overlap_ms=900, silence_ms=5_000,
                            max_chars=10_000),
    )
    assert panes
    assert panes[-1].end_ms == 399 * 100


# --- the gates, applied to live fragments ---------------------------------


def test_a_quote_that_is_not_in_the_window_is_dropped():
    model = ScriptedModel(
        responses=[
            answer(
                request("A 3.3V regulator board", REGULATOR),
                request("A CAN transceiver board", "nobody said this sentence here"),
            )
        ]
    )
    found = requests_from_chunks(
        model,
        [chunk(REGULATOR, 0)],
        policy=WindowPolicy(span_ms=10_000, overlap_ms=100, silence_ms=5_000,
                            max_chars=500),
    )
    assert [r.intent for r in found] == ["A 3.3V regulator board"]


SPLIT_CHUNKS = [
    chunk("right, next thing on the list", 0),
    chunk("we need a little 3.3 volt regulator board", 900),
    chunk("for the sensor rig, usb-c in", 1_800),
]
SPLIT_QUOTE = (
    "we need a little 3.3 volt regulator board for the sensor rig, usb-c in"
)


def _split_model() -> ScriptedModel:
    # Answers only for the window that contains the tail of the sentence; any
    # other window is told the meeting asked for nothing.
    return ScriptedModel(
        by_marker={"usb-c": answer(request("A 3.3V regulator board", SPLIT_QUOTE))},
        responses=[json.dumps({"requests": []})] * 4,
    )


def test_a_request_split_across_a_boundary_is_still_quotable():
    # The sentence starts at 900ms and finishes at 1800ms; with a 1s span
    # those two fragments are in different windows unless the overlap carries
    # the first one forward.
    found = requests_from_chunks(
        _split_model(),
        SPLIT_CHUNKS,
        policy=WindowPolicy(span_ms=1_000, overlap_ms=900, silence_ms=5_000,
                            max_chars=500),
    )
    assert [r.intent for r in found] == ["A 3.3V regulator board"]


def test_without_the_overlap_the_same_request_is_lost_at_the_boundary():
    # The negative half of the test above: it is the overlap doing the work,
    # not the extractor being generous. Without it the quote spans two windows
    # and is in neither, so the containment gate drops it -- correctly.
    warnings: list[str] = []
    with pytest.raises(IntentError) as caught:
        requests_from_chunks(
            _split_model(),
            SPLIT_CHUNKS,
            policy=WindowPolicy(span_ms=1_000, overlap_ms=0, silence_ms=5_000,
                                max_chars=500),
            on_warning=warnings.append,
        )
    assert "not in the transcript" in str(caught.value)
    assert any("not in the transcript" in w for w in warnings)


def test_duplicates_from_overlapping_windows_are_collapsed_once():
    policy = WindowPolicy(span_ms=1_000, overlap_ms=900, silence_ms=5_000,
                          max_chars=500)
    chunks = [chunk(REGULATOR, 0), chunk("anyway moving on to the demo", 1_500)]
    # Every window answers with the same request; without dedupe that is two
    # paid pipeline runs for one sentence.
    model = ScriptedModel(
        by_marker={"TRANSCRIPT": answer(request("A 3.3V regulator board", REGULATOR))}
    )
    found = requests_from_chunks(model, chunks, policy=policy)
    assert len(model.calls) > 1
    assert len(found) == 1


def test_dedupe_keeps_the_higher_confidence_copy():
    from meetings.intent import BoardRequest

    low = BoardRequest(intent="a", quote=REGULATOR, confidence=0.4)
    high = BoardRequest(intent="a", quote=REGULATOR.upper(), confidence=0.8)
    assert dedupe([low, high]) == [high]


def test_one_failing_window_does_not_lose_the_other_window():
    policy = WindowPolicy(span_ms=1_000, overlap_ms=0, silence_ms=5_000,
                          max_chars=500)
    chunks = [chunk("nothing relevant was said here", 0), chunk(BLINKER, 5_000)]
    model = ScriptedModel(
        by_marker={
            "nothing relevant": "not json at all, sorry",
            "555 blinker": answer(request("A 555 blinker board", BLINKER)),
        }
    )
    warnings: list[str] = []
    found = requests_from_chunks(
        model, chunks, policy=policy, on_warning=warnings.append
    )
    assert [r.intent for r in found] == ["A 555 blinker board"]
    assert warnings and "could not read window" in warnings[0]


def test_every_window_failing_raises_rather_than_returning_an_empty_list():
    model = ScriptedModel(by_marker={"TRANSCRIPT": "still not json"})
    with pytest.raises(IntentError) as caught:
        requests_from_chunks(model, [chunk(REGULATOR, 0)])
    assert "no request survived" in str(caught.value)


def test_the_cap_is_applied_after_duplicates_are_collapsed():
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": answer(
                request("A 3.3V regulator board", REGULATOR, confidence=0.9),
                request("A 555 blinker board", BLINKER, confidence=0.5),
            )
        }
    )
    found = requests_from_chunks(
        model, [chunk(REGULATOR, 0), chunk(BLINKER, 100)], max_requests=1
    )
    assert [r.intent for r in found] == ["A 3.3V regulator board"]


def test_max_requests_must_be_at_least_one():
    with pytest.raises(ValueError, match="max_requests"):
        requests_from_chunks(ScriptedModel(), [chunk(REGULATOR, 0)], max_requests=0)


def test_the_post_hoc_path_applies_the_same_quote_gate():
    transcript = f"amira: {REGULATOR}"
    model = ScriptedModel(
        responses=[
            answer(
                request("A 3.3V regulator board", REGULATOR),
                request("An invented board", "a sentence nobody ever uttered here"),
            )
        ]
    )
    found = requests_from_transcript(model, transcript)
    assert [r.intent for r in found] == ["A 3.3V regulator board"]


def test_the_default_policy_is_sane():
    assert DEFAULT_WINDOW_POLICY.overlap_ms < DEFAULT_WINDOW_POLICY.span_ms
    assert DEFAULT_WINDOW_POLICY.silence_ms > 0


def test_the_live_chunk_shape_is_defined_here_because_graph_is_post_hoc():
    """``graph.py`` reads finished WebVTT transcripts and yields no chunks.

    So :class:`teamsbot.agent.Chunk` is the only definition of a live fragment
    in this package, and the structural check is what keeps the calling bot's
    payload and this module's windowing from drifting apart.
    """
    import teamsbot.graph as graph
    from teamsbot.agent import Chunk

    assert not hasattr(graph, "TranscriptChunk")
    assert isinstance(chunk(REGULATOR, 0), Chunk)
    # And the runner's own chat-message wrapper is one too.
    from teamsbot.runner import _Utterance

    assert isinstance(
        _Utterance(meeting_id="m", speaker="amira", text=REGULATOR, at_ms=0), Chunk
    )

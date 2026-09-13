"""Windowing a live transcript, and the gates it inherits from ``meetings``.

Offline: the model is a :class:`~silkscreen.agents.model.ScriptedModel` and the
"stream" is a list of chunks. Nothing here opens a socket or needs a key.

The windowing tests drive :func:`~zoombot.agent.window_chunks` directly, with
its thresholds passed in, because the policy is the thing under test -- a check
that only asserted "some requests came back" would pass with every window rule,
including the broken ones.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.model import ScriptedModel

from meetings.intent import IntentError
from zoombot.agent import (
    WINDOW_MIN_CHARS,
    requests_from_chunks,
    window_chunks,
)
from zoombot.rtms import TranscriptChunk


def chunk(text: str, *, at_ms: int = 0, speaker: str = "carol") -> TranscriptChunk:
    return TranscriptChunk(
        meeting_id="uuid-1", speaker=speaker, text=text, at_ms=at_ms
    )


def answer(*requests) -> str:
    return json.dumps({"requests": list(requests)})


def request(quote: str, *, intent: str = "a 3.3V regulator board", conf=0.9) -> dict:
    return {
        "intent": intent,
        "quote": quote,
        "speaker": "carol",
        "confidence": conf,
    }


# The line every test quotes from. Long enough to clear the 12-character
# minimum ``meetings.intent`` imposes on a quote.
ASK = "we need a little 3.3 volt regulator board for the sensor rig"


# -- windowing --------------------------------------------------------------


def test_a_long_pause_ends_a_window():
    chunks = [
        chunk("first thing", at_ms=0),
        chunk("still the first thing", at_ms=2_000),
        chunk("new topic entirely", at_ms=90_000),
    ]
    windows = window_chunks(chunks, gap_ms=15_000)
    assert [len(w) for w in windows] == [2, 1]
    assert windows[1].chunks[0].text == "new topic entirely"


def test_a_pause_does_not_carry_the_previous_topic_forward():
    """Overlap exists for split sentences, not for unrelated agenda items."""
    chunks = [chunk("about the enclosure", at_ms=0), chunk("anyway", at_ms=99_000)]
    windows = window_chunks(chunks, gap_ms=15_000, overlap=2)
    assert [c.text for c in windows[1].chunks] == ["anyway"]


def test_a_window_is_capped_in_characters_even_with_no_pause():
    chunks = [chunk("x" * 30, at_ms=i * 100) for i in range(5)]
    windows = window_chunks(chunks, max_chars=70, overlap=0)
    assert len(windows) > 1
    for window in windows:
        assert len(window.text) <= 70 + len("carol: ") * len(window)


def test_the_tail_of_a_window_is_repeated_at_the_head_of_the_next():
    """A requirement split across a boundary must stay quotable from one side."""
    chunks = [chunk(f"line {i}", at_ms=i * 100) for i in range(6)]
    windows = window_chunks(chunks, max_chars=20, overlap=2)
    assert len(windows) > 1
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert later.chunks[0] in earlier.chunks


def test_empty_frames_do_not_count_as_speech_or_as_time():
    """RTMS keep-alives carry timing, not words; they must not split a window."""
    chunks = [
        chunk("first half of the sentence", at_ms=0),
        chunk("   ", at_ms=1_000),
        chunk("second half of the sentence", at_ms=2_000),
    ]
    windows = window_chunks(chunks, gap_ms=15_000)
    assert len(windows) == 1
    assert "   " not in windows[0].text


def test_a_window_renders_speaker_labelled_lines():
    windows = window_chunks([chunk("hello", speaker="dev")])
    assert windows[0].text == "dev: hello"
    assert windows[0].meeting_id == "uuid-1"


def test_windowing_is_deterministic():
    chunks = [chunk(f"utterance {i}", at_ms=i * 9_000) for i in range(8)]
    first = window_chunks(chunks)
    again = window_chunks(chunks)
    assert [[c.text for c in w.chunks] for w in first] == [
        [c.text for c in w.chunks] for w in again
    ]


# -- the gates --------------------------------------------------------------


def test_a_quote_that_is_not_in_the_transcript_is_dropped():
    """The hallucination filter, inherited whole from ``meetings.intent``."""
    model = ScriptedModel(
        responses=[
            answer(
                request(ASK),
                request(
                    "and a four layer impedance controlled backplane",
                    intent="a backplane nobody asked for",
                    conf=0.99,
                ),
            )
        ]
    )
    found = requests_from_chunks(model, [chunk(ASK)])
    assert [r.intent for r in found] == ["a 3.3V regulator board"]


def test_a_window_too_short_to_hold_a_quote_is_never_sent():
    """Each window is a paid call, and a five-word one cannot produce a quote."""
    model = ScriptedModel(responses=[])
    assert requests_from_chunks(model, [chunk("hi")]) == []
    assert model.calls == []


def test_a_short_stream_says_why_nothing_was_asked():
    warnings: list[str] = []
    requests_from_chunks(ScriptedModel(), [chunk("hi")], warnings=warnings)
    assert warnings and str(WINDOW_MIN_CHARS) in warnings[0]


def test_one_sentence_seen_in_two_windows_is_one_request():
    """The overlap makes duplicates certain; two runs for one ask is the bug."""
    chunks = [
        chunk("we should talk about the sensor rig power supply today", at_ms=0),
        chunk(ASK, at_ms=1_000),
        chunk("that would unblock the environmental test rig next week", at_ms=2_000),
    ]
    windows = window_chunks(chunks, max_chars=70, overlap=2)
    assert len(windows) > 1
    model = ScriptedModel(responses=[answer(request(ASK))] * len(windows))
    found = requests_from_chunks(model, chunks)
    assert len(found) == 1


def test_requests_come_back_highest_confidence_first_and_capped():
    hub = "and we also need a small usb hub board for the bench"
    chunks = [chunk(ASK, at_ms=0), chunk(hub, at_ms=500)]
    model = ScriptedModel(
        responses=[
            answer(
                request(ASK, intent="regulator", conf=0.4),
                request(hub, intent="usb hub", conf=0.95),
            )
        ]
    )
    found = requests_from_chunks(model, chunks, max_requests=1)
    assert [r.intent for r in found] == ["usb hub"]


def test_every_window_failing_raises_rather_than_returning_nothing():
    """"No requests" must never be readable as "the meeting asked for nothing"."""
    model = ScriptedModel(responses=["not json at all"])
    with pytest.raises(IntentError):
        requests_from_chunks(model, [chunk(ASK)])


def test_one_bad_window_does_not_lose_the_good_ones():
    chunks = [
        chunk(ASK, at_ms=0),
        chunk("completely separate later topic about the enclosure", at_ms=99_000),
    ]
    windows = window_chunks(chunks)
    assert len(windows) == 2
    model = ScriptedModel(responses=[answer(request(ASK)), "{not json"])
    warnings: list[str] = []
    found = requests_from_chunks(model, chunks, warnings=warnings)
    assert [r.intent for r in found] == ["a 3.3V regulator board"]
    assert warnings and "window 1" in warnings[0]


def test_max_requests_must_be_at_least_one():
    with pytest.raises(ValueError):
        requests_from_chunks(ScriptedModel(), [chunk(ASK)], max_requests=0)

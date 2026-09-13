"""Intent extraction, driven by a scripted model.

The bug class this file guards is the one that costs money and looks like
success: a model reads a meeting, invents a hardware requirement nobody stated,
and the pipeline builds a board for it. There is no human between the
transcript and the run, so the transcript itself is the only authority -- every
test here is ultimately asking whether a claim survived being checked against
it.

The second bug class is the quiet empty list. ``[]`` from
:func:`extract_requests` has to mean "the meeting asked for no boards", never
"the model returned garbage and we shrugged", so the failure paths are tested
as carefully as the happy one.

No network and no API key: :class:`ScriptedModel` is the same offline seam
``engine/tests/test_agents.py`` uses.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from silkscreen.agents.model import ModelError, ScriptedModel


def _load_intent():
    """Import ``meetings.intent`` without depending on its siblings.

    ``meetings/__init__.py`` re-exports the whole package, so importing it
    fails while any one sibling module is still being written. These tests
    cover this module alone and must not be hostage to that.
    """
    try:
        return importlib.import_module("meetings.intent")
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "intent.py"
        spec = importlib.util.spec_from_file_location("_meetings_intent", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


intent_module = _load_intent()
BoardRequest = intent_module.BoardRequest
IntentError = intent_module.IntentError
extract_requests = intent_module.extract_requests
MAX_TRANSCRIPT_CHARS = intent_module.MAX_TRANSCRIPT_CHARS


TRANSCRIPT = "\n".join(
    [
        "alice: morning everyone, quick one today",
        "bob: we need a small 3.3V regulator board for the sensor rig",
        "alice: how many layers were you thinking",
        "carol: can someone design a USB-C power delivery breakout for the lab",
        "bob: two layers is fine, nothing exotic",
        "alice: great, that is everything",
    ]
)

REGULATOR_QUOTE = "bob: we need a small 3.3V regulator board for the sensor rig"
USB_QUOTE = "carol: can someone design a USB-C power delivery breakout for the lab"


def payload(*requests: dict) -> str:
    return json.dumps({"requests": list(requests)})


def request_json(
    intent: str,
    quote: str,
    confidence: float = 0.9,
    speaker: str | None = "bob",
) -> dict:
    item = {"intent": intent, "quote": quote, "confidence": confidence}
    if speaker is not None:
        item["speaker"] = speaker
    return item


def scripted(response: str) -> ScriptedModel:
    return ScriptedModel(responses=[response])


# ---------------------------------------------------------------- happy path


def test_a_stated_board_request_comes_back_with_the_line_that_stated_it():
    model = scripted(
        payload(
            request_json("A 3.3V LDO regulator board for a sensor rig", REGULATOR_QUOTE)
        )
    )

    requests = extract_requests(model, TRANSCRIPT)

    assert len(requests) == 1
    assert isinstance(requests[0], BoardRequest)
    assert requests[0].intent == "A 3.3V LDO regulator board for a sensor rig"
    assert requests[0].quote == REGULATOR_QUOTE
    assert requests[0].confidence == pytest.approx(0.9)
    assert requests[0].speaker == "bob"


def test_the_whole_transcript_is_put_in_front_of_the_model():
    model = scripted(payload())

    extract_requests(model, TRANSCRIPT)

    assert len(model.calls) == 1
    assert TRANSCRIPT in model.calls[0]["prompt"]


def test_a_meeting_with_no_hardware_request_returns_an_empty_list():
    # The one legitimate empty result: the model read the meeting and found
    # nothing. It must not raise, because nothing went wrong.
    model = scripted(payload())

    assert extract_requests(model, "alice: nothing to report today") == []


def test_json_wrapped_in_a_markdown_fence_is_still_read():
    fenced = "```json\n" + payload(
        request_json("A 3.3V regulator board", REGULATOR_QUOTE)
    ) + "\n```"

    requests = extract_requests(scripted(fenced), TRANSCRIPT)

    assert [r.quote for r in requests] == [REGULATOR_QUOTE]


def test_a_missing_speaker_is_recovered_from_the_matched_transcript_line():
    # Meet's speaker labels are opaque participant ids; taking one from the
    # line we already matched beats asking the model to reproduce it.
    model = scripted(
        payload(
            request_json(
                "A USB-C PD breakout", USB_QUOTE, confidence=0.8, speaker=None
            )
        )
    )

    assert extract_requests(model, TRANSCRIPT)[0].speaker == "carol"


# -------------------------------------------------------- hallucination filter


def test_a_quote_that_is_not_in_the_transcript_is_dropped():
    # The single most important guarantee in this module. The second request
    # is a fluent, plausible sentence that nobody said.
    model = scripted(
        payload(
            request_json("A 3.3V regulator board", REGULATOR_QUOTE, confidence=0.9),
            request_json(
                "A CAN bus gateway board",
                "dave: and we should do a CAN bus gateway while we are at it",
                confidence=0.95,
                speaker="dave",
            ),
        )
    )

    requests = extract_requests(model, TRANSCRIPT)

    assert [r.quote for r in requests] == [REGULATOR_QUOTE]


def test_a_quote_differing_only_in_whitespace_is_still_accepted():
    # Line rewrapping is not fabrication; rejecting it would train the caller
    # to loosen the check that matters.
    rewrapped = "bob: we need a small 3.3V\n  regulator board for the sensor rig"
    model = scripted(payload(request_json("A 3.3V regulator board", rewrapped)))

    assert len(extract_requests(model, TRANSCRIPT)) == 1


def test_a_response_whose_every_quote_is_invented_raises_instead_of_returning_empty():
    # Returning [] here would read as "this meeting asked for nothing", which
    # is the opposite of what the model claimed.
    model = scripted(
        payload(
            request_json(
                "A CAN bus gateway board",
                "dave: we should do a CAN bus gateway board",
                speaker="dave",
            ),
            request_json(
                "A motor driver board",
                "erin: let us also build a motor driver board this quarter",
                speaker="erin",
            ),
        )
    )

    with pytest.raises(IntentError) as excinfo:
        extract_requests(model, TRANSCRIPT)

    assert "unverifiable" in str(excinfo.value)
    assert len(excinfo.value.errors) == 3  # the summary plus one per drop


def test_a_quote_too_short_to_identify_a_line_is_a_validation_error():
    # "the board" appears in almost any transcript; containment would prove
    # nothing about it, so it is refused rather than trusted.
    model = scripted(payload(request_json("A board", "board")))

    with pytest.raises(IntentError, match="too short"):
        extract_requests(model, TRANSCRIPT)


# ------------------------------------------------------------ malformed output


def test_output_that_is_not_json_raises_with_the_text_that_was_returned():
    model = scripted("I'm sorry, I could not find any board requests.")

    with pytest.raises(IntentError) as excinfo:
        extract_requests(model, TRANSCRIPT)

    message = str(excinfo.value)
    assert "not valid JSON" in message
    assert "I'm sorry" in message  # the actual response, not just a verdict


def test_a_json_array_at_the_top_level_is_rejected_by_name():
    model = scripted(json.dumps([{"intent": "x", "quote": REGULATOR_QUOTE}]))

    with pytest.raises(IntentError, match="got list"):
        extract_requests(model, TRANSCRIPT)


def test_a_json_string_at_the_top_level_is_rejected_by_name():
    model = scripted(json.dumps("no requests"))

    with pytest.raises(IntentError, match="got str"):
        extract_requests(model, TRANSCRIPT)


def test_a_response_missing_the_requests_key_is_not_read_as_an_empty_meeting():
    model = scripted(json.dumps({"board_requests": []}))

    with pytest.raises(IntentError, match="no 'requests' key"):
        extract_requests(model, TRANSCRIPT)


def test_a_request_that_is_not_an_object_is_reported_by_index():
    model = scripted(json.dumps({"requests": ["a 3.3V regulator board"]}))

    with pytest.raises(IntentError, match=r"requests\[0\]"):
        extract_requests(model, TRANSCRIPT)


def test_every_validation_failure_is_collected_into_one_error():
    # One round trip per broken response, not one per field -- the same
    # contract the netlist validator keeps.
    model = scripted(
        json.dumps(
            {
                "requests": [
                    {"quote": REGULATOR_QUOTE, "confidence": 0.9},
                    {"intent": "A USB-C breakout", "confidence": 0.4},
                    {"intent": "A CAN board", "quote": USB_QUOTE},
                ]
            }
        )
    )

    with pytest.raises(IntentError) as excinfo:
        extract_requests(model, TRANSCRIPT)

    assert len(excinfo.value.errors) == 3
    joined = "\n".join(excinfo.value.errors)
    assert "'intent'" in joined and "'quote'" in joined and "'confidence'" in joined


# ------------------------------------------------------------------ confidence


@pytest.mark.parametrize("value", [1.4, -0.2, 42])
def test_a_confidence_outside_zero_to_one_is_a_validation_error(value):
    model = scripted(
        payload(request_json("A 3.3V regulator board", REGULATOR_QUOTE, value))
    )

    with pytest.raises(IntentError, match="between 0 and 1"):
        extract_requests(model, TRANSCRIPT)


@pytest.mark.parametrize("value", ["0.9", None, True])
def test_a_confidence_that_is_not_a_number_is_never_silently_zero(value):
    # Defaulting is what makes a guess look like a fact (or hides a real
    # request behind the ranking). Both defaults are wrong, so neither is used.
    item = {"intent": "A board", "quote": REGULATOR_QUOTE, "confidence": value}
    model = scripted(json.dumps({"requests": [item]}))

    with pytest.raises(IntentError, match="confidence"):
        extract_requests(model, TRANSCRIPT)


def test_a_missing_confidence_is_a_validation_error():
    model = scripted(
        json.dumps({"requests": [{"intent": "A board", "quote": REGULATOR_QUOTE}]})
    )

    with pytest.raises(IntentError, match="confidence"):
        extract_requests(model, TRANSCRIPT)


# ------------------------------------------------------------------ truncation


def test_more_requests_than_the_cap_keeps_the_most_confident_ones():
    model = scripted(
        payload(
            request_json("A 3.3V regulator board", REGULATOR_QUOTE, confidence=0.4),
            request_json("A USB-C PD breakout", USB_QUOTE, confidence=0.95),
        )
    )

    requests = extract_requests(model, TRANSCRIPT, max_requests=1)

    assert [r.intent for r in requests] == ["A USB-C PD breakout"]


def test_requests_come_back_highest_confidence_first():
    model = scripted(
        payload(
            request_json("A 3.3V regulator board", REGULATOR_QUOTE, confidence=0.4),
            request_json("A USB-C PD breakout", USB_QUOTE, confidence=0.95),
        )
    )

    assert [r.confidence for r in extract_requests(model, TRANSCRIPT)] == [0.95, 0.4]


def test_a_cap_below_one_is_refused_before_the_model_is_paid():
    model = scripted(payload())

    with pytest.raises(ValueError, match="max_requests"):
        extract_requests(model, TRANSCRIPT, max_requests=0)

    assert model.calls == []


# ------------------------------------------------------------------- transcript


def test_an_empty_transcript_costs_nothing_and_invents_nothing():
    model = ScriptedModel(responses=[])

    assert extract_requests(model, "   \n  ") == []
    assert model.calls == []  # never asked, so nothing to invent


def test_a_transcript_that_is_not_a_string_is_a_type_error():
    with pytest.raises(TypeError, match="transcript_text"):
        extract_requests(scripted(payload()), ["alice: hello there everyone"])


def test_an_enormous_transcript_is_truncated_from_the_front():
    # Requirements are stated after the agenda, so the tail is the half worth
    # keeping when a three-hour all-hands will not fit in a prompt.
    padding = "alice: filler line about the roadmap\n" * 4000
    long_transcript = padding + REGULATOR_QUOTE
    model = scripted(
        payload(request_json("A 3.3V regulator board", REGULATOR_QUOTE))
    )

    requests = extract_requests(model, long_transcript)

    sent = model.calls[0]["prompt"]
    assert len(long_transcript) > MAX_TRANSCRIPT_CHARS
    assert REGULATOR_QUOTE in sent
    assert len(sent) < len(long_transcript)
    assert [r.quote for r in requests] == [REGULATOR_QUOTE]


def test_a_failed_model_call_is_never_reported_as_a_meeting_with_no_requests():
    model = ScriptedModel(responses=[])  # runs out immediately

    with pytest.raises(ModelError):
        extract_requests(model, TRANSCRIPT)

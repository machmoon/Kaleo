"""The structured spec review: the IR's bounds, and the model loop.

Fully offline -- every model is a :class:`ScriptedModel`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from silkscreen.agents.model import ModelError, ScriptedModel
from silkscreen.agents.specreview import (
    SPECREVIEW_MARKER,
    evidence_block,
    propose_spec_review,
)
from silkscreen.specreview import (
    MAX_ITEMS,
    MAX_TOTAL_MINUTES,
    MIN_TOTAL_MINUTES,
    SUMMARY_MAX_CHARS,
    AgendaItem,
    SpecReview,
    SpecReviewResult,
    SpecReviewValidationError,
    parse_spec_review,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def agenda(**overrides) -> dict:
    """A valid agenda payload, ready to be broken one field at a time."""
    data = {
        "title": "AMS1117 rail review",
        "summary": "Three-part LDO board. Two open questions before fab.",
        "items": [
            {
                "topic": "Output capacitor ESR",
                "why": "The datasheet requires tantalum; a ceramic was placed.",
                "minutes": 15,
                "blocking": True,
                "refs": ["C2"],
            },
            {
                "topic": "Unrouted ground return",
                "why": "GND was left as ratsnest and needs a plane decision.",
                "minutes": 10,
                "blocking": False,
                "refs": ["GND"],
            },
        ],
        "decisions_needed": ["Ship with a ceramic output cap, or respin?"],
        "prepared_from": ["review", "route"],
    }
    data.update(overrides)
    return data


BOARD_REFS = ("U1", "C1", "C2", "R1", "GND", "VOUT")


def errors_of(payload) -> list[str]:
    with pytest.raises(SpecReviewValidationError) as excinfo:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        parse_spec_review(raw)
    return excinfo.value.errors


@dataclass(frozen=True)
class FakeFinding:
    severity: str
    title: str
    detail: str = ""
    parts: tuple[str, ...] = ()


@dataclass(frozen=True)
class FakeClause:
    name: str
    passed: bool
    margin_nm: int
    detail: str


@dataclass(frozen=True)
class FakeRow:
    ref: str
    value: str = ""
    package: str = ""
    mpn_status: str = "none"
    datasheet_url: str | None = None
    datasheet_status: str = "none"


@dataclass
class FakeSourcing:
    parts: list[FakeRow]
    warnings: list[str]


EVIDENCE = {
    "findings": [
        FakeFinding("blocker", "Ceramic output cap", "ESR too low", ("C2",))
    ],
    "unrouted": {"GND": "no path within budget"},
}


# --------------------------------------------------------------------------
# parse_spec_review -- the good case
# --------------------------------------------------------------------------


def test_parses_a_good_agenda():
    review = parse_spec_review(json.dumps(agenda()))
    assert review.title == "AMS1117 rail review"
    assert [i.topic for i in review.items] == [
        "Output capacitor ESR",
        "Unrouted ground return",
    ]
    assert review.items[0].blocking is True
    assert review.items[0].refs == ("C2",)
    assert review.prepared_from == ("review", "route")
    assert review.decisions_needed == ("Ship with a ceramic output cap, or respin?",)
    assert review.dropped == ()


def test_total_minutes_sums_the_items():
    review = parse_spec_review(json.dumps(agenda()))
    assert review.total_minutes() == 25
    assert review.needs_meeting is True
    assert len(review.blocking) == 1


def test_a_dict_is_accepted_as_well_as_text():
    assert parse_spec_review(agenda()).total_minutes() == 25


def test_fenced_json_is_tolerated():
    fenced = "```json\n" + json.dumps(agenda()) + "\n```"
    assert parse_spec_review(fenced).title == "AMS1117 rail review"


def test_as_dict_round_trips():
    review = parse_spec_review(json.dumps(agenda()))
    data = review.as_dict()
    # JSON-safe end to end -- this is what the delivery layer sends.
    assert json.loads(json.dumps(data)) == data
    assert data["total_minutes"] == 25
    assert data["blocking_count"] == 1
    assert data["needs_meeting"] is True
    assert data["items"][0]["refs"] == ["C2"]
    again = parse_spec_review(
        {k: data[k] for k in ("title", "summary", "items", "decisions_needed",
                              "prepared_from")}
    )
    assert again == review


# --------------------------------------------------------------------------
# parse_spec_review -- every bound, all at once
# --------------------------------------------------------------------------


def test_summary_over_the_cap_is_rejected():
    messages = errors_of(agenda(summary="x" * (SUMMARY_MAX_CHARS + 1)))
    assert any("summary" in m and str(SUMMARY_MAX_CHARS) in m for m in messages)


def test_summary_at_the_cap_is_accepted():
    review = parse_spec_review(json.dumps(agenda(summary="x" * SUMMARY_MAX_CHARS)))
    assert len(review.summary) == SUMMARY_MAX_CHARS


def test_empty_item_list_is_rejected():
    messages = errors_of(agenda(items=[]))
    assert any("at least" in m for m in messages)


def test_too_many_items_is_rejected():
    item = {
        "topic": "t",
        "why": "w",
        "minutes": 1,
        "blocking": False,
        "refs": [],
    }
    messages = errors_of(agenda(items=[dict(item) for _ in range(MAX_ITEMS + 1)]))
    assert any(str(MAX_ITEMS) in m and "more than" in m for m in messages)


def test_total_under_the_floor_is_rejected():
    short = agenda()
    short["items"] = [dict(short["items"][0], minutes=5)]
    messages = errors_of(short)
    assert any(str(MIN_TOTAL_MINUTES) in m and "minimum" in m for m in messages)


def test_total_over_the_ceiling_is_rejected():
    long = agenda()
    long["items"] = [dict(long["items"][0], minutes=40) for _ in range(3)]
    messages = errors_of(long)
    assert any(str(MAX_TOTAL_MINUTES) in m and "maximum" in m for m in messages)


def test_every_failure_arrives_in_one_error_not_one_at_a_time():
    broken = agenda(
        title="",
        summary="y" * (SUMMARY_MAX_CHARS + 5),
        prepared_from=["review", "vibes"],
        decisions_needed=[7],
        items=[
            {"topic": "", "why": "w", "minutes": "ten", "blocking": "yes", "refs": [3]},
            {"topic": "t2", "why": "w2", "minutes": 0, "blocking": True, "refs": "C1"},
        ],
    )
    messages = errors_of(broken)
    # One raise, every distinct problem named in it -- the whole batch goes
    # back to the model as a single repair prompt.
    assert any("title is empty" in m for m in messages)
    assert any(m.startswith("summary is") for m in messages)
    assert any("prepared_from[1]" in m and "vibes" in m for m in messages)
    assert any("decisions_needed[0]" in m for m in messages)
    assert any("items[0].topic is empty" in m for m in messages)
    assert any("items[0].minutes" in m for m in messages)
    assert any("items[0].blocking" in m for m in messages)
    assert any("items[0].refs[0]" in m for m in messages)
    assert any("items[1].minutes" in m for m in messages)
    assert any("items[1].refs" in m for m in messages)
    assert len(messages) >= 10
    # And the error's text carries them all, so a repair prompt built from
    # str(exc) alone is still complete.
    with pytest.raises(SpecReviewValidationError) as excinfo:
        parse_spec_review(json.dumps(broken))
    for message in messages:
        assert message in str(excinfo.value)


def test_bad_json_is_one_error_not_a_crash():
    messages = errors_of("not json at all")
    assert len(messages) == 1
    assert "not valid JSON" in messages[0]


def test_non_object_top_level_is_rejected():
    assert "expected a JSON object" in errors_of("[1, 2, 3]")[0]


def test_boolean_minutes_is_not_a_number():
    bad = agenda()
    bad["items"] = [dict(bad["items"][0], minutes=True)]
    assert any("minutes" in m for m in errors_of(bad))


def test_item_longer_than_the_meeting_is_rejected():
    bad = agenda()
    bad["items"] = [dict(bad["items"][0], minutes=MAX_TOTAL_MINUTES + 10)]
    messages = errors_of(bad)
    assert any("longer than the whole meeting" in m for m in messages)


# --------------------------------------------------------------------------
# the hallucination filter
# --------------------------------------------------------------------------


def test_an_item_about_a_part_the_board_lacks_is_dropped_not_reported():
    payload = agenda()
    payload["items"] = [
        payload["items"][0],
        dict(payload["items"][1], topic="Regulator R9", refs=["R9"], minutes=10),
    ]
    review = parse_spec_review(json.dumps(payload), known_refs=BOARD_REFS)
    assert [i.topic for i in review.items] == ["Output capacitor ESR"]
    assert len(review.dropped) == 1
    assert "R9" in review.dropped[0]
    # Dropping is a report, never silent.
    assert "Regulator R9" in review.dropped[0]


def test_a_hallucinated_ref_inside_a_real_item_is_pruned():
    payload = agenda()
    payload["items"] = [dict(payload["items"][0], refs=["C2", "Q7"], minutes=25)]
    review = parse_spec_review(json.dumps(payload), known_refs=BOARD_REFS)
    assert review.items[0].refs == ("C2",)
    assert any("Q7" in d for d in review.dropped)


def test_an_item_naming_nothing_is_kept():
    payload = agenda()
    payload["items"] = [dict(payload["items"][0], refs=[], minutes=25)]
    review = parse_spec_review(json.dumps(payload), known_refs=BOARD_REFS)
    assert len(review.items) == 1


def test_the_filter_may_legitimately_empty_the_agenda():
    payload = agenda()
    payload["items"] = [dict(payload["items"][0], refs=["Q9"], minutes=25)]
    review = parse_spec_review(json.dumps(payload), known_refs=BOARD_REFS)
    assert review.items == ()
    assert review.needs_meeting is False
    assert review.total_minutes() == 0
    assert review.dropped  # never silent


def test_omitting_known_refs_runs_no_filter():
    payload = agenda()
    payload["items"] = [dict(payload["items"][0], refs=["Q9"], minutes=25)]
    review = parse_spec_review(json.dumps(payload))
    assert review.items[0].refs == ("Q9",)
    assert review.dropped == ()


# --------------------------------------------------------------------------
# "nothing to discuss" is a result, not a failure
# --------------------------------------------------------------------------


def test_no_evidence_needs_no_model_call_and_is_reportable():
    model = ScriptedModel()  # would raise ModelError if asked
    result = propose_spec_review(model, board_summary="A 3.3V LDO board.")
    assert result.ok is True
    assert result.review is not None
    assert result.review.items == ()
    assert result.needs_meeting is False
    assert result.warnings == []
    assert model.calls == []
    assert "nothing" in result.review.summary.lower()


def test_empty_agenda_is_distinguishable_from_failure_and_from_never_running():
    nothing = SpecReviewResult(review=SpecReview(title="t", summary="s"))
    failed = SpecReviewResult(review=None, warnings=["no usable answer"])
    assert nothing.ok is True and nothing.needs_meeting is False
    assert failed.ok is False and failed.needs_meeting is False
    assert nothing.as_dict()["review"] is not None
    assert failed.as_dict()["review"] is None
    assert failed.as_dict()["warnings"]
    # And "never ran" is the absence of a result altogether.
    assert nothing != failed


def test_a_non_blocking_agenda_says_no_meeting_is_needed():
    payload = agenda()
    payload["items"] = [
        dict(payload["items"][0], blocking=False, minutes=15),
    ]
    review = parse_spec_review(json.dumps(payload))
    assert review.items
    assert review.needs_meeting is False
    assert review.as_dict()["blocking_count"] == 0


# --------------------------------------------------------------------------
# the model loop
# --------------------------------------------------------------------------


def test_the_marker_appears_verbatim_in_the_prompt():
    model = ScriptedModel(by_marker={SPECREVIEW_MARKER: json.dumps(agenda())})
    result = propose_spec_review(model, known_refs=BOARD_REFS, **EVIDENCE)
    assert result.ok is True
    assert SPECREVIEW_MARKER in model.calls[0]["prompt"]


def test_the_evidence_reaches_the_prompt():
    model = ScriptedModel(by_marker={SPECREVIEW_MARKER: json.dumps(agenda())})
    propose_spec_review(model, known_refs=BOARD_REFS, **EVIDENCE)
    prompt = model.calls[0]["prompt"]
    assert "Ceramic output cap" in prompt
    assert "GND" in prompt
    assert "review, route" in prompt


def test_one_repair_round_then_success():
    events: list[dict] = []
    model = ScriptedModel(
        responses=[json.dumps(agenda(summary="z" * 500)), json.dumps(agenda())]
    )
    result = propose_spec_review(
        model, known_refs=BOARD_REFS, on_event=events.append, **EVIDENCE
    )
    assert result.ok is True
    assert result.review.total_minutes() == 25
    assert len(model.calls) == 2
    # The repair prompt carries the batched errors and the rejected answer.
    repair = model.calls[1]["prompt"]
    assert "rejected" in repair
    assert "summary is 500 characters" in repair
    assert [e["event"] for e in events if e["event"] == "specreview.round"] == [
        "specreview.round"
    ]
    assert events[-1]["event"] == "specreview.agenda"
    assert events[-1]["minutes"] == 25
    assert events[-1]["needs_meeting"] is True


def test_giving_up_is_loud_and_never_half_parses():
    model = ScriptedModel(responses=["not json", "still not json"])
    result = propose_spec_review(model, known_refs=BOARD_REFS, **EVIDENCE)
    assert result.ok is False
    assert result.review is None
    assert result.needs_meeting is False
    assert len(model.calls) == 2  # one call plus one repair, then stop
    assert len(result.warnings) == 1
    assert "2 attempt(s)" in result.warnings[0]
    assert "not valid JSON" in result.warnings[0]


def test_max_repairs_zero_is_one_call():
    model = ScriptedModel(responses=["not json"])
    result = propose_spec_review(
        model, known_refs=BOARD_REFS, max_repairs=0, **EVIDENCE
    )
    assert len(model.calls) == 1
    assert result.ok is False
    assert "1 attempt(s)" in result.warnings[0]


def test_model_error_is_not_wrapped():
    model = ScriptedModel()  # runs out of responses immediately
    with pytest.raises(ModelError):
        propose_spec_review(model, known_refs=BOARD_REFS, **EVIDENCE)


def test_dropped_items_become_warnings_and_events():
    payload = agenda()
    payload["items"] = [dict(payload["items"][0], refs=["Q9"], minutes=25)]
    events: list[dict] = []
    model = ScriptedModel(by_marker={SPECREVIEW_MARKER: json.dumps(payload)})
    result = propose_spec_review(
        model, known_refs=BOARD_REFS, on_event=events.append, **EVIDENCE
    )
    assert result.ok is True
    assert result.review.items == ()
    assert result.warnings and "Q9" in result.warnings[0]
    assert any(e["event"] == "specreview.dropped" for e in events)


# --------------------------------------------------------------------------
# evidence_block
# --------------------------------------------------------------------------


def test_evidence_block_names_its_sources_and_vocabulary():
    text, sources, refs = evidence_block(
        findings=[FakeFinding("blocker", "Bad cap", "ESR", ("C2",))],
        unrouted={"VOUT": "blocked"},
        clauses=[
            FakeClause("board_clash", False, -400000, "0.4 mm of board in the wall"),
            FakeClause("headroom", True, 1000000, "fine"),
        ],
        sourcing=FakeSourcing(
            parts=[FakeRow(ref="R1", value="10k", package="0603")],
            warnings=["parts were not sourced"],
        ),
    )
    assert sources == ("review", "route", "kernel", "sourcing")
    assert set(refs) >= {"C2", "VOUT", "board_clash", "R1"}
    assert "-0.400 mm" in text
    assert "headroom" not in text  # passing clauses are not evidence
    assert "parts were not sourced" in text


def test_evidence_block_is_empty_when_nothing_was_flagged():
    text, sources, refs = evidence_block(
        clauses=[FakeClause("headroom", True, 1, "fine")],
        sourcing=FakeSourcing(parts=[FakeRow(ref="R1", mpn_status="verified")],
                              warnings=[]),
    )
    assert (text, sources, refs) == ("", (), ())


def test_agenda_item_defaults_to_no_refs():
    item = AgendaItem(topic="t", why="w", minutes=5, blocking=False)
    assert item.refs == ()
    assert item.as_dict()["refs"] == []

"""resolve_desk: the PNG rides the existing Document seam.

Offline tests drive :class:`ScriptedModel`, the same way every other agent
stage is tested. The marker is pinned here so a prompt rewrite that breaks
the service route's scripted model announces itself.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.desk import (
    DESK_MARKER,
    DeskCandidate,
    DeskValidationError,
    parse_desk_response,
    resolve_desk,
)
from silkscreen.agents.model import ModelError, ScriptedModel

#: The marker a service test's scripted model keys on too.
MARKER = DESK_MARKER

#: Minimal PNG signature plus padding; resolve_desk only checks the magic.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

CANDIDATES = (
    DeskCandidate(testid="finding-card", attrs={"sev": "blocker"}, tab="review"),
    DeskCandidate(testid="board-well-part", attrs={"ref": "C1"}, tab="board"),
)


def _answer(*, caption: str, abstain: bool, target: dict | None) -> str:
    return json.dumps(
        {"caption": caption, "abstain": abstain, "target": target}
    )


def scripted(text: str) -> ScriptedModel:
    return ScriptedModel(by_marker={MARKER: text})


def test_abstain_returns_caption_and_null_target():
    model = scripted(
        _answer(
            caption="I cannot tell what you are pointing at.",
            abstain=True,
            target=None,
        )
    )
    result = resolve_desk(
        model,
        PNG,
        "what's this",
        cursor_x=100.0,
        cursor_y=200.0,
        width=1512,
        height=982,
        candidates=CANDIDATES,
    )
    assert result.abstain is True
    assert result.target is None
    assert "cannot tell" in result.caption
    assert result.as_dict()["target"] is None


def test_picks_a_listed_candidate():
    model = scripted(
        _answer(
            caption="That blocker is the missing decoupling on C1.",
            abstain=False,
            target={
                "testid": "finding-card",
                "attrs": {"sev": "blocker"},
                "tab": "review",
            },
        )
    )
    result = resolve_desk(
        model,
        PNG,
        "what's this",
        cursor_x=80.4,
        cursor_y=120.6,
        candidates=CANDIDATES,
    )
    assert result.abstain is False
    assert result.target is not None
    assert result.target.testid == "finding-card"
    assert result.target.attrs == {"sev": "blocker"}
    assert result.target.tab == "review"


def test_bad_json_is_a_batched_validation_error():
    model = scripted("not json at all")
    with pytest.raises(DeskValidationError) as excinfo:
        resolve_desk(model, PNG, "what's this", cursor_x=1, cursor_y=1)
    assert excinfo.value.errors
    assert "not JSON" in excinfo.value.errors[0]


def test_png_travels_as_an_inline_document():
    model = scripted(_answer(caption="the prompt bar", abstain=True, target=None))
    resolve_desk(model, PNG, "what's this", cursor_x=10, cursor_y=20)
    (call,) = model.calls
    (doc,) = call["documents"]
    assert doc.data == PNG
    assert doc.url is None
    assert doc.mime_type == "image/png"
    assert MARKER in call["prompt"]
    assert "what's this" in call["prompt"]
    assert "10.0, 20.0" in call["prompt"]


def test_parse_collects_every_shape_error():
    with pytest.raises(DeskValidationError) as excinfo:
        parse_desk_response(
            json.dumps(
                {
                    "caption": 7,
                    "abstain": "yes",
                    "target": {"testid": "", "attrs": {"ref": 1}},
                }
            )
        )
    messages = " ".join(excinfo.value.errors)
    assert "caption" in messages
    assert "abstain" in messages
    assert "testid" in messages
    assert "attrs" in messages


def test_abstain_with_a_target_is_refused():
    with pytest.raises(DeskValidationError, match="abstain"):
        parse_desk_response(
            _answer(
                caption="x",
                abstain=True,
                target={"testid": "finding-card"},
            )
        )


def test_unknown_candidate_is_refused():
    with pytest.raises(DeskValidationError, match="candidate"):
        parse_desk_response(
            _answer(
                caption="something",
                abstain=False,
                target={"testid": "not-a-real-control"},
            ),
            candidates=CANDIDATES,
        )


def test_non_png_is_refused_before_the_model_is_called():
    model = ScriptedModel()
    with pytest.raises(ValueError, match="PNG"):
        resolve_desk(model, b"not a png", "what's this", cursor_x=0, cursor_y=0)
    assert model.calls == []


def test_empty_utterance_is_refused_before_the_model_is_called():
    model = ScriptedModel()
    with pytest.raises(ValueError, match="utterance"):
        resolve_desk(model, PNG, "   ", cursor_x=0, cursor_y=0)
    assert model.calls == []


def test_a_failed_call_raises_rather_than_inventing_a_target():
    model = ScriptedModel()
    with pytest.raises(ModelError):
        resolve_desk(model, PNG, "what's this", cursor_x=0, cursor_y=0)

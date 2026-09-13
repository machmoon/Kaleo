"""The ADK LLM root chooses between clarification and the board tool."""

from __future__ import annotations

from typing import Any

from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from silkscreen.agents.adk.orchestrator import run_orchestrator


class FakeLlm(BaseLlm):
    responses: list[types.Content]
    requests: list[Any] = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        yield LlmResponse(content=self.responses.pop(0))


def text_response(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def tool_response() -> types.Content:
    return types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="generate_board", args={})],
    )


def test_the_orchestrator_can_ask_one_clarification_without_running_the_board():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[text_response("What input voltage should the regulator accept?")],
    )
    events = []
    generated = []

    outcome = run_orchestrator(
        message="make a regulator",
        model=model,
        session_id="s1",
        generate=lambda: generated.append(True),
        emit=events.append,
        debug=True,
    )

    assert outcome.needs_clarification is True
    assert outcome.result is None
    assert generated == []
    assert "input voltage" in outcome.assistant.lower()
    assert [event["event"] for event in events] == [
        "model.request",
        "model.call",
        "model.response",
        "assistant.message",
    ]
    request = events[0]
    assert request["layer"] == "orchestrator"
    assert request["system"]
    assert "make a regulator" in str(request["contents"])


def test_the_orchestrator_calls_the_validated_generator_and_summarizes_it():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[tool_response(), text_response("The board is ready for review.")],
    )
    events = []
    result = {
        "status": "FEASIBLE",
        "parts": [{"ref": "U1"}],
        "nets": ["VIN", "GND", "VOUT"],
        "findings": [],
        "blockers": [],
        "warnings": [],
        "duration_s": 1.2,
    }

    outcome = run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=model,
        session_id="s2",
        generate=lambda: result,
        emit=events.append,
        debug=True,
    )

    assert outcome.needs_clarification is False
    assert outcome.result is result
    assert outcome.assistant == "The board is ready for review."
    names = [event["event"] for event in events]
    assert names.count("model.request") == 2
    assert names.count("model.response") == 2
    assert "tool.start" in names
    assert "tool.done" in names
    assert names[-1] == "assistant.message"
    assert events[names.index("tool.done")]["result"] == {
        "status": "FEASIBLE",
        "parts": 1,
        "nets": 3,
        "findings": 0,
        "blockers": 0,
        "warnings": 0,
        "routed_nets": 0,
        "unrouted": {},
        "duration_s": 1.2,
        "served_by": None,
    }


def test_constraint_receipt_facts_reach_the_orchestrator_tool_summary():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[
            tool_response(),
            text_response("The artifact exists, but one constraint blocks promotion."),
        ],
    )
    events = []
    result = {
        "status": "FEASIBLE",
        "parts": [{"ref": "U1"}],
        "nets": ["VIN"],
        "findings": [],
        "blockers": ["constraint Power/routing: VIN is unrouted"],
        "warnings": [],
        "promotion_status": "constraint_blocked",
        "constraint_manifest": {"version": 2},
        "constraint_receipt": {
            "hard_gate": "blocked",
            "promotable": False,
            "net_classes": [
                {
                    "net_class": "Power",
                    "checks": [
                        {
                            "name": "routing",
                            "status": "violated",
                            "detail": "VIN is unrouted",
                        },
                        {
                            "name": "allowed_layers",
                            "status": "verified",
                            "detail": "Layers match",
                        },
                    ],
                }
            ],
            "mechanical": [
                {
                    "name": "component_height",
                    "status": "unresolved",
                    "detail": "No height metadata",
                }
            ],
            "blockers": [
                {
                    "scope": "Power",
                    "name": "routing",
                    "status": "violated",
                    "detail": "VIN is unrouted",
                    "evidence": {"unrouted": ["VIN"]},
                }
            ],
        },
    }

    run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=model,
        session_id="constraint-summary",
        generate=lambda: result,
        emit=events.append,
        debug=False,
    )

    tool_summary = next(
        event["result"] for event in events if event["event"] == "tool.done"
    )
    assert tool_summary["promotion_status"] == "constraint_blocked"
    assert tool_summary["constraint_manifest_version"] == 2
    assert tool_summary["constraint_receipt"] == {
        "hard_gate": "blocked",
        "promotable": False,
        "verified": 1,
        "violated": 1,
        "unresolved": 1,
        "blockers": [
            {
                "scope": "Power",
                "name": "routing",
                "status": "violated",
                "detail": "VIN is unrouted",
            }
        ],
    }


def test_reasoning_effort_reaches_adk_without_obsolete_sampling_controls():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[text_response("Which input voltage?")],
    )
    before = len(model.requests)

    run_orchestrator(
        message="make a regulator",
        model=model,
        thinking_level="high",
        session_id="thinking-session",
        generate=lambda: None,
        emit=lambda event: None,
        debug=False,
    )

    request = model.requests[before]
    assert request.config.thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert request.config.temperature is None


def test_each_adk_model_round_trip_passes_through_the_pre_call_hook():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[tool_response(), text_response("Done.")],
    )
    calls = []

    run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=model,
        session_id="paced-session",
        generate=lambda: {"status": "FEASIBLE"},
        emit=lambda event: None,
        before_model_call=lambda: calls.append("called"),
    )

    assert calls == ["called", "called"]

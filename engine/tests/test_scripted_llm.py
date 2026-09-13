"""``ScriptedLlm`` -- the offline ``BaseLlm`` stand-in for the ADK root.

Companion to ``service/tests/test_orchestrator.py``'s inline ``FakeLlm``: that
file already covers the orchestrator's event/summary behaviour end to end
against a hand-rolled fake, so this file does not repeat those assertions. It
instead exercises the *adapter itself* -- that it satisfies ``BaseLlm``'s
async-generator contract, that it fails loudly rather than quietly when a test
mis-scripts a turn, and that it can drive a genuine, fully offline run of the
ADK orchestrator (clarification, tool call, and a real ``generate_pcb`` call
underneath) with no network access and no API key, the same guarantee
``ScriptedModel`` gives the plain worker layer.

``google.adk`` is an optional extra (the ``adk`` group in ``pyproject.toml``),
so -- following ``test_adk.py``'s convention -- the third-party import is
guarded and the driver import is not, keeping internal breakage from being
silently reported as "the extra is missing".
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents import ScriptedModel, generate_pcb
from test_agents import GOOD_CIRCUIT

try:
    import google.adk  # noqa: F401

    _HAS_ADK = True
except ImportError:  # a base install has no google.adk; these tests skip
    _HAS_ADK = False

if _HAS_ADK:
    from google.adk.models.llm_request import LlmRequest
    from silkscreen.agents.adk.orchestrator import run_orchestrator
    from silkscreen.agents.adk.scripted_llm import (
        ScriptedLlm,
        ScriptedLlmError,
        text_turn,
        tool_call_turn,
    )

needs_adk = pytest.mark.skipif(not _HAS_ADK, reason="the 'adk' extra is not installed")


def _pipeline_summary(result) -> dict:
    """The subset of the service's tool-result dict the orchestrator reads.

    Not a copy of ``service/app.py``'s wire format (this file must not depend
    on service internals) -- just enough of ``_summary``'s expected shape
    (``status``/``parts``/``nets``/``findings``/``blockers``/``warnings``) for
    a real ``PipelineResult`` to flow through ``run_orchestrator`` unmodified.
    """
    board = result.board
    return {
        "status": str(board.solver_status),
        "parts": [{"ref": p.ref} for p in board.parts],
        "nets": list(board.nets),
        "findings": [f.title for f in result.findings],
        "blockers": [],
        "warnings": list(board.warnings),
        "duration_s": 0.0,
    }


# ---------------------------------------------------------------- the adapter itself


@needs_adk
def test_generate_content_async_yields_one_response_per_scripted_turn():
    llm = ScriptedLlm(model="scripted", responses=[
        text_turn("What input voltage?"),
        tool_call_turn(),
    ])

    async def drive():
        out = []
        async for response in llm.generate_content_async(LlmRequest()):
            out.append(response)
        async for response in llm.generate_content_async(LlmRequest()):
            out.append(response)
        return out

    import asyncio
    responses = asyncio.run(drive())

    assert len(responses) == 2
    assert responses[0].content.parts[0].text == "What input voltage?"
    assert responses[1].content.parts[0].function_call.name == "generate_board"
    # Every request ADK built is recorded, mirroring ScriptedModel.calls.
    assert len(llm.requests) == 2
    assert all(isinstance(r, LlmRequest) for r in llm.requests)


@needs_adk
def test_running_out_of_script_raises_a_named_error_not_an_indexerror():
    llm = ScriptedLlm(model="scripted", responses=[text_turn("only one turn")])

    async def drive():
        async for _ in llm.generate_content_async(LlmRequest()):
            pass
        async for _ in llm.generate_content_async(LlmRequest()):
            pass

    import asyncio
    with pytest.raises(ScriptedLlmError, match="ran out of scripted responses"):
        asyncio.run(drive())


# ------------------------------------------------------------ driving the orchestrator


@needs_adk
def test_scripted_clarification_turn():
    llm = ScriptedLlm(model="scripted", responses=[
        text_turn("What input voltage should the regulator accept?"),
    ])
    events = []
    generated = []

    outcome = run_orchestrator(
        message="make a regulator",
        model=llm,
        session_id="scripted-clarify",
        generate=lambda: generated.append(True),
        emit=events.append,
        debug=True,
    )

    assert outcome.needs_clarification is True
    assert outcome.result is None
    assert generated == []
    assert "input voltage" in outcome.assistant.lower()
    assert [e["event"] for e in events] == [
        "model.request", "model.call", "model.response", "assistant.message",
    ]


@needs_adk
def test_scripted_tool_call_turn_invokes_generate_board():
    llm = ScriptedLlm(model="scripted", responses=[
        tool_call_turn(),
        text_turn("The board is ready for review."),
    ])
    events = []
    result = {
        "status": "FEASIBLE",
        "parts": [{"ref": "U1"}],
        "nets": ["VIN", "GND"],
        "findings": [],
        "blockers": [],
        "warnings": [],
        "duration_s": 0.4,
    }

    outcome = run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=llm,
        session_id="scripted-tool",
        generate=lambda: result,
        emit=events.append,
        debug=False,
    )

    assert outcome.needs_clarification is False
    assert outcome.result is result
    assert outcome.assistant == "The board is ready for review."
    names = [e["event"] for e in events]
    assert "tool.start" in names and "tool.done" in names
    assert events[names.index("tool.done")]["result"]["status"] == "FEASIBLE"


def test_a_second_tool_call_in_one_turn_does_not_run_a_second_paid_pipeline():
    """One request must cost one pipeline, whatever the model emits.

    "Call generate_board exactly once" lived only in the prompt, and a prompt
    is not a guard: a model that emits two function calls in a turn (or
    retries after a tool error) ran the whole pipeline twice -- two full sets
    of paid model calls, with only the second result kept.
    """
    llm = ScriptedLlm(model="scripted", responses=[
        tool_call_turn(),
        tool_call_turn(),
        text_turn("The board is ready for review."),
    ])
    runs = []

    def generate():
        runs.append(1)
        return {
            "status": "FEASIBLE",
            "parts": [{"ref": "U1"}],
            "nets": ["VIN"],
            "findings": [],
            "blockers": [],
            "warnings": [],
            "duration_s": 0.4,
        }

    outcome = run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=llm,
        session_id="scripted-double-tool",
        generate=generate,
        emit=lambda _e: None,
        debug=False,
    )

    assert len(runs) == 1, f"the pipeline ran {len(runs)} times for one request"
    assert outcome.result is not None


@needs_adk
def test_full_offline_orchestrator_run_through_generate_pcb(tmp_path):
    """Clarification, then a scripted tool call that runs the real pipeline.

    No datasheet, no network, no API key: the worker layer is driven by
    ``ScriptedModel`` (the existing offline stand-in for ``Model``) and the
    orchestrator layer by ``ScriptedLlm`` (this file's offline stand-in for
    ``BaseLlm``) -- the two seams composed the way the service composes the
    real ones.
    """
    worker_model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    llm = ScriptedLlm(model="scripted", responses=[
        text_turn("What voltage rail does the motor driver run from?"),
        tool_call_turn(),
        text_turn("Board generated."),
    ])
    events = []

    def generate() -> dict:
        result = generate_pcb(
            worker_model,
            "a motor driver board",
            output=tmp_path / "board.kicad_pcb",
            review=False,
            time_limit_s=10.0,
        )
        return _pipeline_summary(result)

    first = run_orchestrator(
        message="a motor driver board",
        model=llm,
        session_id="offline-full-run",
        generate=generate,
        emit=events.append,
    )
    assert first.needs_clarification is True
    assert not (tmp_path / "board.kicad_pcb").exists()

    second = run_orchestrator(
        message="a motor driver board",
        clarification="12 V",
        model=llm,
        session_id="offline-full-run",
        generate=generate,
        emit=events.append,
    )

    assert second.needs_clarification is False
    assert second.result is not None
    assert second.result["parts"], "the real pipeline placed at least one part"
    assert second.assistant == "Board generated."
    assert worker_model.calls, "the worker model was actually invoked"
    assert (tmp_path / "board.kicad_pcb").exists()

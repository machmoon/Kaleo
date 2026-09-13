"""``ScriptedLlm`` -- the offline stand-in for the ADK ``LlmAgent`` root.

Every other layer in this codebase gets an offline stand-in behind a protocol
seam: ``GeminiModel``/``ScriptedModel`` for the plain :class:`Model` protocol,
``UrllibTransport``/a recorded transport for the Meet integration, a real
``fetch=`` seam for datasheet downloads. ``agents/adk/orchestrator.py`` is a
genuine ADK ``LlmAgent`` root, so its offline stand-in has to satisfy ADK's own
``BaseLlm`` contract instead -- an ``async`` generator method rather than a
synchronous ``generate()`` call.

``google.adk.models.base_llm.BaseLlm.generate_content_async`` is documented as
yielding, in non-streaming mode (``stream=False``, the only mode the ADK
``Runner``/``LlmAgent`` machinery this repo drives ever requests), *exactly
one* :class:`~google.adk.models.llm_response.LlmResponse` carrying the whole
turn. ``ScriptedLlm`` implements exactly that: it is constructed with a
sequence of ``google.genai.types.Content`` turns (built with the
:func:`text_turn` and :func:`tool_call_turn` helpers below, or handed a
``Content`` directly) and yields one scripted ``LlmResponse`` per call, in
order, recording every ``LlmRequest`` it was given so a test can assert on the
prompt ADK actually built.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

__all__ = ["ScriptedLlm", "ScriptedLlmError", "text_turn", "tool_call_turn"]


class ScriptedLlmError(RuntimeError):
    """Raised when a test drives :class:`ScriptedLlm` past its script."""


def text_turn(text: str) -> types.Content:
    """A plain assistant reply -- the shape of a clarification question."""
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def tool_call_turn(
    tool: str = "generate_board", args: dict[str, Any] | None = None
) -> types.Content:
    """A function-call reply -- the shape of an invocation of ``generate_board``."""
    return types.Content(
        role="model",
        parts=[types.Part.from_function_call(name=tool, args=dict(args or {}))],
    )


class ScriptedLlm(BaseLlm):
    """A deterministic ``BaseLlm`` for driving the orchestrator without a key.

    ``responses`` are consumed in order, one ``Content`` turn per call to
    :meth:`generate_content_async`. Every :class:`LlmRequest` ADK builds is
    appended to ``requests``, mirroring ``ScriptedModel.calls`` so a test can
    assert on what the agent was actually asked (system instruction, tool
    declarations, prior turns) rather than only on the scripted answer.

    Running out of scripted responses raises :class:`ScriptedLlmError` rather
    than the ``IndexError`` a bare ``list.pop(0)`` would give -- the same
    convention ``ScriptedModel.generate`` uses (a specific ``ModelError``
    instead of a quiet crash), so a test that mis-scripted a turn fails with a
    message that names the problem instead of a bare traceback.
    """

    responses: list[types.Content]
    requests: list[LlmRequest] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream  # every scripted turn is a complete, non-streamed response
        self.requests.append(llm_request)
        if not self.responses:
            raise ScriptedLlmError(
                f"ScriptedLlm({self.model!r}) ran out of scripted responses "
                f"after {len(self.requests)} call(s)"
            )
        yield LlmResponse(content=self.responses.pop(0))

"""The Google ADK driver for the pipeline stages.

Same stages, same events, a different runner: :mod:`silkscreen.agents.pipeline`
walks the seven stage bodies in a straight line, this package expresses them as
an ADK dynamic workflow. Which one runs is chosen by ``generate_pcb(engine=...)``.

Nothing here is imported eagerly. ``google.adk`` ships in the optional ``adk``
extra, and ``silkscreen.agents`` is imported by the service on every request
path, so :mod:`.workflow` and :mod:`.runner` -- which both import ``google.adk``
at module scope -- are reached only through the lookup below.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_workflow", "generate_pcb_adk"]


def __getattr__(name: str) -> Any:
    if name == "build_workflow":
        from .workflow import build_workflow

        return build_workflow
    if name == "generate_pcb_adk":
        from .runner import generate_pcb_adk

        return generate_pcb_adk
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)

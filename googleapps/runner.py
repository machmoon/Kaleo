"""Running the real pipeline, and remembering how it went.

This is the only module that knows both the engine and this package. It calls
:func:`silkscreen.agents.generate_pcb` exactly as the CLI does -- same
arguments, same defaults, no engine behaviour reimplemented or reached around
-- and collects the stage events into the summary the Chat card and the email
report.

The stage list follows the project's progress-honesty rule: a stage appears
only when its own event arrived, so a run that stalled looks stalled rather
than finished.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents import generate_pcb  # noqa: E402
from silkscreen.agents.model import GeminiModel, Model, ModelError  # noqa: E402

from .config import Config  # noqa: E402

__all__ = ["ModelError", "RunOutcome", "StageLog", "email_body", "run_pipeline"]


class StageLog:
    """Collects ``stage.start`` / ``stage.done`` events into readable lines."""

    def __init__(self) -> None:
        self._started: dict[str, float] = {}
        self._lines: list[str] = []

    def on_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        stage = str(event.get("stage") or "")
        t_s = float(event.get("t_s") or 0.0)
        if name == "stage.start" and stage:
            self._started[stage] = t_s
        elif name == "stage.done" and stage:
            elapsed = t_s - self._started.get(stage, t_s)
            self._lines.append(f"{stage}: done in {elapsed:.1f} s")

    def lines(self) -> list[str]:
        return list(self._lines)


@dataclass
class RunOutcome:
    result: Any
    stage_lines: list[str] = field(default_factory=list)
    duration_s: float = 0.0


def run_pipeline(
    config: Config,
    intent: str,
    output: str | Path,
    *,
    datasheets: dict[str, str] | None = None,
    review: bool = True,
    route: bool = True,
    max_repairs: int = 3,
    time_limit_s: float = 20.0,
    model_name: str | None = None,
    model_factory: Callable[[], Model] | None = None,
) -> RunOutcome:
    """One run, CLI-shaped: read, propose, validate, place, emit, route, review.

    The keyword arguments are the CLI's flags under the CLI's names, so a
    ``python -m silkscreen`` invocation translates to ``python -m googleapps
    run`` one flag at a time.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    model = (model_factory or (lambda: GeminiModel(model_name or config.model)))()
    stages = StageLog()
    started = time.monotonic()
    result = generate_pcb(
        model,
        intent,
        datasheets=datasheets or None,
        output=output,
        review=review,
        route=route,
        max_repairs=max_repairs,
        time_limit_s=time_limit_s,
        on_event=stages.on_event,
    )
    return RunOutcome(
        result=result,
        stage_lines=stages.lines(),
        duration_s=time.monotonic() - started,
    )


def email_body(outcome: RunOutcome) -> str:
    """The plain-text report for the Gmail summary part.

    Reuses ``PipelineResult.summary()`` -- the same line the CLI prints -- and
    then names every unrouted net, because an emailed "board ready" over a
    ratsnest misleads exactly the person who cannot see the ratsnest yet.
    """
    result = outcome.result
    lines = [result.summary(), ""]
    lines.extend(outcome.stage_lines)
    if result.route is not None and result.route.unrouted:
        lines.append("")
        lines.append("Nets left unrouted (finish these in KiCad):")
        for net, reason in sorted(result.route.unrouted.items()):
            lines.append(f"  {net}: {reason}")
    review = getattr(result, "review", None)
    if review is not None and not review.ok:
        # Said outright rather than left to an absent "Review blockers:"
        # section: the reader cannot see that the critic never answered, and
        # a body with no blocker list reads as a board with no blockers.
        lines.append("")
        lines.append(f"Review: {review.note()}.")
        lines.append(
            "  No blocker list follows because none was produced — this board "
            "has not been argued against."
        )
    blockers = list(result.blockers)
    if blockers:
        lines.append("")
        lines.append("Review blockers:")
        lines.extend(f"  - {finding.title}" for finding in blockers)
    lines.append("")
    lines.append("The attached .kicad_pcb opens in KiCad 7 or 8.")
    return "\n".join(lines)

"""Adversarial design review of a finished board, with a thinking slider.

Optional by construction: nothing in the generate pipeline calls this. You run
it at a board file, choose how hard it should think, and get back a written
report plus a render of the board with every finding marked where it is.

    from silkscreen.audit import review_board, write_reports
    result = review_board("board.kicad_pcb", effort="deep", model=model)
    write_reports(result, "review/")

Two halves, kept apart on purpose:

* :mod:`.rules` measures the board and reports only what it measured. Those
  findings are ``proven`` and carry the measurement that proves them.
* :mod:`.judgment` asks a model for the findings no rule can express -- wrong
  component values, floating mode pins, topology mistakes. Those findings are
  ``suggested``, carry no measurement, and at deep effort must survive a
  refutation pass before they appear at all.

The report never merges the two. Deterministic checking is the part of an AI
review that can be trusted, and a reader has to be able to see which half a
given line came from.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .effort import PROFILES, Effort, EffortProfile, profile_for, slider
from .findings import Finding, Origin, Severity
from .geometry import AuditBoard, load_audit_board
from .judgment import JudgeModel, board_digest, judge
from .render import render_svg
from .report import html_report, json_report, text_report
from .result import AuditResult
from .rules import RULES, run_rules

__all__ = [
    "review_board",
    "write_reports",
    "AuditResult",
    "AuditBoard",
    "Effort",
    "EffortProfile",
    "Finding",
    "Origin",
    "Severity",
    "PROFILES",
    "RULES",
    "profile_for",
    "slider",
    "load_audit_board",
    "board_digest",
    "render_svg",
    "text_report",
    "html_report",
    "json_report",
]


def review_board(
    source: str | Path | AuditBoard,
    *,
    effort: Effort | str = Effort.STANDARD,
    model: JudgeModel | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AuditResult:
    """Review a board file. Deterministic rules always run; the model may not.

    Args:
        source: A ``.kicad_pcb`` path, or an already-loaded board.
        effort: ``quick``, ``standard`` or ``deep`` -- see :mod:`.effort`.
        model: Anything with ``generate``. Without one, the review is entirely
            deterministic and says so in its own output rather than quietly
            returning a shorter list.
        on_event: Called with one flat dict per stage, mirroring the shape the
            generate pipeline emits.

    Raises:
        FileNotFoundError: no such board file.
    """
    started = time.monotonic()
    profile = profile_for(effort)

    def emit(**evt: Any) -> None:
        if on_event is not None:
            on_event(evt)

    board = (
        source
        if isinstance(source, AuditBoard)
        else load_audit_board(source)
    )

    emit(event="stage.start", stage="rules", effort=profile.level.value)
    findings, ran = run_rules(board, profile)
    emit(event="stage.done", stage="rules", rules=len(ran), findings=len(findings))

    model_passes: list[str] = []
    skipped = ""
    if not profile.uses_model:
        skipped = f"{profile.level.value} effort is deterministic by design"
    elif model is None:
        skipped = "no model was supplied"
    else:
        emit(event="stage.start", stage="judgment")
        try:
            suggested, model_passes = judge(
                model, board, findings, profile, on_event=on_event
            )
            findings.extend(suggested)
            emit(event="stage.done", stage="judgment", findings=len(suggested))
        except Exception as exc:  # noqa: BLE001 - the proven half must survive
            skipped = f"the model pass failed: {type(exc).__name__}: {exc}"
            emit(event="stage.error", stage="judgment", error=str(exc)[:160])

    return AuditResult(
        board=board,
        profile=profile,
        findings=findings,
        rules_run=ran,
        model_passes=model_passes,
        skipped_reason=skipped,
        elapsed_s=time.monotonic() - started,
        source=board.source,
    )


def write_reports(
    result: AuditResult, out_dir: str | Path, *, stem: str = "review"
) -> list[Path]:
    """Write ``review.html``, ``review.svg`` and ``review.json``.

    The SVG is written separately from the HTML that embeds it so it can go
    into a PR comment, an issue, or a slide without carrying the page with it.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for suffix, text in (
        (".html", html_report(result)),
        (".svg", render_svg(result)),
        (".json", json_report(result)),
    ):
        path = out_dir / f"{stem}{suffix}"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written

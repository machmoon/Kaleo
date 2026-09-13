"""The thinking slider: three levels that buy strictly more review.

The point of the slider is that a higher level is not a bigger token budget on
the same work. Each step up adds *checks that were not run at all* at the level
below, and adds model passes that do different jobs:

    quick     ●───○───○   geometry and connectivity rules only. No model call.
    standard  ○───●───○   + clearance sweeps and design-practice rules,
                          + one model pass for the judgment-shaped findings.
    deep      ○───○───●   + expensive rules (dangling copper, acute angles,
                          silkscreen legibility), stricter thresholds, and the
                          model's own findings are sent back to be refuted
                          before any of them survive into the report.

``groups`` names rule groups from :mod:`silkscreen.audit.rules`; the level
containment is asserted by test, so "deeper" can never quietly mean "different".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..units import DEFAULT_CLEARANCE_NM, mm
from .findings import Severity

__all__ = ["Effort", "EffortProfile", "PROFILES", "profile_for", "slider"]


class Effort(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


@dataclass(frozen=True)
class EffortProfile:
    level: Effort
    #: Rule groups to run. Each level is a superset of the one below it.
    groups: frozenset[str]
    #: Model passes. 0 means the review is entirely deterministic.
    judgment_passes: int
    #: How many times each suggested finding must survive a refutation prompt.
    refute_rounds: int
    #: Ask the model about each multi-pin part separately, not just the board
    #: as a whole. A whole-board prompt reliably finds two or three things and
    #: stops; per-part prompts are how the tail gets found.
    per_part_focus: bool
    #: Findings below this are collected but not shown, so a quick pass is not
    #: buried in notes.
    min_severity: Severity
    clearance_nm: int
    #: Furthest a decoupling capacitor may sit from the pin it decouples.
    decoupling_max_nm: int
    #: How close a courtyard may come to the board edge.
    edge_margin_nm: int
    #: Solver-free, so a deeper level costs only what its extra checks cost.
    description: str

    @property
    def uses_model(self) -> bool:
        return self.judgment_passes > 0


_QUICK_GROUPS = frozenset({"geometry", "connectivity"})
_STANDARD_GROUPS = _QUICK_GROUPS | {"clearance", "practice"}
_DEEP_GROUPS = _STANDARD_GROUPS | {"manufacturing"}


PROFILES: dict[Effort, EffortProfile] = {
    Effort.QUICK: EffortProfile(
        level=Effort.QUICK,
        groups=_QUICK_GROUPS,
        judgment_passes=0,
        refute_rounds=0,
        per_part_focus=False,
        min_severity=Severity.MARGINAL,
        clearance_nm=DEFAULT_CLEARANCE_NM,
        decoupling_max_nm=mm(5.0),
        edge_margin_nm=mm(0.3),
        description="geometry and connectivity, offline, no model call",
    ),
    Effort.STANDARD: EffortProfile(
        level=Effort.STANDARD,
        groups=_STANDARD_GROUPS,
        judgment_passes=1,
        refute_rounds=0,
        per_part_focus=False,
        min_severity=Severity.NOTE,
        clearance_nm=DEFAULT_CLEARANCE_NM,
        decoupling_max_nm=mm(3.0),
        edge_margin_nm=mm(0.5),
        description="+ clearance sweeps, decoupling and track rules, one model pass",
    ),
    Effort.DEEP: EffortProfile(
        level=Effort.DEEP,
        groups=_DEEP_GROUPS,
        judgment_passes=2,
        refute_rounds=1,
        per_part_focus=True,
        min_severity=Severity.NOTE,
        clearance_nm=int(DEFAULT_CLEARANCE_NM * 1.2),
        decoupling_max_nm=mm(2.0),
        edge_margin_nm=mm(0.8),
        description=(
            "+ manufacturing rules, tighter thresholds, per-part model passes, "
            "and every model finding must survive refutation"
        ),
    ),
}


def profile_for(level: Effort | str) -> EffortProfile:
    return PROFILES[Effort(str(level))]


def slider(level: Effort | str, width: int = 3) -> str:
    """The slider itself, for a terminal: ``quick ○───●───○ deep``."""
    order = [Effort.QUICK, Effort.STANDARD, Effort.DEEP]
    here = Effort(str(level))
    dashes = "─" * width
    track = dashes.join("●" if lvl is here else "○" for lvl in order)
    return f"quick {track} deep"

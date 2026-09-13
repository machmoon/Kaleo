"""What a review finding is, and the one distinction that matters about it.

Every finding carries an :class:`Origin`. ``PROVEN`` means a deterministic
check measured it in the board file and the measurement is in ``evidence``;
``SUGGESTED`` means a model argued for it. The two are never merged into an
undifferentiated list, because their trustworthiness is not comparable: a
proven finding is a fact about the geometry, a suggested one is an opinion
about the design. The renderer and the report both keep them visually
distinct, and nothing in this package lets a model delete, downgrade or
reword a proven finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..units import NM_PER_MM
from .geometry import Rect

__all__ = ["Severity", "Origin", "Finding", "SEVERITY_ORDER", "fmt_mm"]


class Severity(StrEnum):
    #: The board will not work as built.
    BLOCKER = "blocker"
    #: It may work, but violates a rule of thumb or a datasheet recommendation.
    MARGINAL = "marginal"
    #: Correct, but worth knowing.
    NOTE = "note"


class Origin(StrEnum):
    #: Measured by a deterministic checker in this package.
    PROVEN = "proven"
    #: Argued by a model. Plausible, not verified.
    SUGGESTED = "suggested"


SEVERITY_ORDER = {Severity.BLOCKER: 0, Severity.MARGINAL: 1, Severity.NOTE: 2}


def fmt_mm(value_nm: float) -> str:
    return f"{value_nm / NM_PER_MM:.3f} mm"


@dataclass
class Finding:
    """One problem, located on the board wherever that is possible."""

    rule: str
    severity: Severity
    origin: Origin
    title: str
    detail: str = ""
    refs: tuple[str, ...] = ()
    nets: tuple[str, ...] = ()
    #: Where it is. ``extent`` is the region to outline; ``point`` is where the
    #: marker goes. A finding with neither is rendered in the list marked "not
    #: located" rather than quietly dropped or pinned to an invented spot.
    extent: Rect | None = None
    point: tuple[int, int] | None = None
    #: The measurement that proves it, for a proven finding: "gap 0.08 mm,
    #: clearance 0.25 mm". Empty for suggested findings by construction.
    evidence: str = ""
    fix: str = ""
    #: Which rule module or model pass produced it.
    source: str = ""
    #: Assigned at report time: F1, F2... shared by the SVG badge and the list.
    id: str = ""
    #: Deep-effort refutation trail for suggested findings.
    checks: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.point is None and self.extent is not None:
            self.point = self.extent.centre

    @property
    def located(self) -> bool:
        return self.point is not None

    @property
    def where(self) -> str:
        parts = list(self.refs) + [f"net {n}" for n in self.nets]
        return ", ".join(parts)

    def __str__(self) -> str:
        where = f" [{self.where}]" if self.where else ""
        mark = "proven" if self.origin is Origin.PROVEN else "suggested"
        return f"{self.severity.value.upper()}{where}: {self.title} ({mark})"


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Most severe first; proven ahead of suggested at equal severity."""
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_ORDER[f.severity],
            0 if f.origin is Origin.PROVEN else 1,
            f.rule,
            f.where,
        ),
    )

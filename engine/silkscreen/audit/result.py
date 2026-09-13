"""What one review produced, including what it did not do.

The honesty of this object is the point: ``rules_run`` says which checks
actually ran, ``model_passes`` says what thinking happened, and
``skipped_reason`` says why the model half is missing when it is. A report
that shows findings without showing coverage lets an empty list read as a
clean board when it may only mean a check never ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .effort import EffortProfile
from .findings import Finding, Origin, Severity, sort_findings
from .geometry import AuditBoard

__all__ = ["AuditResult"]


@dataclass
class AuditResult:
    board: AuditBoard
    profile: EffortProfile
    findings: list[Finding] = field(default_factory=list)
    rules_run: list[str] = field(default_factory=list)
    model_passes: list[str] = field(default_factory=list)
    #: Why the model half did not run, empty when it did.
    skipped_reason: str = ""
    elapsed_s: float = 0.0
    source: Path | None = None

    def __post_init__(self) -> None:
        self.findings = sort_findings(self.findings)
        for index, finding in enumerate(self.findings, start=1):
            finding.id = f"F{index}"

    @property
    def proven(self) -> list[Finding]:
        return [f for f in self.findings if f.origin is Origin.PROVEN]

    @property
    def suggested(self) -> list[Finding]:
        return [f for f in self.findings if f.origin is Origin.SUGGESTED]

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    def visible(self) -> list[Finding]:
        """Findings at or above the profile's severity floor."""
        floor = {Severity.BLOCKER: 0, Severity.MARGINAL: 1, Severity.NOTE: 2}[
            self.profile.min_severity
        ]
        order = {Severity.BLOCKER: 0, Severity.MARGINAL: 1, Severity.NOTE: 2}
        return [f for f in self.findings if order[f.severity] <= floor]

    def counts(self) -> dict[str, int]:
        return {
            "blocker": sum(1 for f in self.findings if f.severity is Severity.BLOCKER),
            "marginal": sum(
                1 for f in self.findings if f.severity is Severity.MARGINAL
            ),
            "note": sum(1 for f in self.findings if f.severity is Severity.NOTE),
            "proven": len(self.proven),
            "suggested": len(self.suggested),
        }

    def headline(self) -> str:
        counts = self.counts()
        if not self.findings:
            return (
                f"{len(self.rules_run)} checks ran and found nothing. "
                "That is a clean bill only for what was checked."
            )
        return (
            f"{counts['blocker']} blocker(s), {counts['marginal']} marginal, "
            f"{counts['note']} note(s) — {counts['proven']} proven by "
            f"measurement, {counts['suggested']} suggested by the model"
        )

    def to_dict(self) -> dict:
        return {
            "source": str(self.source) if self.source else None,
            "effort": self.profile.level.value,
            "elapsed_s": round(self.elapsed_s, 3),
            "rules_run": list(self.rules_run),
            "model_passes": list(self.model_passes),
            "skipped_reason": self.skipped_reason,
            "counts": self.counts(),
            "findings": [
                {
                    "id": f.id,
                    "rule": f.rule,
                    "severity": f.severity.value,
                    "origin": f.origin.value,
                    "title": f.title,
                    "detail": f.detail,
                    "refs": list(f.refs),
                    "nets": list(f.nets),
                    "evidence": f.evidence,
                    "fix": f.fix,
                    "source": f.source,
                    "checks": list(f.checks),
                    "location_nm": list(f.point) if f.point else None,
                }
                for f in self.findings
            ],
        }

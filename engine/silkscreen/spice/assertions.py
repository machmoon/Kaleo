"""Specifications as assertions, and the verdict of checking them.

This is the point of the package. DRC answers "is this manufacturable"; nothing
in the design loop answered "does this behave". An :class:`Assertion` is one
clause of a specification -- *ripple under 50 mV*, *rise time under 1 µs*, *no
node above 3.6 V* -- and :func:`check_all` turns a simulation into a pass/fail
verdict over a whole specification.

The shape is chosen so a caller in a loop can act on the answer without parsing
prose: each :class:`AssertionOutcome` carries the number that was measured
alongside the bound it was compared against, so "failed" always comes with "by
how much".

A measurement that cannot be taken is a :class:`AssertionOutcome` with
``error`` set and ``passed`` False, never a silent pass. A specification clause
whose measurement is undefined has not been satisfied.
"""

from __future__ import annotations

import math
import operator
from dataclasses import dataclass, field

from .errors import MeasurementError
from .measure import Measurement, measure
from .result import SimulationResult

__all__ = ["Assertion", "AssertionOutcome", "VerificationReport", "check", "check_all"]

_OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass(frozen=True)
class Assertion:
    """One clause of a specification.

    ``op`` is a comparison against ``value``; ``"within"`` is the exception,
    meaning ``|measured - value| <= tolerance`` where the tolerance is
    fractional unless :attr:`absolute_tolerance` is set.
    """

    name: str
    measurement: Measurement
    op: str
    value: float
    tolerance: float = 0.0
    absolute_tolerance: bool = False
    unit: str = ""

    def describe(self) -> str:
        if self.op == "within":
            band = (
                f"±{self.tolerance:g}{self.unit}"
                if self.absolute_tolerance
                else f"±{self.tolerance * 100:g}%"
            )
            return f"{self.measurement.describe()} == {self.value:g}{self.unit} {band}"
        return f"{self.measurement.describe()} {self.op} {self.value:g}{self.unit}"


@dataclass(frozen=True)
class AssertionOutcome:
    """The verdict on one clause, with the number behind it."""

    name: str
    passed: bool
    measured: float | None
    expected: float
    op: str
    unit: str = ""
    description: str = ""
    error: str | None = None

    @property
    def margin(self) -> float | None:
        """Signed distance from the bound, positive when the clause holds.

        Lets a caller rank near-misses ahead of comfortable passes without
        re-deriving anything.
        """
        if self.measured is None:
            return None
        if self.op in ("<", "<="):
            return self.expected - self.measured
        if self.op in (">", ">="):
            return self.measured - self.expected
        return -abs(self.measured - self.expected)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "measured": self.measured,
            "expected": self.expected,
            "op": self.op,
            "unit": self.unit,
            "margin": self.margin,
            "description": self.description,
            "error": self.error,
        }


@dataclass(frozen=True)
class VerificationReport:
    """Every clause's verdict, plus the run they were taken from."""

    passed: bool
    outcomes: tuple[AssertionOutcome, ...]
    result: SimulationResult | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def failures(self) -> tuple[AssertionOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.passed)

    def summary(self) -> str:
        total = len(self.outcomes)
        failed = len(self.failures)
        head = (
            f"{total - failed}/{total} assertions passed"
            if total
            else "no assertions checked"
        )
        if not failed:
            return head
        lines = [head]
        for outcome in self.failures:
            if outcome.error:
                lines.append(f"  FAIL {outcome.name}: {outcome.error}")
            else:
                lines.append(
                    f"  FAIL {outcome.name}: measured "
                    f"{outcome.measured:g}{outcome.unit}, expected "
                    f"{outcome.op} {outcome.expected:g}{outcome.unit}"
                )
        return "\n".join(lines)

    def to_dict(self, *, max_points: int = 0) -> dict:
        return {
            "passed": self.passed,
            "assertions": [o.to_dict() for o in self.outcomes],
            "warnings": list(self.warnings),
            "result": (
                self.result.to_dict(max_points=max_points) if self.result else None
            ),
        }


def check(result: SimulationResult, assertion: Assertion) -> AssertionOutcome:
    """Evaluate one clause against a result."""
    try:
        measured = measure(result, assertion.measurement)
    except MeasurementError as exc:
        return AssertionOutcome(
            name=assertion.name,
            passed=False,
            measured=None,
            expected=assertion.value,
            op=assertion.op,
            unit=assertion.unit,
            description=assertion.describe(),
            error=str(exc),
        )

    if not math.isfinite(measured):
        return AssertionOutcome(
            name=assertion.name,
            passed=False,
            measured=None,
            expected=assertion.value,
            op=assertion.op,
            unit=assertion.unit,
            description=assertion.describe(),
            error=f"measurement is {measured}, not a finite number",
        )

    if assertion.op == "within":
        band = (
            assertion.tolerance
            if assertion.absolute_tolerance
            else abs(assertion.value) * assertion.tolerance
        )
        passed = abs(measured - assertion.value) <= band
    else:
        comparator = _OPS.get(assertion.op)
        if comparator is None:
            return AssertionOutcome(
                name=assertion.name,
                passed=False,
                measured=measured,
                expected=assertion.value,
                op=assertion.op,
                unit=assertion.unit,
                description=assertion.describe(),
                error=f"unknown operator {assertion.op!r}; "
                f"known: {sorted(_OPS)} and 'within'",
            )
        passed = bool(comparator(measured, assertion.value))

    return AssertionOutcome(
        name=assertion.name,
        passed=passed,
        measured=measured,
        expected=assertion.value,
        op=assertion.op,
        unit=assertion.unit,
        description=assertion.describe(),
    )


def check_all(
    result: SimulationResult, assertions: list[Assertion]
) -> VerificationReport:
    """Evaluate a whole specification. Passes only if every clause passes."""
    outcomes = tuple(check(result, a) for a in assertions)
    return VerificationReport(
        passed=all(o.passed for o in outcomes),
        outcomes=outcomes,
        result=result,
        warnings=result.warnings,
    )

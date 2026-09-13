"""What a simulation returns: named waveforms, in the caller's own net names.

The deck sanitises net names into SPICE-legal node names, so the simulator talks
about ``v(vout_1)`` when the circuit called the net ``VOUT+``. Undoing that here
means a caller writes and reads one vocabulary -- its own -- and never has to
know the mapping existed.

Lookup is deliberately forgiving about form (``VOUT``, ``v(VOUT)``, ``V(vout)``
all resolve) and deliberately unforgiving about absence: an unknown signal
raises :class:`~silkscreen.spice.errors.MeasurementError` listing what *is*
available. A missing probe returning an empty array is the specific failure this
package exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import MeasurementError

__all__ = ["SimulationResult"]


def _canonical(name: str) -> str:
    return name.strip().lower().replace(" ", "")


@dataclass(frozen=True)
class SimulationResult:
    """The outcome of one successful simulator run.

    There is no "failed" variant of this class on purpose: a failed run raises.
    If you are holding one of these, the simulator converged and produced data.
    """

    #: ``"op"``, ``"tran"``, ``"ac"`` or ``"dc"``.
    analysis: str
    #: Name of the independent variable (``"time"``, ``"frequency"``, ...).
    sweep_name: str
    #: The independent variable's values.
    sweep: tuple[float, ...]
    #: ``{display name: values}`` -- display names use the circuit's net names.
    signals: dict[str, tuple[float, ...] | tuple[complex, ...]]
    #: Non-fatal problems from deck construction and from the simulator.
    warnings: tuple[str, ...] = ()
    #: The exact deck that was run, for reproducing the result by hand.
    deck: str = ""
    #: The simulator's own stdout/stderr.
    log: str = ""
    #: Which simulator produced this.
    simulator: str = ""
    _aliases: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def complex_data(self) -> bool:
        return any(
            values and isinstance(values[0], complex)
            for values in self.signals.values()
        )

    def names(self) -> list[str]:
        """Every signal available, in a stable order."""
        return sorted(self.signals)

    def signal(self, name: str) -> tuple[float, ...] | tuple[complex, ...]:
        """Values for one signal, accepting ``VOUT``, ``v(VOUT)`` or ``V(vout)``."""
        key = _canonical(name)
        for candidate in (key, f"v({key})", key.removeprefix("v(").removesuffix(")")):
            resolved = self._aliases.get(candidate)
            if resolved is not None:
                return self.signals[resolved]
        raise MeasurementError(
            f"no signal named {name!r} in this {self.analysis} result. "
            f"Available: {self.names()}"
        )

    def has_signal(self, name: str) -> bool:
        try:
            self.signal(name)
        except MeasurementError:
            return False
        return True

    def magnitude(self, name: str) -> tuple[float, ...]:
        """A signal as real numbers.

        Complex AC data becomes its magnitude; real data is returned **signed**.
        Taking ``abs`` of a transient would turn a negative rail into a positive
        one and make ``min`` report the wrong number -- a node sitting at -5 V
        would measure as +5 V and pass an absolute-maximum check it violates.
        """
        values = self.signal(name)
        return tuple(abs(v) if isinstance(v, complex) else float(v) for v in values)

    def to_dict(self, *, max_points: int = 0) -> dict:
        """A JSON-safe summary, for handing across a tool boundary.

        ``max_points`` of 0 omits the waveforms entirely and keeps only their
        extremes -- which is what an agent asking "did it pass" wants, and
        avoids shipping ten thousand points through a tool response.
        """
        summary: dict[str, dict] = {}
        for name, values in self.signals.items():
            real = [abs(v) if isinstance(v, complex) else float(v) for v in values]
            entry: dict = {
                "min": min(real) if real else None,
                "max": max(real) if real else None,
                "final": real[-1] if real else None,
                "points": len(real),
            }
            if max_points and real:
                stride = max(1, len(real) // max_points)
                entry["values"] = real[::stride][:max_points]
            summary[name] = entry
        return {
            "analysis": self.analysis,
            "simulator": self.simulator,
            "sweep_name": self.sweep_name,
            "sweep_start": self.sweep[0] if self.sweep else None,
            "sweep_end": self.sweep[-1] if self.sweep else None,
            "points": len(self.sweep),
            "signals": summary,
            "warnings": list(self.warnings),
        }


def build_result(
    *,
    analysis: str,
    sweep_name: str,
    variables: tuple[str, ...],
    data: dict,
    net_of_node: dict[str, str],
    warnings: tuple[str, ...],
    deck: str,
    log: str,
    simulator: str,
) -> SimulationResult:
    """Relabel a raw plot into circuit vocabulary and index it for lookup.

    Kept out of :class:`SimulationResult` so the dataclass stays a plain value
    object that a test can construct without a simulator anywhere near it.
    """
    signals: dict[str, tuple] = {}
    aliases: dict[str, str] = {}
    sweep: tuple[float, ...] = ()

    for index, raw_name in enumerate(variables):
        values = data[raw_name]
        lowered = raw_name.strip().lower()

        display = raw_name
        inner = None
        if lowered.startswith("v(") and lowered.endswith(")"):
            inner = raw_name.strip()[2:-1]
            net = net_of_node.get(inner) or net_of_node.get(inner.lower())
            if net is None:
                # ngspice lowercases node names; match case-insensitively.
                for node, candidate in net_of_node.items():
                    if node.lower() == inner.lower():
                        net = candidate
                        break
            if net is not None:
                display = f"v({net})"

        if index == 0:
            sweep = tuple(v.real if isinstance(v, complex) else v for v in values)

        signals[display] = values
        for alias in {
            _canonical(display),
            _canonical(raw_name),
            _canonical(display).removeprefix("v(").removesuffix(")"),
            _canonical(raw_name).removeprefix("v(").removesuffix(")"),
        }:
            aliases.setdefault(alias, display)
        if inner is not None:
            aliases.setdefault(_canonical(inner), display)

    return SimulationResult(
        analysis=analysis,
        sweep_name=sweep_name,
        sweep=sweep,
        signals=signals,
        warnings=warnings,
        deck=deck,
        log=log,
        simulator=simulator,
        _aliases=aliases,
    )

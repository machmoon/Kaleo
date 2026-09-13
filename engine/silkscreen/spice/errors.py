"""Every way simulation can fail, as a distinct exception.

The design rule for this whole package: **an agent that gets a quiet zero will
confidently conclude the circuit is fine.** A simulator that cannot converge, a
part with no model, a probe on a node that does not exist -- each of those must
raise something specific and self-describing, never return an empty result.

So there is no "simulation returned nothing" path anywhere in this package.
Either :class:`~silkscreen.spice.result.SimulationResult` holds real numbers, or
one of these was raised.
"""

from __future__ import annotations

__all__ = [
    "SpiceError",
    "ValueSyntaxError",
    "DeckError",
    "UnsimulatableError",
    "SimulatorNotFound",
    "SimulationFailed",
    "ConvergenceError",
    "RawParseError",
    "MeasurementError",
]


class SpiceError(Exception):
    """Base for every failure in the simulation path."""


class ValueSyntaxError(SpiceError):
    """A component value is not a number SPICE could read.

    Carries the offending part so a repair prompt can name it.
    """

    def __init__(self, part: str, value: str, detail: str = ""):
        self.part = part
        self.value = value
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{part}: cannot read value {value!r} as a number{suffix}")


class DeckError(SpiceError):
    """The testbench and the circuit do not agree.

    A source wired to a net the circuit does not have, a probe on an unknown
    node, no ground reference -- problems that would otherwise produce a deck
    that simulates happily and answers a question nobody asked.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) building the SPICE deck:\n  - "
            + "\n  - ".join(errors)
        )


class UnsimulatableError(SpiceError):
    """A part in the circuit has no SPICE behaviour available.

    This is the honest edge of the translation: an integrated circuit is a name
    and a pin map in the Silkscreen IR, with no model attached, and no amount of
    netlist generation can invent one. The caller must supply a subcircuit (see
    :class:`~silkscreen.spice.deck.SubcircuitModel`) or accept that this circuit
    cannot be verified by simulation.
    """

    def __init__(self, parts: list[str], reason: str):
        self.parts = parts
        self.reason = reason
        super().__init__(f"cannot simulate {', '.join(sorted(parts))}: {reason}")


class SimulatorNotFound(SpiceError):
    """No SPICE binary could be located.

    Lists what was looked for, because "install a simulator" is not actionable
    on its own.
    """

    def __init__(self, tried: list[str]):
        self.tried = tried
        super().__init__(
            "no SPICE simulator found. Tried: "
            + ", ".join(tried)
            + ". Install ngspice (brew install ngspice / apt-get install ngspice), "
            "or set SILKSCREEN_SPICE to the simulator executable."
        )


class SimulationFailed(SpiceError):
    """The simulator ran and reported an error.

    ``log`` is the simulator's own output, kept whole: the reason a run failed
    is almost always in it verbatim, and paraphrasing it loses the node names.
    """

    def __init__(self, message: str, *, log: str = "", deck: str = ""):
        self.log = log
        self.deck = deck
        detail = f"\n--- simulator output ---\n{log.strip()}" if log.strip() else ""
        super().__init__(f"{message}{detail}")


class ConvergenceError(SimulationFailed):
    """The solver failed to converge.

    Split out from :class:`SimulationFailed` because it means something
    different to a caller in a loop: the circuit may be fine and the analysis
    settings wrong, so retrying with a smaller timestep is a sensible next move,
    whereas a missing model is not retryable.
    """


class RawParseError(SpiceError):
    """The simulator produced output this package could not read."""


class MeasurementError(SpiceError):
    """A measurement could not be taken from a result.

    Asking for the rise time of a signal that never rises, or a node that was
    not probed, is a question with no answer -- and returning 0.0 for it would
    be the exact failure this package exists to prevent.
    """

"""The two calls an agent makes: :func:`simulate` and :func:`verify`.

Everything else in this package is machinery these two assemble. The split is
deliberate:

* :func:`simulate` answers *what does it do* -- waveforms, in the circuit's own
  net names.
* :func:`verify` answers *does it meet the spec* -- a pass/fail verdict with the
  measured number beside every clause.

The second is the one worth having. An agent that can call it in a loop has the
thing hardware design has been missing: a verifier for behaviour, in the same
position a test suite occupies for software. It returns a
:class:`~silkscreen.spice.assertions.VerificationReport` and never a bare
boolean, because "it failed" without "by how much, on which clause" is not
actionable feedback for the next iteration.
"""

from __future__ import annotations

from ..netlist import CircuitSpec
from .assertions import Assertion, VerificationReport, check_all
from .deck import SpiceDeck, Testbench, build_deck
from .result import SimulationResult, build_result
from .simulators import DEFAULT_TIMEOUT_S, Simulator, find_simulator

__all__ = ["simulate", "verify", "simulate_deck"]


def simulate_deck(
    deck: SpiceDeck,
    *,
    simulator: Simulator | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SimulationResult:
    """Run an already-built deck.

    Separate from :func:`simulate` so a caller can inspect or hand-edit the deck
    -- reproducing a result outside this package is a normal thing to want, and
    :attr:`SpiceDeck.text` plus this function is the whole path.
    """
    # ``Simulator`` is a structural protocol, so anything with ``run`` is one --
    # which is what lets a test substitute a stub without importing ngspice.
    if simulator is None or isinstance(simulator, str):
        engine: Simulator = find_simulator(simulator)
    else:
        engine = simulator
    outcome = engine.run(deck, timeout_s=timeout_s)

    plot = outcome.plot
    if not plot.variables:
        # Defensive: the raw parser raises on this, but a stubbed simulator in a
        # test could not, and a zero-signal result is exactly the silent success
        # this package refuses to produce.
        from .errors import SimulationFailed

        raise SimulationFailed(
            "the simulator returned a result with no variables in it",
            log=outcome.log,
            deck=outcome.deck_text,
        )

    return build_result(
        analysis=deck.analysis.kind,
        sweep_name=plot.variables[0],
        variables=plot.variables,
        data=plot.data,
        net_of_node=deck.net_of_node,
        warnings=tuple(deck.warnings) + tuple(outcome.warnings),
        deck=outcome.deck_text,
        log=outcome.log,
        simulator=outcome.simulator,
    )


def simulate(
    spec: CircuitSpec,
    bench: Testbench,
    *,
    simulator: Simulator | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SimulationResult:
    """Build a deck from a circuit and testbench, run it, return the waveforms.

    Raises rather than returning an empty result on every failure path: see
    :mod:`silkscreen.spice.errors`.
    """
    deck = build_deck(spec, bench)
    return simulate_deck(deck, simulator=simulator, timeout_s=timeout_s)


def verify(
    spec: CircuitSpec,
    bench: Testbench,
    assertions: list[Assertion],
    *,
    simulator: Simulator | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> VerificationReport:
    """Simulate a circuit and check it against a specification.

    A failed *assertion* comes back as a report with ``passed`` False. A failed
    *simulation* raises -- the distinction matters to a caller in a loop, since
    the first says the design is wrong and the second says the question was.
    """
    result = simulate(spec, bench, simulator=simulator, timeout_s=timeout_s)
    return check_all(result, assertions)

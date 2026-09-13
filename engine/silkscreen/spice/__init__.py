"""SPICE simulation as a verifier the design loop can call.

DRC answers whether a board can be *made*. Nothing in this repo answered whether
the circuit *works*, and that gap is the reason a design agent cannot close its
own loop: it can produce a plausible schematic and a manufacturable board with no
evidence that the thing does what it was asked for. Simulation is the nearest
thing hardware has to a unit test, so this package is shaped like a test runner
rather than like a waveform viewer.

The whole surface is two calls::

    from silkscreen.spice import (
        Assertion, Measurement, Source, Testbench, Transient, verify,
    )

    bench = Testbench(
        analysis=Transient(step=1e-7, stop=2e-3),
        sources=[Source.pulse("V1", "VIN", "GND", initial=0, pulsed=5,
                              width=1e-3, period=2e-3)],
    )
    report = verify(spec, bench, [
        Assertion(
            name="output settles to 5 V",
            measurement=Measurement(kind="final", signal="VOUT"),
            op="within", value=5.0, tolerance=0.02, unit="V",
        ),
    ])
    report.passed        # -> bool
    report.summary()     # -> which clause failed, and by how much

Design rules, all of which exist because the alternative is an agent confidently
concluding a broken circuit is fine:

* **Nothing returns a quiet zero.** Every failure -- no simulator, no model, a
  probe on a node that does not exist, a solver that will not converge -- raises
  a specific exception from :mod:`silkscreen.spice.errors`.
* **A warning is data, not noise.** ngspice exits zero on a singular matrix and
  writes a well-formed rawfile; that run is surfaced with its warnings attached,
  and ``Testbench(strict=True)`` turns them into errors.
* **The simulator is an implementation detail.** ngspice and LTspice sit behind
  one interface; ngspice is what runs in CI and is what this package is verified
  against. See :class:`~silkscreen.spice.simulators.LTspiceSimulator` for the
  precise state of the LTspice path.
* **The IR's edge is stated, not hidden.** An integrated circuit in the
  Silkscreen IR is a pin map with no behaviour. Simulating one requires a
  :class:`~silkscreen.spice.deck.SubcircuitModel`, and without one the run
  raises rather than silently leaving the part out.
* **Where a model comes from is part of the answer.**
  :mod:`silkscreen.spice.registry` is the only source of device behaviour on
  the agent path -- generic stand-ins this repository wrote, or model files
  the operator installed -- and never text a language model produced, because
  an invented ``.SUBCKT`` yields a simulation that *passes*. A generic
  stand-in is marked as one on the model, in the deck's warnings and in every
  result, since "the LM358 result" and "a generic op-amp's result" support
  different claims.
"""

from .assertions import (
    Assertion,
    AssertionOutcome,
    VerificationReport,
    check,
    check_all,
)
from .deck import (
    ACSweep,
    Analysis,
    DCSweep,
    OperatingPoint,
    PrimitiveModel,
    Source,
    SpiceDeck,
    SubcircuitModel,
    Testbench,
    Transient,
    build_deck,
)
from .errors import (
    ConvergenceError,
    DeckError,
    MeasurementError,
    RawParseError,
    SimulationFailed,
    SimulatorNotFound,
    SpiceError,
    UnsimulatableError,
    ValueSyntaxError,
)
from .library import ModelProvenance
from .measure import MEASUREMENT_KINDS, Measurement, measure
from .raw import RawPlot, parse_rawfile
from .registry import (
    MODEL_PATH_ENV,
    DeviceModel,
    LoadedSubckt,
    ModelRegistry,
    Resolution,
    default_registry,
    load_model_files,
    model_search_paths,
)
from .result import SimulationResult
from .run import simulate, simulate_deck, verify
from .simulators import (
    LTspiceSimulator,
    NgspiceSimulator,
    Simulator,
    available_simulators,
    find_simulator,
)
from .spec import (
    REQUEST_SCHEMA,
    assertions_from_dict,
    testbench_from_dict,
)
from .values import format_value, parse_value

__all__ = [
    # top level
    "simulate",
    "simulate_deck",
    "verify",
    # testbench
    "Testbench",
    "Source",
    "Analysis",
    "OperatingPoint",
    "Transient",
    "ACSweep",
    "DCSweep",
    "PrimitiveModel",
    "SubcircuitModel",
    "SpiceDeck",
    # model registry
    "ModelRegistry",
    "ModelProvenance",
    "DeviceModel",
    "Resolution",
    "LoadedSubckt",
    "default_registry",
    "load_model_files",
    "model_search_paths",
    "MODEL_PATH_ENV",
    "build_deck",
    # measuring and asserting
    "Measurement",
    "MEASUREMENT_KINDS",
    "measure",
    "Assertion",
    "AssertionOutcome",
    "VerificationReport",
    "check",
    "check_all",
    # results
    "SimulationResult",
    "RawPlot",
    "parse_rawfile",
    # simulators
    "Simulator",
    "NgspiceSimulator",
    "LTspiceSimulator",
    "find_simulator",
    "available_simulators",
    # JSON bridge, for tool callers
    "testbench_from_dict",
    "assertions_from_dict",
    "REQUEST_SCHEMA",
    # values
    "parse_value",
    "format_value",
    # errors
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

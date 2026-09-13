"""Measure whether this pipeline actually designs *correct* circuits.

    python scripts/design_quality.py --trials 3
    python scripts/design_quality.py --trials 3 --plan          # with plan.py on
    python scripts/design_quality.py --offline-selftest         # no key, no cost

Every other quality gate in this repository checks something downstream of the
question this script asks. DRC checks geometry, the enclosure kernel checks the
case, the critic asks a model for an opinion. None of them can tell a 3.3 V
supply from a 3.3 V supply wired backwards. Since ``spice/registry.py`` landed
there is a trusted, licence-clean source of device behaviour for op-amps, the
555, fixed regulators and connectors -- so for the first time the *proposal*
stage can be scored against a specification written in volts and hertz.

**This costs money and needs ``GOOGLE_API_KEY``**, so it lives in ``scripts/``
and pytest never collects it -- the ``scripts/simulate_demo.py`` convention.

How the measurement is built
----------------------------

For each prompt in :data:`CASES` the script runs
:func:`silkscreen.agents.propose.propose_circuit` for real, ``--trials`` times,
and records three separate things that are deliberately never folded into one
number:

1. **Did it validate at all**, and after how many repair rounds. This is what
   the repo already knew how to measure.
2. **Structural compliance**: did the answer contain the part that was asked
   for, obey the prompt's explicit prohibitions ("do not add connectors"), give
   every supply pin a decoupling capacitor, and provide a power entry.
3. **Does the circuit work**, by simulation, against clauses written from the
   *English request* rather than from the circuit's own component values. "The
   rail is 3.3 V under a 10 mA load" is a restatement of what the user asked
   for; "the period is ln2*(R1+2*R2)*C" would be a restatement of what the
   model happened to choose, and would pass on a blinker that blinks once an
   hour. Every clause here is of the first kind.

The oracle finds its probe points **structurally**, never by guessing net
names: ``spice/registry.py`` binds each device's pins to its subcircuit's
terminals, so ``model.pins`` says which of the model's pin names is VOUT, which
is VCC and which is IN-. A circuit that calls its rail ``VOUT_3V3`` instead of
``+3V3`` is scored on its electrons, not its vocabulary.

Load resistors and supplies are added by this script, not expected from the
model: the IR says nothing about what drives a board (``spice/deck.py``'s stated
boundary, "a circuit is not a testbench"), so the fixture belongs to the bench.

What this does NOT prove is written out in ``docs/design-quality.md`` and
printed at the end of every run. Read it before quoting a number from here.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from silkscreen.netlist import (  # noqa: E402
    CircuitSpec,
    Connection,
    Passive,
    PassiveType,
    parse_circuit_spec,
)
from silkscreen.spice import (  # noqa: E402
    Assertion,
    Measurement,
    Source,
    Testbench,
    Transient,
    find_simulator,
    verify,
)
from silkscreen.spice.errors import SimulatorNotFound, SpiceError  # noqa: E402
from silkscreen.spice.registry import Resolution, default_registry  # noqa: E402

# ==========================================================================
# Finding the probe points, structurally
# ==========================================================================


def resolve(spec: CircuitSpec) -> Resolution:
    """What the trusted registry covers for this circuit.

    ``default_registry()`` reads ``SILKSCREEN_SPICE_MODELS`` like the agents
    layer does, so an operator model file changes this measurement the same way
    it changes a real run. That is deliberate: the harness must score the
    machine the operator has, not an idealised one.
    """
    return default_registry().resolve(spec)


def device_by_model_prefix(
    spec: CircuitSpec, resolution: Resolution, prefix: str
) -> str | None:
    """The one spec part whose registry model is of this family, or None.

    ``SS_LDO_``/``SS_555_``/``SS_OPAMP_``/``SS_PORT_`` are the subcircuit name
    prefixes ``registry._subckt_name`` writes. Two matches return None rather
    than picking one -- an ambiguous board is not a board this oracle can
    score, and it says so instead of scoring the wrong part.
    """
    hits = [
        part
        for part, model in resolution.models.items()
        if model.name.startswith(prefix)
    ]
    return hits[0] if len(hits) == 1 else None


def net_for_terminal(
    spec: CircuitSpec, resolution: Resolution, part: str, index: int
) -> str | None:
    """The net on the ``index``-th subcircuit terminal of ``part``.

    ``SubcircuitModel.pins`` holds the *device pin names* in the subcircuit's
    terminal order (``registry.ModelRegistry._regulator`` and friends build it
    that way, and ``test_spice.py`` pins it: the AMS1117 binds ``GND, VOUT,
    VIN``). So terminal order plus ``CircuitSpec.nets_of`` gives a probe point
    without ever guessing a net name.
    """
    model = resolution.models.get(part)
    if model is None or index >= len(model.pins):
        return None
    return spec.nets_of(part).get(model.pins[index])


# Terminal orders, from the docstrings in ``spice/library.py``.
LDO_GND, LDO_VOUT, LDO_VIN = 0, 1, 2
T555_GND, T555_TRIG, T555_OUT, T555_RESET = 0, 1, 2, 3
T555_CTRL, T555_THRES, T555_DISCH, T555_VCC = 4, 5, 6, 7


def opamp_sections(resolution: Resolution, part: str) -> int:
    """How many amplifier sections the bound op-amp model has.

    ``opamp_subckt`` emits ``IN+ IN- OUT`` per section then ``V+ V-``.
    """
    model = resolution.models.get(part)
    if model is None:
        return 0
    return (len(model.pins) - 2) // 3


# ==========================================================================
# Bench fixtures the IR cannot carry
# ==========================================================================


def add_load(spec: CircuitSpec, name: str, ohms: float, hot: str, cold: str) -> None:
    """Hang a resistor between two existing nets, in place.

    The circuit IR says nothing about what a board drives, so a load is part of
    the testbench, not of the design. ``Connection`` is frozen, so the joined
    connections are rebuilt rather than mutated.
    """
    spec.passives.append(
        Passive(name=name, type=PassiveType.RESISTOR, value=f"{ohms:g}")
    )
    rebuilt: list[Connection] = []
    for conn in spec.connections:
        if conn.net == hot:
            conn = Connection(conn.net, conn.endpoints + (f"{name}.1",))
        elif conn.net == cold:
            conn = Connection(conn.net, conn.endpoints + (f"{name}.2",))
        rebuilt.append(conn)
    spec.connections = rebuilt


def settled_dc(stop: float = 0.1, max_step: float = 1e-5) -> Transient:
    """A transient run long enough to settle, sampled only at the end.

    Deliberately **not** ``OperatingPoint``. ngspice's DC operating point on
    the op-amp stand-in with a direct unity-gain feedback loop reports ``gmin
    stepping failed`` / ``source stepping failed`` and then writes a
    well-formed rawfile holding a non-solution -- a 2.5 V follower came back at
    0.053 V, self-inconsistent with its own inputs. This is the exact failure
    ``docs/spice-models.md`` names as ngspice's counter-example behaviour
    ("exits zero on a singular matrix"), and ``verify()`` does not refuse on
    it, so an ``.op`` verdict here would have scored a correct circuit as
    broken. A transient run reaches the same answer and converges.
    """
    return Transient(
        step=stop / 1000.0, stop=stop, start=stop * 0.9, max_step=max_step
    )


def bench(
    spec: CircuitSpec,
    resolution: Resolution,
    analysis: Any,
    sources: list[Source],
) -> Testbench:
    """A strict testbench carrying everything the registry resolved.

    ``strict=True`` with every generic stand-in *declared* is the shape
    ``agents/simulate.py`` uses and the shape ``docs/spice-models.md`` argues
    for: the substitution is disclosed in the warnings either way, and naming
    it is what stops strict refusing the run.

    One exception, and it is a gap in ``spice/deck.py`` rather than a choice
    made here: ``accept_generic_models`` is only consulted by ``_note_generic``,
    which runs on *device* subcircuits. The generic-diode warning at
    ``deck.py:805`` is appended straight to ``warnings`` and never reaches
    ``acknowledged``, so naming a diode in ``accept_generic_models`` does not
    stop strict refusing -- there is no way to declare a diode substitution.
    Every LED blinker prompt therefore refuses outright under strict. Rather
    than pretend the LED's part number matters to whether the timer oscillates,
    a circuit carrying an unmodelled diode drops to ``strict=False`` and the
    substitution warnings are carried onto :attr:`Verdict.warnings` verbatim,
    so the disclosure survives even though the refusal does not.
    """
    generic = list(resolution.generic_parts)
    unmodelled_diodes = [
        p.name
        for p in spec.passives
        if p.type in (PassiveType.DIODE, PassiveType.CRYSTAL)
    ]
    return Testbench(
        analysis=analysis,
        sources=sources,
        models=resolution.models,
        accept_generic_models=tuple(sorted(set(generic))),
        strict=not unmodelled_diodes,
    )


# ==========================================================================
# The verdict records
# ==========================================================================


@dataclass
class ClauseOutcome:
    name: str
    passed: bool
    measured: float | None
    expected: float
    op: str
    unit: str
    margin: float | None
    error: str | None = None

    def line(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        if self.measured is None:
            return f"      {mark}  {self.name}: {self.error}"
        return (
            f"      {mark}  {self.name}: measured {self.measured:.6g}{self.unit}, "
            f"expected {self.op} {self.expected:.6g}{self.unit} "
            f"(margin {self.margin:+.4g})"
        )


@dataclass
class Verdict:
    """What one oracle concluded about one proposal.

    ``refusal`` and ``clauses`` are kept apart on purpose. "This board cannot
    be simulated" and "this board failed its specification" are different
    facts, and folding the first into the second would count every ATtiny board
    as broken -- exactly the quiet zero the rest of this repo refuses.
    """

    simulated: bool
    refusal: str | None = None
    clauses: list[ClauseOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def works(self) -> bool | None:
        if not self.simulated:
            return None
        return all(c.passed for c in self.clauses) and bool(self.clauses)


def _report_to_clauses(report: Any) -> list[ClauseOutcome]:
    return [
        ClauseOutcome(
            name=o.name,
            passed=o.passed,
            measured=o.measured,
            expected=o.expected,
            op=o.op,
            unit=o.unit,
            margin=o.margin,
            error=o.error,
        )
        for o in report.outcomes
    ]


def _run(spec: CircuitSpec, tb: Testbench, assertions: list[Assertion]) -> Verdict:
    """Simulate, or say in words why there is no verdict. Never a quiet zero."""
    try:
        report = verify(spec, tb, assertions)
    except SpiceError as exc:
        return Verdict(simulated=False, refusal=f"{type(exc).__name__}: {exc}")
    except ValueError as exc:  # DeckError and friends are ValueErrors
        return Verdict(simulated=False, refusal=f"{type(exc).__name__}: {exc}")
    return Verdict(
        simulated=True,
        clauses=_report_to_clauses(report),
        warnings=list(getattr(report, "warnings", []) or []),
    )


# ==========================================================================
# The oracles: one per specification shape
# ==========================================================================


def regulator_oracle(
    *, vin_volts: float, vout_volts: float, load_ohms: float, tolerance: float = 0.05
) -> Callable[[CircuitSpec, Resolution], Verdict]:
    """"The rail is <vout> V when it is loaded", from the request's own words.

    The expectation is the part's nameplate voltage, which is a fact about what
    was ASKED for -- not a number recomputed from whatever the model proposed,
    which is how an oracle silently starts agreeing with its subject.
    """

    def oracle(spec: CircuitSpec, resolution: Resolution) -> Verdict:
        part = device_by_model_prefix(spec, resolution, "SS_LDO")
        if part is None:
            return Verdict(
                simulated=False,
                refusal="no single fixed-output regulator in the proposal; the "
                "registry bound none, or bound more than one",
            )
        gnd = net_for_terminal(spec, resolution, part, LDO_GND)
        vout = net_for_terminal(spec, resolution, part, LDO_VOUT)
        vin = net_for_terminal(spec, resolution, part, LDO_VIN)
        missing = [
            n for n, v in (("GND", gnd), ("VOUT", vout), ("VIN", vin)) if v is None
        ]
        if missing:
            return Verdict(
                simulated=False,
                refusal=f"{part} has no net on {', '.join(missing)}; the "
                "regulator is not wired up",
            )
        if vin == vout:
            return Verdict(
                simulated=False,
                refusal=f"{part} has VIN and VOUT on the same net ({vin}); the "
                "regulator is shorted across itself",
            )
        add_load(spec, "R_bench_load", load_ohms, vout, gnd)
        tb = bench(
            spec,
            resolution,
            settled_dc(stop=0.1, max_step=1e-5),
            [Source.dc_supply("V_bench", vin, gnd, vin_volts)],
        )
        return _run(
            spec,
            tb,
            [
                Assertion(
                    name=f"rail is {vout_volts:g} V at "
                    f"{vout_volts / load_ohms * 1e3:.0f} mA",
                    measurement=Measurement(kind="final", signal=vout),
                    op="within",
                    value=vout_volts,
                    tolerance=tolerance,
                    unit="V",
                ),
                Assertion(
                    name="rail never exceeds nameplate + 10%",
                    measurement=Measurement(kind="abs_max", signal=vout),
                    op="<=",
                    value=vout_volts * 1.1,
                    unit="V",
                ),
            ],
        )

    return oracle


#: Calibration verdicts, cached by ``(target_hz, supply_volts)``.
_ASTABLE_CALIBRATION: dict[tuple[float, float], tuple[bool, str]] = {}


def calibrate_astable(target_hz: float, supply_volts: float) -> tuple[bool, str]:
    """Check the instrument before trusting its reading.

    Builds the textbook astable -- ``R1 = R2 = 100k`` with ``C`` solved from
    ``T = ln2 * (R1 + 2*R2) * C`` for the target rate -- and simulates it on the
    same bench the oracle uses. A circuit that *is* correct by construction must
    measure within 10% of its closed-form rate; if it does not, the fault is in
    the stand-in or the analysis, not in anything the pipeline proposed, and the
    oracle must refuse rather than report a design failure.

    This is not hypothetical. ``spice/library.py::timer555_subckt`` holds its
    latch in a B-source whose hold branch is the literal expression ``V(Q)``,
    fed back through a 1 ns RC. That is a pure integrator of numerical error
    with no restoring nonlinearity, so the stored state drifts across a long
    hold: the model is accurate at 480 Hz and at 4.8 Hz, and at 1 Hz the
    threshold comparator fires early (measured: charge stops at 5.08 V of a
    6.00 V threshold, giving 1.90 Hz where theory says 1.024 Hz -- identically
    for 470k/1uF and for 10k/47uF, so it is the hold *duration* and not the
    impedance). Without this gate the harness would have reported every 1 Hz
    blinker the pipeline proposes as a design that does not work.
    """
    key = (target_hz, supply_volts)
    if key in _ASTABLE_CALIBRATION:
        return _ASTABLE_CALIBRATION[key]
    r = 100e3
    c = 1.0 / (target_hz * math.log(2.0) * 3.0 * r)
    fixture = {
        "devices": {
            "NE555": {
                "pins": {"GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                         "CTRL": "5", "THRES": "6", "DISCH": "7", "VCC": "8"}
            }
        },
        "passives": {
            "Rc1": {"type": "resistor", "value": f"{r:g}"},
            "Rc2": {"type": "resistor", "value": f"{r:g}"},
            "Cc1": {"type": "capacitor", "value": f"{c:g}"},
            "Cc2": {"type": "capacitor", "value": "10n"},
            "Rcl": {"type": "resistor", "value": "100k"},
        },
        "nets": {
            "VCC": ["NE555.VCC", "NE555.RESET", "Rc1.1"],
            "DIS": ["NE555.DISCH", "Rc1.2", "Rc2.1"],
            "THR": ["NE555.THRES", "NE555.TRIG", "Rc2.2", "Cc1.1"],
            "CTRL": ["NE555.CTRL", "Cc2.1"],
            "OUT": ["NE555.OUT", "Rcl.1"],
            "GND": ["NE555.GND", "Cc1.2", "Cc2.2", "Rcl.2"],
        },
    }
    spec = parse_circuit_spec(fixture)
    resolution = resolve(spec)
    tb = bench(
        spec,
        resolution,
        _astable_analysis(target_hz),
        [Source.dc_supply("V_cal", "VCC", "GND", supply_volts)],
    )
    verdict = _run(
        spec,
        tb,
        [
            Assertion(
                name="calibration",
                measurement=Measurement(kind="frequency", signal="OUT"),
                op="within",
                value=target_hz,
                tolerance=0.10,
                unit="Hz",
            )
        ],
    )
    if not verdict.simulated:
        answer = (
            False,
            f"the calibration circuit would not simulate: {verdict.refusal}",
        )
    else:
        clause = verdict.clauses[0]
        answer = (
            clause.passed,
            f"a textbook {target_hz:g} Hz astable measured "
            f"{clause.measured:.4g} Hz on this bench"
            if clause.measured is not None
            else f"the calibration measurement failed: {clause.error}",
        )
    _ASTABLE_CALIBRATION[key] = answer
    return answer


def _astable_analysis(target_hz: float) -> Transient:
    """Six periods, the first two discarded, resolved 2000 steps per period."""
    period = 1.0 / target_hz
    return Transient(
        step=period / 200.0,
        stop=period * 6.0,
        start=period * 2.0,
        max_step=period / 2000.0,
    )


def astable_oracle(
    *, supply_volts: float, target_hz: float, factor: float = 2.0
) -> Callable[[CircuitSpec, Resolution], Verdict]:
    """"It blinks, at roughly the rate that was asked for."

    ``factor`` is the band "roughly" is worth: a request for 1 Hz is met by
    anything from 0.5 to 2 Hz and is not met by 40 Hz. The band is stated here
    rather than inferred from the resistors the model picked, because the
    resistors are the thing under test.
    """

    def oracle(spec: CircuitSpec, resolution: Resolution) -> Verdict:
        part = device_by_model_prefix(spec, resolution, "SS_555")
        if part is None:
            return Verdict(
                simulated=False, refusal="no single 555 timer in the proposal"
            )
        calibrated, detail = calibrate_astable(target_hz, supply_volts)
        if not calibrated:
            return Verdict(
                simulated=False,
                refusal=(
                    "the 555 stand-in is not trustworthy at this timescale, so "
                    "there is no verdict about the design: " + detail
                ),
            )
        gnd = net_for_terminal(spec, resolution, part, T555_GND)
        vcc = net_for_terminal(spec, resolution, part, T555_VCC)
        out = net_for_terminal(spec, resolution, part, T555_OUT)
        missing = [
            n for n, v in (("GND", gnd), ("VCC", vcc), ("OUT", out)) if v is None
        ]
        if missing:
            return Verdict(
                simulated=False,
                refusal=f"{part} has no net on {', '.join(missing)}",
            )
        # Six periods with the first two discarded (an RC oscillator's first
        # charge starts from zero), resolved finely enough that the model's
        # own latch is not the thing being measured.
        tb = bench(
            spec,
            resolution,
            _astable_analysis(target_hz),
            [Source.dc_supply("V_bench", vcc, gnd, supply_volts)],
        )
        return _run(
            spec,
            tb,
            [
                Assertion(
                    name=f"oscillates no slower than {target_hz / factor:g} Hz",
                    measurement=Measurement(
                        kind="frequency", signal=out
                    ),
                    op=">=",
                    value=target_hz / factor,
                    unit="Hz",
                ),
                Assertion(
                    name=f"oscillates no faster than {target_hz * factor:g} Hz",
                    measurement=Measurement(
                        kind="frequency", signal=out
                    ),
                    op="<=",
                    value=target_hz * factor,
                    unit="Hz",
                ),
                Assertion(
                    name="output actually swings most of the supply",
                    measurement=Measurement(
                        kind="peak_to_peak", signal=out
                    ),
                    op=">=",
                    value=supply_volts * 0.5,
                    unit="V",
                ),
            ],
        )

    return oracle


def follower_oracle(
    *, supply_volts: float, expected_volts: float, tolerance: float = 0.05
) -> Callable[[CircuitSpec, Resolution], Verdict]:
    """"One amplifier is a follower, and it follows to <expected> V."

    The section that is the follower is found electrically -- OUT tied to its
    own IN- -- rather than by name, because which half of a dual op-amp got
    used is the model's choice and not part of the request.

    A dual op-amp usually has *two* sections wired that way, because tying the
    spare one as a buffer on a rail is the standard "defined state" the request
    asks for (``test_spice.py``'s ``AMP_LM358`` does exactly this). So the
    follower under test is the one whose non-inverting input is on neither
    supply rail: the request is for a buffered *divider* node, and a section
    buffering ground is the unused half by construction. Ambiguity that
    survives that rule refuses rather than picking the flattering answer.
    """

    def oracle(spec: CircuitSpec, resolution: Resolution) -> Verdict:
        part = device_by_model_prefix(spec, resolution, "SS_OPAMP")
        if part is None:
            return Verdict(
                simulated=False, refusal="no single op-amp in the proposal"
            )
        model = resolution.models[part]
        nets = spec.nets_of(part)
        sections = opamp_sections(resolution, part)
        vpos = nets.get(model.pins[sections * 3])
        vneg = nets.get(model.pins[sections * 3 + 1])
        if vpos is None or vneg is None:
            return Verdict(
                simulated=False, refusal=f"{part} has an unwired supply pin"
            )
        followers: list[tuple[str, str]] = []
        for s in range(sections):
            n_in_pos = nets.get(model.pins[s * 3])
            n_in_neg = nets.get(model.pins[s * 3 + 1])
            n_out = nets.get(model.pins[s * 3 + 2])
            if n_out is not None and n_out == n_in_neg and n_in_pos is not None:
                followers.append((n_in_pos, n_out))
        if not followers:
            return Verdict(
                simulated=False,
                refusal="no amplifier section is wired as a voltage follower "
                "(no OUT tied back to its own inverting input); the request "
                "asked for one",
            )
        signal = [f for f in followers if f[0] not in (vpos, vneg)]
        if not signal:
            return Verdict(
                simulated=False,
                refusal="every follower section is buffering a supply rail; "
                "nothing buffers the divider node the request asked for",
            )
        out_net = signal[0][1]
        tb = bench(
            spec,
            resolution,
            settled_dc(stop=0.02, max_step=1e-6),
            [Source.dc_supply("V_bench", vpos, vneg, supply_volts)],
        )
        return _run(
            spec,
            tb,
            [
                Assertion(
                    name=f"follower output is {expected_volts:g} V",
                    measurement=Measurement(kind="final", signal=out_net),
                    op="within",
                    value=expected_volts,
                    tolerance=tolerance,
                    unit="V",
                )
            ],
        )

    return oracle


# ==========================================================================
# Structural checks -- deterministic, no model, no simulator
# ==========================================================================


@dataclass
class StructuralResult:
    """Structural findings, split into two kinds that must not be merged.

    ``problems`` are defects: the part the request named is missing, a supply
    pin has no decoupling, there is no power entry where one is needed.

    ``conflicts`` are the request's explicit prohibitions ("do not add
    connectors, headers, switches, buttons, or test points") that the proposal
    broke. They are reported but **not scored**, because `propose.py`'s own
    rule 10 tells the model in capitals that "THE BOARD MUST HAVE A POWER
    INPUT ... as a real part", and TODO.txt's prompt list says the connector
    ban "is gone". The prompt and the demo request contradict each other, and
    which one should win is a product decision, not a measurement. Folding it
    into the correctness figure would score the pipeline for obeying its own
    instructions. Reported loudly instead -- a filter that drops silently is
    indistinguishable from a check nobody wrote.
    """

    passed: bool
    problems: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


_POWER_KINDS = ("connector", "battery")


def structural_check(
    spec: CircuitSpec,
    resolution: Resolution,
    *,
    must_contain: tuple[str, ...],
    forbid_kinds: tuple[str, ...],
    needs_power_entry: bool,
) -> StructuralResult:
    """Everything checkable about the proposal without a simulator.

    Kept apart from the simulated verdict because they answer different
    questions: an ATtiny board can be structurally perfect and permanently
    unsimulatable, and a board can simulate correctly while ignoring an
    explicit instruction ("do not add connectors").
    """
    problems: list[str] = []
    conflicts: list[str] = []
    names_upper = [d.name.upper() for d in spec.devices]
    for wanted in must_contain:
        if not any(wanted.upper() in n for n in names_upper):
            problems.append(
                f"the request named {wanted!r}; the proposal has "
                f"{[d.name for d in spec.devices]}"
            )
    for kind in forbid_kinds:
        offenders = [d.name for d in spec.devices if d.kind == kind]
        if offenders:
            conflicts.append(
                f"the request forbade {kind}s; the proposal added {offenders}"
            )
    if needs_power_entry and not any(d.kind in _POWER_KINDS for d in spec.devices):
        problems.append(
            "no power entry: no connector and no battery holder, so the supply "
            "nets appear from nowhere (propose.py rule 10)"
        )
    problems.extend(_decoupling_problems(spec, resolution))
    return StructuralResult(
        passed=not problems, problems=problems, conflicts=conflicts
    )


def _ground_net(spec: CircuitSpec) -> str | None:
    from silkscreen.spice.deck import GROUND_NAMES

    nets = {c.net for c in spec.connections}
    for name in GROUND_NAMES:
        for net in nets:
            if net.upper() == name:
                return net
    return None


def _decoupling_problems(spec: CircuitSpec, resolution: Resolution) -> list[str]:
    """Every bound IC supply pin should have a capacitor to ground.

    Only checked for devices the registry bound, because those are the only
    ones whose supply pin this script can identify without guessing -- the
    ``why_unmatched`` discipline applied to a rule rather than a model. A part
    the oracle cannot read is not silently scored as compliant: it is not
    scored at all, and the run's report says which parts were skipped.
    """
    gnd = _ground_net(spec)
    if gnd is None:
        return ["no ground net (nothing named GND, VSS, AGND, ...)"]
    caps_between: set[frozenset[str]] = set()
    net_of_endpoint: dict[str, str] = {}
    for conn in spec.connections:
        for ep in conn.endpoints:
            net_of_endpoint[ep] = conn.net
    for passive in spec.passives:
        if passive.type is not PassiveType.CAPACITOR:
            continue
        a = net_of_endpoint.get(f"{passive.name}.1")
        b = net_of_endpoint.get(f"{passive.name}.2")
        if a and b:
            caps_between.add(frozenset((a, b)))
    problems: list[str] = []
    for part, model in resolution.models.items():
        if model.name.startswith("SS_PORT"):
            continue
        nets = spec.nets_of(part)
        for pin_index, role in _supply_terminals(model.name, len(model.pins)):
            if pin_index >= len(model.pins):
                continue
            rail = nets.get(model.pins[pin_index])
            if rail is None or rail == gnd:
                continue
            if frozenset((rail, gnd)) not in caps_between:
                problems.append(
                    f"{part} pin {model.pins[pin_index]} ({role}) sits on "
                    f"{rail} with no capacitor from {rail} to {gnd}"
                )
    return problems


def _supply_terminals(model_name: str, pin_count: int) -> list[tuple[int, str]]:
    """``(terminal index, role)`` for the supply pins of a bound model."""
    if model_name.startswith("SS_555"):
        return [(T555_VCC, "VCC")]
    if model_name.startswith("SS_LDO"):
        return [(LDO_VIN, "VIN"), (LDO_VOUT, "VOUT")]
    if model_name.startswith("SS_OPAMP"):
        sections = (pin_count - 2) // 3
        return [(sections * 3, "V+")]
    return []


# ==========================================================================
# The prompt set
# ==========================================================================


@dataclass(frozen=True)
class Case:
    """One prompt, and what "works" means for it, in words and in clauses."""

    id: str
    intent: str
    #: What the specification says, in English, for the report.
    spec_in_words: str
    must_contain: tuple[str, ...] = ()
    forbid_kinds: tuple[str, ...] = ()
    needs_power_entry: bool = False
    oracle: Callable[[CircuitSpec, Resolution], Verdict] | None = None
    #: Set when the part is knowingly outside SPICE's reach at all.
    known_unsimulatable: str = ""


#: The first five are the demo prompts at the top of ``TODO.txt``, verbatim
#: where they fit on this bench. The rest are written in the same spirit and
#: stay inside ``board.supported_packages_text()``.
CASES: tuple[Case, ...] = (
    Case(
        id="ldo",
        intent=(
            "A minimal 3.3 V LDO regulator using an AMS1117-3.3 in SOT-223, "
            "with a 10 uF input capacitor from VIN to GND and a 22 uF output "
            "capacitor from +3V3 to GND. Use only the regulator and passive "
            "components; do not add connectors, headers, switches, buttons, or "
            "test points."
        ),
        spec_in_words=(
            "driven at 5 V, the output rail sits at 3.3 V (+/-5%) into a 330 "
            "ohm load, and never exceeds 3.63 V"
        ),
        must_contain=("AMS1117",),
        forbid_kinds=("connector", "battery", "switch", "testpoint"),
        oracle=regulator_oracle(vin_volts=5.0, vout_volts=3.3, load_ohms=330.0),
    ),
    Case(
        id="blinker",
        intent=(
            "A roughly 1 Hz LED blinker using an NE555D in SOIC-8 on a +9 V "
            "rail, with astable timing resistors and capacitor, 100 nF supply "
            "decoupling, and one LED with a series current-limiting resistor. "
            "Use only the SOIC IC and two-terminal passives; do not add "
            "connectors, headers, or switches."
        ),
        spec_in_words=(
            "on a 9 V rail the output oscillates between 0.5 Hz and 2 Hz and "
            "swings at least half the supply"
        ),
        must_contain=("555",),
        forbid_kinds=("connector", "battery", "switch", "testpoint"),
        oracle=astable_oracle(supply_volts=9.0, target_hz=1.0),
    ),
    Case(
        id="ref25",
        intent=(
            "A buffered 2.5 V reference on a +5 V rail using an LM358D in "
            "SOIC-8: two 10 kOhm resistors form the divider, one amplifier is "
            "a voltage follower, the unused amplifier is tied to a defined "
            "state, and the supply has a 100 nF decoupling capacitor. Do not "
            "add connectors or test points."
        ),
        spec_in_words=(
            "on a 5 V rail one section is a unity-gain follower whose output "
            "sits at 2.5 V (+/-5%)"
        ),
        must_contain=("LM358",),
        forbid_kinds=("connector", "battery", "testpoint"),
        oracle=follower_oracle(supply_volts=5.0, expected_volts=2.5),
    ),
    Case(
        id="attiny",
        intent=(
            "A minimal ATtiny85-20SU board in SOIC-8 on a +5 V rail, with a "
            "100 nF VCC decoupling capacitor, a 10 kOhm reset pull-up, and a "
            "status LED with a series resistor on PB1. Do not add an ISP "
            "header, connectors, buttons, or test points."
        ),
        spec_in_words="structural only -- see below",
        must_contain=("ATTINY",),
        forbid_kinds=("connector", "battery", "switch", "testpoint"),
        known_unsimulatable=(
            "a microcontroller's behaviour is its firmware; no SPICE model of "
            "it exists, here or anywhere (registry._REFUSALS)"
        ),
    ),
    Case(
        id="barrel_ldo",
        intent=(
            "A 3.3 V supply board that takes power on a 5.5 x 2.1 mm barrel "
            "jack, regulates it with an AMS1117-3.3 in SOT-223 with a 10 uF "
            "input and 22 uF output capacitor, brings +3V3 and GND out on a "
            "2-pin 5.08 mm screw terminal, and breaks +3V3, GND and two spare "
            "pins out on a 1x04 0.1 inch header. Add a 1.5 mm test pad on +3V3."
        ),
        spec_in_words=(
            "driven at 9 V on the jack, the output rail sits at 3.3 V (+/-5%) "
            "into a 330 ohm load"
        ),
        must_contain=("AMS1117",),
        needs_power_entry=True,
        oracle=regulator_oracle(vin_volts=9.0, vout_volts=3.3, load_ohms=330.0),
    ),
    Case(
        id="usbc_ldo",
        intent=(
            "A 3.3 V supply taking power from a 6-way power-only USB-C "
            "receptacle with the two 5.1 kOhm CC pull-downs to GND that make a "
            "charger deliver 5 V, regulated by an AMS1117-3.3 in SOT-223 with "
            "a 10 uF input and 22 uF output capacitor, an SMD tactile push "
            "button pulling a NRST net down against a 10 kOhm pull-up to +3V3, "
            "and a 1x04 header. The receptacle's shield tabs are added by hand."
        ),
        spec_in_words=(
            "driven at 5 V on VBUS, the output rail sits at 3.3 V (+/-5%) into "
            "a 330 ohm load"
        ),
        must_contain=("AMS1117",),
        needs_power_entry=True,
        oracle=regulator_oracle(vin_volts=5.0, vout_volts=3.3, load_ohms=330.0),
    ),
    # ---- written for this harness, same spirit, same package set ----------
    Case(
        id="ldo5v",
        intent=(
            "A 5 V supply board taking 12 V in on a 2-pin 5.08 mm screw "
            "terminal, regulated by an AMS1117-5.0 in SOT-223 with a 10 uF "
            "input capacitor and a 22 uF output capacitor, with +5V and GND "
            "brought out on a 1x02 0.1 inch header."
        ),
        spec_in_words=(
            "driven at 12 V, the output rail sits at 5 V (+/-5%) into a 500 "
            "ohm load"
        ),
        must_contain=("AMS1117",),
        needs_power_entry=True,
        oracle=regulator_oracle(vin_volts=12.0, vout_volts=5.0, load_ohms=500.0),
    ),
    Case(
        id="blinker10",
        intent=(
            "A roughly 10 Hz LED flasher using an NE555 in SOIC-8 on a +5 V "
            "rail from a 2-pin JST PH lead, with astable timing resistors and "
            "capacitor, 100 nF supply decoupling, a 10 nF capacitor on the "
            "control pin, and one LED with a series current-limiting resistor."
        ),
        spec_in_words=(
            "on a 5 V rail the output oscillates between 5 Hz and 20 Hz and "
            "swings at least half the supply"
        ),
        must_contain=("555",),
        needs_power_entry=True,
        oracle=astable_oracle(supply_volts=5.0, target_hz=10.0),
    ),
    Case(
        id="halfrail",
        intent=(
            "A buffered half-rail reference on a +3.3 V supply using a TL072 "
            "in SOIC-8: two equal 10 kOhm resistors divide the rail to 1.65 V, "
            "one section buffers that node as a unity-gain voltage follower "
            "and brings it out on a 1x03 0.1 inch header together with +3V3 "
            "and GND, the unused section is tied to a defined state, and the "
            "supply has a 100 nF decoupling capacitor. Power comes in on a "
            "2-pin JST PH lead."
        ),
        spec_in_words=(
            "on a 3.3 V rail one section is a unity-gain follower whose output "
            "sits at 1.65 V (+/-5%)"
        ),
        must_contain=("TL072",),
        needs_power_entry=True,
        oracle=follower_oracle(supply_volts=3.3, expected_volts=1.65),
    ),
)


# ==========================================================================
# Running one trial
# ==========================================================================


#: A transport failure is retried this many times before the trial is dropped
#: from the measurement entirely -- an overloaded endpoint says nothing about
#: whether the pipeline designs correct circuits.
MODEL_RETRIES = 6
MODEL_RETRY_SLEEP_S = 15.0


@dataclass
class Trial:
    case_id: str
    trial: int
    validated: bool
    rounds: int
    validation_errors: list[str] = field(default_factory=list)
    structural: StructuralResult | None = None
    verdict: Verdict | None = None
    circuit: dict[str, Any] | None = None
    raw: str | None = None
    seconds: float = 0.0
    model_error: str | None = None

    @property
    def correct(self) -> bool | None:
        """Correct = validated, structurally compliant, and simulates clean.

        ``None`` where there is no simulated verdict at all -- an ATtiny board
        cannot be scored on function and must not be counted as either.
        """
        if not self.validated or self.structural is None:
            return False
        # A structural failure is a failure whatever the simulator says --
        # "do not add connectors" and "use an AMS1117" are part of the
        # request, so ignoring them is not a near miss.
        if not self.structural.passed:
            return False
        if self.verdict is None or not self.verdict.simulated:
            return None
        return bool(self.verdict.works)


def spec_as_dict(spec: CircuitSpec) -> dict[str, Any]:
    """The proposal, in the JSON shape it arrived in, for the failure log."""
    return {
        "devices": {
            d.name: (
                {"pins": d.pins}
                if d.kind == "ic"
                else {"kind": d.kind, "package": d.package, "pins": d.pins}
            )
            for d in spec.devices
        },
        "passives": {
            p.name: {"type": p.type.value, "value": p.value} for p in spec.passives
        },
        "nets": {c.net: list(c.endpoints) for c in spec.connections},
    }


def run_trial(model: Any, case: Case, trial_no: int, *, plan: bool) -> Trial:
    from silkscreen.agents.model import ModelError
    from silkscreen.agents.propose import ProposalError, propose_circuit

    started = time.monotonic()
    brief: str | None = None
    # A ModelError is a transport failure, not a design failure, and this
    # script must not report an overloaded endpoint as a bad circuit. Retried
    # here rather than inside propose_circuit, which deliberately lets it
    # propagate unwrapped (the propose.py convention).

    if plan:
        from silkscreen.agents.plan import propose_plan

        try:
            planned = propose_plan(model, case.intent, max_repairs=1)
            brief = planned.plan.brief_text() if planned.plan is not None else None
        except (ValueError, ModelError) as exc:  # never fails the run
            brief = None
            print(f"    plan stage gave nothing usable: {exc}", file=sys.stderr)
    last_transport: str | None = None
    for attempt_no in range(MODEL_RETRIES):
        try:
            spec, attempts = propose_circuit(
                model, case.intent, facts=[], brief=brief, max_repairs=3
            )
        except ProposalError as exc:
            last = exc.attempts[-1] if exc.attempts else None
            return Trial(
                case_id=case.id,
                trial=trial_no,
                validated=False,
                rounds=len(exc.attempts),
                validation_errors=list(last.errors) if last else [],
                raw=last.raw if last else None,
                seconds=time.monotonic() - started,
            )
        except ModelError as exc:
            last_transport = str(exc)
            print(f"    transport failure, retrying: {exc}", file=sys.stderr)
            time.sleep(MODEL_RETRY_SLEEP_S * (attempt_no + 1))
            continue
        return score(spec, attempts, case, trial_no, time.monotonic() - started)

    return Trial(
        case_id=case.id,
        trial=trial_no,
        validated=False,
        rounds=0,
        seconds=time.monotonic() - started,
        model_error=last_transport,
    )


def score(
    spec: CircuitSpec, attempts: list[Any], case: Case, trial_no: int, seconds: float
) -> Trial:
    """Everything after the model call. Deterministic, so it is unit-testable."""
    resolution = resolve(spec)
    circuit = spec_as_dict(spec)
    structural = structural_check(
        spec,
        resolution,
        must_contain=case.must_contain,
        forbid_kinds=case.forbid_kinds,
        needs_power_entry=case.needs_power_entry,
    )
    verdict: Verdict | None
    if case.oracle is None:
        verdict = Verdict(
            simulated=False,
            refusal=case.known_unsimulatable or "no oracle for this case",
        )
    else:
        # The oracle mutates the spec with bench fixtures, so it gets a copy of
        # the connection list; the circuit dict above was taken first.
        verdict = case.oracle(spec, resolution)
    rejected = [a for a in attempts if not a.accepted]
    return Trial(
        case_id=case.id,
        trial=trial_no,
        validated=True,
        rounds=len(attempts),
        validation_errors=[e for a in rejected for e in a.errors],
        structural=structural,
        verdict=verdict,
        circuit=circuit,
        seconds=seconds,
    )


# ==========================================================================
# Reporting
# ==========================================================================

LIMITS = """\
What this number does not prove
-------------------------------
  * A simulation passing is not a board working. The clauses here are checked
    against generic stand-ins from spice/library.py, not against models of the
    parts named -- SS_OPAMP_LM358 is not an LM358 -- so every verdict is about
    a part of that class. docs/spice-models.md s"The honest limits" is the
    full list, and it is short enough to read.
  * Nothing here checks the schematic, the placement, the copper, the case or
    the BOM. This scores exactly one stage: propose.
  * A case with no oracle (the ATtiny board) is scored structurally and is
    counted in neither the numerator nor the denominator of the correctness
    figure. It is not a pass.
  * The clauses are the ones written in this file. A circuit can meet every
    one of them and still be a bad design in ways nobody wrote a clause for.
"""


def summarise(all_trials: list[Trial]) -> dict[str, Any]:
    # A trial the model never answered is dropped from every figure rather
    # than counted as a failure: an overloaded endpoint is not a design.
    trials = [t for t in all_trials if t.model_error is None]
    scored = [t for t in trials if t.correct is not None]
    correct = [t for t in scored if t.correct]
    validated = [t for t in trials if t.validated]
    return {
        "trials": len(trials),
        "validated": len(validated),
        "scored_on_function": len(scored),
        "correct": len(correct),
        "unscored": len(trials) - len(scored),
        "mean_rounds": (
            round(statistics.mean([t.rounds for t in validated]), 3)
            if validated
            else None
        ),
        "first_round_clean": sum(1 for t in validated if t.rounds == 1),
        "dropped_transport_failures": len(all_trials) - len(trials),
        "instruction_conflicts": sum(
            1 for t in trials if t.structural and t.structural.conflicts
        ),
        # The single most comparable figure across a change: how often the
        # proposal names the part the request asked for. Everything downstream
        # of the IR -- BOM, distributor, 3D model, SPICE model, symbol -- is
        # keyed on that name, so a run that fails it cannot be scored on
        # function at all, and the denominator of the correctness figure moves
        # with it. Reported on its own so the two are never confused.
        "named_the_part": sum(
            1
            for t in validated
            if t.structural
            and not any("the request named" in p for p in t.structural.problems)
        ),
        "reached_a_verdict": len(
            [t for t in validated if t.verdict and t.verdict.simulated]
        ),
    }


#: Refusal text -> the one-line bucket the tally prints. Ordered; first hit
#: wins. A refusal that matches nothing is printed verbatim rather than folded
#: into an "other" bucket, because an unnamed bucket is where a new failure
#: mode goes to hide.
_REFUSAL_BUCKETS: tuple[tuple[str, str], ...] = (
    ("no SPICE model of it exists", "microcontroller: no SPICE model exists anywhere"),
    ("555 stand-in is not trustworthy",
     "spice/library.py's 555 latch drifts at this timescale (see docs)"),
    ("kind=\"testpoint\"", "registry has no port model for a test point"),
    ("cannot simulate tp", "registry has no port model for a test point"),
    ("cannot simulate SW_", "registry has no port model for a switch"),
    ("must be positive", "a 0 ohm link is refused by spice/deck.py's value parser"),
    ("no single fixed-output regulator", "registry could not bind the regulator"),
    ("no single 555", "registry could not bind the 555"),
    ("no single op-amp", "registry could not bind the op-amp"),
)


def _refusal_bucket(text: str) -> str:
    lowered = text.lower()
    for needle, bucket in _REFUSAL_BUCKETS:
        if needle.lower() in lowered:
            return bucket
    return text.splitlines()[0][:100]


def print_report(trials: list[Trial], cases: tuple[Case, ...]) -> None:
    by_case: dict[str, list[Trial]] = {}
    for t in trials:
        by_case.setdefault(t.case_id, []).append(t)
    case_by_id = {c.id: c for c in cases}

    print("\n\033[1mPer prompt\033[0m")
    for case_id, group in by_case.items():
        case = case_by_id[case_id]
        s = summarise(group)
        print(f"\n  {case_id}  ({s['validated']}/{s['trials']} validated, "
              f"mean {s['mean_rounds']} rounds)")
        print(f"    spec: {case.spec_in_words}")
        for t in group:
            head = f"    trial {t.trial}: "
            if t.model_error:
                print(head + f"MODEL ERROR (not scored): {t.model_error}")
                continue
            if not t.validated:
                print(head + f"NEVER VALIDATED after {t.rounds} rounds")
                for e in t.validation_errors[:6]:
                    print(f"      - {e}")
                continue
            bits = [f"validated in {t.rounds} round(s)"]
            assert t.structural is not None
            bits.append("structure ok" if t.structural.passed else "STRUCTURE FAILED")
            v = t.verdict
            assert v is not None
            if not v.simulated:
                bits.append("no verdict")
            else:
                bits.append("WORKS" if v.works else "DOES NOT WORK")
            print(head + ", ".join(bits))
            for p in (t.structural.problems if t.structural else []):
                print(f"      structural: {p}")
            for c in (t.structural.conflicts if t.structural else []):
                print(f"      instruction conflict (reported, not scored): {c}")
            if not v.simulated:
                print(f"      no verdict: {v.refusal}")
            else:
                for c in v.clauses:
                    print(c.line())

    print("\n\033[1mOverall\033[0m")
    s = summarise(trials)
    print(f"  proposals attempted            {s['trials']}")
    print(f"  validated (IR accepted)        {s['validated']}/{s['trials']}")
    print(f"  clean on the first round       {s['first_round_clean']}/{s['trials']}")
    print(f"  named the part requested       {s['named_the_part']}/{s['trials']}")
    print(f"  reached a simulated verdict    {s['reached_a_verdict']}/{s['trials']}")
    print(f"  scored on function             {s['scored_on_function']}")
    print(f"  CORRECT (structure + spec)     {s['correct']}/{s['scored_on_function']}")
    print(f"  not scorable on function       {s['unscored']}")
    print(f"  dropped (transport failure)    {s['dropped_transport_failures']}")
    print(
        f"  broke an explicit prohibition   {s['instruction_conflicts']}"
        f" (reported, not scored -- see StructuralResult)"
    )
    reasons: dict[str, int] = {}
    for t in trials:
        if t.verdict is not None and not t.verdict.simulated and t.validated:
            reasons[_refusal_bucket(t.verdict.refusal or "")] = (
                reasons.get(_refusal_bucket(t.verdict.refusal or ""), 0) + 1
            )
    if reasons:
        print("\n  why there was no verdict (these are instrument limits, not")
        print("  design failures -- a refusal is never scored as a pass):")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>3}  {reason}")

    print("\n" + LIMITS)


# ==========================================================================
# Offline self-test: prove the oracles before spending a call
# ==========================================================================

_SELFTEST = {
    "ldo": {
        "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
        "passives": {
            "c_in": {"type": "capacitor", "value": "10uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
            "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2"],
            "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
        },
    },
    "blinker": {
        "devices": {
            "NE555D": {
                "pins": {"GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                         "CTRL": "5", "THRES": "6", "DISCH": "7", "VCC": "8"}
            }
        },
        "passives": {
            "R1": {"type": "resistor", "value": "470k"},
            "R2": {"type": "resistor", "value": "470k"},
            "C1": {"type": "capacitor", "value": "1uF"},
            "Cctrl": {"type": "capacitor", "value": "10nF"},
            "Cdec": {"type": "capacitor", "value": "100nF"},
            "Rled": {"type": "resistor", "value": "1k"},
            "D1": {"type": "diode", "value": "LED"},
        },
        "nets": {
            "+9V": ["NE555D.VCC", "NE555D.RESET", "R1.1", "Cdec.1"],
            "DIS": ["NE555D.DISCH", "R1.2", "R2.1"],
            "THR": ["NE555D.THRES", "NE555D.TRIG", "R2.2", "C1.1"],
            "CTRL": ["NE555D.CTRL", "Cctrl.1"],
            "OUT": ["NE555D.OUT", "Rled.1"],
            "LEDK": ["Rled.2", "D1.1"],
            "GND": ["NE555D.GND", "C1.2", "Cctrl.2", "Cdec.2", "D1.2"],
        },
    },
    "blinker10": {
        "devices": {
            "J1": {"kind": "connector", "package": "JST_PH_2P",
                   "pins": {"1": "1", "2": "2"}},
            "NE555": {
                "pins": {"GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                         "CTRL": "5", "THRES": "6", "DISCH": "7", "VCC": "8"}
            }
        },
        "passives": {
            "R1": {"type": "resistor", "value": "47k"},
            "R2": {"type": "resistor", "value": "47k"},
            "C1": {"type": "capacitor", "value": "1uF"},
            "Cctrl": {"type": "capacitor", "value": "10nF"},
            "Cdec": {"type": "capacitor", "value": "100nF"},
            "Rled": {"type": "resistor", "value": "1k"},
            "D1": {"type": "diode", "value": "LED"},
        },
        "nets": {
            "+5V": ["NE555.VCC", "NE555.RESET", "R1.1", "Cdec.1", "J1.1"],
            "DIS": ["NE555.DISCH", "R1.2", "R2.1"],
            "THR": ["NE555.THRES", "NE555.TRIG", "R2.2", "C1.1"],
            "CTRL": ["NE555.CTRL", "Cctrl.1"],
            "OUT": ["NE555.OUT", "Rled.1"],
            "LEDK": ["Rled.2", "D1.1"],
            "GND": ["NE555.GND", "C1.2", "Cctrl.2", "Cdec.2", "D1.2", "J1.2"],
        },
    },
    "ref25": {
        "devices": {
            "LM358D": {
                "pins": {"OUT1": "1", "IN1-": "2", "IN1+": "3", "GND": "4",
                         "IN2+": "5", "IN2-": "6", "OUT2": "7", "VCC": "8"}
            }
        },
        "passives": {
            "Rtop": {"type": "resistor", "value": "10k"},
            "Rbot": {"type": "resistor", "value": "10k"},
            "Cdec": {"type": "capacitor", "value": "100nF"},
        },
        "nets": {
            "+5V": ["LM358D.VCC", "Rtop.1", "Cdec.1"],
            "GND": ["LM358D.GND", "Rbot.2", "Cdec.2", "LM358D.IN2+"],
            "DIV": ["LM358D.IN1+", "Rtop.2", "Rbot.1"],
            "VREF": ["LM358D.OUT1", "LM358D.IN1-"],
            "BUF2": ["LM358D.OUT2", "LM358D.IN2-"],
        },
    },
}


def offline_selftest() -> int:
    """Score three hand-written *correct* circuits. Every clause must pass.

    An oracle that has never said yes to a known-good circuit is not evidence
    about anything. This runs with no key and no model call; it needs ngspice.
    """
    ok = True
    for case in CASES:
        fixture = _SELFTEST.get(case.id)
        if fixture is None:
            continue
        spec = parse_circuit_spec(fixture)
        trial = score(spec, [], case, 0, 0.0)
        assert trial.structural is not None and trial.verdict is not None
        print(f"\n  {case.id}: {case.spec_in_words}")
        for p in trial.structural.problems:
            print(f"    structural: {p}")
        if not trial.verdict.simulated:
            print(f"    no verdict: {trial.verdict.refusal}")
        for c in trial.verdict.clauses:
            print(c.line())
        # A refusal on a known-good circuit is the instrument saying it
        # cannot take this reading -- reported, and not a wrong oracle. A
        # failed *clause* on a known-good circuit is a wrong oracle.
        if not trial.structural.passed:
            ok = False
        if trial.verdict.simulated and not trial.verdict.works:
            ok = False
    print(
        "\n  self-test: "
        + (
            "no known-good circuit was scored as broken"
            if ok
            else "AN ORACLE IS WRONG"
        )
    )
    return 0 if ok else 1


# ==========================================================================
# main
# ==========================================================================


def rescore(path: Path) -> int:
    """Re-run every deterministic check over a saved run. No model calls.

    The proposals are kept verbatim in the JSON record, so a change to a
    structural rule or an oracle can be applied to a measurement that was
    already paid for. Only the parts of a trial that depend on the model are
    taken from the file; everything else is recomputed here, which is the whole
    point -- a scoring change that is only ever applied to new runs cannot be
    compared with an old number.
    """
    record = json.loads(path.read_text())
    case_by_id = {c.id: c for c in CASES}
    trials: list[Trial] = []
    for row in record["trials"]:
        case = case_by_id[row["case"]]
        if row["circuit"] is None:
            trials.append(
                Trial(
                    case_id=row["case"],
                    trial=row["trial"],
                    validated=row["validated"],
                    rounds=row["rounds"],
                    validation_errors=row["validation_errors"],
                    model_error=row.get("model_error"),
                )
            )
            continue
        spec = parse_circuit_spec(row["circuit"])
        trial = score(spec, [], case, row["trial"], row.get("seconds", 0.0))
        trial.rounds = row["rounds"]
        trial.validation_errors = row["validation_errors"]
        trials.append(trial)
    print(f"  re-scored {path} (model: {record.get('model')}, "
          f"plan: {record.get('plan')})")
    print_report(trials, tuple(case_by_id[c] for c in dict.fromkeys(
        t.case_id for t in trials)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--cases", default="", help="comma-separated case ids")
    parser.add_argument("--plan", action="store_true", help="run plan.py first")
    parser.add_argument("--model", default=None)
    parser.add_argument("--json", type=Path, default=None, help="write the raw record")
    parser.add_argument("--offline-selftest", action="store_true")
    parser.add_argument(
        "--rescore", type=Path, default=None,
        help="re-apply every deterministic check to a saved --json record",
    )
    args = parser.parse_args(argv)

    try:
        find_simulator()
    except SimulatorNotFound as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.offline_selftest:
        return offline_selftest()
    if args.rescore is not None:
        return rescore(args.rescore)

    from silkscreen.cli import _load_dotenv

    _load_dotenv(ROOT / ".env")
    if not os.environ.get("GOOGLE_API_KEY"):
        print(
            "GOOGLE_API_KEY is not set; this script makes real calls",
            file=sys.stderr,
        )
        return 1

    from silkscreen.agents.model import DEFAULT_MODEL, GeminiModel

    model = GeminiModel(model=args.model or DEFAULT_MODEL)

    wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
    cases = tuple(c for c in CASES if not wanted or c.id in wanted)
    trials: list[Trial] = []
    for case in cases:
        for n in range(1, args.trials + 1):
            print(f"  {case.id} trial {n} ...", file=sys.stderr, flush=True)
            trials.append(run_trial(model, case, n, plan=args.plan))

    print_report(trials, cases)
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "model": args.model or DEFAULT_MODEL,
                    "plan": args.plan,
                    "summary": summarise(trials),
                    "trials": [
                        {
                            "case": t.case_id,
                            "trial": t.trial,
                            "validated": t.validated,
                            "rounds": t.rounds,
                            "validation_errors": t.validation_errors,
                            "structural_problems": (
                                t.structural.problems if t.structural else []
                            ),
                            "instruction_conflicts": (
                                t.structural.conflicts if t.structural else []
                            ),
                            "simulated": bool(t.verdict and t.verdict.simulated),
                            "refusal": t.verdict.refusal if t.verdict else None,
                            "clauses": [
                                {
                                    "name": c.name,
                                    "passed": c.passed,
                                    "measured": c.measured,
                                    "expected": c.expected,
                                    "op": c.op,
                                    "unit": c.unit,
                                    "margin": c.margin,
                                    "error": c.error,
                                }
                                for c in (t.verdict.clauses if t.verdict else [])
                            ],
                            "correct": t.correct,
                            "circuit": t.circuit,
                            "raw": t.raw,
                            "seconds": round(t.seconds, 2),
                            "model_error": t.model_error,
                        }
                        for t in trials
                    ],
                },
                indent=2,
            )
        )
        print(f"  raw record written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

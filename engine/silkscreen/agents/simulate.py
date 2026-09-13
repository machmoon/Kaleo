"""Ask the model what the circuit should do, then check whether it does.

The :mod:`silkscreen.agents.sourcing` loop applied to behaviour: the model is
shown the validated circuit -- parts, values, nets -- and the intent it was
built from, and asked for a **testbench** in the :mod:`silkscreen.spice.spec`
JSON format (sources, one analysis) plus the **specification clauses** the
intent implies ("VOUT settles within 5% of 3.3 V"). Its answer goes through
that bridge and through :func:`~silkscreen.spice.deck.build_deck`, every
failure batched back as one repair prompt, and after the budget the loop
gives up loudly with a warning rather than checking a clause nobody wrote.

The four outcomes are four statuses, never a quiet zero, and the order they
are decided in is the order in which they cost nothing:

* ``"unsimulatable"`` -- the circuit holds a part with no SPICE behaviour (a
  :class:`~silkscreen.netlist.Device` is a pin map, a crystal is a motional
  network the IR does not carry). Decided from the spec alone, **before the
  model is called**: asking for a testbench nothing can run is how a paid
  call is spent on an answer with no use.
* ``"unavailable"`` -- no simulator on this machine, with the install hint
  (:class:`~silkscreen.spice.errors.SimulatorNotFound`'s own words). Also
  decided before the model is called.
* ``"no_testbench"`` -- the model gave nothing usable within the repair
  budget; a warning names the last batch of problems.
* ``"ran"`` -- the deck simulated and every clause carries pass/fail, the
  measured value and a signed margin.

A fifth, ``"failed"``, is what :func:`~silkscreen.agents.stages.simulate_stage`
records when the simulator ran and raised (a singular matrix, a timeout, a
strict-promoted warning): this module lets those propagate as the
:class:`~silkscreen.spice.errors.SpiceError` they are, and the stage names
them. A failed *clause* is not a failed *run* -- it is the verdict.

**What a failed clause does to the run** (the decision the ``spice/`` note in
CLAUDE.md said was missing): it becomes a :class:`SimulationFinding` on the
result, ``"blocker"`` when the model marked the clause ``critical`` and
``"warning"`` otherwise. Findings live on
:attr:`SimulationResult.findings`, not on ``PipelineResult.findings``, because
the critic's :class:`~silkscreen.agents.review.Finding` carries no source
field and a simulation verdict dressed as a datasheet citation would be a lie
about where it came from. Nothing here fails the run: the board is the
product and the verdict rides beside it.

``strict`` is forced on for every testbench the model proposes
(:class:`~silkscreen.spice.deck.Testbench` docstring): a run that quietly
substituted a generic diode is not evidence about the design.

Intended live tier: :data:`~silkscreen.agents.model.CHEAP_MODEL` -- turning
"3.3 V rail" into a settling clause is recall, not reasoning. The tier is
the caller's to construct; this module only sees the :class:`Model` protocol.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..netlist import CircuitSpec, _strip_code_fence
from ..spice import (
    Assertion,
    DeckError,
    SimulatorNotFound,
    Testbench,
    UnsimulatableError,
    assertions_from_dict,
    build_deck,
    check_all,
    find_simulator,
    simulate_deck,
    testbench_from_dict,
)
from ..spice.registry import ModelRegistry, Resolution, default_registry
from ..spice.simulators import DEFAULT_TIMEOUT_S, Simulator
from .model import Model

__all__ = [
    "SIMULATE_MARKER",
    "SIMULATE_PROMPT",
    "SIMULATION_STATUSES",
    "ClauseVerdict",
    "SimulationFinding",
    "SimulationResult",
    "TestbenchProposal",
    "TestbenchValidationError",
    "circuit_models",
    "parse_testbench_response",
    "propose_testbench",
    "run_testbench",
    "simulate_circuit",
    "unsimulatable_parts",
]

#: Frozen (the simulation contract): appears verbatim in every prompt so any
#: ``ScriptedModel.by_marker`` can key on it.
SIMULATE_MARKER = "SIMULATE-TESTBENCH v1"

#: Every status a :class:`SimulationResult` may carry.
SIMULATION_STATUSES = frozenset(
    {"ran", "unavailable", "no_testbench", "unsimulatable", "failed"}
)

#: How much of the batched error list a give-up warning carries.
_WARNING_CHARS = 400

#: Hard cap on clauses per testbench: a specification is a handful of
#: sentences, and a hundred clauses is a model padding its answer.
MAX_CLAUSES = 16

SIMULATE_PROMPT = f"""\
You are a hardware engineer writing the acceptance test for a circuit
someone else designed ({SIMULATE_MARKER}). You are given the intent the
circuit was built from and the circuit itself: every part with its value,
and every net with the pins on it. Write ONE SPICE testbench that drives it
the way it will be used, and the specification clauses the intent implies,
each with a concrete expected value you derive from the intent. Respond with
ONE JSON object -- no prose, no code fence:

{{
  "testbench": {{
    "analysis": {{"kind": "tran", "step": <s>, "stop": <s>}}
              | {{"kind": "op"}}
              | {{"kind": "ac", "f_start": <Hz>, "f_stop": <Hz>, "points": <n>}}
              | {{"kind": "dc", "source": "V1",
                 "start": <V>, "stop": <V>, "step": <V>}},
    "sources": [
      {{"name": "V1", "positive": "<net>", "negative": "<net>", "dc": <V>}}
      | {{"name": "V1", "positive": "<net>", "negative": "<net>", "kind": "pulse",
         "initial": <V>, "pulsed": <V>, "width": <s>, "period": <s>}}
      | {{"name": "V1", "positive": "<net>", "negative": "<net>", "kind": "sine",
         "offset": <V>, "amplitude": <V>, "frequency": <Hz>}}
    ],
    "ground": "<net>"
  }},
  "assertions": [
    {{"name": "<what the intent promises, in words>",
     "measurement": {{"kind": "final" | "max" | "min" | "mean" | "rms" |
                      "peak_to_peak" | "abs_max" | "rise_time" | "fall_time" |
                      "settling_time" | "period" | "frequency" |
                      "duty_cycle" | "gain" | "gain_db" | "bandwidth_3db",
                     "signal": "<net>", "window": [<start>, <end>] | null}},
     "op": "<" | "<=" | ">" | ">=" | "within",
     "value": <number>, "tolerance": <fraction, for within>, "unit": "<V|s|Hz|dB>",
     "critical": true | false}}
  ]
}}

Hard rules -- an answer breaking any of these is rejected automatically:

1. Every net you name (source terminals, ground, measured signals) must be
   one of the circuit's nets, spelled exactly as listed.
2. Source names start with V or I. One analysis only. Numbers are plain JSON
   numbers in SI units (seconds, volts, hertz), never strings with suffixes.
3. Between 1 and {MAX_CLAUSES} assertions. "critical" is true only for a
   clause whose failure means the board does not do what was asked (the
   output rail is wrong); a margin or a nice-to-have is false.
4. Derive each expected value from the intent and the part values. Do not
   invent a specification the intent does not imply; a circuit with nothing
   checkable gets one clause about its obvious output.
5. A transient must be long enough for the output to settle (several time
   constants) and its step fine enough to resolve the fastest edge, but no
   more than 100000 points.
"""


class TestbenchValidationError(ValueError):
    """The model's answer is not a usable testbench. Carries every problem.

    A ``ValueError`` so the stage's failure isolation (the sourcing
    convention) catches an answer that escaped the repair loop.
    """

    #: Not a pytest test class, despite the name (the ``Testbench`` rule).
    __test__ = False

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            f"{len(self.errors)} problem(s) in the testbench:\n  - "
            + "\n  - ".join(self.errors)
        )


@dataclass(frozen=True)
class TestbenchProposal:
    """What the model proposed, validated and cross-checked against the spec."""

    bench: Testbench
    assertions: tuple[Assertion, ...]
    #: ``critical`` per assertion, in the same order.
    critical: tuple[bool, ...]


@dataclass(frozen=True)
class ClauseVerdict:
    """One clause's verdict, the number behind it, and whether it mattered."""

    name: str
    passed: bool
    measured: float | None
    expected: float
    op: str
    unit: str
    margin: float | None
    description: str
    error: str | None
    critical: bool

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def line(self) -> str:
        """One terminal line: verdict, name, measured against expected, margin."""
        mark = "PASS" if self.passed else "FAIL"
        if self.measured is None:
            return f"{mark} {self.name}: {self.error or 'no measurement'}"
        margin = "" if self.margin is None else f" (margin {self.margin:+g}{self.unit})"
        return (
            f"{mark} {self.name}: measured {self.measured:g}{self.unit}, "
            f"expected {self.op} {self.expected:g}{self.unit}{margin}"
        )


@dataclass(frozen=True)
class SimulationFinding:
    """A failed clause, in the shape a reviewer reads.

    ``severity`` is ``"blocker"`` when the clause was critical and
    ``"warning"`` otherwise -- the run's decision about a failed clause,
    stated here once.
    """

    severity: str
    title: str
    detail: str
    clause: str
    measured: float | None
    expected: float
    margin: float | None
    unit: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class SimulationResult:
    """What the simulation stage produced, including the fact that it could
    not produce a verdict.

    Not :class:`silkscreen.spice.SimulationResult` -- that one is a set of
    waveforms and has no failed variant by design; this one is the stage's
    receipt and exists precisely to carry the outcomes that are not a run.
    ``status`` is one of :data:`SIMULATION_STATUSES`; ``clauses`` and
    ``findings`` are non-empty only for ``"ran"``; ``detail`` is the sentence
    for every other status, and ``parts`` names the parts that made the
    circuit unsimulatable.
    """

    status: str
    clauses: list[ClauseVerdict] = field(default_factory=list)
    findings: list[SimulationFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    detail: str = ""
    parts: tuple[str, ...] = ()
    #: Every model the registry supplied for this run, with its provenance and
    #: its ``generic`` flag. Present on a ``"ran"`` result and empty otherwise.
    #: This is the receipt that says what the verdict is *about*: a clause that
    #: passed on a generic stand-in is a claim about a class of part, and a
    #: reader who cannot see that here would read it as a claim about the part
    #: number in the design.
    substitutions: list[dict[str, Any]] = field(default_factory=list)
    analysis: str = ""
    simulator: str = ""
    repair_rounds: int = 0

    def __post_init__(self) -> None:
        if self.status not in SIMULATION_STATUSES:
            raise ValueError(
                f"unknown simulation status {self.status!r}; "
                f"expected one of {sorted(SIMULATION_STATUSES)}"
            )

    @property
    def ran(self) -> bool:
        return self.status == "ran"

    @property
    def passed(self) -> bool | None:
        """Every clause held -- or ``None`` when there was no run to judge."""
        if not self.ran:
            return None
        return all(c.passed for c in self.clauses)

    @property
    def failed(self) -> list[ClauseVerdict]:
        return [c for c in self.clauses if not c.passed]

    @property
    def blockers(self) -> list[SimulationFinding]:
        return [f for f in self.findings if f.severity == "blocker"]

    @property
    def generic_parts(self) -> tuple[str, ...]:
        """Parts simulated with a stand-in rather than a model of themselves."""
        return tuple(
            str(s["part"]) for s in self.substitutions if s.get("generic")
        )

    def note(self) -> str:
        """The one sentence a report prints when there is no clause list."""
        if self.ran:
            total = len(self.clauses)
            line = f"simulation: {total - len(self.failed)}/{total} clause(s) passed"
            generic = self.generic_parts
            if generic:
                line += (
                    f" (generic stand-in used for {', '.join(generic)}; the "
                    f"verdict is about parts of that class, not those part "
                    f"numbers)"
                )
            return line
        return f"simulation {self.status}: {self.detail}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "clauses": [c.as_dict() for c in self.clauses],
            "findings": [f.as_dict() for f in self.findings],
            "warnings": list(self.warnings),
            "detail": self.detail,
            "parts": list(self.parts),
            "substitutions": list(self.substitutions),
            "generic_parts": list(self.generic_parts),
            "analysis": self.analysis,
            "simulator": self.simulator,
            "repair_rounds": self.repair_rounds,
        }


# ---------------------------------------------------------------- prompt


def _circuit_block(
    spec: CircuitSpec, resolution: Resolution | None = None
) -> str:
    """The circuit rendered for the prompt, in the model's own vocabulary.

    A device carries the standing of the model behind it, because that changes
    which clauses are worth writing: no clause about an op-amp's offset is
    answerable when the amplifier is a single-pole stand-in, and a clause the
    instrument cannot answer fails vacuously rather than informatively.
    """
    refs = spec.assign_refs()
    generic = set(resolution.generic_parts) if resolution else set()
    covered = set(resolution.models) if resolution else set()
    lines = ["Parts:"]
    for device in spec.devices:
        if device.name in generic:
            standing = (
                "simulated with a GENERIC STAND-IN for its class, not a model "
                "of this part number: first-order behaviour only, so do not "
                "write clauses about offset, noise, stability or part-specific "
                "limits"
            )
        elif device.name in covered:
            standing = "simulated with a supplied model of this part"
        else:
            standing = "no SPICE model"
        lines.append(f"  - {device.name} ({refs[device.name]}): device, pins "
                     f"{', '.join(sorted(device.pins))} [{standing}]")
    for passive in spec.passives:
        lines.append(
            f"  - {passive.name} ({refs[passive.name]}): "
            f"{passive.type.value} {passive.value}"
        )
    lines.append("Nets:")
    for conn in spec.connections:
        lines.append(f"  - {conn.net}: {', '.join(conn.endpoints)}")
    return "\n".join(lines)


def circuit_models(
    spec: CircuitSpec, registry: ModelRegistry | None = None
) -> Resolution:
    """What the trusted registry can supply for this circuit.

    The registry is the *only* source of device behaviour on this path.
    Nothing here asks a model for SPICE text and the testbench schema has no
    field to carry any, because a ``.SUBCKT`` a language model invented would
    produce a simulation that **passes**, and a passing verdict carries
    authority a guess has not earned.
    """
    return (registry or default_registry()).resolve(spec)


def unsimulatable_parts(
    spec: CircuitSpec, registry: ModelRegistry | None = None
) -> tuple[list[str], str]:
    """The parts :func:`build_deck` would still refuse, and why.

    Decided from the spec and the registry alone, before a call is spent: a
    device the registry cannot cover (a microcontroller, an unrecognised IC)
    and every crystal. The reasons are the registry's own
    (:meth:`~silkscreen.spice.registry.ModelRegistry.why_unmatched`), so the
    sentence a run reports is the sentence that names the fix -- install a
    model file, spell the regulator's output voltage into the part number, or
    accept that the part cannot be simulated at all.
    """
    resolution = circuit_models(spec, registry)
    return list(resolution.uncovered), "; ".join(resolution.reasons)


def parse_testbench_response(
    raw: str, spec: CircuitSpec, resolution: Resolution | None = None
) -> TestbenchProposal:
    """Validate raw model output into a testbench the deck accepts.

    Every failure -- JSON, shape, the :mod:`~silkscreen.spice.spec` bridge's
    own list, and :func:`build_deck`'s cross-check against the circuit (a
    source on a net the circuit lacks, no ground) -- is collected into one
    :class:`TestbenchValidationError`, so the whole batch goes back to the
    model as a single repair prompt. ``strict`` is forced on.

    An :class:`~silkscreen.spice.errors.UnsimulatableError` from the deck is
    not a model mistake and is re-raised as itself.

    ``resolution`` carries the registry's models onto the testbench, together
    with the parts whose model is a generic stand-in. Those parts are named in
    ``accept_generic_models`` so ``strict`` does not refuse the run -- the
    substitution is *declared*, not silenced: the deck still warns about every
    one of them and the warning reaches the result.
    """
    errors: list[str] = []
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise TestbenchValidationError([f"response is not valid JSON: {exc}"]) from exc
    if not isinstance(data, dict):
        raise TestbenchValidationError(
            [f"response must be a JSON object, got {type(data).__name__}"]
        )
    unknown = sorted(set(data) - {"testbench", "assertions"})
    for key in unknown:
        errors.append(f"unknown top-level field {key!r}")

    bench: Testbench | None = None
    raw_bench = data.get("testbench")
    if not isinstance(raw_bench, dict):
        errors.append("'testbench' must be an object")
    else:
        try:
            bench = testbench_from_dict(raw_bench)
        except DeckError as exc:
            errors.extend(exc.errors)

    raw_assertions = data.get("assertions")
    critical: list[bool] = []
    assertions: list[Assertion] = []
    if not isinstance(raw_assertions, list):
        errors.append("'assertions' must be a list")
    elif not raw_assertions:
        errors.append("'assertions' must name at least one clause")
    elif len(raw_assertions) > MAX_CLAUSES:
        errors.append(
            f"'assertions' has {len(raw_assertions)} clauses; at most "
            f"{MAX_CLAUSES}"
        )
    else:
        cleaned: list[Any] = []
        for index, entry in enumerate(raw_assertions, start=1):
            if not isinstance(entry, dict):
                errors.append(f"assertion {index}: must be an object")
                continue
            flag = entry.get("critical", False)
            if not isinstance(flag, bool):
                errors.append(
                    f"assertion {index}: 'critical' must be true or false"
                )
                flag = False
            critical.append(flag)
            cleaned.append({k: v for k, v in entry.items() if k != "critical"})
        try:
            assertions = assertions_from_dict(cleaned)
        except DeckError as exc:
            errors.extend(exc.errors)

    if bench is not None:
        bench = dataclasses.replace(
            bench,
            strict=True,
            models={**(resolution.models if resolution else {}), **bench.models},
            accept_generic_models=(
                resolution.generic_parts if resolution else ()
            ),
        )
        try:
            build_deck(spec, bench)
        except DeckError as exc:
            errors.extend(exc.errors)
        # UnsimulatableError is deliberately not caught: it is the circuit's
        # fact, not the answer's, and the caller decided it before asking.

    if bench is not None and not errors:
        nets = {conn.net for conn in spec.connections}
        for assertion in assertions:
            measurement = assertion.measurement
            for signal in (measurement.signal, measurement.reference):
                if signal is not None and signal not in nets:
                    errors.append(
                        f"assertion {assertion.name!r} measures {signal!r}, "
                        f"which is not a net of the circuit "
                        f"(nets: {sorted(nets)[:8]})"
                    )

    if errors or bench is None:
        raise TestbenchValidationError(errors or ["could not build the testbench"])
    return TestbenchProposal(
        bench=bench, assertions=tuple(assertions), critical=tuple(critical)
    )


def propose_testbench(
    model: Model,
    spec: CircuitSpec,
    *,
    intent: str = "",
    max_repairs: int = 1,
    resolution: Resolution | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[TestbenchProposal | None, list[str], int]:
    """Ask the model for a testbench; ``(proposal, warnings, repair_rounds)``.

    One model call, plus at most ``max_repairs`` more when the answer fails
    :func:`parse_testbench_response` -- the batched errors go back as a
    single repair prompt each time. When the budget is spent the proposal is
    ``None`` with one warning saying so: an unverified circuit is honest, a
    half-parsed testbench is not.

    ``on_event`` receives one ``simulation.round`` per rejected answer --
    the count and the first error, never the model's text.

    Raises:
        ModelError: the model could not be reached. Deliberately not
            wrapped (the :func:`~silkscreen.agents.propose.propose_circuit`
            convention).
        UnsimulatableError: the spec holds a part with no behaviour. Callers
            decide this before asking (:func:`unsimulatable_parts`); it is
            re-raised here only if they did not.
    """
    circuit = _circuit_block(spec, resolution)
    stated = intent.strip() or "(not stated)"
    head = f"{SIMULATE_PROMPT}\nIntent:\n  {stated}\n\n{circuit}\n"
    prompt = head

    proposal: TestbenchProposal | None = None
    last_errors: list[str] = []
    rounds = 0
    for round_no in range(max_repairs + 1):
        # A transport failure is NOT wrapped: ModelError propagates so a
        # FallbackModel's failover -- and the service's 502 -- stay intact.
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=4096)
        try:
            proposal = parse_testbench_response(raw, spec, resolution)
        except TestbenchValidationError as exc:
            last_errors = list(exc.errors)
        else:
            break
        if on_event is not None:
            on_event(
                {
                    "event": "simulation.round",
                    "round": round_no + 1,
                    "errors": len(last_errors),
                    "first_error": str(last_errors[0])[:160] if last_errors else "",
                }
            )
        if round_no == max_repairs:
            break
        rounds += 1
        problems = "\n".join(f"  - {e}" for e in last_errors)
        prompt = (
            f"{head}\nYour previous answer was rejected. Fix ALL of these and "
            f"return the corrected JSON object:\n{problems}\n\n"
            f"Your previous answer was:\n{raw}\n"
        )

    warnings: list[str] = []
    if proposal is None:
        detail = "; ".join(last_errors)[:_WARNING_CHARS]
        warnings.append(
            f"circuit was not simulated: no usable testbench after "
            f"{max_repairs + 1} attempt(s) ({detail})"
        )
    return proposal, warnings, rounds


# ---------------------------------------------------------------- running


def _verdict(outcome: Any, critical: bool) -> ClauseVerdict:
    return ClauseVerdict(
        name=outcome.name,
        passed=outcome.passed,
        measured=outcome.measured,
        expected=outcome.expected,
        op=outcome.op,
        unit=outcome.unit,
        margin=outcome.margin,
        description=outcome.description,
        error=outcome.error,
        critical=critical,
    )


def _finding(verdict: ClauseVerdict) -> SimulationFinding:
    """The run's decision about a failed clause, applied to one clause."""
    if verdict.measured is None:
        detail = verdict.error or "the measurement has no answer"
    else:
        detail = (
            f"measured {verdict.measured:g}{verdict.unit}, expected "
            f"{verdict.op} {verdict.expected:g}{verdict.unit}"
        )
        if verdict.margin is not None:
            detail += f" (margin {verdict.margin:+g}{verdict.unit})"
    return SimulationFinding(
        severity="blocker" if verdict.critical else "warning",
        title=f"simulation: {verdict.name}",
        detail=detail,
        clause=verdict.name,
        measured=verdict.measured,
        expected=verdict.expected,
        margin=verdict.margin,
        unit=verdict.unit,
    )


def run_testbench(
    spec: CircuitSpec,
    proposal: TestbenchProposal,
    *,
    simulator: Simulator | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> SimulationResult:
    """Build the deck, run it, judge every clause. Status ``"ran"``.

    ``simulator`` is the seam: ``None`` finds one on this machine, a name
    picks one, an object with ``run`` (the :class:`~silkscreen.spice.
    simulators.Simulator` protocol) is used as is -- which is how the suite
    exercises the verdict path with no ngspice anywhere near it.

    ``on_event`` receives one ``simulation.clause`` per clause: name,
    verdict, margin, critical. Never the deck text or the waveforms.

    Raises:
        SimulatorNotFound: no simulator, when ``simulator`` was ``None`` or a
            name. Callers decide this before the model is asked; it is
            re-raised here only if they did not.
        UnsimulatableError, DeckError, SimulationFailed, ConvergenceError:
            the deck's and the simulator's own failures, as themselves.
    """
    deck = build_deck(spec, proposal.bench)
    result = simulate_deck(deck, simulator=simulator, timeout_s=timeout_s)
    report = check_all(result, list(proposal.assertions))
    clauses = [
        _verdict(outcome, flag)
        for outcome, flag in zip(report.outcomes, proposal.critical, strict=True)
    ]
    for clause in clauses:
        if on_event is not None:
            on_event(
                {
                    "event": "simulation.clause",
                    "name": clause.name[:160],
                    "passed": clause.passed,
                    "measured": clause.measured,
                    "margin": clause.margin,
                    "critical": clause.critical,
                }
            )
    return SimulationResult(
        status="ran",
        clauses=clauses,
        findings=[_finding(c) for c in clauses if not c.passed],
        warnings=list(report.warnings),
        analysis=result.analysis,
        simulator=result.simulator,
    )


def simulate_circuit(
    model: Model,
    spec: CircuitSpec,
    *,
    intent: str = "",
    simulator: Simulator | str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_repairs: int = 1,
    registry: ModelRegistry | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> SimulationResult:
    """The whole loop: decide, ask, run, judge.

    In cost order. The circuit is checked for parts with no behaviour and
    the machine for a simulator **before** the model is called, so neither
    ``"unsimulatable"`` nor ``"unavailable"`` spends a call. Then
    :func:`propose_testbench` (``"no_testbench"`` on a spent budget) and
    :func:`run_testbench` (``"ran"``). The simulator's own failures
    propagate as :class:`~silkscreen.spice.errors.SpiceError`; the stage
    turns them into ``"failed"`` with the reason, because that is a fact
    about this run and not about the design.
    """
    resolution = circuit_models(spec, registry)
    if resolution.uncovered:
        named = ", ".join(sorted(resolution.uncovered))
        return SimulationResult(
            status="unsimulatable",
            detail=f"cannot simulate {named}: " + "; ".join(resolution.reasons),
            parts=tuple(sorted(resolution.uncovered)),
        )

    if simulator is None or isinstance(simulator, str):
        try:
            simulator = find_simulator(simulator)
        except SimulatorNotFound as exc:
            return SimulationResult(status="unavailable", detail=str(exc))

    try:
        proposal, warnings, rounds = propose_testbench(
            model,
            spec,
            intent=intent,
            max_repairs=max_repairs,
            resolution=resolution,
            on_event=on_event,
        )
    except UnsimulatableError as exc:
        # The precheck above covers every case build_deck refuses today; this
        # is insurance against a new one, answered the same way.
        return SimulationResult(
            status="unsimulatable", detail=str(exc), parts=tuple(sorted(exc.parts))
        )
    if proposal is None:
        return SimulationResult(
            status="no_testbench",
            warnings=warnings,
            detail=warnings[0] if warnings else "no usable testbench",
            repair_rounds=rounds,
        )

    result = run_testbench(
        spec, proposal, simulator=simulator, timeout_s=timeout_s, on_event=on_event
    )
    result.warnings = warnings + result.warnings
    result.repair_rounds = rounds
    result.substitutions = [d.as_dict() for d in resolution.resolved]
    return result

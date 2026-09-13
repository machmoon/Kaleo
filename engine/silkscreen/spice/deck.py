"""From a validated :class:`~silkscreen.netlist.CircuitSpec` to a SPICE deck.

This is the plumbing the whole package hangs off. The IR already holds what a
netlist needs -- parts with values, and nets whose endpoints are pin-level -- so
translation is mostly mechanical. Three things are not mechanical, and they are
where the honesty lives:

**A circuit is not a testbench.** The IR describes what is wired to what. It
says nothing about what you *drive it with* or what question you are asking.
Neither does a schematic. So :class:`Testbench` is a separate object the caller
supplies: sources, an analysis, and the models that make the parts mean
something. Simulating "the circuit" alone is not a well-formed request.

**An integrated circuit has no behaviour here.** A :class:`~silkscreen.netlist.Device`
is a name and a pin map. There is no model attached and none can be invented, so
a device with no :class:`SubcircuitModel` raises
:class:`~silkscreen.spice.errors.UnsimulatableError` naming it. That is the
boundary of what this package can verify, and it is stated rather than papered
over with a passthrough.

**Ground is not optional.** SPICE solves relative to node ``0``. A circuit with
no ground reference gives a singular matrix, which ngspice reports in a way easy
to mistake for an empty result, so the ground net is resolved and checked here
instead.

Node names are sanitised for SPICE and mapped back to the original net names on
the way out (:attr:`SpiceDeck.net_of_node`), so a caller writes and reads its own
net names throughout and never sees the sanitised form.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..netlist import CircuitSpec, PassiveType
from .errors import DeckError, UnsimulatableError, ValueSyntaxError
from .values import format_value, parse_value

__all__ = [
    "Source",
    "Analysis",
    "OperatingPoint",
    "Transient",
    "ACSweep",
    "DCSweep",
    "PrimitiveModel",
    "SubcircuitModel",
    "Testbench",
    "SpiceDeck",
    "build_deck",
    "GROUND_NAMES",
    "GENERIC_DIODE",
]

#: Net names treated as the ground reference when the testbench does not name
#: one, in priority order.
GROUND_NAMES: tuple[str, ...] = (
    "0",
    "GND",
    "GROUND",
    "AGND",
    "DGND",
    "VSS",
    "GNDA",
    "GNDD",
)

#: Stand-in for a diode whose part number has no model. Small-signal silicon,
#: roughly a 1N4148. Using it is always reported as a warning -- the answer is
#: real, but it is an answer about *a* diode, not about the one specified.
GENERIC_DIODE = ".model SS_GENERIC_D D(IS=2.52n RS=0.568 N=1.752 CJO=4p BV=100)"

_SPICE_NODE_RE = re.compile(r"[^A-Za-z0-9_]")
_SPICE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:$-]*\Z")


def _single_line(value: object, label: str, errors: list[str]) -> bool:
    """Require an emitted field to remain one printable simulator line."""
    if not isinstance(value, str) or not value:
        errors.append(f"{label} must be a non-empty string")
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        errors.append(f"{label} must not contain control characters or newlines")
        return False
    return True


def _token(value: object, label: str, errors: list[str]) -> bool:
    if not _single_line(value, label, errors):
        return False
    if _SPICE_TOKEN_RE.fullmatch(value) is None:
        errors.append(
            f"{label} {value!r} is not a valid SPICE identifier; use letters, "
            "digits, '_', '.', ':', '$' or '-'"
        )
        return False
    return True


def _finite(value: object, label: str, errors: list[str]) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} must be a finite number, got {value!r}")
        return False
    try:
        finite = math.isfinite(value)
    except OverflowError:
        errors.append(f"{label} must be a finite number, got {value!r}")
        return False
    if not finite:
        errors.append(f"{label} must be a finite number, got {value!r}")
        return False
    return True


def _validate_model_program(
    model: PrimitiveModel | SubcircuitModel, part: str, errors: list[str]
) -> None:
    """Reject simulator-control blocks embedded in otherwise valid model text.

    Model programs are accepted only by the trusted Python API, not by the MCP
    JSON bridge. This final check still prevents a copied vendor file from
    escaping its model role and taking over the simulator driver.
    """
    _token(model.name, f"model name for {part!r}", errors)
    if not isinstance(model.text, str) or not model.text.strip():
        errors.append(f"model text for {part!r} must be a non-empty string")
        return
    if "\x00" in model.text:
        errors.append(f"model text for {part!r} must not contain NUL bytes")
    for line_number, line in enumerate(model.text.splitlines(), start=1):
        directive = line.lstrip().split(maxsplit=1)[0].lower() if line.strip() else ""
        if directive in {".control", ".end"}:
            errors.append(
                f"model text for {part!r} line {line_number} contains forbidden "
                f"simulator directive {directive!r}"
            )


def _validate_analysis(analysis: Analysis, errors: list[str]) -> None:
    if isinstance(analysis, OperatingPoint):
        return
    if isinstance(analysis, Transient):
        valid = all(
            (
                _finite(analysis.step, "transient step", errors),
                _finite(analysis.stop, "transient stop", errors),
                _finite(analysis.start, "transient start", errors),
            )
        )
        if analysis.max_step is not None:
            valid = _finite(analysis.max_step, "transient max_step", errors) and valid
        if valid:
            if analysis.step <= 0:
                errors.append("transient step must be greater than zero")
            if analysis.start < 0 or analysis.stop <= analysis.start:
                errors.append("transient requires stop > start >= 0")
            if analysis.max_step is not None and analysis.max_step <= 0:
                errors.append("transient max_step must be greater than zero")
        return
    if isinstance(analysis, ACSweep):
        valid = all(
            (
                _finite(analysis.f_start, "AC f_start", errors),
                _finite(analysis.f_stop, "AC f_stop", errors),
            )
        )
        if (
            isinstance(analysis.points, bool)
            or not isinstance(analysis.points, int)
            or analysis.points <= 0
        ):
            errors.append("AC points must be a positive integer")
        if analysis.sweep not in ("dec", "oct", "lin"):
            errors.append("AC sweep must be 'dec', 'oct' or 'lin'")
        if valid and (analysis.f_start <= 0 or analysis.f_stop <= analysis.f_start):
            errors.append("AC sweep requires f_stop > f_start > 0")
        return
    if isinstance(analysis, DCSweep):
        _token(analysis.source, "DC sweep source", errors)
        valid = all(
            (
                _finite(analysis.start, "DC start", errors),
                _finite(analysis.stop, "DC stop", errors),
                _finite(analysis.step, "DC step", errors),
            )
        )
        if valid and (
            analysis.step == 0 or (analysis.stop - analysis.start) * analysis.step <= 0
        ):
            errors.append("DC step must be nonzero and move from start toward stop")
        return
    errors.append(f"unsupported analysis type {type(analysis).__name__}")


def _validate_testbench(bench: Testbench) -> list[str]:
    errors: list[str] = []
    _single_line(bench.title, "testbench title", errors)
    _validate_analysis(bench.analysis, errors)

    if bench.ground is not None:
        _single_line(bench.ground, "testbench ground", errors)
    for probe in bench.probes:
        _single_line(probe, "probe name", errors)
    if bench.temperature_c is not None:
        _finite(bench.temperature_c, "testbench temperature", errors)
    for option in bench.options:
        _single_line(option, "SPICE option", errors)

    for source in bench.sources:
        _token(source.name, "source name", errors)
        _single_line(source.positive, f"source {source.name!r} positive net", errors)
        _single_line(source.negative, f"source {source.name!r} negative net", errors)
        if source.dc is not None:
            _finite(source.dc, f"source {source.name!r} DC value", errors)
        if source.ac_magnitude is not None:
            _finite(
                source.ac_magnitude,
                f"source {source.name!r} AC magnitude",
                errors,
            )
        if source.transient is not None:
            _single_line(
                source.transient, f"source {source.name!r} transient", errors
            )

    for part, model in bench.models.items():
        if not isinstance(model, (PrimitiveModel, SubcircuitModel)):
            errors.append(
                f"model for {part!r} must be PrimitiveModel or SubcircuitModel"
            )
            continue
        _validate_model_program(model, part, errors)
        if isinstance(model, SubcircuitModel):
            if not model.pins:
                errors.append(f"subcircuit model for {part!r} must list its pins")
            for pin in model.pins:
                _single_line(pin, f"subcircuit pin for {part!r}", errors)
    return errors


def _element_name(letter: str, ref: str) -> str:
    """SPICE element name for a reference designator.

    SPICE reads the element type from the first letter, and KiCad's designators
    already start with it -- so ``R1`` is used as-is rather than becoming
    ``RR1``. A designator that does not match (or a subcircuit, which must start
    with ``X`` whatever the part is called) gets the letter prefixed.
    """
    if ref[:1].upper() == letter.upper():
        return ref
    return f"{letter}{ref}"


#: SPICE element letter for each passive type. Crystals have no primitive: a
#: quartz resonator is a motional RLC plus a shunt capacitance, and those values
#: are not in the IR, so it is unsimulatable without a supplied model.
_ELEMENT_LETTER = {
    PassiveType.RESISTOR: "R",
    PassiveType.CAPACITOR: "C",
    PassiveType.INDUCTOR: "L",
    PassiveType.DIODE: "D",
}


# --------------------------------------------------------------------------
# Testbench pieces
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """An independent voltage or current source added by the testbench.

    ``name`` must start with ``V`` or ``I``; SPICE decides what an element is
    from its first letter, so this is checked rather than assumed.

    ``dc``, ``ac_magnitude`` and ``transient`` are independent and combine the
    way SPICE combines them: the DC value sets the operating point, the AC
    magnitude is what a :class:`ACSweep` perturbs by, and the transient
    specification takes over during a :class:`Transient`.
    """

    name: str
    positive: str
    negative: str
    dc: float | None = None
    ac_magnitude: float | None = None
    transient: str | None = None

    @classmethod
    def dc_supply(cls, name: str, positive: str, negative: str, volts: float) -> Source:
        return cls(name=name, positive=positive, negative=negative, dc=volts)

    @classmethod
    def ac_probe(
        cls,
        name: str,
        positive: str,
        negative: str,
        magnitude: float = 1.0,
        dc: float = 0.0,
    ) -> Source:
        """A source for frequency response: 1 V AC by default, so the output
        voltage *is* the transfer function."""
        return cls(
            name=name,
            positive=positive,
            negative=negative,
            dc=dc,
            ac_magnitude=magnitude,
        )

    @classmethod
    def pulse(
        cls,
        name: str,
        positive: str,
        negative: str,
        *,
        initial: float,
        pulsed: float,
        delay: float = 0.0,
        rise: float = 1e-9,
        fall: float = 1e-9,
        width: float,
        period: float,
    ) -> Source:
        spec = (
            f"PULSE({initial:g} {pulsed:g} {delay:g} {rise:g} "
            f"{fall:g} {width:g} {period:g})"
        )
        return cls(
            name=name,
            positive=positive,
            negative=negative,
            dc=initial,
            transient=spec,
        )

    @classmethod
    def sine(
        cls,
        name: str,
        positive: str,
        negative: str,
        *,
        offset: float,
        amplitude: float,
        frequency: float,
        delay: float = 0.0,
    ) -> Source:
        spec = f"SIN({offset:g} {amplitude:g} {frequency:g} {delay:g})"
        return cls(
            name=name,
            positive=positive,
            negative=negative,
            dc=offset,
            transient=spec,
        )

    def card(self, node_of: dict[str, str]) -> str:
        parts = [self.name, node_of[self.positive], node_of[self.negative]]
        if self.dc is not None:
            parts.append(f"DC {self.dc:g}")
        if self.ac_magnitude is not None:
            parts.append(f"AC {self.ac_magnitude:g}")
        if self.transient:
            parts.append(self.transient)
        if len(parts) == 3:
            parts.append("DC 0")
        return " ".join(parts)


@dataclass(frozen=True)
class Analysis:
    """Base for the analysis kinds. Subclasses render one SPICE analysis card."""

    def card(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def kind(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def sweep_name(self) -> str:
        """Name of the independent variable in the result."""
        return "sweep"


@dataclass(frozen=True)
class OperatingPoint(Analysis):
    """Bias point only: every node's steady DC voltage, one point."""

    def card(self) -> str:
        return ".op"

    @property
    def kind(self) -> str:
        return "op"

    @property
    def sweep_name(self) -> str:
        return "v-sweep"


@dataclass(frozen=True)
class Transient(Analysis):
    """Time-domain. ``step`` is the output interval, not the internal timestep."""

    step: float
    stop: float
    start: float = 0.0
    max_step: float | None = None

    def card(self) -> str:
        card = f".tran {self.step:g} {self.stop:g} {self.start:g}"
        if self.max_step is not None:
            card += f" {self.max_step:g}"
        return card

    @property
    def kind(self) -> str:
        return "tran"

    @property
    def sweep_name(self) -> str:
        return "time"


@dataclass(frozen=True)
class ACSweep(Analysis):
    """Small-signal frequency response, linearised about the operating point."""

    f_start: float
    f_stop: float
    points: int = 20
    sweep: str = "dec"

    def card(self) -> str:
        return f".ac {self.sweep} {self.points:d} {self.f_start:g} {self.f_stop:g}"

    @property
    def kind(self) -> str:
        return "ac"

    @property
    def sweep_name(self) -> str:
        return "frequency"


@dataclass(frozen=True)
class DCSweep(Analysis):
    """Sweep one source's value and record the bias point at each step."""

    source: str
    start: float
    stop: float
    step: float

    def card(self) -> str:
        return f".dc {self.source} {self.start:g} {self.stop:g} {self.step:g}"

    @property
    def kind(self) -> str:
        return "dc"

    @property
    def sweep_name(self) -> str:
        return "v-sweep"


@dataclass(frozen=True)
class PrimitiveModel:
    """A ``.model`` card for a SPICE primitive, e.g. a specific diode."""

    name: str
    text: str


@dataclass(frozen=True)
class SubcircuitModel:
    """A ``.subckt`` block standing in for a device.

    ``pins`` lists the *device pin names* in the subcircuit's terminal order --
    the mapping between what the datasheet calls a pin and what position it
    occupies in the ``.subckt`` line. Getting that order wrong is the classic
    way to simulate a completely different circuit and never notice, so the
    names are checked against the device's own pin map.

    ``generic`` says this subcircuit stands in for a *class* of part rather
    than modelling the part number in the design. It is not decoration: a
    generic substitution raises a warning naming the part, and
    ``Testbench(strict=True)`` promotes that warning to an error unless the
    caller named the part in :attr:`Testbench.accept_generic_models`. The
    diode path has behaved this way since the package was written (see
    :data:`GENERIC_DIODE`); this carries the same rule to devices, which is
    where it matters more, because nobody mistakes "a diode" for a part
    number and plenty of people would read "the LM358 result" as being about
    an LM358.

    ``provenance`` is one line saying where the text came from and under what
    licence, produced by
    :meth:`~silkscreen.spice.library.ModelProvenance.line`. It travels into
    the warning verbatim so a result can be traced to a source.
    """

    name: str
    pins: tuple[str, ...]
    text: str
    generic: bool = False
    provenance: str = ""


@dataclass
class Testbench:
    """Everything the circuit does not say about itself.

    ``models`` is keyed by *part name as it appears in the spec* -- a
    :class:`SubcircuitModel` for a device, a :class:`PrimitiveModel` for a diode
    or crystal.

    ``strict`` promotes every warning to an error. An agent verifying against a
    specification usually wants this on: a run that quietly substituted a
    generic diode for the part number in the design is not evidence about the
    design.

    ``accept_generic_models`` is how a caller says it knows. It names the
    parts -- by their spec names -- for which a generic stand-in is knowingly
    accepted, which turns a *silent* substitution into a *declared* one and is
    the only thing that stops ``strict`` refusing the run. Naming a part the
    circuit does not have is a hard error, not a no-op, the same convention
    ``edge_refs`` and ``rotatable_refs`` follow in :mod:`silkscreen.board`:
    the whole value of the acknowledgement is that it is checked. The warning
    is still emitted and still reaches every result -- accepting a stand-in
    silences the refusal, never the disclosure.
    """

    #: Not a pytest test class, despite the name -- pytest collects anything
    #: called ``Test*`` that a test module imports, and warns when it cannot.
    __test__ = False

    analysis: Analysis
    sources: list[Source] = field(default_factory=list)
    models: dict[str, PrimitiveModel | SubcircuitModel] = field(default_factory=dict)
    ground: str | None = None
    probes: tuple[str, ...] = ()
    temperature_c: float | None = None
    options: tuple[str, ...] = ()
    strict: bool = False
    accept_generic_models: tuple[str, ...] = ()
    title: str = "silkscreen simulation"


@dataclass(frozen=True)
class SpiceDeck:
    """A renderable netlist plus the maps needed to read its output back.

    ``text`` is the deck without any simulator driver attached -- components,
    models, sources and the analysis card. Each simulator adds its own way of
    being told to run, because ngspice takes a ``.control`` block and LTspice
    does not.
    """

    text: str
    analysis: Analysis
    #: SPICE node name -> original net name, for relabelling results.
    net_of_node: dict[str, str]
    #: Original net name -> SPICE node name.
    node_of_net: dict[str, str]
    #: Spec part name -> reference designator used in the deck.
    refs: dict[str, str]
    ground: str
    warnings: tuple[str, ...] = ()

    def probe_node(self, net: str) -> str:
        """SPICE node for a net name, accepting either form."""
        if net in self.node_of_net:
            return self.node_of_net[net]
        if net in self.net_of_node:
            return net
        raise DeckError([f"no net named {net!r} in this deck"])


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def _sanitize_nodes(
    nets: list[str], ground: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Map net names to SPICE-legal node names, ground to ``0``.

    Collisions after sanitising are resolved with a numeric suffix rather than
    allowed to merge two distinct nets into one node -- which would short two
    parts of the circuit together and simulate perfectly.
    """
    node_of: dict[str, str] = {}
    used: set[str] = {"0"}
    for net in nets:
        if net == ground:
            node_of[net] = "0"
            continue
        base = _SPICE_NODE_RE.sub("_", net) or "n"
        if base[0].isdigit():
            base = f"n_{base}"
        candidate = base
        index = 1
        while candidate.lower() in {u.lower() for u in used}:
            index += 1
            candidate = f"{base}_{index}"
        used.add(candidate)
        node_of[net] = candidate
    net_of = {node: net for net, node in node_of.items()}
    return node_of, net_of


def _resolve_ground(spec: CircuitSpec, bench: Testbench, nets: list[str]) -> str:
    if bench.ground is not None:
        if bench.ground not in nets:
            raise DeckError(
                [
                    f"testbench ground {bench.ground!r} is not a net in this "
                    f"circuit (nets: {sorted(nets)[:8]}...)"
                ]
            )
        return bench.ground
    upper = {net.upper(): net for net in nets}
    for candidate in GROUND_NAMES:
        if candidate in upper:
            return upper[candidate]
    raise DeckError(
        [
            "no ground reference: none of the nets is named "
            + "/".join(GROUND_NAMES[:5])
            + f" (nets: {sorted(nets)[:8]}). SPICE solves relative to node 0; "
            "set Testbench.ground to the net that should be it."
        ]
    )


def build_deck(spec: CircuitSpec, bench: Testbench) -> SpiceDeck:
    """Translate a validated circuit plus a testbench into a SPICE deck.

    Raises :class:`~silkscreen.spice.errors.DeckError` when the testbench and
    circuit disagree, and
    :class:`~silkscreen.spice.errors.UnsimulatableError` when a part has no
    behaviour available. Every problem of the first kind is collected so a
    repair prompt can address them together, the same convention
    :func:`~silkscreen.netlist.parse_circuit_spec` follows.
    """
    spec.validate()

    nets = [conn.net for conn in spec.connections]
    errors = _validate_testbench(bench)
    warnings: list[str] = []

    # Warnings the caller declared it knows about. Kept apart from ``warnings``
    # rather than removed from it, because an acknowledged substitution is
    # still a substitution and must still reach the result: what the
    # acknowledgement buys is that ``strict`` does not refuse the run.
    acknowledged: list[str] = []
    part_names = {p.name for p in spec.passives} | {d.name for d in spec.devices}
    accepted = set(bench.accept_generic_models)
    for unknown_part in sorted(accepted - part_names):
        errors.append(
            f"accept_generic_models names {unknown_part!r}, which the circuit "
            f"does not have (parts: {sorted(part_names)[:8]})"
        )

    def _note_generic(model: SubcircuitModel, part: str) -> None:
        """Disclose a generic stand-in, and record whether it was declared."""
        provenance = f" {model.provenance}" if model.provenance else ""
        text = (
            f"{part} is simulated with a generic stand-in ({model.name}), not "
            f"with a model of the part. Results are about a part of that "
            f"class, not about {part}.{provenance}"
        )
        warnings.append(text)
        if part in accepted:
            acknowledged.append(text)

    # Validate every value that can reach the simulator before resolving nets
    # or rendering cards. This is both a clearer error boundary and prevents a
    # malformed direct Python Testbench from failing with an incidental
    # TypeError halfway through deck construction.
    if errors:
        raise DeckError(errors)

    ground = _resolve_ground(spec, bench, nets)
    node_of, net_of = _sanitize_nodes(nets, ground)
    refs = spec.assign_refs()

    # --- testbench consistency -------------------------------------------
    source_names: set[str] = set()
    for source in bench.sources:
        if not source.name or source.name[0].upper() not in ("V", "I"):
            errors.append(
                f"source {source.name!r} must start with 'V' or 'I'; SPICE reads "
                f"the element type from the first letter"
            )
        if source.name.upper() in source_names:
            errors.append(f"duplicate source name {source.name!r}")
        source_names.add(source.name.upper())
        terminals = (("positive", source.positive), ("negative", source.negative))
        for terminal, net in terminals:
            if net not in node_of:
                errors.append(
                    f"source {source.name!r} {terminal} terminal is on net "
                    f"{net!r}, which the circuit does not have"
                )
        if source.dc is None and source.ac_magnitude is None and not source.transient:
            warnings.append(
                f"source {source.name!r} has no DC, AC or transient value; "
                f"it will act as a 0 V short"
            )

    for probe in bench.probes:
        if probe not in node_of:
            errors.append(f"probe on net {probe!r}, which the circuit does not have")

    if (
        isinstance(bench.analysis, DCSweep)
        and bench.analysis.source.upper() not in source_names
    ):
        errors.append(
            f"DC sweep names source {bench.analysis.source!r}, "
            f"which the testbench does not define"
        )

    if isinstance(bench.analysis, ACSweep) and not any(
        s.ac_magnitude for s in bench.sources
    ):
        errors.append(
            "AC analysis with no source carrying an AC magnitude: every node "
            "would be exactly zero. Use Source.ac_probe() for the stimulus."
        )

    if errors:
        raise DeckError(errors)

    # --- parts ------------------------------------------------------------
    cards: list[str] = []
    model_cards: list[str] = []
    emitted_models: set[str] = set()
    unsimulatable: list[str] = []
    unsimulatable_reason = ""

    for passive in spec.passives:
        ref = refs[passive.name]
        pins = spec.nets_of(passive.name)
        node1, node2 = node_of[pins["1"]], node_of[pins["2"]]

        if passive.type is PassiveType.CRYSTAL:
            model = bench.models.get(passive.name)
            if model is None:
                unsimulatable.append(passive.name)
                unsimulatable_reason = (
                    "a crystal is a motional RLC network whose values are not in "
                    "the circuit IR. Supply a SubcircuitModel for it, or exclude "
                    "it from the analysis"
                )
                continue
            if isinstance(model, SubcircuitModel):
                cards.append(f"{_element_name('X', ref)} {node1} {node2} {model.name}")
                if model.generic:
                    _note_generic(model, passive.name)
                if model.name not in emitted_models:
                    model_cards.append(model.text)
                    emitted_models.add(model.name)
                continue
            errors.append(
                f"crystal {passive.name!r} needs a SubcircuitModel, not a "
                f"PrimitiveModel"
            )
            continue

        letter = _ELEMENT_LETTER[passive.type]

        if passive.type is PassiveType.DIODE:
            model = bench.models.get(passive.name)
            if isinstance(model, SubcircuitModel):
                cards.append(f"{_element_name('X', ref)} {node1} {node2} {model.name}")
                if model.generic:
                    _note_generic(model, passive.name)
                if model.name not in emitted_models:
                    model_cards.append(model.text)
                    emitted_models.add(model.name)
                continue
            if isinstance(model, PrimitiveModel):
                model_name = model.name
                if model.name not in emitted_models:
                    model_cards.append(model.text)
                    emitted_models.add(model.name)
            else:
                model_name = "SS_GENERIC_D"
                if model_name not in emitted_models:
                    model_cards.append(GENERIC_DIODE)
                    emitted_models.add(model_name)
                described = f" ({passive.value})" if passive.value else ""
                warnings.append(
                    f"diode {passive.name}{described} has no model; simulated "
                    f"with a generic small-signal silicon diode. Results are "
                    f"about a diode, not about this part number."
                )
            cards.append(f"{_element_name(letter, ref)} {node1} {node2} {model_name}")
            continue

        try:
            magnitude, value_warning = parse_value(passive.value, part=ref)
        except ValueSyntaxError as exc:
            errors.append(str(exc))
            continue
        if value_warning:
            warnings.append(value_warning)
        if magnitude <= 0:
            errors.append(
                f"{ref}: value {passive.value!r} is {magnitude:g}; a "
                f"{passive.type.value} must be positive"
            )
            continue
        element = _element_name(letter, ref)
        cards.append(f"{element} {node1} {node2} {format_value(magnitude)}")

    for device in spec.devices:
        ref = refs[device.name]
        model = bench.models.get(device.name)
        if model is None:
            unsimulatable.append(device.name)
            unsimulatable_reason = (
                "a device in the Silkscreen IR is a pin map with no behaviour. "
                "Supply Testbench.models[name] = SubcircuitModel(...) with the "
                "part's SPICE model to simulate it"
            )
            continue
        if not isinstance(model, SubcircuitModel):
            errors.append(
                f"device {device.name!r} needs a SubcircuitModel; got "
                f"{type(model).__name__}"
            )
            continue

        connected = spec.nets_of(device.name)
        nodes: list[str] = []
        for pin_name in model.pins:
            if pin_name not in device.pins:
                errors.append(
                    f"subcircuit for {device.name!r} lists terminal {pin_name!r}, "
                    f"which is not a pin of the device "
                    f"(pins: {sorted(device.pins)[:8]})"
                )
                nodes.append("0")
                continue
            net = connected.get(pin_name)
            if net is None:
                dangling = f"{ref}_{_SPICE_NODE_RE.sub('_', pin_name)}_nc"
                nodes.append(dangling)
                warnings.append(
                    f"{ref} pin {pin_name!r} is unconnected in the circuit; "
                    f"simulated as a floating node ({dangling})"
                )
                continue
            nodes.append(node_of[net])
        cards.append(f"{_element_name('X', ref)} {' '.join(nodes)} {model.name}")
        if model.generic:
            _note_generic(model, device.name)
        if model.name not in emitted_models:
            model_cards.append(model.text)
            emitted_models.add(model.name)

    if unsimulatable:
        raise UnsimulatableError(unsimulatable, unsimulatable_reason)
    if errors:
        raise DeckError(errors)

    if bench.strict:
        # An acknowledged generic substitution does not promote: the caller
        # named the part, so the substitution is declared rather than silent,
        # and silence is the thing strict exists to catch. Everything else,
        # including a generic the caller did not name, still refuses.
        unacknowledged = [w for w in warnings if w not in acknowledged]
        if unacknowledged:
            raise DeckError(
                [
                    f"strict testbench, warning promoted to error: {w}"
                    for w in unacknowledged
                ]
            )

    # --- assemble ---------------------------------------------------------
    lines = [f"* {bench.title}"]
    lines.extend(cards)
    lines.extend(source.card(node_of) for source in bench.sources)
    lines.extend(model_cards)
    if bench.temperature_c is not None:
        lines.append(f".temp {bench.temperature_c:g}")
    lines.extend(f".options {opt}" for opt in bench.options)

    return SpiceDeck(
        text="\n".join(lines) + "\n",
        analysis=bench.analysis,
        net_of_node=net_of,
        node_of_net=node_of,
        refs=refs,
        ground=ground,
        warnings=tuple(warnings),
    )

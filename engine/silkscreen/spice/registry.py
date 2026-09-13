"""A trusted registry mapping a part to a subcircuit model, with provenance.

The gap this closes: a :class:`~silkscreen.netlist.Device` in the Silkscreen IR
is a pin map with no behaviour, so every circuit holding an IC came back
``unsimulatable`` and the one thing in this repo that checks *function* could
only ever check RC networks. The fix is not to let a model invent a
``.SUBCKT`` -- a hallucinated model produces a **simulation that passes**,
which is worse than no simulation, because a passing verdict carries
authority. The fix is a registry the code owns.

Two sources, and they are never confused with each other:

1. **Built-in generic stand-ins** (:mod:`silkscreen.spice.library`), written
   here from published topology, under this repository's licence. Each one is
   marked ``generic`` and that mark travels into the deck's warnings and into
   every result. A generic substitution the caller did not name is promoted to
   an error by ``Testbench(strict=True)``, exactly as the generic-diode
   substitution already was.
2. **Operator-supplied model files** on this machine, found through
   ``SILKSCREEN_SPICE_MODELS``. Manufacturer models are freely downloadable
   and generally not freely redistributable (see the licence discussion in
   :mod:`silkscreen.spice.library`), so this package ships none and reads the
   operator's instead. Their provenance says exactly that: the file's path,
   and a licence this repository has not read and does not assert.

**No third source exists.** Nothing here accepts model text from a language
model, and :func:`~silkscreen.agents.simulate.simulate_circuit` never asks for
any: the testbench JSON schema has no field for one.

**Where this follows KiCad and where it does not.** KiCad's ``eeschema/sim``
layer resolves a symbol to a model through ``Sim.Library``/``Sim.Name`` fields
naming a file the user supplies, indexes ``.subckt`` blocks out of it, and
``SIM_LIBRARY::FindModel`` returns ``nullptr`` on a miss rather than
substituting anything -- the loader and the refusal here are the same shape.
It binds symbol pins to subcircuit terminals through a ``Sim.Pins`` field
holding ``<symbol-pin>=<model-pin>`` pairs (the form
``SCH_IO_KICAD_SEXPR::MigrateSimModel`` writes when it converts a legacy
schematic). This registry has no such field to read -- the Silkscreen IR
carries pin *names* the proposing model chose, and the pin *numbers* it
carries come from the same place -- so binding is by name against an alias
table, and an unmatched terminal is a refusal naming the terminal. A wrong
name refuses; a wrong number would have simulated the wrong circuit quietly,
which is the trade this makes deliberately.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..netlist import CircuitSpec, Device, PassiveType
from .deck import SubcircuitModel
from .library import (
    GENERIC_KIND,
    OPERATOR_KIND,
    SILKSCREEN_LICENCE,
    ModelProvenance,
    linear_regulator_subckt,
    opamp_subckt,
    port_subckt,
    timer555_subckt,
)

__all__ = [
    "DeviceModel",
    "LoadedSubckt",
    "ModelRegistry",
    "Resolution",
    "MODEL_PATH_ENV",
    "default_registry",
    "load_model_files",
    "model_search_paths",
]

#: Colon-separated directories of operator-supplied ``.lib``/``.sub``/``.cir``
#: model files. Named after the module rather than after ngspice so it cannot
#: be mistaken for a variable the simulator itself reads.
MODEL_PATH_ENV = "SILKSCREEN_SPICE_MODELS"

#: Files bigger than this are refused rather than read. A vendor library can be
#: megabytes of models for parts nothing here asked for, and the deck embeds
#: whatever it takes.
MAX_MODEL_FILE_BYTES = 2_000_000

#: Subcircuits taken from one file, at most.
MAX_SUBCKTS_PER_FILE = 500

_MODEL_FILE_SUFFIXES = (".lib", ".sub", ".cir", ".mod", ".spice")

_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(\S+)((?:\s+\S+)*)\s*$", re.IGNORECASE)
_ENDS_RE = re.compile(r"^\s*\.ends\b", re.IGNORECASE)


# ==========================================================================
# Records
# ==========================================================================


@dataclass(frozen=True)
class DeviceModel:
    """One part, the subcircuit standing for it, and where that came from."""

    part: str
    model: SubcircuitModel
    provenance: ModelProvenance

    @property
    def generic(self) -> bool:
        return self.provenance.generic

    def as_dict(self) -> dict[str, object]:
        return {
            "part": self.part,
            "model": self.model.name,
            "generic": self.provenance.generic,
            "kind": self.provenance.kind,
            "source": self.provenance.source,
            "licence": self.provenance.licence,
            "note": self.provenance.note,
        }

    def line(self) -> str:
        """The sentence a report prints for this substitution."""
        return f"{self.part} -> {self.model.name}: {self.provenance.line()}"


@dataclass(frozen=True)
class Resolution:
    """What the registry could and could not cover for one circuit.

    ``uncovered`` and ``reasons`` are kept as a pair rather than folded into
    one string because the caller answers ``unsimulatable`` by naming the
    parts, and the reasons are what a person needs to fix it.
    """

    models: dict[str, SubcircuitModel]
    resolved: tuple[DeviceModel, ...]
    uncovered: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def generic_parts(self) -> tuple[str, ...]:
        return tuple(d.part for d in self.resolved if d.generic)


@dataclass(frozen=True)
class LoadedSubckt:
    """One ``.subckt`` block lifted out of an operator's model file."""

    name: str
    terminals: tuple[str, ...]
    text: str
    path: str


# ==========================================================================
# Pin-name matching
# ==========================================================================


def _normalise_pin(name: str) -> str:
    """Pin name reduced to what an alias table can compare.

    Case, separators and the ``~``/``/`` active-low marks go; ``+`` and ``-``
    stay, because they are the entire difference between an op-amp's two
    inputs.
    """
    text = name.strip().upper().replace("−", "-").replace("–", "-")
    text = text.lstrip("~!/\\")
    return re.sub(r"[^A-Z0-9+\-]", "", text)


#: Role -> the normalised pin names that mean it. Ordered inside each tuple by
#: how unambiguous the spelling is, but matching is set membership: a device
#: pin matches at most one role, and two pins matching the same role is an
#: ambiguity that refuses rather than picking one.
_SUPPLY_POSITIVE = ("VCC", "VDD", "V+", "VS+", "VP", "VPLUS", "VSUP", "VS")
_GROUND = ("GND", "GROUND", "VSS", "AGND", "DGND", "COM", "0")


def _find_role(
    pins: Iterable[str], aliases: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """``(pin_name, error)``. Exactly one match, or nothing, or an ambiguity."""
    wanted = set(aliases)
    hits = [pin for pin in pins if _normalise_pin(pin) in wanted]
    if not hits:
        return None, None
    if len(hits) > 1:
        return None, (
            f"pins {sorted(hits)} all read as the same terminal "
            f"({aliases[0]}); the pin map is ambiguous"
        )
    return hits[0], None


# ==========================================================================
# Part-name families
# ==========================================================================


def _normalise_part(name: str) -> str:
    return re.sub(r"\s+", "", name.strip().upper())


def _alnum(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", _normalise_part(name))


#: 555 timers, bipolar and CMOS, by the one token they all share.
_TIMER555_RE = re.compile(r"^(NE|SE|SA|LM|MC|KA|TLC|TS|LMC|ICM7|CA|NA)?555[A-Z0-9]*$")

#: Op-amp part numbers this registry recognises, mapped to how many amplifiers
#: the part has. The count is cross-checked against the pins actually present:
#: a disagreement refuses rather than choosing one of the two answers.
_OPAMP_SECTIONS: dict[str, int] = {
    "LM741": 1, "UA741": 1, "TL071": 1, "TL081": 1, "MCP6001": 1,
    "MCP601": 1, "OPA340": 1, "LMV321": 1, "LM321": 1, "AD8541": 1,
    "OPA1641": 1, "NE5534": 1, "LM358": 2, "LM2904": 2, "TL072": 2,
    "TL082": 2, "MCP6002": 2, "MCP602": 2, "NE5532": 2, "LM4562": 2,
    "OPA2340": 2, "AD8542": 2, "LM324": 4, "LM2902": 4, "TL074": 4,
    "TL084": 4, "MCP6004": 4, "MCP604": 4, "OPA4340": 4, "AD8544": 4,
}

#: Fixed-output regulator families whose output voltage is a suffix on the
#: part number. The suffix decoder is deliberately narrow -- see
#: :func:`_voltage_from_suffix`.
_FIXED_LDO_BASES = (
    "AMS1117", "LM1117", "LD1117", "AZ1117", "TLV1117", "AP2112K", "AP2112",
    "LP2985", "RT9013", "SPX3819", "MCP1700", "MCP1702", "MCP1703",
    "XC6206P", "XC6206", "TPS7A02", "TLV70", "NCP1117",
)

#: Whole-volt 78xx regulators: the last two digits are the output in volts,
#: a different scheme from every part above, which is why it is its own rule.
_78XX_RE = re.compile(r"^(LM|MC|L|KA|UA|NJM|AN|TA)?78[LMSTC]?(\d\d)[A-Z0-9]*$")

#: Two digits after this base are tenths of a volt (HT7333 is 3.3 V).
_HT73_RE = re.compile(r"^HT73(\d\d)[A-Z0-9]*$")

#: Parts this registry refuses on purpose, each with the reason. Checked before
#: the families, so a specific refusal always beats a generic near-miss.
_REFUSALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^(LM|LT|AZ|NCP|TS)?(117|317|337|1085|1086|1084|3080)[A-Z0-9]*$"),
        "an adjustable regulator's output is set by external resistors, so a "
        "fixed-output stand-in would simulate a different rail than the board "
        "produces",
    ),
    (
        re.compile(
            r"^(ATTINY|ATMEGA|ATSAM|STM32|STM8|ESP32|ESP8266|RP2040|NRF5"
            r"|MSP430|CH32|GD32|PIC1|PIC2|PIC3|AT89|LPC1|SAMD)[A-Z0-9\-]*$"
        ),
        "a microcontroller's behaviour is its firmware; no SPICE model of it "
        "exists, here or anywhere",
    ),
    (
        re.compile(r"^(24[LAC]{1,2}\d|W25Q|AT24|SST25|MX25|IS25)[A-Z0-9\-]*$"),
        "a memory device's behaviour is a digital protocol, not an analogue "
        "response; SPICE is the wrong instrument for it",
    ),
    (
        re.compile(r"^(SSD1306|ST7735|ILI9341|HD44780|SH1106)[A-Z0-9\-]*$"),
        "a display controller has no analogue behaviour worth simulating",
    ),
)


def _voltage_from_suffix(suffix: str) -> float | None:
    """Output volts read out of a part-number suffix, or ``None``.

    Only three spellings are accepted, all of them unambiguous:
    ``3.3``, ``3V3``, and exactly two digits meaning tenths (``33`` is 3.3 V,
    ``50`` is 5.0 V). Manufacturer three- and four-digit order codes
    (``XC6206P332``, ``MCP1700-3302``) are **refused** rather than decoded,
    because the schemes differ between manufacturers and guessing one wrong
    means simulating a rail the board does not have. The refusal names the
    spelling that works.
    """
    text = suffix.strip().upper().strip("-_ ")
    if not text:
        return None
    if re.fullmatch(r"\d+\.\d+", text):
        return float(text)
    if match := re.fullmatch(r"(\d)V(\d)", text):
        return float(f"{match.group(1)}.{match.group(2)}")
    if match := re.fullmatch(r"(\d)(\d)", text):
        return float(f"{match.group(1)}.{match.group(2)}")
    return None


# ==========================================================================
# Operator model files
# ==========================================================================


def model_search_paths(environ: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Directories named by :data:`MODEL_PATH_ENV`, in order."""
    raw = (environ if environ is not None else os.environ).get(MODEL_PATH_ENV, "")
    return tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip())


def load_model_files(paths: Iterable[Path]) -> dict[str, LoadedSubckt]:
    """Index every ``.subckt`` in every model file under ``paths``.

    The same shape as KiCad's ``SIM_LIBRARY_SPICE::ReadFile`` -- walk the file,
    register each subcircuit under its name -- and the same answer on a miss:
    the name is simply absent from the index, and the caller reports that.

    Earlier paths win, so an operator can shadow a shared directory with a
    local one. A file that cannot be read is skipped rather than raising: a
    stray unreadable file in a model directory must not take the whole
    simulation path down.
    """
    found: dict[str, LoadedSubckt] = {}
    for directory in paths:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _MODEL_FILE_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_MODEL_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for block in _subckts_in(text, str(path)):
                found.setdefault(block.name.upper(), block)
    return found


def _subckts_in(text: str, path: str) -> list[LoadedSubckt]:
    """Every complete ``.subckt``/``.ends`` block in one file's text.

    Nested subcircuits are kept inside their parent block rather than indexed
    separately: a nested definition is scoped to its parent in SPICE, and
    lifting it out would produce a block that does not stand alone.
    """
    blocks: list[LoadedSubckt] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines) and len(blocks) < MAX_SUBCKTS_PER_FILE:
        match = _SUBCKT_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        name = match.group(1)
        terminals = tuple(t for t in match.group(2).split() if "=" not in t)
        depth = 1
        body = [lines[index]]
        cursor = index + 1
        while cursor < len(lines) and depth:
            body.append(lines[cursor])
            if _SUBCKT_RE.match(lines[cursor]):
                depth += 1
            elif _ENDS_RE.match(lines[cursor]):
                depth -= 1
            cursor += 1
        if depth == 0 and terminals:
            blocks.append(
                LoadedSubckt(
                    name=name,
                    terminals=terminals,
                    text="\n".join(body),
                    path=path,
                )
            )
        index = cursor
    return blocks


# ==========================================================================
# The registry
# ==========================================================================


def _subckt_name(part: str, prefix: str) -> str:
    """A SPICE-legal subcircuit name that still says which part it is for."""
    body = re.sub(r"[^A-Za-z0-9]", "_", part).strip("_") or "PART"
    return f"{prefix}_{body.upper()}"


class ModelRegistry:
    """Part -> subcircuit, with provenance, or nothing and a reason why.

    ``model_for``/``why_unmatched`` mirror :mod:`silkscreen.models3d`, which
    answers the same shape of question about 3D models: a match is a claim,
    a non-match is a sentence.
    """

    def __init__(
        self,
        *,
        files: dict[str, LoadedSubckt] | None = None,
        builtins: bool = True,
    ) -> None:
        self._files = dict(files or {})
        self._builtins = builtins

    # -- files ---------------------------------------------------------

    @property
    def file_models(self) -> dict[str, LoadedSubckt]:
        return dict(self._files)

    def _from_files(self, device: Device) -> tuple[DeviceModel | None, str]:
        """An operator-supplied subcircuit for this device, if one matches.

        Matching is on the part name (case-insensitive, and again with every
        non-alphanumeric character folded to ``_``, because a file names a
        subcircuit ``AMS1117_33`` where the spec says ``AMS1117-3.3``).
        """
        part = _normalise_part(device.name)
        candidates = {part, re.sub(r"[^A-Z0-9]", "_", part), _alnum(part)}
        block = next(
            (self._files[key] for key in candidates if key in self._files), None
        )
        if block is None:
            return None, ""

        bound: list[str] = []
        for terminal in block.terminals:
            wanted = _normalise_pin(terminal)
            hits = [p for p in device.pins if _normalise_pin(p) == wanted]
            if len(hits) != 1:
                return None, (
                    f"{device.name}: the subcircuit {block.name!r} in "
                    f"{block.path} has a terminal {terminal!r} that matches "
                    f"{len(hits)} pin(s) of the part (pins: "
                    f"{sorted(device.pins)[:8]}). Rename the pin or the "
                    f"terminal so the two agree; this registry binds by name "
                    f"because it has no field saying which terminal is which."
                )
            bound.append(hits[0])

        provenance = ModelProvenance(
            source=f"operator-supplied file {block.path}",
            licence=(
                "not asserted by silkscreen; whatever the operator's file "
                "carries"
            ),
            generic=False,
            kind=OPERATOR_KIND,
            note=(
                "This repository has not read, verified or vetted this model. "
                "It is trusted exactly as far as the operator who installed it."
            ),
        )
        model = SubcircuitModel(
            name=block.name,
            pins=tuple(bound),
            text=block.text,
            generic=False,
            provenance=provenance.line(),
        )
        return DeviceModel(device.name, model, provenance), ""

    # -- built-ins -----------------------------------------------------

    def _port(self, device: Device) -> DeviceModel:
        pins = sorted(device.pins)
        terminals = tuple(f"T{index}" for index in range(1, len(pins) + 1))
        name = _subckt_name(device.name, "SS_PORT")
        provenance = ModelProvenance(
            source="silkscreen.spice.library.port_subckt",
            licence=SILKSCREEN_LICENCE,
            generic=True,
            kind=GENERIC_KIND,
            note=(
                f"A {device.kind} is where the circuit stops and the outside "
                "world starts: modelled as open terminals with no source and "
                "no contact resistance. Whatever it connects to must come "
                "from the testbench."
            ),
        )
        return DeviceModel(
            device.name,
            SubcircuitModel(
                name=name,
                pins=tuple(pins),
                text=port_subckt(name, terminals),
                generic=True,
                provenance=provenance.line(),
            ),
            provenance,
        )

    def _opamp(self, device: Device, declared: int) -> tuple[DeviceModel | None, str]:
        pins = list(device.pins)
        sections: list[tuple[str, str, str]] = []
        errors: list[str] = []

        def role(aliases: tuple[str, ...]) -> str | None:
            pin, err = _find_role(pins, aliases)
            if err:
                errors.append(f"{device.name}: {err}")
            return pin

        if declared == 1:
            plus = role(("IN+", "+IN", "INP", "NONINV", "VIN+", "IN1+", "+INA"))
            minus = role(("IN-", "-IN", "INN", "INV", "VIN-", "IN1-", "-INA"))
            out = role(("OUT", "VOUT", "OUTPUT", "OUT1", "OUTA"))
            if plus and minus and out:
                sections.append((plus, minus, out))
        else:
            for index in range(1, declared + 1):
                letter = "ABCD"[index - 1]
                plus = role(
                    (f"IN{index}+", f"+IN{index}", f"{index}IN+", f"INP{index}",
                     f"IN{letter}+", f"+IN{letter}")
                )
                minus = role(
                    (f"IN{index}-", f"-IN{index}", f"{index}IN-", f"INN{index}",
                     f"IN{letter}-", f"-IN{letter}")
                )
                out = role((f"OUT{index}", f"{index}OUT", f"OUT{letter}"))
                if plus and minus and out:
                    sections.append((plus, minus, out))

        vp = role(_SUPPLY_POSITIVE)
        vn_pin, vn_err = _find_role(pins, ("VEE", "VSS", "V-", "VS-", "VMINUS"))
        if vn_err:
            errors.append(f"{device.name}: {vn_err}")
        if vn_pin is None:
            vn_pin, vn_err = _find_role(pins, _GROUND)
            if vn_err:
                errors.append(f"{device.name}: {vn_err}")

        if errors:
            return None, "; ".join(errors)
        if len(sections) != declared:
            return None, (
                f"{device.name}: the part number says {declared} amplifier "
                f"section(s) but only {len(sections)} could be read off the "
                f"pin map (pins: {sorted(device.pins)}). A stand-in wired to "
                f"the wrong pins simulates a different circuit, so this "
                f"refuses instead of guessing."
            )
        if vp is None or vn_pin is None:
            missing = "positive supply" if vp is None else "negative supply/ground"
            return None, (
                f"{device.name}: no pin reads as the {missing} "
                f"(pins: {sorted(device.pins)})"
            )

        name = _subckt_name(device.name, "SS_OPAMP")
        ordered: list[str] = []
        for plus, minus, out in sections:
            ordered.extend([plus, minus, out])
        ordered.extend([vp, vn_pin])
        provenance = ModelProvenance(
            source="silkscreen.spice.library.opamp_subckt",
            licence=SILKSCREEN_LICENCE,
            generic=True,
            kind=GENERIC_KIND,
            note=(
                "A single-pole macromodel standing in for a general-purpose "
                "op-amp: 100 dB open loop, 1 MHz gain-bandwidth, ideal inputs. "
                "It carries no offset, no bias current, no input common-mode "
                "limit, no slew-rate limit and no second pole, so it cannot "
                "answer a stability, noise or offset question about "
                f"{device.name}."
            ),
        )
        return (
            DeviceModel(
                device.name,
                SubcircuitModel(
                    name=name,
                    pins=tuple(ordered),
                    text=opamp_subckt(name, declared),
                    generic=True,
                    provenance=provenance.line(),
                ),
                provenance,
            ),
            "",
        )

    def _timer555(self, device: Device) -> tuple[DeviceModel | None, str]:
        pins = list(device.pins)
        wanted: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("GND", _GROUND),
            ("TRIG", ("TRIG", "TRIGGER", "TR")),
            ("OUT", ("OUT", "OUTPUT", "Q")),
            ("RESET", ("RESET", "RST", "R", "MR")),
            ("CTRL", ("CTRL", "CONT", "CONTROL", "CV", "CTL", "VC")),
            ("THRES", ("THRES", "THRESHOLD", "THR", "TH")),
            ("DISCH", ("DISCH", "DISCHARGE", "DIS", "DSCHG")),
            ("VCC", ("VCC", "VDD", "V+", "VS", "VPLUS")),
        )
        ordered: list[str] = []
        missing: list[str] = []
        errors: list[str] = []
        for role, aliases in wanted:
            pin, err = _find_role(pins, aliases)
            if err:
                errors.append(f"{device.name}: {err}")
            elif pin is None:
                missing.append(role)
            else:
                ordered.append(pin)
        if errors:
            return None, "; ".join(errors)
        if missing:
            return None, (
                f"{device.name}: a 555 needs all eight terminals and no pin "
                f"reads as {', '.join(missing)} (pins: {sorted(device.pins)})"
            )

        name = _subckt_name(device.name, "SS_555")
        provenance = ModelProvenance(
            source="silkscreen.spice.library.timer555_subckt",
            licence=SILKSCREEN_LICENCE,
            generic=True,
            kind=GENERIC_KIND,
            note=(
                "A behavioural 555 built from the datasheet block diagram: two "
                "comparators on a 5k/5k/5k divider, a latch, a discharge "
                "switch. Timing follows from the divider and is the thing it "
                "gets right; supply current, edge rates, comparator delay and "
                f"any CMOS variant's rail-to-rail output are not modelled, so "
                f"it is a 555, not {device.name}."
            ),
        )
        return (
            DeviceModel(
                device.name,
                SubcircuitModel(
                    name=name,
                    pins=tuple(ordered),
                    text=timer555_subckt(name),
                    generic=True,
                    provenance=provenance.line(),
                ),
                provenance,
            ),
            "",
        )

    def _regulator(
        self, device: Device, vout: float
    ) -> tuple[DeviceModel | None, str]:
        pins = list(device.pins)
        errors: list[str] = []

        def role(aliases: tuple[str, ...]) -> str | None:
            pin, err = _find_role(pins, aliases)
            if err:
                errors.append(f"{device.name}: {err}")
            return pin

        vin = role(("VIN", "IN", "INPUT", "VI", "V+IN", "VINPUT"))
        vout_pin = role(("VOUT", "OUT", "OUTPUT", "VO", "VOUTPUT"))
        gnd = role(("GND", "GROUND", "VSS", "COM", "ADJ/GND"))
        enable = role(("EN", "ENABLE", "SHDN", "CE", "ON/OFF"))
        if errors:
            return None, "; ".join(errors)
        missing = [
            label
            for label, pin in (("VIN", vin), ("VOUT", vout_pin), ("GND", gnd))
            if pin is None
        ]
        if missing:
            return None, (
                f"{device.name}: no pin reads as {', '.join(missing)} "
                f"(pins: {sorted(device.pins)})"
            )
        assert vin is not None and vout_pin is not None and gnd is not None

        claimed = {vin, vout_pin, gnd} | ({enable} if enable else set())
        leftover = sorted(set(device.pins) - claimed)
        known_spare = [
            pin
            for pin in leftover
            if _normalise_pin(pin) in {"NC", "BYP", "NR", "PAD", "TAB", "SENSE"}
        ]
        if len(known_spare) != len(leftover):
            unknown = sorted(set(leftover) - set(known_spare))
            return None, (
                f"{device.name}: pin(s) {unknown} are not terminals of a "
                f"three-terminal regulator and this registry will not guess "
                f"what they do"
            )

        name = _subckt_name(device.name, "SS_LDO")
        spares = tuple(f"SPARE{i}" for i in range(1, len(known_spare) + 1))
        ordered = [gnd, vout_pin, vin]
        if enable:
            ordered.append(enable)
        ordered.extend(known_spare)
        provenance = ModelProvenance(
            source="silkscreen.spice.library.linear_regulator_subckt",
            licence=SILKSCREEN_LICENCE,
            generic=True,
            kind=GENERIC_KIND,
            note=(
                f"A generic fixed {vout:g} V linear regulator: the output is "
                "min(nameplate, input minus dropout) behind a small output "
                "resistance, and there is no control loop inside it at all. "
                "It answers headroom and load regulation and nothing else -- "
                "not stability, not output-capacitor ESR, not transient "
                "response, not PSRR, not current limit, which are the usual "
                f"reasons to simulate {device.name}."
            ),
        )
        return (
            DeviceModel(
                device.name,
                SubcircuitModel(
                    name=name,
                    pins=tuple(ordered),
                    text=linear_regulator_subckt(
                        name, vout, enable=bool(enable), extra_pins=spares
                    ),
                    generic=True,
                    provenance=provenance.line(),
                ),
                provenance,
            ),
            "",
        )

    # -- dispatch ------------------------------------------------------

    def _builtin(self, device: Device) -> tuple[DeviceModel | None, str]:
        if device.kind in {"connector", "battery"}:
            return self._port(device), ""

        part = _normalise_part(device.name)
        flat = _alnum(part)

        for pattern, reason in _REFUSALS:
            if pattern.match(part) or pattern.match(flat):
                return None, f"{device.name}: {reason}"

        if _TIMER555_RE.match(flat):
            return self._timer555(device)

        for base, sections in _OPAMP_SECTIONS.items():
            if flat.startswith(base):
                return self._opamp(device, sections)

        if match := _78XX_RE.match(flat):
            return self._regulator(device, float(int(match.group(2))))
        if match := _HT73_RE.match(flat):
            volts = _voltage_from_suffix(match.group(1))
            if volts is None:
                return None, f"{device.name}: could not read an output voltage"
            return self._regulator(device, volts)
        for base in _FIXED_LDO_BASES:
            if not part.startswith(base):
                continue
            volts = _voltage_from_suffix(part[len(base) :])
            if volts is None:
                return None, (
                    f"{device.name}: this is a fixed-output regulator family "
                    f"but the output voltage cannot be read from the part "
                    f"name. Only '3.3', '3V3' and two digits meaning tenths "
                    f"('33') are accepted -- manufacturer order codes are not "
                    f"decoded, because the schemes differ and guessing wrong "
                    f"means simulating a rail the board does not have. Name "
                    f"the part '{base}-3.3' to get a model."
                )
            return self._regulator(device, volts)

        return None, (
            f"{device.name}: no model. This registry holds generic stand-ins "
            f"for op-amps, 555 timers, fixed linear regulators and "
            f"connectors, and reads real models from files named by "
            f"{MODEL_PATH_ENV}; this part matches none of them."
        )

    # -- public --------------------------------------------------------

    def model_for(self, device: Device) -> DeviceModel | None:
        """The model for this device, or ``None``. See :meth:`why_unmatched`.

        An operator-supplied file wins over a built-in stand-in, always: the
        whole point of a generic is that it is what you use when you have
        nothing better, and the operator installing a real model is something
        better.
        """
        found, reason = self._from_files(device)
        if found is not None:
            return found
        if reason:
            # A file names this part and its terminals could not be bound.
            # Falling through to a stand-in here would answer a question about
            # the operator's own model with a generic one, silently, which is
            # the substitution this whole module refuses to make.
            return None
        if not self._builtins:
            return None
        found, _ = self._builtin(device)
        return found

    def why_unmatched(self, device: Device) -> str:
        """Why there is no model for this device, in one sentence.

        Never "not found": a person reading this needs to know whether to
        rename a pin, name the voltage in the part number, install a model
        file, or accept that the part cannot be simulated at all.
        """
        found, reason = self._from_files(device)
        if found is not None:
            return ""
        if reason:
            return reason
        if not self._builtins:
            return (
                f"{device.name}: no operator-supplied model file defines a "
                f"subcircuit for it, and the built-in stand-ins are switched "
                f"off"
            )
        found, reason = self._builtin(device)
        if found is not None:
            return ""
        return reason

    def resolve(self, spec: CircuitSpec) -> Resolution:
        """Every device in one circuit, covered or named as uncovered.

        Crystals are always uncovered and say why: a motional RLC network is
        four numbers per part that the IR does not carry, and a stand-in built
        from the nominal frequency alone would set the oscillator's frequency
        by assumption rather than measure it.
        """
        models: dict[str, SubcircuitModel] = {}
        resolved: list[DeviceModel] = []
        uncovered: list[str] = []
        reasons: list[str] = []

        for device in spec.devices:
            found = self.model_for(device)
            if found is None:
                uncovered.append(device.name)
                reasons.append(self.why_unmatched(device))
                continue
            models[device.name] = found.model
            resolved.append(found)

        for passive in spec.passives:
            if passive.type is not PassiveType.CRYSTAL:
                continue
            uncovered.append(passive.name)
            reasons.append(
                f"{passive.name}: a crystal is a motional RLC network whose "
                f"four values are not in the circuit IR, and a stand-in built "
                f"from the nominal frequency would decide the oscillator's "
                f"frequency instead of measuring it"
            )

        return Resolution(
            models=models,
            resolved=tuple(resolved),
            uncovered=tuple(uncovered),
            reasons=tuple(reasons),
        )


def default_registry(environ: dict[str, str] | None = None) -> ModelRegistry:
    """The registry a caller gets when it does not build one.

    Built-in stand-ins plus whatever :data:`MODEL_PATH_ENV` points at on this
    machine. Reading the environment here rather than at import time keeps the
    suite's control over it, the ``model_factory`` convention the service uses.
    """
    return ModelRegistry(files=load_model_files(model_search_paths(environ)))

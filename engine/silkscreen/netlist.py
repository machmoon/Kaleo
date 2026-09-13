"""Validated intermediate representation for a circuit.

The original pipeline passed raw ``json.loads`` output from an LLM directly into
SKiDL part construction. Every downstream bug traced back to that: unvalidated
pin numbers, component types outside the supported set, references to nets that
were never created, and silently dropped connections.

This module is the contract. An LLM proposes a :class:`CircuitSpec`; nothing
touches KiCad until it validates. Failures name the offending field so they can
be fed back to the model for repair rather than crashing a worker thread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

# The one place this module looks outward. A package name is only meaningful
# if some land-pattern generator can draw it, and the whole value of the
# batched ``ValidationError`` is that the model hears about a package nobody
# can build in the same repair prompt as everything else -- not later, from
# ``build_board``, where nothing can fix it. ``footprints`` imports only
# ``units``, so this does not make the IR depend on KiCad or create a cycle.
from .footprints import (
    BATTERY_PACKAGES,
    CONNECTOR_PACKAGES,
    SWITCH_PACKAGES,
    TESTPOINT_PACKAGES,
)

__all__ = [
    "PassiveType",
    "Passive",
    "Device",
    "Connection",
    "CircuitSpec",
    "ValidationError",
    "KINDS",
    "REF_PREFIX",
    "parse_circuit_spec",
]

#: Reference designator prefixes KiCad expects for each passive type.
_REF_PREFIX = {
    "resistor": "R",
    "capacitor": "C",
    "inductor": "L",
    "diode": "D",
    "crystal": "Y",
}

#: What a :class:`Device` *is*, mechanically. Before this existed every device
#: was an IC and ``board._footprint_for_device`` chose its land pattern from
#: the pin count alone -- so a two-pin power connector was padded to four pins
#: and drawn as a SOIC-4, a surface-mount chip. The board then had no power
#: input, the schematic claimed one, the case cut no hole, and nothing raised.
#: The kind is what the board dispatches on, ahead of the count.
#:
#: ``switch`` and ``testpoint`` are here for the same reason and not as
#: sub-cases of ``connector``: the reference designator is part of what a
#: reader and ``sourcing.bom_rows`` (which reads the ref prefix) understand by
#: the part, and a button called ``J3`` is a button nobody can find on the
#: board.
KINDS = ("ic", "connector", "battery", "switch", "testpoint")

#: Reference designator prefix per kind. KiCad's own convention: U for an
#: integrated circuit, J for a connector, BT for a battery or its holder,
#: SW for a switch, TP for a test point.
REF_PREFIX = {
    "ic": "U",
    "connector": "J",
    "battery": "BT",
    "switch": "SW",
    "testpoint": "TP",
}

#: Kinds whose land pattern is selected by name rather than by pin count, so a
#: ``package`` is mandatory for them and meaningless on an IC.
_PACKAGED_KINDS = ("connector", "battery", "switch", "testpoint")

_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./+-]*$")


class ValidationError(ValueError):
    """Raised when a proposed circuit is not internally consistent.

    ``errors`` holds one human-readable message per problem so the whole batch
    can be returned to a model in a single repair prompt.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in circuit spec:\n  - "
            + "\n  - ".join(errors)
        )


class PassiveType(StrEnum):
    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    INDUCTOR = "inductor"
    DIODE = "diode"
    CRYSTAL = "crystal"


@dataclass(frozen=True)
class Passive:
    """A two-terminal auxiliary part. Pins are always 1 and 2."""

    name: str
    type: PassiveType
    value: str

    @property
    def ref_prefix(self) -> str:
        return _REF_PREFIX[self.type.value]


def normalise_pin_number(value: object) -> str:
    """One spelling per physical pin.

    A model writes the same pin as ``"1"``, ``"01"`` or ``" 1 "`` from one line
    of a datasheet to the next, and every one of those reaches a different
    consumer: the schematic keys its nets on the string it was handed, while
    the board looks the pad up by exact number and silently finds nothing.
    Collapsing numeric spellings here means the ambiguity is gone before
    validation runs, so the duplicate-number rule below sees the aliases as
    the one pin they are. Non-numeric numbers (BGA ``"A1"``) keep their text.
    """
    text = str(value).strip()
    # ``isdecimal`` rather than ``int``, which also accepts ``"+1"`` and
    # ``"1_0"`` -- neither is a pin number, and quietly rewriting them would
    # hide a malformed spec instead of letting validation see it. Not
    # ``isdigit`` either: that is true of superscripts, which ``int`` rejects.
    return str(int(text)) if text.isdecimal() else text


@dataclass(frozen=True)
class Device:
    """A main part: an IC, a connector, or a battery holder.

    ``pins`` maps a datasheet pin *name* (e.g. ``"AVDD"``) to its physical pin
    number. The original code let the model invent both sides of this mapping
    and never checked the numbers against the symbol it actually instantiated.
    """

    name: str
    pins: dict[str, str]
    #: Resolved KiCad symbol, e.g. ``"MCU_ST_STM32F0:STM32F030C8Tx"``. Left
    #: unset until symbol resolution runs, so this module stays KiCad-free.
    symbol: str | None = None
    #: One of :data:`KINDS`. Defaults to ``"ic"`` so every spec written before
    #: connectors existed stays valid and keeps its ``U`` reference.
    kind: str = "ic"
    #: The named land pattern, required for a connector or a battery and an
    #: error on an IC (an IC's package follows from its pin count).
    package: str | None = None

    def pin_names(self) -> set[str]:
        return set(self.pins)

    @property
    def ref_prefix(self) -> str:
        """The designator prefix for this kind, ``"U"`` for anything unknown.

        A bad ``kind`` is a validation error, not a numbering error, so this
        falls back rather than raising: ``validate`` has already collected it
        and a second exception here would escape the batch.
        """
        return REF_PREFIX.get(self.kind, "U")


@dataclass(frozen=True)
class Connection:
    """One electrical net: a named node joined to a set of endpoints.

    An endpoint is either ``"<device>.<pin_name>"`` or ``"<passive>.1"`` /
    ``"<passive>.2"``. It is split on the **last** dot, because real part names
    contain dots ("AMS1117-3.3", "LM317-2.5") while pin names never do.
    Requiring an explicit terminal is the fix for the
    original's inability to express "cap leg 1 to AVDD, leg 2 to AVSS" -- it
    only ever connected whole parts to nets, never specific pins.
    """

    net: str
    endpoints: tuple[str, ...]


def _packages_for(kind: str) -> dict[str, int]:
    """The package vocabulary for one kind: ``{name: pin count}``."""
    return {
        "connector": CONNECTOR_PACKAGES,
        "battery": BATTERY_PACKAGES,
        "switch": SWITCH_PACKAGES,
        "testpoint": TESTPOINT_PACKAGES,
    }[kind]


def _kind_errors(devices: list[Device]) -> list[str]:
    """Every ``kind``/``package`` problem across all devices, as messages.

    Returns rather than raises, because this module's whole contract is that
    the model gets one repair prompt holding every failure at once. Stopping
    at the first bad package would spend a round trip per device.
    """
    errors: list[str] = []
    for dev in devices:
        if dev.kind not in KINDS:
            errors.append(
                f"device {dev.name!r} has unknown kind {dev.kind!r}; "
                f"allowed: {list(KINDS)}"
            )
            continue
        if dev.kind in _PACKAGED_KINDS:
            known = _packages_for(dev.kind)
            if dev.package is None:
                errors.append(
                    f"{dev.kind} {dev.name!r} needs a 'package'; its land "
                    f"pattern cannot be guessed from the pin count. "
                    f"Choose one of: {sorted(known)}"
                )
            elif dev.package not in known:
                errors.append(
                    f"{dev.kind} {dev.name!r} has unsupported package "
                    f"{dev.package!r}; supported: {sorted(known)}"
                )
        elif dev.package is not None:
            # An IC's land pattern comes from its pin count, so a package here
            # is either a name the builder will ignore -- silently drawing
            # something else -- or a connector that forgot to say so.
            errors.append(
                f"device {dev.name!r} is an 'ic' but names package "
                f"{dev.package!r}; an IC's package follows from its pin "
                f"count. Set 'kind' to one of {list(_PACKAGED_KINDS)} if this "
                f"is not an IC."
            )
    return errors


@dataclass
class CircuitSpec:
    devices: list[Device] = field(default_factory=list)
    passives: list[Passive] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)

    def validate(self) -> None:
        """Check internal consistency. Raises :class:`ValidationError`."""
        errors: list[str] = []

        # A spec with no parts passes every other check vacuously. Without this
        # a model that returns the wrong JSON shape entirely -- no "devices",
        # no "nets" -- yields a valid empty circuit and a board with nothing on
        # it, silently.
        if not self.devices and not self.passives:
            errors.append(
                "circuit contains no devices and no passives; "
                "the response was probably not a circuit at all"
            )

        device_by_name = {d.name: d for d in self.devices}
        passive_by_name = {p.name: p for p in self.passives}

        for name in list(device_by_name) + list(passive_by_name):
            if not _IDENT_RE.match(name):
                errors.append(f"part name {name!r} is not a valid identifier")

        dupes = set(device_by_name) & set(passive_by_name)
        for name in sorted(dupes):
            errors.append(f"{name!r} is declared as both a device and a passive")

        if len(device_by_name) != len(self.devices):
            errors.append("duplicate device names")
        if len(passive_by_name) != len(self.passives):
            errors.append("duplicate passive names")

        seen_nets: set[str] = set()
        for conn in self.connections:
            if conn.net in seen_nets:
                errors.append(f"net {conn.net!r} is declared more than once")
            seen_nets.add(conn.net)

            if len(conn.endpoints) < 2:
                errors.append(
                    f"net {conn.net!r} has {len(conn.endpoints)} endpoint(s); "
                    f"a net joining fewer than 2 pins is not a connection"
                )

            for ep in conn.endpoints:
                if "." not in ep:
                    errors.append(
                        f"net {conn.net!r}: endpoint {ep!r} must be "
                        f"'<part>.<pin>', not a bare part name"
                    )
                    continue
                part, _, pin = ep.rpartition(".")
                if part in device_by_name:
                    dev = device_by_name[part]
                    if pin not in dev.pins:
                        errors.append(
                            f"net {conn.net!r}: {part!r} has no pin named "
                            f"{pin!r} (known: {sorted(dev.pins)[:8]}...)"
                        )
                elif part in passive_by_name:
                    if pin not in ("1", "2"):
                        errors.append(
                            f"net {conn.net!r}: passive {part!r} has only "
                            f"pins 1 and 2, got {pin!r}"
                        )
                else:
                    errors.append(
                        f"net {conn.net!r}: endpoint {ep!r} refers to unknown "
                        f"part {part!r}"
                    )

        # One physical pin, one net. Two nets sharing a pin are electrically
        # one net, so every consumer downstream has to guess which name wins:
        # nets_of() keeps the last, the schematic's labels disagree with each
        # other, and the board's pad-to-net map takes a third answer. Nothing
        # raises and the two files describe different circuits. Caught here so
        # the repair loop sends it back to the model, which is the only place
        # that knows which net was meant.
        pin_nets: dict[str, list[str]] = {}
        for conn in self.connections:
            for ep in conn.endpoints:
                nets = pin_nets.setdefault(ep, [])
                if conn.net not in nets:
                    nets.append(conn.net)
        for ep, nets in pin_nets.items():
            if len(nets) > 1:
                errors.append(
                    f"pin {ep!r} is on {len(nets)} nets ({', '.join(sorted(nets))}); "
                    f"a pin joins exactly one net -- if these are the same node, "
                    f"merge them into one net"
                )

        # Two pin names on one pin number is the same ambiguity one level down:
        # the number is what reaches the footprint and the symbol, so the
        # second name silently overwrites the first and one specified
        # connection disappears from both files.
        for dev in self.devices:
            by_number: dict[str, list[str]] = {}
            for pin_name, number in dev.pins.items():
                by_number.setdefault(str(number), []).append(pin_name)
            for number, names in by_number.items():
                if len(names) > 1:
                    errors.append(
                        f"device {dev.name!r} maps {len(names)} pin names "
                        f"({', '.join(sorted(names))}) to pin number {number!r}; "
                        f"each pin number names one physical pin"
                    )

        errors.extend(_kind_errors(self.devices))

        # A passive wired on only one leg is almost always a model error, and
        # the original silently emitted these as floating parts.
        for passive in self.passives:
            legs = {
                ep.rpartition(".")[2]
                for conn in self.connections
                for ep in conn.endpoints
                if ep.rpartition(".")[0] == passive.name
            }
            missing = {"1", "2"} - legs
            if missing:
                errors.append(
                    f"passive {passive.name!r} has no connection on pin(s) "
                    f"{sorted(missing)}; it would be left floating"
                )

        if errors:
            raise ValidationError(errors)

    def assign_refs(self) -> dict[str, str]:
        """Map each part's spec name to its reference designator.

        The schematic and the board must agree on what ``C3`` is, or the two
        files describe different circuits while both looking plausible. Both
        emitters call this rather than numbering parts themselves, so the
        mapping is defined once: devices first in spec order, then passives in
        spec order, each counted per prefix.

        Devices no longer all take ``U``: a connector is ``J``, a battery
        ``BT``, a switch ``SW`` and a test point ``TP``
        (:data:`REF_PREFIX`), counted independently, so one pass over the
        devices in spec order gives ``U1..Un``, ``J1..Jn``, ``BT1..BTn`` and
        so on.
        """
        counters: dict[str, int] = {}

        def next_ref(prefix: str) -> str:
            counters[prefix] = counters.get(prefix, 0) + 1
            return f"{prefix}{counters[prefix]}"

        refs = {device.name: next_ref(device.ref_prefix) for device in self.devices}
        for passive in self.passives:
            refs[passive.name] = next_ref(passive.ref_prefix)
        return refs

    def nets_of(self, part_name: str) -> dict[str, str]:
        """``{pin_name: net}`` for one part, from the connection list.

        A pin absent from every net is absent from the mapping: an unconnected
        pin and a pin tied to a net named ``""`` are different circuits.
        """
        found: dict[str, str] = {}
        for conn in self.connections:
            for endpoint in conn.endpoints:
                part, _, pin = endpoint.rpartition(".")
                if part == part_name:
                    found[pin] = conn.net
        return found

    def part_count(self) -> int:
        return len(self.devices) + len(self.passives)

    def net_count(self) -> int:
        return len(self.connections)


def _strip_code_fence(text: str) -> str:
    """Remove a ``` fence if the model wrapped its JSON in one.

    The original prompt's own example output was fenced, so fenced responses
    were both likely and fatal -- ``json.loads`` raised inside a worker thread
    with no handler.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _mapping(data: dict, key: str, errors: list[str]) -> dict:
    """``data[key]`` as a mapping, or ``{}`` with the reason recorded.

    The whole point of this module is that a hostile answer comes back to the
    model as one batched repair prompt. A model that writes ``"nets": [...]``
    where an object belongs used to raise ``AttributeError`` out of
    ``.items()``, which escapes ``propose_circuit``'s ``except ValidationError``
    and kills the run with a traceback -- the one shape of bad answer the
    repair loop could not repair. The sibling parsers (sourcing, spec review,
    enclosure, testbench) all handle this already; this one feeds KiCad.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{key!r} must be an object, got {type(value).__name__}")
        return {}
    return value


def parse_circuit_spec(raw: str | dict) -> CircuitSpec:
    """Parse and validate model output into a :class:`CircuitSpec`.

    Accepts either a already-decoded dict or raw model text, tolerating a
    Markdown code fence. Raises :class:`ValidationError` with every problem
    collected, so a repair prompt can address them all at once.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError as exc:
            raise ValidationError([f"response is not valid JSON: {exc}"]) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise ValidationError([f"expected a JSON object, got {type(data).__name__}"])

    errors: list[str] = []

    devices: list[Device] = []
    for name, spec in _mapping(data, "devices", errors).items():
        if not isinstance(spec, dict) or not isinstance(spec.get("pins"), dict):
            errors.append(f"device {name!r} must have a 'pins' object")
            continue
        # ``kind`` and ``package`` are carried through as given and checked in
        # ``validate`` rather than here, so a bad one joins the batch that
        # includes the net and pin errors instead of being raised alone.
        package = spec.get("package")
        devices.append(
            Device(
                name=name,
                pins={str(k): normalise_pin_number(v) for k, v in spec["pins"].items()},
                symbol=spec.get("symbol"),
                kind=str(spec.get("kind", "ic")),
                package=None if package is None else str(package),
            )
        )

    passives: list[Passive] = []
    for name, spec in _mapping(data, "passives", errors).items():
        if not isinstance(spec, dict):
            errors.append(f"passive {name!r} must be an object")
            continue
        try:
            ptype = PassiveType(str(spec.get("type", "")).lower())
        except ValueError:
            errors.append(
                f"passive {name!r} has unsupported type {spec.get('type')!r}; "
                f"allowed: {[t.value for t in PassiveType]}"
            )
            continue
        passives.append(
            Passive(name=name, type=ptype, value=str(spec.get("value", "")))
        )

    connections: list[Connection] = []
    for net, endpoints in _mapping(data, "nets", errors).items():
        if not isinstance(endpoints, list):
            errors.append(f"net {net!r} must map to a list of endpoints")
            continue
        connections.append(
            Connection(net=str(net), endpoints=tuple(str(e) for e in endpoints))
        )

    if errors:
        raise ValidationError(errors)

    spec = CircuitSpec(devices=devices, passives=passives, connections=connections)
    spec.validate()
    return spec

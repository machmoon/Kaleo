"""Approved PCB constraints and deterministic promotion checks.

The verifier reports only what the generated circuit and routed copper prove.
Unsupported checks are ``unresolved`` and block promotion just like violations;
an approved user value is an input, not evidence that the board satisfies it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from .board import BoardResult, PlacedPart, part_anchor, placed_half_extents
from .netlist import CircuitSpec, PassiveType
from .packing import Layer
from .routing import RouteResult, Track
from .spice.values import parse_value
from .units import NM_PER_MM

MAX_NET_CLASSES = 24
MAX_NETS_PER_CLASS = 64
MAX_LAYERS_PER_CLASS = 16
MAX_MECHANICAL_ITEMS = 128
MAX_TEXT = 160
MAX_LAYER_TRANSITIONS = 32
MAX_VIAS_PER_NET = 64
MAX_DISTANCE_MM = 10_000.0
MAX_FREQUENCY_HZ = 100_000_000_000.0
CHECK_STATUSES = {"verified", "violated", "unresolved", "not_required"}
LAYER_NAMES = {Layer.TOP: "F.Cu", Layer.BOTTOM: "B.Cu"}
SUPPORTED_COPPER_LAYERS = ("F.Cu", "B.Cu")
NET_CLASS_KINDS = {
    "signal",
    "i2c",
    "usb",
    "ethernet",
    "spi",
    "clock",
    "power",
    "analog",
    "rf",
}


def _reject_unknown(
    raw: dict[str, Any], allowed: set[str], owner: str, *, strict: bool
) -> None:
    if not strict:
        return
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown {owner} field(s): {', '.join(unknown)}")


def _require_unique(values: list[str] | tuple[str, ...], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"'{field_name}' cannot contain duplicates")


def _text(value: Any, field_name: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{field_name}' must be a string")
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f"'{field_name}' cannot be empty")
    if len(cleaned) > MAX_TEXT:
        raise ValueError(f"'{field_name}' is too long")
    return cleaned


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    cleaned = _text(value, field_name, required=False)
    return cleaned or None


def _strings(value: Any, field_name: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{field_name}' must be a non-empty array")
    if len(value) > limit:
        raise ValueError(f"'{field_name}' has too many entries")
    cleaned = tuple(_text(item, field_name) for item in value)
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"'{field_name}' cannot contain duplicates")
    return cleaned


def _optional_number(
    value: Any,
    field_name: str,
    *,
    low: float,
    high: float,
) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{field_name}' must be a number or null")
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError(f"'{field_name}' is outside the supported range")
    return number


def _bounded_int(value: Any, field_name: str, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field_name}' must be an integer")
    if value < 0 or value > high:
        raise ValueError(f"'{field_name}' is outside the supported range")
    return value


def _switch(raw: dict[str, Any], field_name: str) -> bool:
    value = raw.get(field_name, False)
    if not isinstance(value, bool):
        raise ValueError(f"'{field_name}' must be a boolean")
    return value


def _required_number(value: float | None, field_name: str, *, when: bool) -> None:
    if when and value is None:
        raise ValueError(f"enabled constraint requires an approved '{field_name}'")


def _concerns(raw: dict[str, Any]) -> tuple[str, ...]:
    values = raw.get("concerns", [])
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError("'concerns' must be an array with at most 16 entries")
    return tuple(_text(item, "concerns") for item in values)


def _thermal_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > MAX_MECHANICAL_ITEMS:
        raise ValueError("'thermal_pairs' must be a bounded array")
    pairs: list[tuple[str, str]] = []
    canonical: set[tuple[str, str]] = set()
    for raw_pair in value:
        if not isinstance(raw_pair, list) or len(raw_pair) != 2:
            raise ValueError("each thermal pair must contain exactly two references")
        pair = (
            _text(raw_pair[0], "thermal_pairs"),
            _text(raw_pair[1], "thermal_pairs"),
        )
        if pair[0] == pair[1]:
            raise ValueError("a thermal pair must name two different references")
        normalized = tuple(sorted(pair))
        if normalized in canonical:
            raise ValueError("'thermal_pairs' cannot contain duplicates")
        canonical.add(normalized)
        pairs.append(pair)
    return tuple(pairs)


@dataclass(frozen=True)
class NetClassConstraint:
    name: str
    kind: str
    nets: tuple[str, ...]
    allowed_layers: tuple[str, ...]
    max_layer_transitions: int
    max_vias_per_net: int
    signal_voltage_v: float | None = None
    max_frequency_hz: float | None = None
    pullups_required: bool = False
    pullup_rail: str | None = None
    pullup_min_ohms: float | None = None
    pullup_max_ohms: float | None = None
    bus_capacitance_pf: float | None = None
    max_rise_time_ns: float | None = None
    controlled_impedance: bool = False
    impedance_ohms: float | None = None
    impedance_tolerance_percent: float | None = None
    pair_spacing_mm: float | None = None
    reference_plane: str | None = None
    min_trace_width_mm: float | None = None
    max_length_mm: float | None = None
    max_skew_mm: float | None = None
    max_stub_length_mm: float | None = None
    expected_current_a: float | None = None
    copper_weight_oz: float | None = None
    max_voltage_drop_v: float | None = None
    min_separation_mm: float | None = None
    min_thermal_separation_mm: float | None = None
    thermal_pairs: tuple[tuple[str, str], ...] = ()
    guard_required: bool = False
    concerns: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, *, strict: bool = True) -> NetClassConstraint:
        if not isinstance(raw, dict):
            raise ValueError("each net class must be an object")
        _reject_unknown(
            raw,
            {
                "name", "kind", "nets", "allowed_layers",
                "max_layer_transitions", "max_vias_per_net",
                "signal_voltage_v", "pullup_voltage_v", "max_frequency_hz",
                "pullups_required", "pullup_rail", "pullup_min_ohms",
                "pullup_max_ohms", "bus_capacitance_pf", "max_rise_time_ns",
                "controlled_impedance", "impedance_ohms",
                "impedance_tolerance_percent", "pair_spacing_mm",
                "reference_plane", "min_trace_width_mm", "max_length_mm",
                "max_skew_mm", "max_stub_length_mm", "expected_current_a",
                "copper_weight_oz", "max_voltage_drop_v", "min_separation_mm",
                "min_thermal_separation_mm", "thermal_pairs", "guard_required",
                "concerns",
            },
            "net-class",
            strict=strict,
        )
        kind = _text(raw.get("kind", "signal"), "kind").lower()
        if strict and kind not in NET_CLASS_KINDS:
            raise ValueError(f"unsupported net-class kind {kind!r}")
        layers = _strings(
            raw.get("allowed_layers"), "allowed_layers", MAX_LAYERS_PER_CLASS
        )
        if strict:
            unknown_layers = sorted(set(layers) - set(SUPPORTED_COPPER_LAYERS))
            if unknown_layers:
                raise ValueError(
                    "unsupported copper layer name(s): " + ", ".join(unknown_layers)
                )
        item = cls(
            name=_text(raw.get("name"), "name"),
            kind=kind,
            nets=_strings(raw.get("nets"), "nets", MAX_NETS_PER_CLASS),
            allowed_layers=layers,
            max_layer_transitions=_bounded_int(
                raw.get("max_layer_transitions"),
                "max_layer_transitions",
                MAX_LAYER_TRANSITIONS,
            ),
            max_vias_per_net=_bounded_int(
                raw.get("max_vias_per_net"), "max_vias_per_net", MAX_VIAS_PER_NET
            ),
            signal_voltage_v=_optional_number(
                raw.get("signal_voltage_v", raw.get("pullup_voltage_v")),
                "signal_voltage_v",
                low=0.01,
                high=2_000,
            ),
            max_frequency_hz=_optional_number(
                raw.get("max_frequency_hz"),
                "max_frequency_hz",
                low=1,
                high=MAX_FREQUENCY_HZ,
            ),
            pullups_required=_switch(raw, "pullups_required"),
            pullup_rail=_optional_text(raw.get("pullup_rail"), "pullup_rail"),
            pullup_min_ohms=_optional_number(
                raw.get("pullup_min_ohms"), "pullup_min_ohms", low=0.1, high=1e9
            ),
            pullup_max_ohms=_optional_number(
                raw.get("pullup_max_ohms"), "pullup_max_ohms", low=0.1, high=1e9
            ),
            bus_capacitance_pf=_optional_number(
                raw.get("bus_capacitance_pf"), "bus_capacitance_pf", low=0.01, high=1e7
            ),
            max_rise_time_ns=_optional_number(
                raw.get("max_rise_time_ns"), "max_rise_time_ns", low=0.01, high=1e9
            ),
            controlled_impedance=_switch(raw, "controlled_impedance"),
            impedance_ohms=_optional_number(
                raw.get("impedance_ohms"), "impedance_ohms", low=1, high=1_000
            ),
            impedance_tolerance_percent=_optional_number(
                raw.get("impedance_tolerance_percent"),
                "impedance_tolerance_percent",
                low=0.01,
                high=100,
            ),
            pair_spacing_mm=_optional_number(
                raw.get("pair_spacing_mm"),
                "pair_spacing_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
            reference_plane=_optional_text(
                raw.get("reference_plane"), "reference_plane"
            ),
            min_trace_width_mm=_optional_number(
                raw.get("min_trace_width_mm"),
                "min_trace_width_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
            max_length_mm=_optional_number(
                raw.get("max_length_mm"),
                "max_length_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
            max_skew_mm=_optional_number(
                raw.get("max_skew_mm"),
                "max_skew_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
            max_stub_length_mm=_optional_number(
                raw.get("max_stub_length_mm"),
                "max_stub_length_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
            expected_current_a=_optional_number(
                raw.get("expected_current_a"),
                "expected_current_a",
                low=1e-9,
                high=10_000,
            ),
            copper_weight_oz=_optional_number(
                raw.get("copper_weight_oz"), "copper_weight_oz", low=0.01, high=100
            ),
            max_voltage_drop_v=_optional_number(
                raw.get("max_voltage_drop_v"),
                "max_voltage_drop_v",
                low=1e-9,
                high=2_000,
            ),
            min_separation_mm=_optional_number(
                raw.get("min_separation_mm"),
                "min_separation_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
            min_thermal_separation_mm=_optional_number(
                raw.get("min_thermal_separation_mm"),
                "min_thermal_separation_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
            thermal_pairs=_thermal_pairs(raw.get("thermal_pairs", [])),
            guard_required=_switch(raw, "guard_required"),
            concerns=_concerns(raw),
        )
        if strict:
            _validate_kind_requirements(item)
        return item

    def to_dict(self) -> dict[str, Any]:
        result = {
            name: list(value) if isinstance(value, tuple) else value
            for name, value in vars(self).items()
        }
        result["thermal_pairs"] = [list(pair) for pair in self.thermal_pairs]
        return result


def _validate_kind_requirements(item: NetClassConstraint) -> None:
    _validate_pullup_requirements(item)
    _validate_impedance_requirements(item)
    requirements = {
        "i2c": (
            "signal_voltage_v",
            "max_frequency_hz",
            "bus_capacitance_pf",
            "max_rise_time_ns",
        ),
        "spi": ("max_frequency_hz", "max_length_mm", "max_skew_mm"),
        "clock": ("max_frequency_hz", "max_length_mm", "max_skew_mm"),
        "power": (
            "expected_current_a",
            "min_trace_width_mm",
            "copper_weight_oz",
            "max_voltage_drop_v",
            "min_thermal_separation_mm",
        ),
        "analog": ("min_separation_mm",),
        "rf": ("min_separation_mm", "max_length_mm"),
    }
    for field_name in requirements.get(item.kind, ()):
        _required_number(getattr(item, field_name), field_name, when=True)
    if item.kind in {"spi", "clock", "analog", "rf"} and not item.reference_plane:
        raise ValueError(f"{item.kind} constraints require an approved reference plane")


def _validate_pullup_requirements(item: NetClassConstraint) -> None:
    if not item.pullups_required:
        return
    if not item.pullup_rail:
        raise ValueError("pull-up constraints require an approved 'pullup_rail'")
    for field_name in ("pullup_min_ohms", "pullup_max_ohms"):
        _required_number(getattr(item, field_name), field_name, when=True)
    assert item.pullup_min_ohms is not None and item.pullup_max_ohms is not None
    if item.pullup_min_ohms > item.pullup_max_ohms:
        raise ValueError("pull-up resistance range is reversed")


def _validate_impedance_requirements(item: NetClassConstraint) -> None:
    if not item.controlled_impedance:
        return
    for field_name in (
        "impedance_ohms",
        "impedance_tolerance_percent",
        "pair_spacing_mm",
        "max_skew_mm",
    ):
        _required_number(getattr(item, field_name), field_name, when=True)
    if not item.reference_plane:
        raise ValueError(
            "controlled-impedance nets require an approved 'reference_plane'"
        )


@dataclass(frozen=True)
class KeepoutConstraint:
    name: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float

    @classmethod
    def from_dict(cls, raw: Any, *, strict: bool = True) -> KeepoutConstraint:
        if not isinstance(raw, dict):
            raise ValueError("each mechanical keepout must be an object")
        _reject_unknown(
            raw,
            {"name", "x_mm", "y_mm", "width_mm", "height_mm"},
            "keepout",
            strict=strict,
        )
        values = {
            "x_mm": _optional_number(
                raw.get("x_mm"), "x_mm", low=0, high=MAX_DISTANCE_MM
            ),
            "y_mm": _optional_number(
                raw.get("y_mm"), "y_mm", low=0, high=MAX_DISTANCE_MM
            ),
            "width_mm": _optional_number(
                raw.get("width_mm"), "width_mm", low=0.001, high=MAX_DISTANCE_MM
            ),
            "height_mm": _optional_number(
                raw.get("height_mm"), "height_mm", low=0.001, high=MAX_DISTANCE_MM
            ),
        }
        if any(value is None for value in values.values()):
            raise ValueError("mechanical keepouts require x, y, width, and height")
        return cls(name=_text(raw.get("name"), "name"), **values)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return vars(self)


@dataclass(frozen=True)
class FixedPlacementConstraint:
    ref: str
    x_mm: float
    y_mm: float
    tolerance_mm: float

    @classmethod
    def from_dict(cls, raw: Any, *, strict: bool = True) -> FixedPlacementConstraint:
        if not isinstance(raw, dict):
            raise ValueError("each fixed placement must be an object")
        _reject_unknown(
            raw,
            {"ref", "x_mm", "y_mm", "tolerance_mm"},
            "fixed-placement",
            strict=strict,
        )
        values = {
            "x_mm": _optional_number(
                raw.get("x_mm"), "x_mm", low=0, high=MAX_DISTANCE_MM
            ),
            "y_mm": _optional_number(
                raw.get("y_mm"), "y_mm", low=0, high=MAX_DISTANCE_MM
            ),
            "tolerance_mm": _optional_number(
                raw.get("tolerance_mm"),
                "tolerance_mm",
                low=0.001,
                high=MAX_DISTANCE_MM,
            ),
        }
        if any(value is None for value in values.values()):
            raise ValueError("fixed placements require x, y, and tolerance")
        return cls(ref=_text(raw.get("ref"), "ref"), **values)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return vars(self)


@dataclass(frozen=True)
class MechanicalConstraints:
    max_board_width_mm: float | None = None
    max_board_height_mm: float | None = None
    max_component_height_mm: float | None = None
    mounting_hole_refs: tuple[str, ...] = ()
    keepouts: tuple[KeepoutConstraint, ...] = ()
    fixed_placements: tuple[FixedPlacementConstraint, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, *, strict: bool = True) -> MechanicalConstraints:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("'mechanical' must be an object")
        _reject_unknown(
            raw,
            {
                "max_board_width_mm", "max_board_height_mm",
                "max_component_height_mm", "mounting_hole_refs", "keepouts",
                "fixed_placements",
            },
            "mechanical",
            strict=strict,
        )
        holes = raw.get("mounting_hole_refs", [])
        keepouts = raw.get("keepouts", [])
        fixed = raw.get("fixed_placements", [])
        for name, values in (
            ("mounting_hole_refs", holes),
            ("keepouts", keepouts),
            ("fixed_placements", fixed),
        ):
            if not isinstance(values, list) or len(values) > MAX_MECHANICAL_ITEMS:
                raise ValueError(f"'{name}' must be a bounded array")
        parsed_holes = tuple(_text(item, "mounting_hole_refs") for item in holes)
        _require_unique(parsed_holes, "mounting_hole_refs")
        parsed_keepouts = tuple(
            KeepoutConstraint.from_dict(item, strict=strict) for item in keepouts
        )
        _require_unique(
            tuple(item.name for item in parsed_keepouts), "mechanical keepout names"
        )
        parsed_fixed = tuple(
            FixedPlacementConstraint.from_dict(item, strict=strict) for item in fixed
        )
        _require_unique(
            tuple(item.ref for item in parsed_fixed), "fixed-placement refs"
        )
        return cls(
            max_board_width_mm=_optional_number(
                raw.get("max_board_width_mm"),
                "max_board_width_mm",
                low=0.01,
                high=MAX_DISTANCE_MM,
            ),
            max_board_height_mm=_optional_number(
                raw.get("max_board_height_mm"),
                "max_board_height_mm",
                low=0.01,
                high=MAX_DISTANCE_MM,
            ),
            max_component_height_mm=_optional_number(
                raw.get("max_component_height_mm"),
                "max_component_height_mm",
                low=0.01,
                high=MAX_DISTANCE_MM,
            ),
            mounting_hole_refs=parsed_holes,
            keepouts=parsed_keepouts,
            fixed_placements=parsed_fixed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_board_width_mm": self.max_board_width_mm,
            "max_board_height_mm": self.max_board_height_mm,
            "max_component_height_mm": self.max_component_height_mm,
            "mounting_hole_refs": list(self.mounting_hole_refs),
            "keepouts": [item.to_dict() for item in self.keepouts],
            "fixed_placements": [item.to_dict() for item in self.fixed_placements],
        }


@dataclass(frozen=True)
class SoftPreferences:
    fewer_vias: float = 0
    shorter_traces: float = 0
    compact_grouping: float = 0
    thermal_separation: float = 0
    connector_accessibility: float = 0

    @classmethod
    def from_dict(cls, raw: Any, *, strict: bool = True) -> SoftPreferences:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("'soft_preferences' must be an object")
        _reject_unknown(
            raw,
            set(vars(cls())),
            "soft-preference",
            strict=strict,
        )
        values = {
            name: _optional_number(raw.get(name, 0), name, low=0, high=1_000)
            for name in vars(cls())
        }
        return cls(**{name: value or 0 for name, value in values.items()})

    def to_dict(self) -> dict[str, float]:
        return vars(self)


def _manifest_version(raw: dict[str, Any]) -> int:
    version = raw.get("version", 1)
    if version not in (1, 2):
        raise ValueError("unsupported constraint manifest version")
    if raw.get("approved") is not True:
        raise ValueError("constraints must be approved before build")
    return version


def _manifest_board_layers(raw: dict[str, Any], version: int) -> int:
    value = raw.get("board_layers")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
        raise ValueError("'board_layers' must be an integer between 1 and 32")
    if version >= 2 and value != len(SUPPORTED_COPPER_LAYERS):
        raise ValueError("the generated board supports exactly two copper layers")
    return value


def _manifest_net_classes(
    raw: dict[str, Any], version: int
) -> tuple[NetClassConstraint, ...]:
    values = raw.get("net_classes")
    if not isinstance(values, list) or not values:
        raise ValueError("'net_classes' must be a non-empty array")
    if len(values) > MAX_NET_CLASSES:
        raise ValueError(f"at most {MAX_NET_CLASSES} net classes are supported")
    parsed = tuple(
        NetClassConstraint.from_dict(item, strict=version >= 2) for item in values
    )
    if len({item.name for item in parsed}) != len(parsed):
        raise ValueError("net-class names must be unique")
    owners: dict[str, str] = {}
    for item in parsed:
        for net in item.nets:
            previous = owners.setdefault(net, item.name)
            if previous != item.name:
                raise ValueError(
                    f"net {net!r} belongs to both {previous!r} and {item.name!r}"
                )
    return parsed


@dataclass(frozen=True)
class ConstraintManifest:
    board_layers: int
    net_classes: tuple[NetClassConstraint, ...]
    mechanical: MechanicalConstraints = field(default_factory=MechanicalConstraints)
    soft_preferences: SoftPreferences = field(default_factory=SoftPreferences)
    version: int = 2
    approved: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> ConstraintManifest:
        if not isinstance(raw, dict):
            raise ValueError("'constraints' must be an object")
        version = _manifest_version(raw)
        strict = version >= 2
        _reject_unknown(
            raw,
            {
                "version", "approved", "board_layers", "net_classes",
                "mechanical", "soft_preferences",
            },
            "constraint-manifest",
            strict=strict,
        )
        return cls(
            version=version,
            board_layers=_manifest_board_layers(raw, version),
            net_classes=_manifest_net_classes(raw, version),
            mechanical=MechanicalConstraints.from_dict(
                raw.get("mechanical"), strict=strict
            ),
            soft_preferences=SoftPreferences.from_dict(
                raw.get("soft_preferences"), strict=strict
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "approved": self.approved,
            "board_layers": self.board_layers,
            "net_classes": [item.to_dict() for item in self.net_classes],
            "mechanical": self.mechanical.to_dict(),
            "soft_preferences": self.soft_preferences.to_dict(),
        }

    def prompt_block(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return (
            "\n\nAPPROVED PCB CONSTRAINT MANIFEST\n"
            "Hard constraints are mandatory. Soft preferences are advisory "
            "scoring terms and never override a hard check. "
            "Do not invent null values. Preserve exact net and reference names.\n"
            f"{payload}"
        )


def _check(name: str, status: str, detail: str, **evidence: Any) -> dict[str, Any]:
    if status not in CHECK_STATUSES:
        raise ValueError(f"unknown constraint status {status!r}")
    return {"name": name, "status": status, "detail": detail, "evidence": evidence}


def _route_lengths(route: RouteResult | None) -> dict[str, float]:
    lengths: dict[str, float] = {}
    for track in route.tracks if route else ():
        lengths[track.net] = lengths.get(track.net, 0) + track.length_nm / NM_PER_MM
    return lengths


def _vias_by_net(route: RouteResult | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for via in route.vias if route else ():
        counts[via.net] = counts.get(via.net, 0) + 1
    return counts


def _tracks_by_net(route: RouteResult | None) -> dict[str, list[Track]]:
    found: dict[str, list[Track]] = {}
    for track in route.tracks if route else ():
        found.setdefault(track.net, []).append(track)
    return found


def _passive_nets(spec: CircuitSpec) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for connection in spec.connections:
        for endpoint in connection.endpoints:
            part, _, _ = endpoint.rpartition(".")
            found.setdefault(part, set()).add(connection.net)
    return found


def _pullup_values(
    constraint: NetClassConstraint, spec: CircuitSpec
) -> tuple[dict[str, float], dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    passive_nets = _passive_nets(spec)
    components: dict[str, list[dict[str, Any]]] = {}
    invalid: dict[str, list[str]] = {}
    for net in constraint.nets:
        for passive in spec.passives:
            if passive.type is not PassiveType.RESISTOR or passive_nets.get(
                passive.name
            ) != {net, constraint.pullup_rail}:
                continue
            try:
                value, _ = parse_value(passive.value, part=passive.name)
            except ValueError:
                invalid.setdefault(net, []).append(passive.name)
                continue
            if not math.isfinite(value) or value <= 0:
                invalid.setdefault(net, []).append(passive.name)
                continue
            components.setdefault(net, []).append({"ref": passive.name, "ohms": value})
    equivalents = {
        net: 1 / sum(1 / item["ohms"] for item in items)
        for net, items in components.items()
    }
    return equivalents, components, invalid


def _verify_pullups(
    constraint: NetClassConstraint, spec: CircuitSpec
) -> dict[str, Any]:
    if not constraint.pullups_required:
        return _check("pullups", "not_required", "No pull-ups were required.")
    if (
        constraint.pullup_rail is None
        or constraint.pullup_min_ohms is None
        or constraint.pullup_max_ohms is None
    ):
        return _check(
            "pullups",
            "unresolved",
            "The legacy manifest does not define a pull-up rail and resistance range.",
        )
    values, components, invalid = _pullup_values(constraint, spec)
    if invalid:
        return _check(
            "pullups",
            "unresolved",
            "At least one candidate pull-up value could not be validated.",
            invalid_resistor_refs=invalid,
            found=components,
        )
    missing = [net for net in constraint.nets if net not in values]
    outside = {
        net: value
        for net, value in values.items()
        if not constraint.pullup_min_ohms <= value <= constraint.pullup_max_ohms
    }
    if missing or outside:
        return _check(
            "pullups",
            "violated",
            "Required pull-ups are missing or outside the approved range.",
            missing=missing,
            outside_range_ohms=outside,
            found_ohms=values,
        )
    return _verify_rise_time(constraint, values, components)


def _verify_signal_voltage(constraint: NetClassConstraint) -> dict[str, Any]:
    if constraint.signal_voltage_v is None:
        return _check("signal_voltage", "not_required", "No signal voltage was set.")
    return _check(
        "signal_voltage",
        "unresolved",
        "The circuit IR and routed copper do not independently prove signal voltage.",
        target_v=constraint.signal_voltage_v,
    )


def _verify_rise_time(
    constraint: NetClassConstraint,
    values: dict[str, float],
    components: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if constraint.bus_capacitance_pf is None or constraint.max_rise_time_ns is None:
        return _check(
            "pullups",
            "unresolved",
            "Bus capacitance and rise-time limit are required for an RC check.",
        )
    rise_times = {
        net: 0.8473 * resistance * constraint.bus_capacitance_pf * 1e-3
        for net, resistance in values.items()
    }
    failed = {
        net: value
        for net, value in rise_times.items()
        if value > constraint.max_rise_time_ns
    }
    return _check(
        "pullups",
        "violated" if failed else "verified",
        "Pull-up presence, resistance, and RC rise time were measured.",
        resistance_ohms=values,
        resistors=components,
        rise_time_ns=rise_times,
        limit_ns=constraint.max_rise_time_ns,
    )


def _missing_track_geometry(
    constraint: NetClassConstraint, tracks: dict[str, list[Track]]
) -> list[str]:
    return [net for net in constraint.nets if not tracks.get(net)]


def _verify_route_completion(
    constraint: NetClassConstraint,
    route: RouteResult,
    tracks: dict[str, list[Track]],
) -> dict[str, Any]:
    failed = [net for net in constraint.nets if net in route.unrouted]
    missing = [
        net
        for net in constraint.nets
        if net not in route.routed and net not in route.unrouted
    ]
    geometry_missing = _missing_track_geometry(constraint, tracks)
    status = (
        "violated"
        if failed or missing
        else "unresolved"
        if geometry_missing
        else "verified"
    )
    return _check(
        "routing",
        status,
        "Every approved net must be accounted for and have routed copper geometry.",
        unrouted=failed,
        absent_from_router=missing,
        missing_geometry=geometry_missing,
    )


def _verify_layers(
    constraint: NetClassConstraint, tracks: dict[str, list[Track]]
) -> dict[str, Any]:
    used = {
        net: sorted(
            {
                LAYER_NAMES.get(track.layer, str(track.layer))
                for track in tracks.get(net, [])
            }
        )
        for net in constraint.nets
    }
    illegal = {
        net: [layer for layer in layers if layer not in constraint.allowed_layers]
        for net, layers in used.items()
    }
    illegal = {net: layers for net, layers in illegal.items() if layers}
    missing = _missing_track_geometry(constraint, tracks)
    status = "violated" if illegal else "unresolved" if missing else "verified"
    return _check(
        "allowed_layers",
        status,
        "Track layers were compared with the approved layer set.",
        used=used,
        illegal=illegal,
        missing_geometry=missing,
    )


def _verify_vias(
    constraint: NetClassConstraint,
    vias: dict[str, int],
    tracks: dict[str, list[Track]],
) -> dict[str, Any]:
    measured = {net: vias.get(net, 0) for net in constraint.nets}
    failed = {
        net: count
        for net, count in measured.items()
        if count > constraint.max_vias_per_net
        or count > constraint.max_layer_transitions
    }
    missing = _missing_track_geometry(constraint, tracks)
    status = "violated" if failed else "unresolved" if missing else "verified"
    return _check(
        "vias_and_layer_transitions",
        status,
        "Through-via count is the measurable layer-transition count.",
        measured=measured,
        max_vias=constraint.max_vias_per_net,
        max_layer_transitions=constraint.max_layer_transitions,
        failed=failed,
        missing_geometry=missing,
    )


def _verify_width(
    constraint: NetClassConstraint, tracks: dict[str, list[Track]]
) -> dict[str, Any]:
    if constraint.min_trace_width_mm is None:
        return _check("trace_width", "not_required", "No minimum width was set.")
    measured = {
        net: min(track.width_nm / NM_PER_MM for track in tracks[net])
        for net in constraint.nets
        if tracks.get(net)
    }
    failed = {
        net: width
        for net, width in measured.items()
        if width < constraint.min_trace_width_mm
    }
    missing = _missing_track_geometry(constraint, tracks)
    status = "violated" if failed else "unresolved" if missing else "verified"
    return _check(
        "trace_width",
        status,
        "Minimum routed track width was measured.",
        measured_mm=measured,
        minimum_mm=constraint.min_trace_width_mm,
        failed=failed,
        missing_geometry=missing,
    )


def _verify_length(
    constraint: NetClassConstraint,
    lengths: dict[str, float],
    tracks: dict[str, list[Track]],
) -> dict[str, Any]:
    if constraint.max_length_mm is None:
        return _check("trace_length", "not_required", "No length limit was set.")
    measured = {net: lengths[net] for net in constraint.nets if net in lengths}
    failed = {
        net: length
        for net, length in measured.items()
        if length > constraint.max_length_mm
    }
    missing = _missing_track_geometry(constraint, tracks)
    status = "violated" if failed else "unresolved" if missing else "verified"
    return _check(
        "trace_length",
        status,
        "Routed copper length was measured per net.",
        measured_mm=measured,
        maximum_mm=constraint.max_length_mm,
        failed=failed,
        missing_geometry=missing,
    )


def _verify_skew(
    constraint: NetClassConstraint,
    lengths: dict[str, float],
    tracks: dict[str, list[Track]],
) -> dict[str, Any]:
    if constraint.max_skew_mm is None:
        return _check("skew", "not_required", "No skew limit was set.")
    missing = _missing_track_geometry(constraint, tracks)
    if missing:
        return _check(
            "skew",
            "unresolved",
            "Skew cannot be proven while member nets lack routed geometry.",
            missing_geometry=missing,
            maximum_mm=constraint.max_skew_mm,
        )
    measured = [lengths[net] for net in constraint.nets]
    skew = max(measured, default=0) - min(measured, default=0)
    return _check(
        "skew",
        "violated" if skew > constraint.max_skew_mm else "verified",
        "Skew is the longest routed member minus the shortest.",
        measured_mm=skew,
        maximum_mm=constraint.max_skew_mm,
    )


def _verify_impedance(constraint: NetClassConstraint) -> dict[str, Any]:
    if not constraint.controlled_impedance:
        return _check(
            "controlled_impedance", "not_required", "No impedance target was set."
        )
    return _check(
        "controlled_impedance",
        "unresolved",
        "This router has no field solver or verified fabricator stackup.",
        target_ohms=constraint.impedance_ohms,
        tolerance_percent=constraint.impedance_tolerance_percent,
        pair_spacing_mm=constraint.pair_spacing_mm,
        reference_plane=constraint.reference_plane,
    )


def _verify_stubs(constraint: NetClassConstraint) -> dict[str, Any]:
    if constraint.max_stub_length_mm is None:
        return _check("stubs", "not_required", "No stub limit was set.")
    return _check(
        "stubs",
        "unresolved",
        "Stub extraction requires routed topology tied back to terminal roles.",
        maximum_mm=constraint.max_stub_length_mm,
    )


def _verify_power_drop(
    constraint: NetClassConstraint,
    lengths: dict[str, float],
    tracks: dict[str, list[Track]],
) -> dict[str, Any]:
    required = (
        constraint.expected_current_a,
        constraint.copper_weight_oz,
        constraint.max_voltage_drop_v,
    )
    if all(value is None for value in required):
        return _check("voltage_drop", "not_required", "No voltage-drop limit was set.")
    if any(value is None for value in required):
        return _check(
            "voltage_drop",
            "unresolved",
            "Current, copper weight, and drop limit are all required.",
        )
    current, copper_weight, maximum_drop = _power_inputs(constraint)
    missing = _missing_track_geometry(constraint, tracks)
    if missing:
        return _check(
            "voltage_drop",
            "unresolved",
            "Voltage drop cannot be estimated while routed geometry is missing.",
            missing_geometry=missing,
        )
    drops = _voltage_drops(
        constraint.nets,
        current=current,
        copper_weight=copper_weight,
        lengths=lengths,
        tracks=tracks,
    )
    return _check(
        "voltage_drop",
        "unresolved",
        "Only a zero conservative lower bound is provable without source/load "
        "topology, branch current, via, and contact evidence.",
        conservative_lower_bound_v={net: 0.0 for net in constraint.nets},
        copper_only_estimate_v=drops,
        maximum_v=maximum_drop,
        exceeds_limit={
            net: drop
            for net, drop in drops.items()
            if drop is None or drop > maximum_drop
        },
    )


def _power_inputs(constraint: NetClassConstraint) -> tuple[float, float, float]:
    assert constraint.expected_current_a is not None
    assert constraint.copper_weight_oz is not None
    assert constraint.max_voltage_drop_v is not None
    return (
        constraint.expected_current_a,
        constraint.copper_weight_oz,
        constraint.max_voltage_drop_v,
    )


def _voltage_drops(
    nets: tuple[str, ...],
    *,
    current: float,
    copper_weight: float,
    lengths: dict[str, float],
    tracks: dict[str, list[Track]],
) -> dict[str, float | None]:
    thickness_m = copper_weight * 34.8e-6
    drops: dict[str, float | None] = {}
    for net in nets:
        width_m = min(
            (track.width_nm / 1e9 for track in tracks.get(net, [])), default=0
        )
        drops[net] = (
            None
            if width_m == 0
            else current
            * 1.724e-8
            * (lengths.get(net, 0) / 1_000)
            / (width_m * thickness_m)
        )
    return drops


def _verify_separation(constraint: NetClassConstraint) -> dict[str, Any]:
    if constraint.min_separation_mm is None and not constraint.guard_required:
        return _check("isolation", "not_required", "No isolation rule was set.")
    return _check(
        "isolation",
        "unresolved",
        "Guard geometry and sensitive-net separation need zone and keepout-aware DRC.",
        minimum_separation_mm=constraint.min_separation_mm,
        guard_required=constraint.guard_required,
    )


def _verify_reference_plane(constraint: NetClassConstraint) -> dict[str, Any]:
    if constraint.reference_plane is None:
        return _check(
            "reference_plane", "not_required", "No reference plane was required."
        )
    return _check(
        "reference_plane",
        "unresolved",
        "The generated board has no plane-zone model for continuity checking.",
        required_plane=constraint.reference_plane,
    )


def _verify_routing(
    constraint: NetClassConstraint,
    route: RouteResult | None,
    lengths: dict[str, float],
    vias: dict[str, int],
    tracks: dict[str, list[Track]],
) -> list[dict[str, Any]]:
    if route is None:
        return [_check("routing", "unresolved", "No routed copper was produced.")]
    return [
        _verify_route_completion(constraint, route, tracks),
        _verify_layers(constraint, tracks),
        _verify_vias(constraint, vias, tracks),
        _verify_width(constraint, tracks),
        _verify_length(constraint, lengths, tracks),
        _verify_skew(constraint, lengths, tracks),
        _verify_impedance(constraint),
        _verify_stubs(constraint),
        _verify_power_drop(constraint, lengths, tracks),
        _verify_separation(constraint),
        _verify_reference_plane(constraint),
    ]


def _part_rect_mm(part: PlacedPart) -> tuple[float, float, float, float]:
    half_w, half_h = placed_half_extents(part)
    return (
        part.x_nm / NM_PER_MM,
        part.y_nm / NM_PER_MM,
        (part.x_nm + 2 * half_w) / NM_PER_MM,
        (part.y_nm + 2 * half_h) / NM_PER_MM,
    )


_THERMAL_TOKENS = (
    "driver",
    "motor",
    "mosfet",
    "regulator",
    "l293",
    "ams1117",
    "power",
)
_CONNECTOR_TOKENS = ("connector", "header", "jack", "usb", "jst", "terminal")


def _parts_matching(board: BoardResult, tokens: tuple[str, ...]) -> list[PlacedPart]:
    return [
        part
        for part in board.parts
        if any(token in f"{part.ref} {part.value}".lower() for token in tokens)
    ]


def _part_center_mm(part: PlacedPart) -> tuple[float, float]:
    x_nm, y_nm = part_anchor(part)
    return x_nm / NM_PER_MM, y_nm / NM_PER_MM


def _pair_distances(parts: list[PlacedPart]) -> list[float]:
    centers = [_part_center_mm(part) for part in parts]
    return [
        math.dist(centers[left], centers[right])
        for left in range(len(centers))
        for right in range(left + 1, len(centers))
    ]


def _verify_thermal_separation(
    constraint: NetClassConstraint, board: BoardResult
) -> dict[str, Any]:
    if constraint.min_thermal_separation_mm is None:
        return _check(
            "thermal_separation", "not_required", "No thermal separation was set."
        )
    if not constraint.thermal_pairs:
        return _check(
            "thermal_separation",
            "unresolved",
            "No explicit approved thermal reference pairs were supplied.",
        )
    by_ref = {part.ref: part for part in board.parts}
    missing = sorted(
        {ref for pair in constraint.thermal_pairs for ref in pair if ref not in by_ref}
    )
    if missing:
        return _check(
            "thermal_separation",
            "unresolved",
            "Approved thermal references are absent from the generated board.",
            missing_refs=missing,
        )
    measured = {
        f"{left}/{right}": math.dist(
            _part_center_mm(by_ref[left]), _part_center_mm(by_ref[right])
        )
        for left, right in constraint.thermal_pairs
    }
    failed = {
        pair: distance
        for pair, distance in measured.items()
        if distance < constraint.min_thermal_separation_mm
    }
    return _check(
        "thermal_separation",
        "violated" if failed else "verified",
        "Approved thermal reference-pair distances were measured.",
        measured_mm=measured,
        minimum_mm=constraint.min_thermal_separation_mm,
        failed=failed,
    )


def _rectangles_overlap(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> bool:
    return (
        left[0] < right[2]
        and right[0] < left[2]
        and left[1] < right[3]
        and right[1] < left[3]
    )


def _verify_outline(
    mechanical: MechanicalConstraints, board: BoardResult
) -> dict[str, Any]:
    width, height = board.size_mm
    if mechanical.max_board_width_mm is None and mechanical.max_board_height_mm is None:
        return _check("board_outline", "not_required", "No outline limit was set.")
    failed = []
    if (
        mechanical.max_board_width_mm is not None
        and width > mechanical.max_board_width_mm
    ):
        failed.append("width")
    if (
        mechanical.max_board_height_mm is not None
        and height > mechanical.max_board_height_mm
    ):
        failed.append("height")
    return _check(
        "board_outline",
        "violated" if failed else "verified",
        "Generated board dimensions were compared with approved maxima.",
        measured_mm=[width, height],
        maximum_mm=[mechanical.max_board_width_mm, mechanical.max_board_height_mm],
        failed=failed,
    )


def _verify_keepouts(
    mechanical: MechanicalConstraints, board: BoardResult
) -> dict[str, Any]:
    if not mechanical.keepouts:
        return _check("mechanical_keepouts", "not_required", "No keepout was set.")
    collisions: dict[str, list[str]] = {}
    for keepout in mechanical.keepouts:
        area = (
            keepout.x_mm,
            keepout.y_mm,
            keepout.x_mm + keepout.width_mm,
            keepout.y_mm + keepout.height_mm,
        )
        refs = [
            part.ref
            for part in board.parts
            if _rectangles_overlap(_part_rect_mm(part), area)
        ]
        if refs:
            collisions[keepout.name] = refs
    return _check(
        "mechanical_keepouts",
        "violated" if collisions else "verified",
        "Placed courtyards were tested against approved keepout rectangles.",
        collisions=collisions,
    )


def _verify_fixed_placements(
    mechanical: MechanicalConstraints, board: BoardResult
) -> dict[str, Any]:
    if not mechanical.fixed_placements:
        return _check("fixed_placements", "not_required", "No fixed placement was set.")
    by_ref = {part.ref: part for part in board.parts}
    failed: dict[str, Any] = {}
    for fixed in mechanical.fixed_placements:
        part = by_ref.get(fixed.ref)
        if part is None:
            failed[fixed.ref] = "missing"
            continue
        x_nm, y_nm = part_anchor(part)
        delta = math.hypot(x_nm / NM_PER_MM - fixed.x_mm, y_nm / NM_PER_MM - fixed.y_mm)
        if delta > fixed.tolerance_mm:
            failed[fixed.ref] = {"delta_mm": delta, "tolerance_mm": fixed.tolerance_mm}
    return _check(
        "fixed_placements",
        "violated" if failed else "verified",
        "Footprint anchors were compared with approved positions.",
        failed=failed,
    )


def _verify_mounting_holes(
    mechanical: MechanicalConstraints, board: BoardResult
) -> dict[str, Any]:
    if not mechanical.mounting_hole_refs:
        return _check(
            "mounting_holes", "not_required", "No mounting-hole refs were set."
        )
    by_ref = {part.ref: part for part in board.parts}
    missing = [ref for ref in mechanical.mounting_hole_refs if ref not in by_ref]
    without_drill = [
        ref
        for ref in mechanical.mounting_hole_refs
        if ref in by_ref
        and not any(
            getattr(pad, "drill_nm", 0) > 0 for pad in by_ref[ref].footprint.pads
        )
    ]
    status = "violated" if missing else "unresolved" if without_drill else "verified"
    return _check(
        "mounting_holes",
        status,
        "Required mounting-hole references must exist and carry drilled-pad evidence.",
        missing=missing,
        without_drill_evidence=without_drill,
    )


def _verify_mechanical(
    mechanical: MechanicalConstraints, board: BoardResult
) -> list[dict[str, Any]]:
    checks = [
        _verify_outline(mechanical, board),
        _verify_keepouts(mechanical, board),
        _verify_fixed_placements(mechanical, board),
        _verify_mounting_holes(mechanical, board),
    ]
    if mechanical.max_component_height_mm is not None:
        checks.append(
            _check(
                "component_height",
                "unresolved",
                "The footprint library does not carry verified component heights.",
                maximum_mm=mechanical.max_component_height_mm,
            )
        )
    return checks


def _verify_board_layers(
    manifest: ConstraintManifest, route: RouteResult | None
) -> dict[str, Any]:
    routed_layers = sorted(
        {LAYER_NAMES.get(track.layer, str(track.layer)) for track in route.tracks}
        if route
        else set()
    )
    unknown = sorted(set(routed_layers) - set(SUPPORTED_COPPER_LAYERS))
    valid = manifest.board_layers == len(SUPPORTED_COPPER_LAYERS) and not unknown
    return _check(
        "board_layers",
        "verified" if valid else "violated",
        "Manifest and routed copper were checked against the two-layer board model.",
        declared_count=manifest.board_layers,
        supported_names=list(SUPPORTED_COPPER_LAYERS),
        routed_names=routed_layers,
        unknown_routed_names=unknown,
    )


def _soft_score(
    preferences: SoftPreferences, board: BoardResult, route: RouteResult | None
) -> dict[str, Any]:
    thermal_distances = _pair_distances(_parts_matching(board, _THERMAL_TOKENS))
    connector_distances = []
    width, height = board.size_mm
    for part in _parts_matching(board, _CONNECTOR_TOKENS):
        x_mm, y_mm = _part_center_mm(part)
        connector_distances.append(min(x_mm, y_mm, width - x_mm, height - y_mm))
    metrics = {
        "via_count": len(route.vias) if route else 0,
        "trace_length_mm": route.routed_length_nm / NM_PER_MM if route else 0,
        "board_area_mm2": board.size_mm[0] * board.size_mm[1],
        "thermal_inverse_separation": sum(
            1 / (distance + 1) for distance in thermal_distances
        ),
        "connector_edge_distance_mm": sum(connector_distances),
    }
    terms = {
        "fewer_vias": preferences.fewer_vias * metrics["via_count"],
        "shorter_traces": preferences.shorter_traces * metrics["trace_length_mm"],
        "compact_grouping": preferences.compact_grouping * metrics["board_area_mm2"],
        "thermal_separation": preferences.thermal_separation
        * metrics["thermal_inverse_separation"],
        "connector_accessibility": preferences.connector_accessibility
        * metrics["connector_edge_distance_mm"],
    }
    return {"cost": round(sum(terms.values()), 6), "terms": terms, "metrics": metrics}


def verify_constraint_manifest(
    manifest: ConstraintManifest,
    spec: CircuitSpec,
    board: BoardResult,
    route: RouteResult | None,
) -> dict[str, Any]:
    """Return the hard promotion gate and an advisory soft diagnostic score."""
    spec.validate()
    _require_unique(tuple(part.ref for part in board.parts), "board part refs")
    _require_unique(tuple(board.nets), "board nets")
    if route is not None:
        _require_unique(tuple(route.routed), "routed nets")
        _require_unique(tuple(route.unrouted), "unrouted nets")
        overlap = sorted(set(route.routed) & set(route.unrouted))
        if overlap:
            raise ValueError(
                "router cannot mark nets both routed and unrouted: "
                + ", ".join(overlap)
            )
    net_names = {connection.net for connection in spec.connections}
    lengths = _route_lengths(route)
    vias = _vias_by_net(route)
    tracks = _tracks_by_net(route)
    groups = []
    for constraint in manifest.net_classes:
        missing = [net for net in constraint.nets if net not in net_names]
        checks = [
            _check(
                "connectivity",
                "violated" if missing else "verified",
                "Approved net names must exist in the validated circuit IR.",
                missing=missing,
            ),
            _verify_signal_voltage(constraint),
            _verify_pullups(constraint, spec),
        ]
        checks.extend(_verify_routing(constraint, route, lengths, vias, tracks))
        checks.append(_verify_thermal_separation(constraint, board))
        groups.append(
            {"net_class": constraint.name, "kind": constraint.kind, "checks": checks}
        )
    mechanical_checks = [_verify_board_layers(manifest, route)]
    mechanical_checks.extend(_verify_mechanical(manifest.mechanical, board))
    blockers = [
        {"scope": group["net_class"], **check}
        for group in groups
        for check in group["checks"]
        if check["status"] in {"violated", "unresolved"}
    ]
    blockers.extend(
        {"scope": "mechanical", **check}
        for check in mechanical_checks
        if check["status"] in {"violated", "unresolved"}
    )
    return {
        "hard_gate": "blocked" if blockers else "passed",
        "promotable": not blockers,
        "net_classes": groups,
        "mechanical": mechanical_checks,
        "blockers": blockers,
        "soft_preferences": _soft_score(manifest.soft_preferences, board, route),
    }


def parse_constraint_manifest(raw: Any) -> ConstraintManifest | None:
    """Parse an optional manifest while keeping old request bodies valid."""
    if raw is None:
        return None
    return ConstraintManifest.from_dict(raw)

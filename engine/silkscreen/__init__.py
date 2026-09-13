"""Silkscreen layout engine: pure Python, no KiCad and no network calls.

The name is the PCB layer that carries the human-readable marks -- reference
designators, polarity dots, the annotations that tell a person what a board is
doing. That is what this project is for.

Everything in this package is deterministic and unit-testable. KiCad interop and
model calls live in separate packages so the solver can be exercised without a
KiCad install or an API key.
"""

from .netlist import (
    CircuitSpec,
    Connection,
    Device,
    Passive,
    PassiveType,
    ValidationError,
    parse_circuit_spec,
)
from .packing import (
    Keepout,
    Layer,
    Net,
    PackResult,
    PackStatus,
    Part,
    Placement,
    Wire,
    pack,
)
from .routing import RoutePad, RouteResult, Track, Via, route
from .schematic import (
    SchematicResult,
    build_schematic,
    emit_kicad_sch,
    write_schematic,
)
from .units import (
    DEFAULT_CLEARANCE_NM,
    DEFAULT_GRID_NM,
    NM_PER_MM,
    mil,
    mm,
    to_mm,
)

__all__ = [
    # packing
    "Part",
    "Wire",
    "Net",
    "Keepout",
    "Layer",
    "Placement",
    "PackResult",
    "PackStatus",
    "pack",
    # circuit IR
    "CircuitSpec",
    "Device",
    "Passive",
    "PassiveType",
    "Connection",
    "ValidationError",
    "parse_circuit_spec",
    # schematic
    "SchematicResult",
    "build_schematic",
    "emit_kicad_sch",
    "write_schematic",
    # routing
    "RoutePad",
    "RouteResult",
    "Track",
    "Via",
    "route",
    # units
    "mm",
    "to_mm",
    "mil",
    "NM_PER_MM",
    "DEFAULT_GRID_NM",
    "DEFAULT_CLEARANCE_NM",
]

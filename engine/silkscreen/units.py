"""Unit handling for the layout engine.

KiCad's internal unit (IU) is the nanometre. Every public API in this package
speaks nanometres as ``int`` so that nothing silently round-trips through float
millimetres. The CP-SAT model works on a coarser integer *grid* because solving
at 1 nm resolution is intractable.
"""

from __future__ import annotations

import math

NM_PER_MM = 1_000_000
NM_PER_MIL = 25_400

#: Default solver grid. 0.05 mm is finer than any realistic placement tolerance
#: and keeps the CP-SAT domains ~1000x smaller than the nanometre-resolution
#: model this replaces.
DEFAULT_GRID_NM = 50_000

#: Default edge-to-edge gap between neighbouring courtyards (0.25 mm).
DEFAULT_CLEARANCE_NM = 250_000


def mm(value: float) -> int:
    """Millimetres -> nanometres."""
    return int(round(value * NM_PER_MM))


def to_mm(value_nm: int) -> float:
    """Nanometres -> millimetres."""
    return value_nm / NM_PER_MM


def mil(value: float) -> int:
    """Thousandths of an inch -> nanometres."""
    return int(round(value * NM_PER_MIL))


def cells_ceil(value_nm: int, grid_nm: int) -> int:
    """Nanometres -> grid cells, rounding **up** so parts never overlap."""
    return math.ceil(value_nm / grid_nm)


def cells_round(value_nm: int, grid_nm: int) -> int:
    """Nanometres -> grid cells, rounding to nearest.

    Used for pin offsets, where rounding up would bias every wire in the same
    direction and skew the objective.
    """
    return int(round(value_nm / grid_nm))


def nm_from_cells(cells: int, grid_nm: int) -> int:
    """Grid cells -> nanometres."""
    return cells * grid_nm

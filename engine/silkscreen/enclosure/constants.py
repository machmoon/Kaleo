"""Physical constants shared by the emitter and the verifier.

These live in one module on purpose: :mod:`.emit` draws the ring lip and
the standoffs with them, and :mod:`.verify` subtracts the same numbers when it
computes the receipt. When the two modules each carried their own copy, the
emitter grew a 2 mm lip into the cavity and the verifier never heard about
it, so the fit report said +1 mm while the lid crushed the tallest part by
1 mm. Importing from here is what stops that drifting again.

Integer nanometres, like everything else in the package.
"""

from __future__ import annotations

from ..units import mm

__all__ = ["LIP_CLEARANCE_NM", "LIP_HEIGHT_NM", "STANDOFF_HEIGHT_NM"]

#: Fit gap between a lip lid's lip and the cavity wall, per side.
LIP_CLEARANCE_NM: int = mm(0.2)
#: How far a lip lid's lip descends into the cavity. The cavity is
#: budgeted taller by exactly this much for a lip lid, and the verifier
#: measures the Z margin to the lip's underside, not the lid's.
LIP_HEIGHT_NM: int = mm(2.0)
#: Height of the board standoffs (and screw bosses) above the base floor.
STANDOFF_HEIGHT_NM: int = mm(2.0)

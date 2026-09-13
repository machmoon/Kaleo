"""Error taxonomy for the enclosure feature.

Frozen in docs/ai-cad-plan.md. Every failure path in the enclosure packages
raises one of these — nothing returns a quiet zero, because an agent that gets
an empty result concludes the case fits. The OpenSCAD render errors were
removed with the v1 path on 2026-09-08 (docs/ai-cad-plan.md v3); nothing in
this package execs a binary any more.
"""

from __future__ import annotations

__all__ = [
    "EnclosureError",
    "EnclosureValidationError",
    "CavityFitError",
    "CutoutError",
    "WallError",
    "KernelUnavailable",
    "KernelError",
    "FAILURE_CLASSES",
    "KernelFitError",
    "AssemblyError",
    "BoardModelUnavailable",
    "FreeCADUnavailable",
]


class EnclosureError(Exception):
    """Base class for every enclosure failure."""


class EnclosureValidationError(EnclosureError):
    """A proposed spec is invalid.

    ``errors`` holds one human-readable message per problem so the whole batch
    goes back to the model as a single repair prompt — the ``netlist.py``
    ``ValidationError`` convention.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in enclosure spec:\n  - "
            + "\n  - ".join(errors)
        )


class CavityFitError(EnclosureError):
    """The board does not fit the cavity.

    ``margins_nm`` is signed, keyed ``"x"``/``"y"``/``"z"``; a negative margin
    is a collision, and the number says by how much.
    """

    def __init__(self, message: str, margins_nm: dict[str, int]):
        self.margins_nm = margins_nm
        super().__init__(message)


class CutoutError(EnclosureError):
    """A cutout cannot be realised: bad ref, bad face, or overlap."""


class WallError(EnclosureError):
    """A wall violates a physical limit (e.g. below ``MIN_WALL_NM``)."""


class KernelUnavailable(EnclosureError):
    """``build123d`` is not installed (``pip install -e ".[cad]"``).

    Since 2026-09-08 (docs/ai-cad-plan.md v3) there is no second path: the
    kernel builds the geometry, measures the acceptance clauses and exports
    the STEP, so without it the case step refuses in words naming the extra
    rather than degrading to a lesser case.
    """


class KernelError(EnclosureError):
    """The OCCT kernel could not build the geometry the spec asked for.

    ``failure_class`` is one of the ``FAILURE_CLASSES`` names
    (``FILLET_FAILED``, ``SHELL_FAILED``, ``BOOLEAN_FAILED``, ``TEXT_FAILED``,
    ``INVALID_SHAPE``) so the repair prompt can say what to change, the
    Multi-Agent-CAD convention; ``detail`` is the kernel's own message.
    """

    def __init__(self, failure_class: str, detail: str):
        self.failure_class = failure_class
        self.detail = detail
        super().__init__(f"{failure_class}: {detail}")


#: Every ``KernelError.failure_class`` value.
FAILURE_CLASSES: tuple[str, ...] = (
    "FILLET_FAILED",
    "SHELL_FAILED",
    "BOOLEAN_FAILED",
    "TEXT_FAILED",
    "INVALID_SHAPE",
)


class KernelFitError(EnclosureError):
    """The built geometry failed one or more kernel clauses.

    ``failed`` lists the failing clause names; ``margins_nm`` carries every
    clause's signed margin (negative = violated, by that much), the
    ``CavityFitError`` convention extended to the whole report.
    """

    def __init__(self, message: str, failed: list[str], margins_nm: dict[str, int]):
        self.failed = failed
        self.margins_nm = margins_nm
        super().__init__(message)


class AssemblyError(EnclosureError):
    """The board could not be seated in the case.

    Raised before any measurement, for the things that make an assembly
    impossible rather than wrong: a board STEP with no solid in it, an
    :class:`~.cad.EnclosureModel` carrying no ``board_boxes`` to seat against.
    A *wrong* assembly is not an error -- it is a failing clause with a signed
    margin, the same split :mod:`~.kernel` makes.
    """


class BoardModelUnavailable(EnclosureError):
    """``kicad-cli`` is not on this machine, or it refused the export.

    The message always names the exact command that was tried, so the fix is
    the sentence rather than a search -- the ``KernelUnavailable`` rule
    applied to the second binary this feature can need. There is deliberately
    no fallback board model: a case verified against an invented board is
    worse than one verified against no board at all.
    """


class FreeCADUnavailable(EnclosureError):
    """``freecadcmd`` is not on this machine.

    FreeCAD is only ever the *independent* reader of a finished STEP
    (:mod:`~.freecad_check`), never a builder, and it is always
    out-of-process: FreeCAD and build123d each carry their own OCCT, and two
    OCCTs in one process is a segfault waiting to happen. Its absence costs
    the cross-check and nothing else.
    """

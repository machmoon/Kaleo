"""AI-generated enclosures for the boards the pipeline produces.

build123d/OCCT B-rep, and nothing else. The v1 OpenSCAD text emitter and its
offline fit verifier were removed on 2026-09-08 (docs/ai-cad-plan.md, v3):
they built a measurably different box from the kernel's behind a receipt that
could not fail, so where build123d is absent the case step now refuses in
words naming the ``cad`` extra rather than degrading to a worse case.

The package mirrors the engine's layering: this IR layer (``ir.py``,
``errors.py``) validates what a model proposes and knows nothing about any
board; ``board_shape.py``/``heights.py`` measure the board; ``cad.py`` builds
the B-rep and ``kernel.py`` is the acceptance gate; ``snapshot.py`` renders
the advisory packet. Contracts are frozen in ``docs/ai-cad-plan.md``.

This ``__init__`` re-exports the IR and error names only; everything else is
imported from its submodule directly, so the other workstreams never edit
this file.
"""

from .errors import (
    CavityFitError,
    CutoutError,
    EnclosureError,
    EnclosureValidationError,
    KernelError,
    KernelFitError,
    KernelUnavailable,
    WallError,
)
from .ir import (
    DEFAULT_CLEARANCE_NM,
    DEFAULT_WALL_NM,
    FACES,
    INSERT_CHOICES,
    LIDS,
    MATERIAL_CHOICES,
    MIN_WALL_NM,
    MOUNTS,
    Cutout,
    EnclosureSpec,
    parse_enclosure_spec,
)

__all__ = [
    "EnclosureError",
    "EnclosureValidationError",
    "CavityFitError",
    "CutoutError",
    "WallError",
    "KernelUnavailable",
    "KernelError",
    "KernelFitError",
    "MIN_WALL_NM",
    "DEFAULT_WALL_NM",
    "DEFAULT_CLEARANCE_NM",
    "FACES",
    "LIDS",
    "MOUNTS",
    "INSERT_CHOICES",
    "MATERIAL_CHOICES",
    "Cutout",
    "EnclosureSpec",
    "parse_enclosure_spec",
]

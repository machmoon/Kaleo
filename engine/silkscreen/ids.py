"""Stable UUIDs for emitted KiCad files.

KiCad tags nearly every object with a UUID. Generating them randomly would make
two runs of the same design produce byte-different files, which destroys the
one cheap review tool a hardware project has: ``git diff`` on the board.

Seeding from a string instead makes output reproducible, so a diff shows what
actually changed rather than a wall of new identifiers.
"""

from __future__ import annotations

import hashlib

__all__ = ["stable_uuid"]


def stable_uuid(seed: str) -> str:
    """A UUID-shaped identifier derived from ``seed``.

    Not a random UUID and not claimed to be one: it only has to be unique
    within a file and identical across runs. Collisions would need a SHA-1
    prefix collision between two seeds we chose ourselves.
    """
    h = hashlib.sha1(seed.encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

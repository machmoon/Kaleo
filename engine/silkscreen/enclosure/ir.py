"""Validated intermediate representation for an enclosure.

The ``netlist.py`` founding lesson, applied to 3D: a model proposes a JSON
:class:`EnclosureSpec`; nothing builds geometry until it validates. The model
chooses *style within bounds* — lid type, wall thickness, which connectors get
cutouts — and never types a board millimetre; every measured dimension is
injected by the deterministic emitter from the ``.kicad_pcb``.

Dimensions are **integer nanometres** everywhere inside the IR. The JSON at
the model boundary uses mm floats; the conversion happens exactly once, here,
in :func:`parse_enclosure_spec`.

Ref *existence* is deliberately not checked here — the IR does not know the
board. ``verify.py``'s ``verify_fit`` owns that check (a cutout naming an
absent ref is a hard ``CutoutError`` there, per the ``edge_refs`` convention).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from silkscreen.units import mm

from .errors import EnclosureValidationError

__all__ = [
    "MIN_WALL_NM",
    "DEFAULT_WALL_NM",
    "DEFAULT_CLEARANCE_NM",
    "FACES",
    "LIDS",
    "LIP_LIDS",
    "MOUNTS",
    "INSERT_CHOICES",
    "MATERIAL_CHOICES",
    "Cutout",
    "EnclosureSpec",
    "corner_wall_ok",
    "parse_enclosure_spec",
]

#: Printable FDM minimum wall. Anything thinner prints as lace.
MIN_WALL_NM: int = mm(1.2)

#: Default wall thickness when the model does not choose one.
DEFAULT_WALL_NM: int = mm(2.0)

#: Default board-to-cavity clearance.
DEFAULT_CLEARANCE_NM: int = mm(1.0)

#: Faces a cutout may sit on. ``bottom`` is absent on purpose: the board rests
#: on the base and a bottom opening would be under it.
FACES: tuple[str, ...] = ("left", "right", "front", "back", "top")

#: Lid styles the emitter knows how to draw. ``lip`` is a ring lip that
#: registers in the cavity (a sliding fit); ``screw`` adds corner bosses and
#: through-holes; ``snap`` adds a bead-and-groove detent to the lip.
#: ``friction`` is the v1 name for ``lip`` and still parses.
LIDS: tuple[str, ...] = ("lip", "screw", "snap", "none")

#: The lid styles that hang a ring lip into the cavity and are installed
#: lip-down: the cavity budgets the lip's height and the lid assembles
#: flipped. ``screw`` and ``none`` are neither.
LIP_LIDS: frozenset[str] = frozenset({"lip", "snap"})

#: How the board is held. ``holes``: standoffs at the board's own mounting
#: holes with insert bores (needs holes on the board); ``pins``: standoffs
#: with locating pins, the lid presses the board down; ``corners``: four
#: plain corner standoffs (v1); ``none``: the board rests on the floor.
MOUNTS: tuple[str, ...] = ("holes", "pins", "corners", "none")

#: Insert/screw sizes the emitter can bore for; ``self_tap`` is a plain
#: undersize hole for a self-tapping screw.
INSERT_CHOICES: tuple[str, ...] = ("M2", "M2.5", "M3", "M4", "self_tap")

#: Filaments with a shrinkage/fit entry in ``rules.MATERIALS``.
MATERIAL_CHOICES: tuple[str, ...] = ("PLA", "PETG", "ABS")

#: Board refs look like ``J1``/``USB3`` — letters then digits, same shape the
#: rest of the engine assigns via ``CircuitSpec.assign_refs``.
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*[0-9]$")

#: Cutout ids: a plain identifier, unique within the spec.
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: Characters allowed in an embossed lid label after sanitisation.
_LABEL_OK_RE = re.compile(r"[^A-Za-z0-9 ._+-]")

_MAX_LABEL_LEN = 32


def corner_wall_ok(wall_nm: int, radius_nm: int, minimum_nm: int = MIN_WALL_NM) -> bool:
    """Whether a rounded outer corner still leaves ``minimum_nm`` of wall.

    The outer shell is rounded to ``radius_nm`` but the cavity stays
    square-cornered, so the wall is thinnest on the corner's diagonal. With
    the arc centred ``r`` in from both faces and the cavity corner ``w`` in,
    that thickness is ``r - sqrt(2) * (r - w)`` -- equivalently
    ``sqrt(2) * w - r * (sqrt(2) - 1)`` -- once ``r`` exceeds ``w``; below
    that the flat faces are the thinnest point and ``w`` itself governs. It
    reaches zero at ``r = 3.414 w``: a radius a little over three walls
    opens a hole at every corner, which nothing bounded by half the side
    length ever noticed.

    Exact integer arithmetic: ``r - m >= sqrt(2) * (r - w)`` is squared
    once both sides are known non-negative, so no float ever decides.
    """
    if radius_nm <= wall_nm:
        return wall_nm >= minimum_nm
    lhs = radius_nm - minimum_nm
    if lhs < 0:
        return False
    return lhs * lhs >= 2 * (radius_nm - wall_nm) ** 2


@dataclass(frozen=True)
class Cutout:
    """A rectangular opening for one board part.

    The model names the part and the face; the engine resolves the actual
    geometry from the part's courtyard, so the opening can never disagree with
    the board.
    """

    id: str          # unique within the spec
    ref: str         # board ref, e.g. "J1" — engine resolves geometry
    face: str        # member of FACES
    margin_nm: int   # opening margin around the resolved courtyard interval


@dataclass(frozen=True)
class EnclosureSpec:
    """A validated two-piece case description. All dimensions integer nm."""

    wall_nm: int
    clearance_nm: int
    lid: str                      # member of LIDS
    corner_radius_nm: int         # 0 = square
    cutouts: tuple[Cutout, ...]
    standoffs: bool               # False only when mount == "none" (v1 field)
    vents: bool
    label: str | None             # embossed text on the lid, sanitised
    #: v2 (ENCLOSURE-SPEC v2). Defaults keep every v1 spec valid.
    mount: str = "corners"        # member of MOUNTS
    insert: str = "M3"            # member of INSERT_CHOICES
    material: str = "PLA"         # member of MATERIAL_CHOICES


def _strip_code_fence(text: str) -> str:
    """Remove a ``` fence if the model wrapped its JSON in one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _sanitise_label(value: object, errors: list[str]) -> str | None:
    """Emboss-safe lid text, or ``None``.

    Sanitisation is a transform, not a rejection: strip disallowed characters,
    collapse whitespace, cap the length. Only a non-string non-null value is a
    validation error — a label that sanitises to nothing becomes ``None``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(
            f"'label' must be a string or null, got {type(value).__name__}"
        )
        return None
    cleaned = " ".join(_LABEL_OK_RE.sub("", value).split())
    cleaned = cleaned[:_MAX_LABEL_LEN].rstrip()
    return cleaned or None


def _dim_nm(
    data: dict,
    key: str,
    errors: list[str],
    *,
    default_nm: int,
    minimum_nm: int,
    what: str,
) -> int:
    """Read one mm-float dimension from the JSON, converting to nm once.

    Missing key -> ``default_nm``. A non-numeric value or one below
    ``minimum_nm`` appends to ``errors`` and returns the default so the other
    checks still run and the batch stays complete.
    """
    if key not in data or data[key] is None:
        return default_nm
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{key!r} must be a number (mm), got {value!r}")
        return default_nm
    value_nm = mm(float(value))
    if value_nm < minimum_nm:
        if minimum_nm == 0:
            limit = "not negative"
        elif minimum_nm == 1:
            limit = "positive"
        else:
            limit = f"at least {minimum_nm / 1_000_000} mm"
        errors.append(f"{key!r} is {float(value)} mm; {what} must be {limit}")
        return default_nm
    return value_nm


def parse_enclosure_spec(text: str | dict) -> EnclosureSpec:
    """Parse and validate model output into an :class:`EnclosureSpec`.

    Accepts raw model text (a Markdown code fence is tolerated, dimensions are
    mm floats) or an already-decoded dict. **Collects every failure** into one
    :class:`EnclosureValidationError` so the whole batch goes back to the model
    as a single repair prompt.
    """
    if isinstance(text, str):
        try:
            data = json.loads(_strip_code_fence(text))
        except json.JSONDecodeError as exc:
            raise EnclosureValidationError(
                [f"response is not valid JSON: {exc}"]
            ) from exc
    else:
        data = text

    if not isinstance(data, dict):
        raise EnclosureValidationError(
            [f"expected a JSON object, got {type(data).__name__}"]
        )

    errors: list[str] = []

    wall_nm = _dim_nm(
        data, "wall_mm", errors,
        default_nm=DEFAULT_WALL_NM, minimum_nm=1,
        what="wall thickness",
    )
    # A positive-but-thin wall is its own message so the model learns the
    # actual limit, not just "positive".
    if wall_nm < MIN_WALL_NM:
        errors.append(
            f"'wall_mm' is {wall_nm / 1_000_000} mm, below the printable FDM "
            f"minimum of {MIN_WALL_NM / 1_000_000} mm"
        )
        wall_nm = DEFAULT_WALL_NM

    clearance_nm = _dim_nm(
        data, "clearance_mm", errors,
        default_nm=DEFAULT_CLEARANCE_NM, minimum_nm=1,
        what="board-to-cavity clearance",
    )

    corner_radius_nm = _dim_nm(
        data, "corner_radius_mm", errors,
        default_nm=0, minimum_nm=0,
        what="corner radius",
    )
    # The wall is thinnest on the corner diagonal once the radius passes the
    # wall thickness; model-fixable, so it joins the batch with the limit
    # spelled out rather than surfacing later as a WallError.
    if corner_radius_nm > 0 and not corner_wall_ok(wall_nm, corner_radius_nm):
        errors.append(
            f"'corner_radius_mm' is {corner_radius_nm / 1_000_000} mm, which "
            f"thins the {wall_nm / 1_000_000} mm wall below "
            f"{MIN_WALL_NM / 1_000_000} mm at the corners (the cavity corner "
            "is square); reduce the radius or thicken the wall"
        )
        corner_radius_nm = 0

    lid = str(data.get("lid", "lip"))
    if lid == "friction":  # v1 name
        lid = "lip"
    if lid not in LIDS:
        errors.append(f"'lid' is {lid!r}; allowed: {list(LIDS)}")
        lid = "lip"

    mount = data.get("mount")
    mount_given = mount is not None
    mount = "corners" if mount is None else str(mount)
    if mount not in MOUNTS:
        errors.append(f"'mount' is {mount!r}; allowed: {list(MOUNTS)}")
        mount = "corners"

    insert = str(data.get("insert", "M3"))
    if insert not in INSERT_CHOICES:
        errors.append(
            f"'insert' is {insert!r}; allowed: {list(INSERT_CHOICES)}"
        )
        insert = "M3"

    material = str(data.get("material", "PLA")).upper()
    if material not in MATERIAL_CHOICES:
        errors.append(
            f"'material' is {material!r}; allowed: {list(MATERIAL_CHOICES)}"
        )
        material = "PLA"

    raw_cutouts = data.get("cutouts", [])
    if raw_cutouts is None:
        raw_cutouts = []
    if not isinstance(raw_cutouts, list):
        errors.append(
            f"'cutouts' must be a list, got {type(raw_cutouts).__name__}"
        )
        raw_cutouts = []

    cutouts: list[Cutout] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cutouts):
        where = f"cutout[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object, got {item!r}")
            continue

        cid = str(item.get("id", "")).strip()
        if not _ID_RE.match(cid):
            errors.append(
                f"{where}: 'id' {cid!r} is not a valid identifier"
            )
        elif cid in seen_ids:
            errors.append(f"{where}: duplicate cutout id {cid!r}")
        seen_ids.add(cid)

        ref = str(item.get("ref", "")).strip()
        if not _REF_RE.match(ref):
            errors.append(
                f"{where}: 'ref' {ref!r} is not a reference designator "
                f"(expected e.g. 'J1', 'U3')"
            )

        face = str(item.get("face", ""))
        if face not in FACES:
            errors.append(
                f"{where}: 'face' is {face!r}; allowed: {list(FACES)}"
            )

        margin_nm = _dim_nm(
            item, "margin_mm", errors,
            default_nm=mm(0.5), minimum_nm=0,
            what=f"{where} opening margin",
        )

        cutouts.append(
            Cutout(id=cid, ref=ref, face=face, margin_nm=margin_nm)
        )

    standoffs = data.get("standoffs", mount != "none")
    if not isinstance(standoffs, bool):
        errors.append(f"'standoffs' must be a boolean, got {standoffs!r}")
        standoffs = True
    # ``standoffs`` (v1) and ``mount`` (v2) describe one thing. A v1 spec
    # that says ``standoffs: false`` means ``mount: none``; a v2 spec that
    # names a mount and also says ``standoffs: false`` contradicts itself.
    if not standoffs:
        if mount_given and mount != "none":
            errors.append(
                f"'standoffs' is false but 'mount' is {mount!r}; drop "
                f"'standoffs' or set 'mount' to 'none'"
            )
        mount = "none"

    vents = data.get("vents", False)
    if not isinstance(vents, bool):
        errors.append(f"'vents' must be a boolean, got {vents!r}")
        vents = False

    # A screw lid's pilot holes bite into the standoff bosses; without
    # standoffs they open onto an empty floor and the screws hold nothing.
    # Model-fixable, so it joins the batch rather than raising later.
    if lid == "screw" and not standoffs:
        errors.append(
            "'lid' is 'screw' but 'standoffs' is false: screw pilot holes "
            "need standoff bosses to bite into; set 'standoffs' to true or "
            "choose a different lid"
        )

    label = _sanitise_label(data.get("label"), errors)

    known = {
        "wall_mm", "clearance_mm", "corner_radius_mm", "lid", "cutouts",
        "standoffs", "vents", "label", "mount", "insert", "material",
    }
    for key in sorted(set(data) - known):
        errors.append(f"unknown field {key!r}; allowed: {sorted(known)}")

    if errors:
        raise EnclosureValidationError(errors)

    return EnclosureSpec(
        wall_nm=wall_nm,
        clearance_nm=clearance_nm,
        lid=lid,
        corner_radius_nm=corner_radius_nm,
        cutouts=tuple(cutouts),
        standoffs=standoffs,
        vents=vents,
        label=label,
        mount=mount,
        insert=insert,
        material=material,
    )

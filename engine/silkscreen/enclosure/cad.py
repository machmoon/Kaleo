"""build123d (OCCT) emitter for the enclosure: the v2 geometry half.

The model proposes an :class:`~.ir.EnclosureSpec`; this module turns it and a
:class:`~.board_shape.BoardEnvelope` into real B-rep solids, with every
measured number coming from the board and every design number from
:mod:`.rules`. It is the only enclosure emitter: the v1 OpenSCAD text path
was removed on 2026-09-08 (docs/ai-cad-plan.md v3), so without the ``cad``
extra there is no case and the callers say so rather than degrading.

Frames (docs/ai-cad-plan.md v2)
-------------------------------

Geometry is in the **assembled** frame: X as KiCad, Y flipped so ``front``
is the KiCad max-Y board edge (the package's frame map), Z up, origin at the
base's outer bottom-left-front corner. The map from a KiCad absolute point
``(x, y)`` to the assembled frame is::

    X = board_x0 + (x - envelope.x_min_nm)        board_x0 = wall + strip + clearance
    Y = board_y0 + (envelope.y_max_nm - y)        board_y0 = wall + clearance
    Z(board bottom face) = wall + standoff height

where ``strip`` is the corner-boss strip a screw lid adds on the left and
right of the cavity (zero otherwise). :func:`_ax`/:func:`_ay` are the only
two places that cross, mirroring ``emit._sx``/``emit._sy``.

The lid is **built** in the assembled frame (so cutouts, vents and the label
use the same coordinates as the base) and **stored** in its printed
orientation -- outer face on the bed at z = 0, lip pointing +Z -- by moving
it through the inverse of ``lid_assembled``. ``lid_assembled`` is the
build123d ``Location`` ``((0, outer_y, base_z + lid_z), (180, 0, 0))``: a
half-turn about X, so printed ``(x, y, z)`` lands at assembled
``(x, outer_y - y, base_z + lid_z - z)``. Printability checks run on
``model.lid`` as stored; mating checks run on ``model.lid.moved(model.lid_assembled)``.

Lid style ``"none"`` stores ``lid = None`` and ``lid_assembled = None``; the
case is then the base alone and ``outer_nm[2] == base_z``.

Units: everything is integer nanometres until the single :func:`_mm`
crossing that feeds the kernel, which works in mm floats. ``params_mm`` is
the receipt of every number used, mm floats rounded to three places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..packing import Layer
from ..units import mm, to_mm
from .board_shape import BoardEnvelope, PartExtent, find_part
from .errors import CutoutError, KernelError, KernelUnavailable
from .ir import FACES, MIN_WALL_NM, EnclosureSpec
from .rules import (
    BOSS_MIN_WALL_NM,
    CUTOUT_CHAMFER_NM,
    ELEPHANT_FOOT_NM,
    FIT_SLIDE_NM,
    HEADROOM_NM,
    HOLE_COMPENSATION_NM,
    INSERTS,
    INSERTS_SHORT,
    LIP_DEPTH_NM,
    LIP_SLACK_NM,
    LIP_VERTICAL_GAP_NM,
    LIP_WIDTH_NM,
    LOCATING_PIN_NM,
    LOCATING_PIN_SLACK_NM,
    MATERIALS,
    MIN_CORNER_RADIUS_NM,
    SCREW_HEAD_DEPTH_NM,
    SCREW_HEAD_POCKET_NM,
    SELF_TAP_UNDERSIZE_NM,
    SNAP_BEAD_NM,
    SNAP_CATCH_HEIGHT_NM,
    SNAP_CATCH_NM,
    SNAP_GAP_NM,
    SNAP_HOOK_SLOT_NM,
    SNAP_HOOK_WIDTH_NM,
    STANDOFF_HEIGHT_NM,
    STANDOFF_MIN_FLOOR_NM,
    STANDOFF_OD_NM,
    VENT_BOSS_KEEPOUT_NM,
    VENT_MAX_BRIDGE_NM,
    VENT_PITCH_NM,
    VENT_SLOT_NM,
    plug_envelope,
    snap_arm_length_nm,
    snap_strain_ppm,
)

__all__ = [
    "EnclosureModel",
    "ExportPaths",
    "kernel_available",
    "require_kernel",
    "build_enclosure",
    "export_model",
]

#: Overshoot so subtracted solids never share a face with what they cut.
EPS_NM: int = mm(0.01)
#: Depth and character size of the lid label. The label is a **deboss** cut
#: into the outer face: that face is the bed when the lid prints, so an emboss
#: there would put the letters under a 0.6 mm ceiling and fail ``overhang``
#: honestly. ``params_mm["label_emboss"]`` stays 0.0 -- nothing stands proud.
LABEL_DEPTH_NM: int = mm(0.6)
LABEL_EMBOSS_NM: int = 0
LABEL_TEXT_SIZE_NM: int = mm(6.0)
#: A side cutout is refused when its part is further than this from the
#: named wall -- the same 3 mm rule ``agents/enclosure.py`` uses to tell the
#: model which faces a part is near.
EDGE_NEAR_NM: int = mm(3.0)
#: Extra bore depth past the insert length (rules.Insert: "length + 1 mm").
INSERT_BORE_EXTRA_NM: int = mm(1.0)
#: Printable minimum plate left under a screw-head pocket.
MIN_PLATE_UNDER_POCKET_NM: int = mm(1.2)


def kernel_available() -> bool:
    """True when build123d imports (the ``cad`` extra is installed)."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        return False
    return True


#: The one sentence said whenever the kernel is missing. It is a module
#: constant because more than one caller has to say it -- ``require_kernel``
#: here, and :func:`~silkscreen.agents.enclosure.propose_enclosure`, which
#: refuses before it spends a model call -- and two wordings would mean two
#: install hints to keep true. It is deliberately under 160 characters: the
#: event seam truncates there, and the hint has to survive that.
KERNEL_MISSING: str = (
    "build123d is not installed; the enclosure feature needs the CAD kernel "
    '-- install it with `pip install -e ".[cad]"`'
)


def require_kernel() -> Any:
    """Import and return the ``build123d`` module, or raise
    :class:`KernelUnavailable` naming the extra to install."""
    try:
        import build123d
    except ImportError as exc:
        raise KernelUnavailable(KERNEL_MISSING) from exc
    return build123d


@dataclass(frozen=True)
class EnclosureModel:
    """The built case, the board it is built around, and the plugs it must admit.

    ``base``/``lid``/``board`` are build123d ``Part`` objects (typed ``Any``
    so this module imports without the kernel). ``board`` is the keep-out
    solid: substrate slab plus one prism per part on each side, assembled
    frame. ``plugs`` holds ``(cutout id, prism)`` pairs, each prism the plug
    envelope extruded from inside the cavity through the wall, so "the opening
    admits the plug" is one boolean. ``standoffs`` holds ``(hole ref or
    corner name, centre x, centre y)`` in nm for the concentricity clause.
    ``params_mm`` is the receipt of every number the emitter used.

    ``lid`` and ``lid_assembled`` are ``None`` for lid style ``"none"``.
    ``board_boxes`` is the keep-out as plain boxes -- ``(ref, side, x0, y0,
    z0, x1, y1, z1)`` in nm, assembled frame, the substrate first under ref
    ``"board"`` -- so a preview can draw it without touching the kernel.
    """

    base: Any
    lid: Any
    board: Any
    lid_assembled: Any                      # build123d Location
    plugs: tuple[tuple[str, Any], ...]
    standoffs: tuple[tuple[str, int, int], ...]
    outer_nm: tuple[int, int, int]          # outer x, y, z of the assembled case
    params_mm: dict[str, float]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    board_boxes: tuple[tuple[str, str, int, int, int, int, int, int], ...] = ()


@dataclass(frozen=True)
class ExportPaths:
    step: Path
    base_stl: Path
    lid_stl: Path


# ------------------------------------------------------------ nm -> mm seam


def _mm(value_nm: int) -> float:
    """The one nm -> mm crossing that feeds the kernel."""
    return to_mm(value_nm)


# --------------------------------------------------------------- dimensions


@dataclass(frozen=True)
class _Dims:
    """Every derived dimension in nm; the receipt is built from this."""

    wall: int
    clearance: int
    corner_radius: int        # effective outer radius (0 = square)
    inner_radius: int         # cavity corner radius
    strip: int                # screw-lid corner-boss strip per side (x)
    standoff_h: int
    board_x: int
    board_y: int
    board_z: int
    parts_z: int
    headroom: int
    cavity_x: int
    cavity_y: int
    cavity_z: int
    outer_x: int
    outer_y: int
    base_z: int
    lid_z: int                # lid plate thickness; 0 for lid "none"
    lip_depth: int            # 0 unless lip/snap
    lip_slack: int            # per side, material fit included
    board_x0: int             # assembled X of the board's KiCad x_min
    board_y0: int             # assembled Y of the board's KiCad y_max
    board_bottom: int         # assembled Z of the board's bottom face
    board_top: int
    label_emboss: int
    boss_d: int               # insert boss OD (hole standoffs, screw bosses)
    snap_arm: int             # hook depth below the plate incl. bead; 0 unless snap
    snap_deflection: int      # hook tip deflection at assembly; 0 unless snap


def _ax(x_nm: int, d: _Dims, envelope: BoardEnvelope) -> int:
    """KiCad absolute X -> assembled X (nm)."""
    return d.board_x0 + (x_nm - envelope.x_min_nm)


def _ay(y_nm: int, d: _Dims, envelope: BoardEnvelope) -> int:
    """KiCad absolute Y (down) -> assembled Y (up). The one flip."""
    return d.board_y0 + (envelope.y_max_nm - y_nm)


def _mount(spec: EnclosureSpec) -> str:
    return spec.mount if spec.standoffs else "none"


def _near_faces(part: PartExtent, envelope: BoardEnvelope) -> list[str]:
    """Faces a part sits within :data:`EDGE_NEAR_NM` of (agents/enclosure.py)."""
    faces: list[str] = []
    if part.x_min_nm - envelope.x_min_nm <= EDGE_NEAR_NM:
        faces.append("left")
    if envelope.x_max_nm - part.x_max_nm <= EDGE_NEAR_NM:
        faces.append("right")
    if envelope.y_max_nm - part.y_max_nm <= EDGE_NEAR_NM:
        faces.append("front")
    if part.y_min_nm - envelope.y_min_nm <= EDGE_NEAR_NM:
        faces.append("back")
    return faces


def _resolve(cutout, envelope: BoardEnvelope) -> PartExtent:
    if cutout.face not in FACES:
        raise CutoutError(
            f"cutout {cutout.id!r} names unknown face {cutout.face!r}; "
            f"expected one of {list(FACES)}"
        )
    part = find_part(envelope, cutout.ref)
    if part is None:
        known = sorted(p.ref for p in envelope.parts)
        raise CutoutError(
            f"cutout {cutout.id!r} names ref {cutout.ref!r}, which is not on "
            f"this board. Available: {known}"
        )
    if cutout.face != "top":
        near = _near_faces(part, envelope)
        if cutout.face not in near:
            where = f"near faces: {', '.join(near)}" if near else "near no face"
            raise CutoutError(
                f"cutout {cutout.id!r} puts {cutout.ref} on the {cutout.face} "
                f"face, but the part is more than {to_mm(EDGE_NEAR_NM):g} mm "
                f"from that edge ({where})"
            )
    return part


def _plug_for(part: PartExtent, face: str):
    """The plug envelope for a side cutout, width measured along the wall."""
    if face in ("left", "right"):
        along = part.y_max_nm - part.y_min_nm
    else:
        along = part.x_max_nm - part.x_min_nm
    return plug_envelope(part.connector, along, part.height_nm)


def _opening_z(board_top: int, part: PartExtent, plug) -> tuple[int, int]:
    """Vertical extent of a side opening, margin excluded: the plug envelope
    centred on the receptacle's axis (board top + half the part's height,
    plus the envelope's axis offset). See :class:`~.rules.PlugEnvelope`."""
    axis = board_top + part.height_nm // 2 + plug.axis_offset_nm
    return axis - plug.height_nm // 2, axis + plug.height_nm - plug.height_nm // 2


def _insert_bore(spec: EnclosureSpec, *, short: bool) -> tuple[int, int, str]:
    """(bore diameter, bore depth, description) for the spec's insert.

    ``short`` selects the standoff row (:data:`~.rules.INSERTS_SHORT`) over
    the full-length row a lid boss takes. The bore is the insert's own hole
    size, never widened by :data:`~.rules.HOLE_COMPENSATION_NM`: that
    allowance is for a screw to pass, and an insert's knurl needs the
    plastic it would remove.
    """
    table = INSERTS_SHORT if short else INSERTS
    if spec.insert == "self_tap":
        thread = INSERTS["M3"]
        return (
            thread.thread_nm - SELF_TAP_UNDERSIZE_NM,
            table["M3"].length_nm + INSERT_BORE_EXTRA_NM,
            "self-tapping M3",
        )
    ins = table[spec.insert]
    return (
        ins.bore_nm,
        ins.length_nm + INSERT_BORE_EXTRA_NM,
        f"{ins.name} insert ({to_mm(ins.length_nm):.1f} mm)",
    )


def _dims(
    spec: EnclosureSpec, envelope: BoardEnvelope, warnings: list[str]
) -> tuple[_Dims, list[tuple[Any, PartExtent, Any]]]:
    """Derive every dimension; also resolves the cutouts (a bad cutout is a
    hard error before any geometry exists)."""
    wall = spec.wall_nm
    clearance = spec.clearance_nm
    mount = _mount(spec)
    standoff_h = 0 if mount == "none" else STANDOFF_HEIGHT_NM
    bore_d, bore_depth, what = _insert_bore(spec, short=True)
    if mount == "holes" and envelope.mounting_holes:
        # The standoff holds the whole insert plus a floor; the bore is never
        # shortened to fit, since a proud insert puts the board on the brass.
        needed = bore_depth + STANDOFF_MIN_FLOOR_NM
        if needed > standoff_h:
            warnings.append(
                f"standoffs raised from {to_mm(standoff_h):.2f} mm to "
                f"{to_mm(needed):.2f} mm to hold the {what} bore "
                f"{to_mm(bore_depth):.2f} mm deep over a "
                f"{to_mm(STANDOFF_MIN_FLOOR_NM):.2f} mm floor"
            )
            standoff_h = needed
    boss_d = max(STANDOFF_OD_NM, bore_d + 2 * BOSS_MIN_WALL_NM)
    strip = boss_d if spec.lid == "screw" else 0
    lid_z = 0 if spec.lid == "none" else wall
    lip = spec.lid in ("lip", "snap")
    lip_depth = LIP_DEPTH_NM if lip else 0
    material = MATERIALS[spec.material]
    lip_slack = LIP_SLACK_NM + material.fit_extra_nm
    snap_arm = 0
    snap_deflection = 0
    if spec.lid == "snap":
        # Cantilever hooks: the bead engages the catch ledge by the ledge's
        # protrusion less the slack, and the arm is as long as the material's
        # allowable strain needs for that tip deflection (rules.snap_arm_length_nm).
        snap_deflection = SNAP_CATCH_NM - lip_slack
        snap_arm = SNAP_BEAD_NM + snap_arm_length_nm(
            LIP_WIDTH_NM, snap_deflection, material
        )
    if lip:
        # The ring lip descends into the cavity just inside the wall, so the
        # board-to-wall gap must hold slack + ring + a sliding fit to the
        # board edge; otherwise the ring overhangs the board and a tall edge
        # part meets it (the kernel's headroom clause measures exactly that).
        # A snap ring sits a catch further in, and its hooks flex inward by
        # the deflection on the way on.
        needed = lip_slack + LIP_WIDTH_NM + FIT_SLIDE_NM
        if spec.lid == "snap":
            needed += SNAP_CATCH_NM + snap_deflection
        if clearance < needed:
            warnings.append(
                f"clearance widened from {to_mm(clearance):.2f} mm to "
                f"{to_mm(needed):.2f} mm so the {to_mm(LIP_WIDTH_NM):.1f} mm "
                "ring lip clears the board edge"
            )
            clearance = needed

    board_x = envelope.x_max_nm - envelope.x_min_nm
    board_y = envelope.y_max_nm - envelope.y_min_nm
    board_bottom = wall + standoff_h
    board_top = board_bottom + envelope.thickness_nm

    # Headroom: rules.HEADROOM_NM above the tallest top part, and under a
    # ring lip also the lip depth plus LIP_VERTICAL_GAP_NM, so the ring never
    # lands on a part and the seam closes on the walls (YAPP ridgeGap). Snap
    # hooks hang deeper than the ring, alongside the board edge, and must
    # stop a sliding fit above the board's underside.
    headroom = HEADROOM_NM
    if lip:
        headroom = max(headroom, lip_depth + LIP_VERTICAL_GAP_NM)
    if snap_arm:
        headroom = max(
            headroom,
            snap_arm + FIT_SLIDE_NM - envelope.thickness_nm - envelope.max_height_nm,
        )
    cavity_z = standoff_h + envelope.thickness_nm + envelope.max_height_nm + headroom

    cutouts = []
    for cutout in spec.cutouts:
        part = _resolve(cutout, envelope)
        plug = None
        if cutout.face != "top":
            plug = _plug_for(part, cutout.face)
            # A plug opening may stand taller than the receptacle: the rim
            # (and any lip ring) must clear the opening's top edge.
            _z0, z1 = _opening_z(board_top, part, plug)
            need = (
                z1 - wall + cutout.margin_nm + lip_depth
                + (LIP_VERTICAL_GAP_NM if lip else 0)
            )
            if need > cavity_z:
                warnings.append(
                    f"cavity raised to {to_mm(need + wall):.2f} mm so the "
                    f"{cutout.id!r} opening for {part.ref} clears the rim"
                )
                cavity_z = need
        cutouts.append((cutout, part, plug))

    cavity_x = board_x + 2 * clearance + 2 * strip
    cavity_y = board_y + 2 * clearance
    outer_x = cavity_x + 2 * wall
    outer_y = cavity_y + 2 * wall
    base_z = wall + cavity_z

    radius = spec.corner_radius_nm
    if 0 < radius < MIN_CORNER_RADIUS_NM:
        radius = MIN_CORNER_RADIUS_NM
    # The cavity corners are rounded by (radius - wall) so the wall stays
    # uniform; capping the radius at wall + clearance keeps the cavity's
    # corner arc outside the board's own corner.
    cap = wall + clearance
    if radius > cap:
        warnings.append(
            f"corner radius {to_mm(spec.corner_radius_nm):.2f} mm clamped to "
            f"{to_mm(cap):.2f} mm (wall + clearance) so the cavity clears the "
            f"board corners"
        )
        radius = cap
    half = min(outer_x, outer_y) // 2 - EPS_NM
    radius = min(radius, half)
    inner_radius = max(radius - wall, 0)

    dims = _Dims(
        wall=wall, clearance=clearance, corner_radius=radius,
        inner_radius=inner_radius, strip=strip, standoff_h=standoff_h,
        board_x=board_x, board_y=board_y, board_z=envelope.thickness_nm,
        parts_z=envelope.max_height_nm, headroom=headroom,
        cavity_x=cavity_x, cavity_y=cavity_y, cavity_z=cavity_z,
        outer_x=outer_x, outer_y=outer_y, base_z=base_z, lid_z=lid_z,
        lip_depth=lip_depth, lip_slack=lip_slack,
        board_x0=wall + strip + clearance, board_y0=wall + clearance,
        board_bottom=board_bottom, board_top=board_top,
        label_emboss=LABEL_EMBOSS_NM if (spec.label and lid_z) else 0,
        boss_d=boss_d, snap_arm=snap_arm, snap_deflection=snap_deflection,
    )
    return dims, cutouts


def _params(d: _Dims) -> dict[str, float]:
    """The receipt: every number used, as mm floats rounded to 3 places."""
    names = (
        "board_x", "board_y", "board_z", "parts_z", "clearance", "wall",
        "corner_radius", "inner_radius", "strip", "standoff_h", "headroom",
        "cavity_x", "cavity_y", "cavity_z", "outer_x", "outer_y", "base_z",
        "lid_z", "lip_depth", "lip_slack", "board_x0", "board_y0",
        "board_bottom", "board_top", "label_emboss", "boss_d",
    )
    return {name: round(to_mm(getattr(d, name)), 3) for name in names}


# ------------------------------------------------------------- kernel shims


def _op(failure_class: str, what: str, fn):
    """Run one kernel operation, wrapping any OCCT failure as KernelError."""
    try:
        result = fn()
    except (KernelError, CutoutError):
        raise
    except Exception as exc:  # OCP raises Standard_Failure, ValueError, ...
        raise KernelError(failure_class, f"{what}: {exc}") from exc
    if result is None:
        raise KernelError(failure_class, f"{what}: kernel returned nothing")
    return result


class _K:
    """The build123d names this module uses, bound once per build."""

    def __init__(self, b) -> None:
        self.b = b
        self.MIN = (b.Align.MIN, b.Align.MIN, b.Align.MIN)
        self.MIN2 = (b.Align.MIN, b.Align.MIN)
        self.CCMIN = (b.Align.CENTER, b.Align.CENTER, b.Align.MIN)

    def box(self, x0: int, y0: int, z0: int, x1: int, y1: int, z1: int):
        """Axis-aligned box from nm corner to nm corner, assembled frame."""
        b = self.b
        return b.Pos(_mm(x0), _mm(y0), _mm(z0)) * b.Box(
            _mm(x1 - x0), _mm(y1 - y0), _mm(z1 - z0), align=self.MIN
        )

    def rrect(self, x0: int, y0: int, x1: int, y1: int, r: int):
        """Rounded (or square when r == 0) rectangle sketch on the XY plane."""
        b = self.b
        w, h = _mm(x1 - x0), _mm(y1 - y0)
        if r > 0:
            sk = b.RectangleRounded(w, h, _mm(r), align=self.MIN2)
        else:
            sk = b.Rectangle(w, h, align=self.MIN2)
        return b.Pos(_mm(x0), _mm(y0), 0) * sk

    def rbox(self, x0: int, y0: int, x1: int, y1: int, z0: int, z1: int, r: int):
        """Rounded box: outer shell or cavity."""
        b = self.b
        sk = self.rrect(x0, y0, x1, y1, r)
        return b.Pos(0, 0, _mm(z0)) * b.extrude(sk, _mm(z1 - z0))

    def cyl_z(self, cx: int, cy: int, z0: int, z1: int, diameter: int):
        b = self.b
        return b.Pos(_mm(cx), _mm(cy), _mm(z0)) * b.Cylinder(
            _mm(diameter) / 2, _mm(z1 - z0), align=self.CCMIN
        )

    def cyl_x(self, x0: int, x1: int, cy: int, cz: int, diameter: int):
        """Cylinder whose axis runs along X from x0 to x1."""
        b = self.b
        return b.Pos(_mm(x0 + x1) / 2, _mm(cy), _mm(cz)) * b.Cylinder(
            _mm(diameter) / 2, _mm(x1 - x0), rotation=(0, 90, 0)
        )

    def cyl_y(self, y0: int, y1: int, cx: int, cz: int, diameter: int):
        b = self.b
        return b.Pos(_mm(cx), _mm(y0 + y1) / 2, _mm(cz)) * b.Cylinder(
            _mm(diameter) / 2, _mm(y1 - y0), rotation=(90, 0, 0)
        )

    def fuse(self, what: str, a, parts):
        def run():
            out = a
            for p in parts:
                out = out + p
            return out
        return _op("BOOLEAN_FAILED", what, run)

    def cut(self, what: str, a, parts):
        def run():
            out = a
            for p in parts:
                out = out - p
            return out
        return _op("BOOLEAN_FAILED", what, run)

    def chamfer_edges(self, what: str, shape, edges, size_nm: int):
        b = self.b
        if not edges:
            return shape
        return _op("FILLET_FAILED", what, lambda: b.chamfer(edges, _mm(size_nm)))

    @staticmethod
    def edges_on_plane(shape, axis: str, at_nm: int, tol_nm: int = mm(0.001)):
        """Edges of ``shape`` lying entirely in the plane ``axis == at``."""
        at = _mm(at_nm)
        tol = _mm(tol_nm)
        out = []
        for e in shape.edges():
            bb = e.bounding_box()
            lo, hi = getattr(bb.min, axis), getattr(bb.max, axis)
            if abs(lo - at) <= tol and abs(hi - at) <= tol:
                out.append(e)
        return out

    @staticmethod
    def edges_within(edges, x: tuple[int, int], y: tuple[int, int], z: tuple[int, int]):
        """Those of ``edges`` whose bbox sits inside the nm ranges given."""
        tol = _mm(mm(0.001))
        out = []
        for e in edges:
            bb = e.bounding_box()
            if (
                _mm(x[0]) - tol <= bb.min.X and _mm(x[1]) + tol >= bb.max.X
                and _mm(y[0]) - tol <= bb.min.Y and _mm(y[1]) + tol >= bb.max.Y
                and _mm(z[0]) - tol <= bb.min.Z and _mm(z[1]) + tol >= bb.max.Z
            ):
                out.append(e)
        return out


def _check_solid(shape, what: str) -> None:
    """A finished part must be one valid solid with positive volume."""
    if shape is None:
        raise KernelError("INVALID_SHAPE", f"{what}: kernel returned nothing")
    if not shape.is_valid:
        raise KernelError("INVALID_SHAPE", f"{what} failed BRepCheck")
    solids = shape.solids()
    if len(solids) != 1:
        raise KernelError(
            "INVALID_SHAPE", f"{what} is {len(solids)} solids, expected 1"
        )
    if shape.volume <= 0:
        raise KernelError("INVALID_SHAPE", f"{what} has no volume")


# ---------------------------------------------------------------- standoffs


def _corner_standoffs(d: _Dims) -> list[tuple[str, int, int, int]]:
    """Four plain standoffs inset from the board's corners, at the v2 boss
    diameter: ``(name, cx, cy, dia)``."""
    # Tangent to the board edge when the board is big enough.
    inset = min(
        d.clearance + STANDOFF_OD_NM // 2,
        (d.cavity_x - 2 * d.strip) // 4, d.cavity_y // 4,
    )
    dia = min(STANDOFF_OD_NM, 2 * inset)
    x0 = d.board_x0 - d.clearance + inset
    x1 = d.board_x0 + d.board_x + d.clearance - inset
    y0 = d.board_y0 - d.clearance + inset
    y1 = d.board_y0 + d.board_y + d.clearance - inset
    return [
        ("front_left", x0, y0, dia), ("front_right", x1, y0, dia),
        ("back_left", x0, y1, dia), ("back_right", x1, y1, dia),
    ]


def _screw_bosses(d: _Dims) -> list[tuple[str, int, int]]:
    """Screw-lid corner bosses, in the strips outside the board."""
    r = d.boss_d // 2
    x0, x1 = d.wall + r, d.outer_x - d.wall - r
    y0, y1 = d.wall + r, d.outer_y - d.wall - r
    return [
        ("screw_front_left", x0, y0), ("screw_front_right", x1, y0),
        ("screw_back_left", x0, y1), ("screw_back_right", x1, y1),
    ]


def _plug_path_clear(
    d: _Dims, envelope: BoardEnvelope, cutouts, cx: int, cy: int, r: int
) -> str | None:
    """The id of the side cutout whose plug path a column of radius ``r`` at
    ``(cx, cy)`` stands in, or None.

    A plug has to travel from the outer face to the board edge, so the path is
    the opening's along-wall interval from the outside to the board, over the
    opening's height; ``kernel._plug_prisms`` checks the same box.
    """
    for cutout, part, plug in cutouts:
        if cutout.face == "top":
            continue
        lo, hi = _along_interval(d, envelope, cutout, part, plug)
        z_bot, z_top = _opening_z(d.board_top, part, plug)
        if z_top < d.wall or z_bot > d.base_z:
            continue
        if cutout.face == "left":
            rect = (0, lo, d.board_x0, hi)
        elif cutout.face == "right":
            rect = (d.board_x0 + d.board_x, lo, d.outer_x, hi)
        elif cutout.face == "front":
            rect = (lo, 0, hi, d.board_y0)
        else:
            rect = (lo, d.board_y0 + d.board_y, hi, d.outer_y)
        if _rect_near(rect, cx, cy, r):
            return cutout.id
    return None


def _clear_screw_bosses(
    d: _Dims, envelope: BoardEnvelope, cutouts, warnings: list[str] | None
) -> list[tuple[str, int, int]]:
    """The screw-lid bosses no side cutout's plug has to pass through.

    A corner boss runs floor to rim in the strip between wall and board, which
    is exactly where a plug entering near a corner travels; keeping it would
    build a case the connector cannot be plugged into. The boss is skipped by
    name (the ``_press_bosses`` convention), and a lid left with no screw is
    said to be unretained. ``warnings`` is None on the second (lid) call so
    each skip is reported once.
    """
    out = []
    for name, cx, cy in _screw_bosses(d):
        blocked = _plug_path_clear(d, envelope, cutouts, cx, cy, d.boss_d // 2)
        if blocked is not None:
            if warnings is not None:
                warnings.append(
                    f"no screw boss at {name}: it stands in the plug path of "
                    f"cutout {blocked!r}"
                )
            continue
        out.append((name, cx, cy))
    if warnings is not None and not out:
        warnings.append(
            "lid is not retained: every screw boss stands in a plug path; "
            "use a snap or friction lid, or move connectors off the corners"
        )
    return out


def _build_standoffs(
    k: _K, spec: EnclosureSpec, envelope: BoardEnvelope, d: _Dims,
    warnings: list[str],
) -> tuple[list, list, list[tuple[str, int, int]], dict[str, float], list]:
    """Boss solids to fuse, bore solids to cut, the standoff record, the
    receipt entries for the mount, and the standoffs the lid must press the
    board onto -- ``(name, cx, cy, diameter)`` -- when nothing else holds
    the board down (mount ``pins`` and ``corners``; ``holes`` screws it)."""
    mount = _mount(spec)
    adds: list = []
    cuts: list = []
    record: list[tuple[str, int, int]] = []
    receipt: dict[str, float] = {}
    press: list[tuple[str, int, int, int]] = []
    if mount == "none":
        return adds, cuts, record, receipt, press

    z0, z1 = d.wall, d.wall + d.standoff_h
    if mount in ("holes", "pins") and not envelope.mounting_holes:
        warnings.append(
            f"board has no mounting holes; mount {mount!r} fell back to "
            f"corner standoffs"
        )
        mount = "corners"

    if mount == "corners":
        for name, cx, cy, dia in _corner_standoffs(d):
            adds.append(k.cyl_z(cx, cy, z0, z1, dia))
            record.append((name, cx, cy))
            press.append((name, cx, cy, dia))
        receipt["standoff_d"] = round(to_mm(_corner_standoffs(d)[0][3]), 3)
        return adds, cuts, record, receipt, press

    receipt["standoff_d"] = round(to_mm(d.boss_d), 3)
    if mount == "holes":
        bore_d, bore_depth, _what = _insert_bore(spec, short=True)
        assert bore_depth + STANDOFF_MIN_FLOOR_NM <= d.standoff_h  # _dims raised it
        receipt["bore_d"] = round(to_mm(bore_d), 3)
        receipt["bore_depth"] = round(to_mm(bore_depth), 3)
        receipt["insert_length"] = round(to_mm(bore_depth - INSERT_BORE_EXTRA_NM), 3)
    else:
        pin_d = LOCATING_PIN_NM - LOCATING_PIN_SLACK_NM
        receipt["pin_d"] = round(to_mm(pin_d), 3)
        receipt["pin_h"] = round(to_mm(envelope.thickness_nm), 3)

    for hole in envelope.mounting_holes:
        cx, cy = _ax(hole.x_nm, d, envelope), _ay(hole.y_nm, d, envelope)
        adds.append(k.cyl_z(cx, cy, z0, z1, d.boss_d))
        record.append((hole.ref, cx, cy))
        if mount == "pins":
            press.append((hole.ref, cx, cy, d.boss_d))
        if mount == "holes":
            if spec.insert != "self_tap":
                need = INSERTS[spec.insert].thread_nm
                if hole.drill_nm < need:
                    warnings.append(
                        f"mounting hole {hole.ref} drills "
                        f"{to_mm(hole.drill_nm):.2f} mm, under the "
                        f"{to_mm(need):.2f} mm {spec.insert} thread"
                    )
            cuts.append(k.cyl_z(cx, cy, z1 - bore_depth, z1 + EPS_NM, bore_d))
        else:
            pin_d = LOCATING_PIN_NM - LOCATING_PIN_SLACK_NM
            fit = hole.drill_nm - LOCATING_PIN_SLACK_NM
            if fit < pin_d:
                warnings.append(
                    f"mounting hole {hole.ref} drills "
                    f"{to_mm(hole.drill_nm):.2f} mm; locating pin narrowed to "
                    f"{to_mm(fit):.2f} mm to fit it"
                )
                pin_d = fit
            if pin_d > 0:
                adds.append(
                    k.cyl_z(cx, cy, z1 - EPS_NM, z1 + envelope.thickness_nm, pin_d)
                )
    return adds, cuts, record, receipt, press


# ------------------------------------------------------------------ cutouts


def _along_interval(
    d: _Dims, envelope: BoardEnvelope, cutout, part: PartExtent, plug
) -> tuple[int, int]:
    """The opening's extent along its wall, margin included, in the
    assembled frame (Y for left/right faces, X for front/back)."""
    if cutout.face in ("left", "right"):
        along = (
            _ay(part.y_max_nm, d, envelope) + _ay(part.y_min_nm, d, envelope)
        ) // 2
    else:
        along = (
            _ax(part.x_min_nm, d, envelope) + _ax(part.x_max_nm, d, envelope)
        ) // 2
    half = plug.width_nm // 2 + cutout.margin_nm
    return along - half, along + half


def _opening(
    k: _K, d: _Dims, envelope: BoardEnvelope, cutout, part: PartExtent, plug
) -> tuple[Any, Any, tuple, tuple, tuple]:
    """(opening solid, plug prism, x-range, y-range, z-range of the opening
    on the outer face) for one side cutout."""
    m = cutout.margin_nm
    face = cutout.face
    # Centred along the wall on the receptacle, and vertically on its axis.
    lo, hi = _along_interval(d, envelope, cutout, part, plug)
    along = (lo + hi) // 2
    z_bot, _z_top = _opening_z(d.board_top, part, plug)
    half_w = plug.width_nm // 2

    if face == "left":
        t0, t1 = -d.wall, d.wall + d.clearance   # through-wall axis (x)
        o0, o1 = -EPS_NM, d.wall + d.clearance
    elif face == "right":
        t0, t1 = d.outer_x - d.wall - d.clearance, d.outer_x + d.wall
        o0, o1 = d.outer_x - d.wall - d.clearance, d.outer_x + EPS_NM
    elif face == "front":
        t0, t1 = -d.wall, d.wall + d.clearance
        o0, o1 = -EPS_NM, d.wall + d.clearance
    else:  # back
        t0, t1 = d.outer_y - d.wall - d.clearance, d.outer_y + d.wall
        o0, o1 = d.outer_y - d.wall - d.clearance, d.outer_y + EPS_NM

    if plug.round:
        dia = plug.width_nm
        cz = z_bot + dia // 2
        if face in ("left", "right"):
            opening = k.cyl_x(o0, o1, along, cz, dia + 2 * m)
            prism = k.cyl_x(t0, t1, along, cz, dia)
            xr, yr = (o0, o1), (along - half_w - m, along + half_w + m)
        else:
            opening = k.cyl_y(o0, o1, along, cz, dia + 2 * m)
            prism = k.cyl_y(t0, t1, along, cz, dia)
            xr, yr = (along - half_w - m, along + half_w + m), (o0, o1)
        zr = (cz - half_w - m, cz + half_w + m)
        return opening, prism, xr, yr, zr

    z0, z1 = z_bot, z_bot + plug.height_nm
    if face in ("left", "right"):
        opening = k.box(o0, along - half_w - m, z0 - m, o1, along + half_w + m, z1 + m)
        prism = k.box(t0, along - half_w, z0, t1, along + half_w, z1)
        xr, yr = (o0, o1), (along - half_w - m, along + half_w + m)
    else:
        opening = k.box(along - half_w - m, o0, z0 - m, along + half_w + m, o1, z1 + m)
        prism = k.box(along - half_w, t0, z0, along + half_w, t1, z1)
        xr, yr = (along - half_w - m, along + half_w + m), (o0, o1)
    return opening, prism, xr, yr, (z0 - m, z1 + m)


def _top_opening(k: _K, d: _Dims, envelope: BoardEnvelope, cutout, part) -> tuple:
    """Top-face window: courtyard + margin, through the whole lid."""
    m = cutout.margin_nm
    x0 = _ax(part.x_min_nm, d, envelope) - m
    x1 = _ax(part.x_max_nm, d, envelope) + m
    y0 = _ay(part.y_max_nm, d, envelope) - m
    y1 = _ay(part.y_min_nm, d, envelope) + m
    z0 = d.base_z - d.lip_depth - EPS_NM
    z1 = d.base_z + d.lid_z + d.label_emboss + EPS_NM
    return k.box(x0, y0, z0, x1, y1, z1), (x0, x1), (y0, y1)


# --------------------------------------------------------------------- vents


def _vent_slots(
    d: _Dims, keepout: list[tuple[int, int]]
) -> list[tuple[int, int, int, int]]:
    """Lid vent slots as (x0, y0, x1, y1) nm rectangles, assembled frame.

    Slots run along Y, :data:`VENT_SLOT_NM` wide at :data:`VENT_PITCH_NM`
    pitch, no longer than :data:`VENT_MAX_BRIDGE_NM`, inside the lip ring,
    and none within :data:`VENT_BOSS_KEEPOUT_NM` of a standoff or boss.
    """
    inset = d.wall + (d.lip_slack + LIP_WIDTH_NM if d.lip_depth else 0)
    x_lo, x_hi = inset + VENT_BOSS_KEEPOUT_NM, d.outer_x - inset - VENT_BOSS_KEEPOUT_NM
    y_lo, y_hi = inset + VENT_BOSS_KEEPOUT_NM, d.outer_y - inset - VENT_BOSS_KEEPOUT_NM
    length = min(VENT_MAX_BRIDGE_NM, y_hi - y_lo)
    usable = x_hi - x_lo - VENT_SLOT_NM
    if length <= 0 or usable < 0:
        return []
    count = usable // VENT_PITCH_NM + 1
    cx0 = (d.outer_x - (count - 1) * VENT_PITCH_NM) // 2
    cy = d.outer_y // 2
    slots = []
    for i in range(count):
        cx = cx0 + i * VENT_PITCH_NM
        rect = (cx - VENT_SLOT_NM // 2, cy - length // 2,
                cx + VENT_SLOT_NM // 2, cy + length // 2)
        if any(_rect_near(rect, bx, by, VENT_BOSS_KEEPOUT_NM + STANDOFF_OD_NM // 2)
               for bx, by in keepout):
            continue
        slots.append(rect)
    return slots


def _rect_near(rect, px: int, py: int, dist: int) -> bool:
    """True when point (px, py) is within ``dist`` of the rectangle."""
    x0, y0, x1, y1 = rect
    dx = max(x0 - px, 0, px - x1)
    dy = max(y0 - py, 0, py - y1)
    return dx * dx + dy * dy < dist * dist


# --------------------------------------------------------------------- build


def _build_board(k: _K, envelope: BoardEnvelope, d: _Dims):
    """The keep-out solid and its box list, from the envelope alone.

    The substrate slab is drilled at the mounting holes (the board is), so a
    pin or screw passing through one never reads as a board clash; the box
    list keeps the plain slab for the preview."""
    x0 = d.board_x0
    y0 = d.board_y0
    boxes = [(
        "board", "substrate",
        x0, y0, d.board_bottom, x0 + d.board_x, y0 + d.board_y, d.board_top,
    )]
    for part in envelope.parts:
        if part.height_nm <= 0:
            continue
        px0, px1 = _ax(part.x_min_nm, d, envelope), _ax(part.x_max_nm, d, envelope)
        py0, py1 = _ay(part.y_max_nm, d, envelope), _ay(part.y_min_nm, d, envelope)
        if part.side is Layer.TOP:
            pz0, pz1 = d.board_top, d.board_top + part.height_nm
            side = "top"
        else:
            pz0, pz1 = d.board_bottom - part.height_nm, d.board_bottom
            side = "bottom"
        boxes.append((part.ref, side, px0, py0, pz0, px1, py1, pz1))
    slab = k.box(*boxes[0][2:])
    if envelope.mounting_holes:
        # The substrate really is drilled there: a locating pin or a screw
        # through the hole is not a clash with the board.
        slab = k.cut(
            "board keep-out drills", slab,
            [k.cyl_z(_ax(h.x_nm, d, envelope), _ay(h.y_nm, d, envelope),
                     d.board_bottom - EPS_NM, d.board_top + EPS_NM, h.drill_nm)
             for h in envelope.mounting_holes],
        )
    solid = k.fuse("board keep-out", slab, [k.box(*bx[2:]) for bx in boxes[1:]])
    return solid, tuple(boxes)


def _build_base(k: _K, spec, envelope, d: _Dims, cutouts, warnings, receipt):
    """The base: shell, chamfer, standoffs/bosses, openings."""
    outer = k.rbox(0, 0, d.outer_x, d.outer_y, 0, d.base_z, d.corner_radius)
    cavity = k.rbox(
        d.wall, d.wall, d.outer_x - d.wall, d.outer_y - d.wall,
        d.wall, d.base_z + EPS_NM, d.inner_radius,
    )
    base = k.cut("base shell", outer, [cavity])
    # Elephant-foot chamfer on the bed-facing outer edges, before anything
    # else touches z = 0 so the edge set is unambiguous.
    base = k.chamfer_edges(
        "base elephant-foot chamfer", base, k.edges_on_plane(base, "Z", 0),
        ELEPHANT_FOOT_NM,
    )

    adds, cuts, record, mount_receipt, press = _build_standoffs(
        k, spec, envelope, d, warnings
    )
    receipt.update(mount_receipt)
    keepout = [(cx, cy) for _, cx, cy in record]

    if spec.lid == "screw":
        bore_d, bore_depth, _ = _insert_bore(spec, short=False)
        receipt["screw_bore_d"] = round(to_mm(bore_d), 3)
        receipt["screw_bore_depth"] = round(to_mm(bore_depth), 3)
        for _name, cx, cy in _clear_screw_bosses(d, envelope, cutouts, warnings):
            adds.append(k.cyl_z(cx, cy, d.wall - EPS_NM, d.base_z, d.boss_d))
            cuts.append(
                k.cyl_z(cx, cy, d.base_z - bore_depth, d.base_z + EPS_NM, bore_d)
            )
            keepout.append((cx, cy))
    if spec.lid == "snap":
        # Catch ledge the hooks' beads latch under: an inward band on the
        # cavity wall, SNAP_GAP above the seated bead. It thickens the wall
        # rather than grooving it, so the wall is never thinner than spec.
        ridge = SNAP_CATCH_NM
        z_bot = d.base_z - d.snap_arm + SNAP_BEAD_NM + SNAP_GAP_NM
        z_top = z_bot + SNAP_CATCH_HEIGHT_NM
        band = k.cut(
            "snap catch ledge",
            k.rbox(d.wall - EPS_NM, d.wall - EPS_NM, d.outer_x - d.wall + EPS_NM,
                   d.outer_y - d.wall + EPS_NM, z_bot, z_top, d.inner_radius),
            [k.rbox(d.wall + ridge, d.wall + ridge, d.outer_x - d.wall - ridge,
                    d.outer_y - d.wall - ridge, z_bot - EPS_NM, z_top + EPS_NM,
                    max(d.inner_radius - ridge, 0))],
        )
        adds.append(band)
        receipt["snap_catch"] = round(to_mm(ridge), 3)

    if adds:
        base = k.fuse("base standoffs and bosses", base, adds)
    if cuts:
        base = k.cut("base insert bores", base, cuts)

    plugs = []
    for cutout, part, plug in cutouts:
        if cutout.face == "top":
            continue
        opening, prism, xr, yr, zr = _opening(k, d, envelope, cutout, part, plug)
        base = k.cut(f"cutout {cutout.id!r}", base, [opening])
        plugs.append((cutout.id, prism))
        m2 = 2 * cutout.margin_nm
        receipt[f"cutout_{cutout.id}_w"] = round(to_mm(plug.width_nm + m2), 3)
        receipt[f"cutout_{cutout.id}_h"] = round(to_mm(plug.height_nm + m2), 3)
        # Lead-in chamfer on the outside edges of the opening, where it holds.
        axis, at = {"left": ("X", 0), "right": ("X", d.outer_x),
                    "front": ("Y", 0), "back": ("Y", d.outer_y)}[cutout.face]
        edges = k.edges_within(k.edges_on_plane(base, axis, at), xr, yr, zr)
        try:
            base = k.chamfer_edges(
                f"cutout {cutout.id!r} chamfer", base, edges, CUTOUT_CHAMFER_NM
            )
        except KernelError as exc:
            warnings.append(
                f"cutout {cutout.id!r}: lead-in chamfer skipped ({exc.detail})"
            )
    return base, tuple(plugs), tuple(record), keepout, press


def _wall_box(
    k: _K, d: _Dims, face: str, r0: int, r1: int, a0: int, a1: int, z0: int, z1: int
):
    """A box stated relative to one wall: ``r`` is depth in from that face's
    outer surface, ``a`` runs along the wall (Y for left/right, X for
    front/back), ``z`` is assembled Z. The four faces differ only here."""
    if face == "left":
        return k.box(r0, a0, z0, r1, a1, z1)
    if face == "right":
        return k.box(d.outer_x - r1, a0, z0, d.outer_x - r0, a1, z1)
    if face == "front":
        return k.box(a0, r0, z0, a1, r1, z1)
    return k.box(a0, d.outer_y - r1, z0, a1, d.outer_y - r0, z1)


def _snap_hooks(
    d: _Dims, envelope: BoardEnvelope, cutouts, warnings: list[str]
) -> list[tuple[str, int, int]]:
    """Where the cantilever hooks go: ``(face, lo, hi)`` along-wall intervals.

    One hook mid-side, two on a side long enough for three, never over a
    side opening (the ledge is cut away there and the arm would sit in the
    plug's way). A face left with no room is named in a warning, and so is a
    lid holding on by fewer than two hooks.
    """
    w, slot = SNAP_HOOK_WIDTH_NM, SNAP_HOOK_SLOT_NM
    blocked: dict[str, list[tuple[int, int]]] = {f: [] for f in FACES}
    for cutout, part, plug in cutouts:
        if cutout.face != "top":
            blocked[cutout.face].append(
                _along_interval(d, envelope, cutout, part, plug)
            )
    hooks: list[tuple[str, int, int]] = []
    for face in ("left", "right", "front", "back"):
        span = d.outer_y if face in ("left", "right") else d.outer_x
        lo = d.wall + d.inner_radius + slot
        hi = span - d.wall - d.inner_radius - slot
        usable = hi - lo
        count = 2 if usable >= 3 * (w + 2 * slot) else (1 if usable >= w else 0)
        placed = 0
        for i in range(count):
            c = lo + usable * (i + 1) // (count + 1)
            a, b = c - w // 2 - slot, c + w // 2 + slot
            if any(a < o_hi and b > o_lo for o_lo, o_hi in blocked[face]):
                continue
            hooks.append((face, c - w // 2, c + w // 2))
            placed += 1
        if not placed:
            why = (
                "an opening is in the way" if blocked[face] else "the side is too short"
            )
            warnings.append(f"no snap hook on the {face} face: {why}")
    if len(hooks) < 2:
        warnings.append(
            f"snap lid holds by {len(hooks)} hook(s); a lip or screw lid would "
            "retain it better on this board"
        )
    return hooks


def _press_bosses(
    d: _Dims, envelope: BoardEnvelope, press, warnings: list[str]
) -> list[tuple[str, int, int, int]]:
    """Which standoffs get a boss hanging from the lid to press the board
    onto them: every one whose column over the board is free of top-side
    parts (plus a sliding fit). A boss with a part under it is skipped by
    name; a board left with none is said to be unretained."""
    out = []
    for name, cx, cy, dia in press:
        r = dia // 2 + FIT_SLIDE_NM
        clash = None
        for part in envelope.parts:
            if part.side is not Layer.TOP or part.height_nm <= 0:
                continue
            px0, px1 = _ax(part.x_min_nm, d, envelope), _ax(part.x_max_nm, d, envelope)
            py0, py1 = _ay(part.y_max_nm, d, envelope), _ay(part.y_min_nm, d, envelope)
            if _rect_near((px0, py0, px1, py1), cx, cy, r):
                clash = part.ref
                break
        if clash is not None:
            warnings.append(
                f"no lid press boss over standoff {name}: {clash} is under it"
            )
            continue
        out.append((name, cx, cy, dia))
    if press and not out:
        warnings.append(
            "board is not retained: no standoff is free of parts for a lid "
            "press boss; add mounting holes or move parts off the corners"
        )
    return out


def _build_lid(
    k: _K, spec, envelope, d: _Dims, cutouts, keepout, press, warnings, receipt
):
    """The lid, built in the assembled frame; returns (assembled lid, top-z)."""
    z0, z1 = d.base_z, d.base_z + d.lid_z
    lid = k.rbox(0, 0, d.outer_x, d.outer_y, z0, z1, d.corner_radius)
    # Outer face is the bed: elephant-foot chamfer on its perimeter.
    lid = k.chamfer_edges(
        "lid elephant-foot chamfer", lid, k.edges_on_plane(lid, "Z", z1),
        ELEPHANT_FOOT_NM,
    )
    adds: list = []
    cuts: list = []
    if d.lip_depth:
        # A ring lip: cavity minus slack outside, LIP_WIDTH wide. A snap ring
        # sits a catch further in so it clears the ledge; only its hooks reach.
        inset = d.wall + d.lip_slack
        if spec.lid == "snap":
            inset += SNAP_CATCH_NM
        ring_r = max(d.inner_radius - (inset - d.wall), 0)
        ring = k.cut(
            "lip ring",
            k.rbox(inset, inset, d.outer_x - inset, d.outer_y - inset,
                   z0 - d.lip_depth, z0 + EPS_NM, ring_r),
            [k.rbox(inset + LIP_WIDTH_NM, inset + LIP_WIDTH_NM,
                    d.outer_x - inset - LIP_WIDTH_NM, d.outer_y - inset - LIP_WIDTH_NM,
                    z0 - d.lip_depth - EPS_NM, z0 + 2 * EPS_NM,
                    max(ring_r - LIP_WIDTH_NM, 0))],
        )
        adds.append(ring)
        receipt["lip_width"] = round(to_mm(LIP_WIDTH_NM), 3)
        if spec.lid == "snap":
            # Cantilever hooks: a ring segment freed by a slot each side,
            # hanging snap_arm deep, with the bead at its tip reaching out
            # to the wall less the slack so it latches under the ledge.
            bead_out = d.wall + d.lip_slack
            zb0 = z0 - d.snap_arm
            hooks = _snap_hooks(d, envelope, cutouts, warnings)
            slot = SNAP_HOOK_SLOT_NM
            ring_top, ring_bot = z0 + EPS_NM, z0 - d.lip_depth - EPS_NM
            for face, lo, hi in hooks:
                adds.append(_wall_box(
                    k, d, face, inset, inset + LIP_WIDTH_NM, lo, hi, zb0, ring_top
                ))
                adds.append(_wall_box(
                    k, d, face, bead_out, inset + EPS_NM, lo, hi,
                    zb0, zb0 + SNAP_BEAD_NM,
                ))
                for a0, a1 in ((lo - slot, lo), (hi, hi + slot)):
                    cuts.append(_wall_box(
                        k, d, face, inset - EPS_NM, inset + LIP_WIDTH_NM + EPS_NM,
                        a0, a1, ring_bot, ring_top,
                    ))
            arm_len = d.snap_arm - SNAP_BEAD_NM
            receipt["snap_bead"] = round(to_mm(SNAP_BEAD_NM), 3)
            receipt["snap_hooks"] = float(len(hooks))
            receipt["snap_hook_w"] = round(to_mm(SNAP_HOOK_WIDTH_NM), 3)
            receipt["snap_arm_len"] = round(to_mm(arm_len), 3)
            receipt["snap_arm_t"] = round(to_mm(LIP_WIDTH_NM), 3)
            receipt["snap_deflection"] = round(to_mm(d.snap_deflection), 3)
            receipt["snap_strain_ppm"] = float(
                snap_strain_ppm(LIP_WIDTH_NM, d.snap_deflection, arm_len)
            )
    if press:
        # Press bosses: columns from the plate to a sliding fit above the
        # board, over every standoff nothing else holds the board onto.
        bosses = _press_bosses(d, envelope, press, warnings)
        zb = d.board_top + FIT_SLIDE_NM
        for _name, cx, cy, dia in bosses:
            adds.append(k.cyl_z(cx, cy, zb, z0 - d.lip_depth + EPS_NM, dia))
            adds.append(k.cyl_z(cx, cy, z0 - d.lip_depth - EPS_NM, z0 + EPS_NM, dia))
        receipt["press_bosses"] = float(len(bosses))
        receipt["press_boss_bottom"] = round(to_mm(zb), 3)
    if spec.lid == "screw":
        ins = INSERTS["M3" if spec.insert == "self_tap" else spec.insert]
        clear_d = ins.clearance_nm + HOLE_COMPENSATION_NM
        pocket_depth = min(SCREW_HEAD_DEPTH_NM, d.lid_z - MIN_PLATE_UNDER_POCKET_NM)
        if pocket_depth < SCREW_HEAD_DEPTH_NM:
            warnings.append(
                f"screw head pockets shallowed to {to_mm(max(pocket_depth, 0)):.2f} mm "
                f"in a {to_mm(d.lid_z):.2f} mm lid; a thicker wall restores "
                f"{to_mm(SCREW_HEAD_DEPTH_NM):.2f} mm"
            )
        for _, cx, cy in _clear_screw_bosses(d, envelope, cutouts, None):
            cuts.append(k.cyl_z(cx, cy, z0 - EPS_NM, z1 + EPS_NM, clear_d))
            if pocket_depth > 0:
                cuts.append(k.cyl_z(
                    cx, cy, z1 - pocket_depth, z1 + d.label_emboss + EPS_NM,
                    SCREW_HEAD_POCKET_NM,
                ))
        receipt["screw_clear_d"] = round(to_mm(clear_d), 3)
        receipt["screw_pocket_d"] = round(to_mm(SCREW_HEAD_POCKET_NM), 3)
        receipt["screw_pocket_depth"] = round(to_mm(max(pocket_depth, 0)), 3)

    if spec.label:
        try:
            b = k.b
            # Cut from just above the outer face down LABEL_DEPTH_NM into the
            # plate, leaving at least MIN_WALL_NM of plate under the letters.
            depth = min(LABEL_DEPTH_NM, max(d.lid_z - MIN_WALL_NM, 0))
            if depth <= 0:
                raise ValueError("lid too thin to engrave")
            sketch = b.Pos(
                _mm(d.outer_x // 2), _mm(d.outer_y // 2), _mm(z1 - depth)
            ) * b.Text(spec.label, _mm(LABEL_TEXT_SIZE_NM))
            text = b.extrude(sketch, _mm(depth + EPS_NM))
            if text is None or text.volume <= 0:
                raise ValueError("text extruded to nothing")
            cuts.append(text)
            receipt["label_size"] = round(to_mm(LABEL_TEXT_SIZE_NM), 3)
            receipt["label_depth"] = round(to_mm(depth), 3)
        except Exception as exc:  # a label is decoration, never a failure
            warnings.append(f"label {spec.label!r} skipped: TEXT_FAILED: {exc}")

    if adds:
        lid = k.fuse("lid ring and label", lid, adds)

    for cutout, part, _ in cutouts:
        if cutout.face != "top":
            continue
        window, xr, yr = _top_opening(k, d, envelope, cutout, part)
        cuts.append(window)
        receipt[f"cutout_{cutout.id}_w"] = round(to_mm(xr[1] - xr[0]), 3)
        receipt[f"cutout_{cutout.id}_h"] = round(to_mm(yr[1] - yr[0]), 3)
    if spec.vents:
        slots = _vent_slots(d, keepout)
        for x0, y0, x1, y1 in slots:
            cuts.append(k.box(x0, y0, z0 - d.lip_depth - EPS_NM, x1, y1,
                              z1 + d.label_emboss + EPS_NM))
        receipt["vent_count"] = float(len(slots))
        receipt["vent_slot"] = round(to_mm(VENT_SLOT_NM), 3)
        if not slots:
            warnings.append("vents requested but no slot fits clear of the bosses")
    if cuts:
        lid = k.cut("lid openings", lid, cuts)
    return lid, z1


def build_enclosure(spec: EnclosureSpec, envelope: BoardEnvelope) -> EnclosureModel:
    """Build base, lid and keep-out solids for ``spec`` around ``envelope``.

    Raises :class:`~.errors.KernelUnavailable` without build123d,
    :class:`~.errors.KernelError` (with a ``failure_class``) when OCCT cannot
    make the geometry, and :class:`~.errors.CutoutError` for a cutout naming
    an absent ref or a face the part is nowhere near (the ``edge_refs``
    convention).

    Mount fallbacks are warnings, not errors: ``holes``/``pins`` on a board
    with no mounting holes becomes ``corners`` and says so in
    ``model.warnings``. Lid ``"none"`` yields ``lid is None``.
    """
    b = require_kernel()
    k = _K(b)
    warnings: list[str] = []
    d, cutouts = _dims(spec, envelope, warnings)
    receipt = _params(d)

    board, boxes = _build_board(k, envelope, d)
    base, plugs, record, keepout, press = _build_base(
        k, spec, envelope, d, cutouts, warnings, receipt
    )
    _check_solid(base, "base")

    lid = None
    lid_assembled = None
    top_z = d.base_z
    if spec.lid != "none":
        lid_asm, top_z = _build_lid(
            k, spec, envelope, d, cutouts, keepout, press, warnings, receipt
        )
        _check_solid(lid_asm, "lid")
        lid_assembled = b.Location((0, _mm(d.outer_y), _mm(top_z)), (180, 0, 0))
        lid = _op(
            "INVALID_SHAPE", "lid to printed orientation",
            lambda: lid_asm.moved(lid_assembled.inverse()),
        )
        if "label_size" in receipt:
            top_z += d.label_emboss
        else:
            receipt["label_emboss"] = 0.0
    else:
        if any(c.face == "top" for c in spec.cutouts):
            warnings.append("top-face cutouts ignored: lid style is 'none'")
        if press:
            warnings.append(
                f"board is not retained: mount {_mount(spec)!r} relies on the "
                "lid pressing the board down and lid style is 'none'"
            )
    if spec.vents and spec.lid == "none":
        warnings.append("vents requested but lid style is 'none': vents are lid slots")

    return EnclosureModel(
        base=base,
        lid=lid,
        board=board,
        lid_assembled=lid_assembled,
        plugs=plugs,
        standoffs=record,
        outer_nm=(d.outer_x, d.outer_y, top_z),
        params_mm=receipt,
        warnings=tuple(warnings),
        board_boxes=boxes,
    )


# -------------------------------------------------------------------- export


def export_model(
    model: EnclosureModel, directory: str | Path, stem: str = "enclosure"
) -> ExportPaths:
    """Write ``<stem>.step`` (assembly labelled ``base``/``lid``),
    ``<stem>-base.stl`` and ``<stem>-lid.stl`` into ``directory``.

    The STEP is the primary artifact: ISO 10303-21 is ASCII and
    self-contained, so it carries the whole assembly with no companion file.

    The STLs are in printed orientation; the STEP carries the lid placed via
    ``lid_assembled``. With no lid, the STEP has one child and the lid STL is
    not written (``ExportPaths.lid_stl`` still names where it would be)."""
    b = require_kernel()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = ExportPaths(
        step=directory / f"{stem}.step",
        base_stl=directory / f"{stem}-base.stl",
        lid_stl=directory / f"{stem}-lid.stl",
    )
    children = [b.Part(model.base.wrapped, label="base")]
    if model.lid is not None:
        placed = model.lid.moved(model.lid_assembled)
        children.append(b.Part(placed.wrapped, label="lid"))
    assembly = b.Compound(label=stem, children=children)
    _op("INVALID_SHAPE", "STEP export",
        lambda: b.export_step(assembly, paths.step) or None)
    _op("INVALID_SHAPE", "base STL export",
        lambda: b.export_stl(model.base, paths.base_stl) or None)
    if model.lid is not None:
        _op("INVALID_SHAPE", "lid STL export",
            lambda: b.export_stl(model.lid, paths.lid_stl) or None)
    return paths

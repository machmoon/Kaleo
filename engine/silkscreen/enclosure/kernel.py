"""Kernel-level verification of a built :class:`~.cad.EnclosureModel`.

This is the acceptance gate (docs/ai-cad-plan.md v2, decision 16): every
clause is measured on the B-rep by OCCT and reported with a **signed margin**
in nanometres -- negative means violated, by that much. The list of clauses
is the contract; a clause that cannot be evaluated fails with a reason rather
than passing vacuously (the ``spice/assertions.py`` rule).

Clause names (stable, they are the vocabulary of the repair prompt):

``valid_topology``      BRepCheck on base and lid
``positive_volume``     every solid's own signed volume > 0 (never aggregated)
``solid_count``         base and lid are each exactly one solid
``bbox``                outer size equals ``model.outer_nm`` within 1 um
``board_clash``         volume(base ∩ board keep-out) == 0, assembled
``headroom``            lid underside over each top part minus that part ≥ HEADROOM
``underside``           tallest bottom part fits the standoff gap
``standoff_concentric`` each boss axis within 50 um of its mounting hole
``cutout_admits_plug``  volume(plug − case) == volume(plug) for every cutout
``lid_mates``           volume(base ∩ lid assembled) == 0, the seam closes on the
                        rim, and the lip gap is in [0, 2·slack]
``min_wall``            sampled wall thickness ≥ spec.wall_nm − 50 um
``overhang``            no down-facing face steeper than rules.OVERHANG_LIMIT_DEG
                        off the bed, in each part's printed orientation
``vent_keepout``        no vent slot within rules.VENT_BOSS_KEEPOUT_NM of a boss

Measurement methods, one line each (the detail string of every clause repeats
the numbers it used):

* ``valid_topology``: ``BRepCheck_Analyzer`` on each part.
* ``positive_volume``: ``GProp`` signed volume of every solid, never summed.
* ``solid_count``: ``solids()`` on each part.
* ``bbox``: union of the base and assembled-lid bounding boxes vs
  ``outer_nm`` and the origin (the frames contract puts it at 0, 0, 0).
* ``board_clash``: kernel boolean ``base ∩ board`` (and ``lid ∩ board``);
  the pass margin is ``BRepExtrema`` distance between the bodies.
* ``headroom``: for each top-side part, the lowest point of the lid material
  inside a prism over that part's own footprint, minus the part's top -- so a
  press boss over a bare corner is not a lid pressing on a part, and a lid
  pressing on a part is.
* ``underside``: downward rays from the slab bottom at five points; the
  largest floor distance is the gap (a ray landing on a boss is not the floor).
* ``standoff_concentric``: hole centres mapped into the model frame through
  the keep-out's bounding box, vs ``model.standoffs``.
* ``cutout_admits_plug``: the plug prism is built **here**, from the spec's
  cutouts, ``rules.plug_envelope`` and the envelope's part rectangles --
  centred on the receptacle, from outside the case to the board's edge --
  never taken from ``model.plugs``, which the emitter drew with the same
  numbers as the opening and so cannot disagree with it (the
  ``test_kicad.py`` lesson). ``volume(prism − base − lid)`` vs
  ``volume(prism)``; pass margin is the prism's distance to the case.
* ``lid_mates``: kernel boolean ``base ∩ lid``; a ray up from the rim at the
  middle of each wall must meet the lid within 10 um (the seam closes); then
  the lid part below the rim is sliced into horizontal bands, the widest
  band's XY extent is the lip (or hook bead) face, and four rays from its
  centre find the cavity walls. The slack is the receipt's ``lip_slack``, and
  a snap lid's ring may sit a further ``SNAP_CATCH_NM`` in, since only its
  hooks reach the wall.
* ``min_wall``: on every outer face (a face is outer when a ray along its
  normal never meets the part again) five parametric sample points cast a ray
  into the material; the exit distance is the thickness. Floors under a
  standoff bore, pocket ceilings, and walls around a bore (the ray exits
  into a void narrower than a boss and re-enters) are held to the printable
  minimum (``STANDOFF_MIN_FLOOR_NM`` / ``MIN_WALL_NM``), not the spec wall.
* ``overhang``: per face in the printed orientation -- planar faces by their
  normal, curved faces triangle by triangle -- the angle from vertical of
  every down-facing face above the part's lowest point; flat faces that are
  short, span an enclosed opening, or are narrower than a nozzle are bridges,
  not overhangs (long bridges become warnings).
* ``vent_keepout``: slot-shaped inner wires of planar faces (short side ≤ 1.5
  × ``VENT_SLOT_NM``, long side ≥ 2 × short) vs standoff centres, in XY.

Margin conventions
------------------
Every ``Clause.margin_nm`` is a signed integer; ``KernelReport.text`` prints it
divided by 1e6. Where a clause measures a length the margin *is* that length in
nm. Three kinds of clause cannot be lengths and use a documented stand-in:

* **volume clauses** (``board_clash``, ``cutout_admits_plug``, the intersection
  half of ``lid_mates``, ``positive_volume``): a volume ``V`` is reported as the
  side of the cube with that volume, ``cbrt(V)``, so a 1 mm³ clash reads as
  −1.000 mm. When the volume is zero the clause's positive margin is the
  measured clearance between the two bodies, which *is* a length.
* **angle clause** (``overhang``): the margin is degrees × 1e6, so
  ``text()`` prints it as a number of degrees, and the detail string says so.
* **boolean/count clauses** (``valid_topology``, ``solid_count``): pass is 0,
  fail is −1e6 per defect (one "millimetre" per invalid shape or extra solid).

A clause whose evaluation *raises* inside the kernel fails with
:data:`UNEVALUATED_NM` as its margin and the exception in its detail. A
clause with genuinely nothing to measure (no cutouts, no lid, no mounting-hole
standoffs) passes with margin 0 and a detail beginning ``nothing to check``.
Both are stated in words; neither is a silent zero.

The kernel works in mm floats; the single crossing back to integer nanometres
is :func:`_nm`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import rules
from .board_shape import BoardEnvelope, Layer, MountingHole
from .cad import EnclosureModel, require_kernel
from .errors import KernelFitError
from .ir import MIN_WALL_NM, EnclosureSpec

__all__ = ["CLAUSES", "Clause", "KernelReport", "UNEVALUATED_NM", "verify_model"]

CLAUSES: tuple[str, ...] = (
    "valid_topology",
    "positive_volume",
    "solid_count",
    "bbox",
    "board_clash",
    "headroom",
    "underside",
    "standoff_concentric",
    "cutout_admits_plug",
    "lid_mates",
    "min_wall",
    "overhang",
    "vent_keepout",
)

#: Margin of a clause the kernel could not evaluate (an exception, not a
#: measurement). Deliberately not zero and deliberately negative.
UNEVALUATED_NM: int = -1_000_000

#: Boolean clauses: one "millimetre" per defect.
_DEFECT_NM: int = 1_000_000

#: Volumes below this (mm³ -- a 10 um cube) are zero: OCCT booleans leave
#: slivers of this order on coincident faces.
_VOLUME_TOL_MM3: float = 1e-6
#: The ``bbox`` clause's tolerance, 1 um in mm.
_BBOX_TOL_MM: float = 1e-3
#: ``standoff_concentric`` tolerance, 50 um in mm.
_CONCENTRIC_TOL_MM: float = 0.05
#: ``min_wall`` tolerance under the required thickness, 50 um in mm.
_WALL_TOL_MM: float = 0.05
#: A face whose highest point is within this of the part's lowest point is
#: on the print bed.
_BED_Z_MM: float = 1e-3
#: A down-facing planar face within this many degrees of horizontal is flat
#: (a bridge or a step), not a sloped overhang.
_BRIDGE_DEG: float = 89.0
#: A flat step narrower than one nozzle width is not an overhang at all.
_STEP_MM: float = 0.5
#: ``overhang`` tolerance in degrees (tessellation noise on curved faces).
_ANGLE_TOL_DEG: float = 0.5
#: Tessellation used for curved faces in the overhang clause.
_TESS_TOL_MM: float = 0.2
_TESS_ANG: float = 0.3
#: Sample parameters on each face for ``min_wall``.
_WALL_SAMPLES: tuple[tuple[float, float], ...] = (
    (0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75),
)
#: ``min_wall`` samples at most this many faces per part (largest first).
_MAX_WALL_FACES: int = 400
#: Horizontal bands the lip is sliced into to find its widest face.
_LIP_BANDS: int = 6
#: Rays must travel at least this far before a hit counts (mm).
_RAY_EPS_MM: float = 1e-4
#: Lid styles that register a ring in the cavity.
_LIP_STYLES: frozenset[str] = frozenset({"lip", "snap", "friction"})
#: The rim-to-lid seam may be open by at most this (mm) and still count as
#: closed: a tenth of a layer, well inside what a print can resolve.
_SEAM_TOL_MM: float = 0.01
#: Clauses whose margin is a length in nm. Only these can be "thin": a
#: boolean clause has no margin to be nominal by, ``bbox``'s margin is a
#: tolerance remainder, ``min_wall``'s the 50 um tolerance, ``overhang``'s
#: degrees, and ``vent_keepout``'s a spacing rule rather than a fit.
_LENGTH_CLAUSES: frozenset[str] = frozenset(
    {"board_clash", "headroom", "underside", "cutout_admits_plug", "lid_mates"}
)
#: Detail prefix of a clause that had nothing to measure.
_NOTHING = "nothing to check"


@dataclass(frozen=True)
class Clause:
    name: str
    passed: bool
    margin_nm: int      # signed; the receipt
    detail: str         # what was measured, in words, with mm numbers


@dataclass(frozen=True)
class KernelReport:
    clauses: tuple[Clause, ...]
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.clauses)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.clauses if not c.passed]

    @property
    def margins_nm(self) -> dict[str, int]:
        return {c.name: c.margin_nm for c in self.clauses}

    def thin(self, band_nm: int = rules.THIN_MARGIN_NM) -> list[str]:
        """Names of clauses that passed, but by less than ``band_nm``.

        A pass this close to zero is a warning, not a result: the margin is
        inside the printer's own process tolerance, so the next revision
        moves it across. Clause order, and never a failing clause -- those
        are already in :attr:`failed`. Only the clauses whose margin *is* a
        length (:data:`_LENGTH_CLAUSES`) take part, and one that had nothing
        to measure is not nominal, it is absent.
        """
        return [
            c.name for c in self.clauses
            if c.passed and c.name in _LENGTH_CLAUSES and c.margin_nm < band_nm
            and not c.detail.startswith(_NOTHING)
        ]

    def raise_for_failures(self) -> None:
        """Raise :class:`KernelFitError` naming every failing clause."""
        if self.passed:
            return
        lines = [f"{c.name}: {c.detail}" for c in self.clauses if not c.passed]
        raise KernelFitError(
            f"{len(lines)} kernel clause(s) failed:\n  - " + "\n  - ".join(lines),
            failed=self.failed,
            margins_nm=self.margins_nm,
        )

    def text(self) -> str:
        """Compact report for a repair prompt: one line per clause."""
        out = []
        for c in self.clauses:
            mark = "PASS" if c.passed else "FAIL"
            out.append(
                f"{mark} {c.name} margin={c.margin_nm / 1e6:+.3f} mm: {c.detail}"
            )
        out.extend(f"WARN {w}" for w in self.warnings)
        return "\n".join(out)


# --- unit helpers ---------------------------------------------------------------

def _nm(value_mm: float) -> int:
    """The one mm -> nm crossing in this module."""
    return int(round(value_mm * 1e6))


def _mm(value_nm: int) -> float:
    return value_nm / 1e6


def _cbrt(volume_mm3: float) -> float:
    return math.copysign(abs(volume_mm3) ** (1.0 / 3.0), volume_mm3)


def _fmt(value_mm: float) -> str:
    return f"{value_mm:.3f} mm"


def _fmt_pt(v: Any) -> str:
    return f"({v.X:.2f}, {v.Y:.2f}, {v.Z:.2f})"


# --- geometry helpers (all mm) --------------------------------------------------

def _solids(shape: Any) -> list[Any]:
    return list(shape.solids())


def _volume(shape: Any) -> float:
    """Sum of the solid volumes in ``shape`` (a Part, Solid, or Compound)."""
    return float(sum(s.volume for s in _solids(shape)))


def _intersection_volume(a: Any, b: Any) -> float:
    """Volume of ``a ∩ b``; the boolean is done by the kernel, here."""
    result = a.intersect(b)
    if result is None:
        return 0.0
    total = 0.0
    for item in result:
        for solid in item.solids():
            total += float(solid.volume)
    return total


def _bbox(shape: Any) -> Any:
    return shape.bounding_box(optimal=True)


def _ray_hits(shape: Any, origin: Any, direction: Any) -> list[tuple[float, Any, Any]]:
    """Forward hits of the ray ``origin + t*direction`` (``t > eps``) on
    ``shape``, sorted by ``t``: ``(t, point, normal)``. OCCT intersects the
    whole line, so the filter by ``t`` is what makes this a ray."""
    from build123d import Axis

    d = direction.normalized()
    out = []
    for point, normal in shape.find_intersection_points(Axis(origin, d)):
        t = (point - origin).dot(d)
        if t > _RAY_EPS_MM:
            out.append((t, point, normal))
    out.sort(key=lambda h: h[0])
    return out


def _moved_point(loc: Any, x: float, y: float, z: float) -> tuple[float, float, float]:
    """``(x, y, z)`` carried through a build123d ``Location``. Done on the
    ``gp_Trsf`` directly: ``Vertex.moved`` only re-labels the vertex's
    location and ``tuple(vertex)`` reads the untransformed point back."""
    from OCP.gp import gp_Pnt

    pt = gp_Pnt(x, y, z).Transformed(loc.wrapped.Transformation())
    return pt.X(), pt.Y(), pt.Z()


def _split_z(shape: Any, z: float, *, below: bool) -> Any | None:
    """The part of ``shape`` below (or above) the horizontal plane at ``z``;
    None when nothing is."""
    from build123d import Keep, Plane, split

    plane = Plane(origin=(0, 0, z), x_dir=(1, 0, 0), z_dir=(0, 0, 1))
    try:
        part = split(shape, bisect_by=plane, keep=Keep.BOTTOM if below else Keep.TOP)
    except Exception:
        return None
    if part is None or not _solids(part):
        return None
    return part


# --- evaluation context ---------------------------------------------------------

@dataclass
class _Context:
    model: EnclosureModel
    spec: EnclosureSpec
    envelope: BoardEnvelope
    warnings: list[str] = field(default_factory=list)
    _cache: dict[str, Any] = field(default_factory=dict)

    @property
    def base(self) -> Any:
        return self.model.base

    @property
    def lid(self) -> Any:
        return self.model.lid

    @property
    def board(self) -> Any:
        return self.model.board

    @property
    def lid_assembled(self) -> Any | None:
        """The lid moved onto the base (None without a lid)."""
        if "lid_asm" not in self._cache:
            lid = self.model.lid
            self._cache["lid_asm"] = (
                None if lid is None else lid.moved(self.model.lid_assembled)
            )
        return self._cache["lid_asm"]

    @property
    def base_bb(self) -> Any:
        if "base_bb" not in self._cache:
            self._cache["base_bb"] = _bbox(self.base)
        return self._cache["base_bb"]

    @property
    def board_bb(self) -> Any:
        if "board_bb" not in self._cache:
            self._cache["board_bb"] = _bbox(self.board)
        return self._cache["board_bb"]

    def kicad_to_model_mm(self, x_nm: int, y_nm: int) -> tuple[float, float]:
        """Map a KiCad (Y-down) board coordinate into the model frame.

        The keep-out solid is the board in the model frame, so its XY bounding
        box locates the outline: X keeps its direction, Y flips (front = KiCad
        max-Y). Courtyards overhanging the outline would shift the box, which
        is checked once and surfaced as a warning.
        """
        env = self.envelope
        bb = self.board_bb
        if "origin_checked" not in self._cache:
            self._cache["origin_checked"] = True
            want_x = _mm(env.x_max_nm - env.x_min_nm)
            want_y = _mm(env.y_max_nm - env.y_min_nm)
            if max(abs(bb.size.X - want_x), abs(bb.size.Y - want_y)) > 0.01:
                self.warnings.append(
                    "board keep-out XY extent "
                    f"{bb.size.X:.3f} x {bb.size.Y:.3f} mm differs from the outline "
                    f"{want_x:.3f} x {want_y:.3f} mm (courtyards overhang?); hole "
                    "positions are mapped from the keep-out bounding box"
                )
        x = bb.min.X + _mm(x_nm - env.x_min_nm)
        y = bb.min.Y + _mm(env.y_max_nm - y_nm)
        return x, y

    @property
    def slab_top_mm(self) -> float:
        """The board's top surface: the keep-out's lowest point is the tallest
        bottom-side part, and the substrate sits on top of that."""
        return (
            self.board_bb.min.Z
            + _mm(self.envelope.max_height_bottom_nm)
            + _mm(self.envelope.thickness_nm)
        )

    def part_rect_mm(self, part: Any) -> tuple[float, float, float, float]:
        """A part's XY rectangle in the model frame (x0, y0, x1, y1)."""
        x0, y1 = self.kicad_to_model_mm(part.x_min_nm, part.y_min_nm)
        x1, y0 = self.kicad_to_model_mm(part.x_max_nm, part.y_max_nm)
        return x0, y0, x1, y1

    def boss_radius_mm(self) -> float:
        """Half the boss diameter the receipt states, never under the rule's."""
        params = self.model.params_mm or {}
        stated = float(params.get("boss_d", 0.0))
        return max(_mm(rules.STANDOFF_OD_NM), stated) / 2


def _parts(ctx: _Context) -> list[tuple[str, Any]]:
    out = [("base", ctx.base)]
    if ctx.lid is not None:
        out.append(("lid", ctx.lid))
    return out


# --- clauses --------------------------------------------------------------------

def _valid_topology(ctx: _Context) -> Clause:
    from OCP.BRepCheck import BRepCheck_Analyzer

    bad = []
    words = []
    for name, shape in _parts(ctx):
        ok = bool(shape.is_valid) and BRepCheck_Analyzer(shape.wrapped).IsValid()
        words.append(f"{name} {'valid' if ok else 'INVALID'}")
        if not ok:
            bad.append(name)
    detail = "BRepCheck_Analyzer: " + ", ".join(words)
    if ctx.lid is None:
        detail += "; no lid"
    return Clause("valid_topology", not bad, -_DEFECT_NM * len(bad), detail)


def _positive_volume(ctx: _Context) -> Clause:
    worst: float | None = None
    words = []
    for name, shape in _parts(ctx):
        solids = _solids(shape)
        if not solids:
            words.append(f"{name}: no solids")
            worst = 0.0 if worst is None else min(worst, 0.0)
            continue
        for i, solid in enumerate(solids):
            v = float(solid.volume)
            words.append(f"{name}[{i}] {v:.3f} mm³")
            worst = v if worst is None else min(worst, v)
    if worst is None:
        return Clause(
            "positive_volume", False, UNEVALUATED_NM, "no solids in any part"
        )
    return Clause(
        "positive_volume", worst > _VOLUME_TOL_MM3, _nm(_cbrt(worst)),
        "signed volume per solid: " + ", ".join(words)
        + " (margin = cbrt of the smallest)",
    )


def _solid_count(ctx: _Context) -> Clause:
    nb = len(_solids(ctx.base))
    defects = abs(nb - 1)
    words = [f"base has {nb} solid(s)"]
    if ctx.lid is not None:
        nl = len(_solids(ctx.lid))
        defects += abs(nl - 1)
        words.append(f"lid has {nl} solid(s)")
    else:
        words.append("no lid")
    return Clause("solid_count", defects == 0, -_DEFECT_NM * defects, "; ".join(words))


def _bbox_clause(ctx: _Context) -> Clause:
    bb = ctx.base_bb
    lo = [bb.min.X, bb.min.Y, bb.min.Z]
    hi = [bb.max.X, bb.max.Y, bb.max.Z]
    if ctx.lid_assembled is not None:
        lb = _bbox(ctx.lid_assembled)
        lmin, lmax = (lb.min.X, lb.min.Y, lb.min.Z), (lb.max.X, lb.max.Y, lb.max.Z)
        lo = [min(a, b) for a, b in zip(lo, lmin, strict=True)]
        hi = [max(a, b) for a, b in zip(hi, lmax, strict=True)]
    size = [h - low for h, low in zip(hi, lo, strict=True)]
    want = [_mm(v) for v in ctx.model.outer_nm]
    size_dev = max(abs(s - w) for s, w in zip(size, want, strict=True))
    origin_dev = max(abs(v) for v in lo)
    dev = max(size_dev, origin_dev)
    detail = (
        f"assembled bbox {size[0]:.3f} x {size[1]:.3f} x {size[2]:.3f} mm vs outer_nm "
        f"{want[0]:.3f} x {want[1]:.3f} x {want[2]:.3f} mm (size off by "
        f"{size_dev:.4f} mm); origin ({lo[0]:.4f}, {lo[1]:.4f}, {lo[2]:.4f}) off by "
        f"{origin_dev:.4f} mm; tolerance {_BBOX_TOL_MM:.3f} mm"
    )
    return Clause("bbox", dev <= _BBOX_TOL_MM, _nm(_BBOX_TOL_MM - dev), detail)


def _board_clash(ctx: _Context) -> Clause:
    v_base = _intersection_volume(ctx.base, ctx.board)
    v_lid = 0.0
    if ctx.lid_assembled is not None:
        v_lid = _intersection_volume(ctx.lid_assembled, ctx.board)
    total = v_base + v_lid
    if total > _VOLUME_TOL_MM3:
        side = _cbrt(total)
        return Clause(
            "board_clash", False, -_nm(side),
            f"keep-out overlaps the case: base ∩ board {v_base:.4f} mm³, "
            f"lid ∩ board {v_lid:.4f} mm³ (margin = -cbrt of the total, {side:.3f} mm)",
        )
    clearance = float(ctx.base.distance(ctx.board))
    words = f"base ∩ board = 0; base-to-board clearance {_fmt(clearance)}"
    if ctx.lid_assembled is not None:
        lid_clear = float(ctx.lid_assembled.distance(ctx.board))
        words += f"; lid ∩ board = 0; lid-to-board clearance {_fmt(lid_clear)}"
        clearance = min(clearance, lid_clear)
    return Clause("board_clash", True, _nm(clearance), words)


def _headroom(ctx: _Context) -> Clause:
    from build123d import Align, Box, Location

    if ctx.lid_assembled is None:
        return Clause("headroom", True, 0, f"{_NOTHING}: no lid (open top)")
    want = _mm(rules.HEADROOM_NM)
    lid = ctx.lid_assembled
    lid_bb = _bbox(lid)
    slab_top = ctx.slab_top_mm
    # One probe per top-side part, over that part's own footprint: the lid
    # material inside it is what can press on the part. A board with no top
    # parts is probed over its whole footprint at the slab top.
    probes: list[tuple[str, float, float, float, float, float]] = []
    for part in ctx.envelope.parts:
        if part.side is not Layer.TOP or part.height_nm <= 0:
            continue
        x0, y0, x1, y1 = ctx.part_rect_mm(part)
        probes.append((part.ref, x0, y0, x1, y1, slab_top + _mm(part.height_nm)))
    if not probes:
        bb = ctx.board_bb
        probes.append(
            ("the bare board", bb.min.X, bb.min.Y, bb.max.X, bb.max.Y, slab_top)
        )
    worst: tuple[float, str, float, float] | None = None
    for ref, x0, y0, x1, y1, z_top in probes:
        height = max(lid_bb.max.Z - z_top, 0.0) + 1.0
        probe = Box(
            x1 - x0, y1 - y0, height, align=(Align.MIN, Align.MIN, Align.MIN)
        ).moved(Location((x0, y0, z_top - 0.5)))
        above = lid.intersect(probe)
        hit = [item for item in (above or []) if item.solids()]
        underside = min(_bbox(item).min.Z for item in hit) if hit else lid_bb.max.Z
        gap = underside - z_top
        if worst is None or gap < worst[0]:
            worst = (gap, ref, underside, z_top)
    gap, ref, underside, z_top = worst
    detail = (
        f"lid underside over {ref} at {underside:.3f} mm, its top at {z_top:.3f} mm: "
        f"{_fmt(gap)} headroom vs {_fmt(want)} required (worst of {len(probes)} "
        "top-side part(s), each probed over its own footprint)"
    )
    return Clause("headroom", gap >= want - 1e-6, _nm(gap - want), detail)


def _underside(ctx: _Context) -> Clause:
    from build123d import Vector

    bb = ctx.board_bb
    need = _mm(ctx.envelope.max_height_bottom_nm)
    slab_bottom = bb.min.Z + need
    cx, cy = (bb.min.X + bb.max.X) / 2, (bb.min.Y + bb.max.Y) / 2
    qx, qy = bb.size.X / 4, bb.size.Y / 4
    samples = [
        (cx, cy), (cx - qx, cy - qy), (cx + qx, cy - qy),
        (cx - qx, cy + qy), (cx + qx, cy + qy),
    ]
    gaps = []
    for x, y in samples:
        hits = _ray_hits(ctx.base, Vector(x, y, slab_bottom), Vector(0, 0, -1))
        if hits:
            gaps.append((hits[0][0], x, y))
    if not gaps:
        return Clause(
            "underside", False, UNEVALUATED_NM,
            "no base material found under any of 5 sample points below the board",
        )
    gap, x, y = max(gaps)
    detail = (
        f"floor-to-board gap {_fmt(gap)} (largest of {len(gaps)} downward rays from "
        f"the slab bottom at z={slab_bottom:.3f} mm, at ({x:.2f}, {y:.2f})) vs "
        f"tallest bottom-side part {_fmt(need)}"
    )
    return Clause("underside", gap >= need - 1e-6, _nm(gap - need), detail)


def _standoff_concentric(ctx: _Context) -> Clause:
    holes: dict[str, MountingHole] = {h.ref: h for h in ctx.envelope.mounting_holes}
    checked = []
    corners = []
    worst = 0.0
    for ref, x_nm, y_nm in ctx.model.standoffs:
        hole = holes.get(ref)
        if hole is None:
            corners.append(ref)
            continue
        hx, hy = ctx.kicad_to_model_mm(hole.x_nm, hole.y_nm)
        dev = math.hypot(_mm(x_nm) - hx, _mm(y_nm) - hy)
        worst = max(worst, dev)
        checked.append(f"{ref} off by {dev:.4f} mm")
    if not checked:
        what = (
            f"{len(corners)} corner standoff(s), no mounting-hole standoffs"
            if corners else "no standoffs"
        )
        return Clause("standoff_concentric", True, 0, f"{_NOTHING}: {what}")
    detail = (
        "boss axis vs hole: " + ", ".join(checked)
        + f" (tolerance {_CONCENTRIC_TOL_MM:.3f} mm)"
    )
    if corners:
        detail += f"; {len(corners)} corner standoff(s) have no hole to match"
    return Clause(
        "standoff_concentric", worst <= _CONCENTRIC_TOL_MM,
        _nm(_CONCENTRIC_TOL_MM - worst), detail,
    )


def _plug_prisms(ctx: _Context) -> list[tuple[str, Any]]:
    """The plug prisms, derived here from the spec, the rules and the
    envelope: ``rules.plug_envelope`` for the part's connector class,
    centred along the wall on the part and vertically on the receptacle's
    axis (slab top + half the part height + the envelope's axis offset),
    running from 1 mm outside the case to the board's edge."""
    from build123d import Align, Box, Cylinder, Location

    out = []
    base_bb = ctx.base_bb
    board_bb = ctx.board_bb
    slab_top = ctx.slab_top_mm
    for cutout in ctx.spec.cutouts:
        if cutout.face == "top":
            continue
        part = next((p for p in ctx.envelope.parts if p.ref == cutout.ref), None)
        if part is None:
            out.append((cutout.id, None))
            continue
        x0, y0, x1, y1 = ctx.part_rect_mm(part)
        face = cutout.face
        along_nm = (
            part.y_max_nm - part.y_min_nm if face in ("left", "right")
            else part.x_max_nm - part.x_min_nm
        )
        plug = rules.plug_envelope(part.connector, along_nm, part.height_nm)
        axis_z = slab_top + _mm(part.height_nm) / 2 + _mm(plug.axis_offset_nm)
        width = _mm(plug.width_nm)
        height = width if plug.round else _mm(plug.height_nm)
        if face in ("left", "right"):
            along = (y0 + y1) / 2
            t0, t1 = (
                (base_bb.min.X - 1.0, board_bb.min.X) if face == "left"
                else (board_bb.max.X, base_bb.max.X + 1.0)
            )
        else:
            along = (x0 + x1) / 2
            t0, t1 = (
                (base_bb.min.Y - 1.0, board_bb.min.Y) if face == "front"
                else (board_bb.max.Y, base_bb.max.Y + 1.0)
            )
        length = t1 - t0
        if plug.round:
            rot = (0, 90, 0) if face in ("left", "right") else (90, 0, 0)
            solid = Cylinder(width / 2, length, rotation=rot)
            centre = (
                ((t0 + t1) / 2, along, axis_z) if face in ("left", "right")
                else (along, (t0 + t1) / 2, axis_z)
            )
            solid = solid.moved(Location(centre))
        else:
            mn = (Align.MIN, Align.MIN, Align.MIN)
            if face in ("left", "right"):
                solid = Box(length, width, height, align=mn).moved(
                    Location((t0, along - width / 2, axis_z - height / 2))
                )
            else:
                solid = Box(width, length, height, align=mn).moved(
                    Location((along - width / 2, t0, axis_z - height / 2))
                )
        out.append((cutout.id, solid))
    return out


def _cutout_admits_plug(ctx: _Context) -> Clause:
    plugs = _plug_prisms(ctx)
    if not plugs:
        return Clause("cutout_admits_plug", True, 0, f"{_NOTHING}: no side cutouts")
    words = []
    margins = []
    failed = False
    for cid, prism in plugs:
        if prism is None:
            words.append(f"{cid}: names a ref the board does not have")
            failed = True
            margins.append(-1.0)
            continue
        vp = _volume(prism)
        if vp <= _VOLUME_TOL_MM3:
            words.append(f"{cid}: plug prism has no volume")
            failed = True
            margins.append(-1.0)
            continue
        remaining = prism.cut(ctx.base)
        if ctx.lid_assembled is not None:
            remaining = remaining.cut(ctx.lid_assembled)
        missing = vp - _volume(remaining)
        if missing > _VOLUME_TOL_MM3:
            words.append(f"{cid}: {missing:.4f} mm³ of the plug is inside the case")
            failed = True
            margins.append(-_cbrt(missing))
        else:
            clear = float(prism.distance(ctx.base))
            words.append(f"{cid}: admitted, {_fmt(clear)} to the nearest case material")
            margins.append(clear)
    return Clause(
        "cutout_admits_plug", not failed, _nm(min(margins)),
        "; ".join(words) + " (plug prisms from rules.plug_envelope centred on "
        "the receptacle; failure margin = -cbrt of the blocked volume)",
    )


def _widest_band(lip: Any, z_lo: float, z_hi: float) -> tuple[Any, float]:
    """Slice the lip into horizontal bands and return the bounding box of the
    widest one (lowest wins a tie) and that band's mid height. For a plain
    ring every band is the lip face; for a snap lid it is the bead."""
    step = (z_hi - z_lo) / _LIP_BANDS
    best: tuple[float, Any, float] | None = None
    for i in range(_LIP_BANDS):
        a, b = z_lo + i * step, z_lo + (i + 1) * step
        band = _split_z(lip, a, below=False)
        band = None if band is None else _split_z(band, b, below=True)
        if band is None:
            continue
        bb = _bbox(band)
        extent = bb.size.X + bb.size.Y
        if best is None or extent > best[0] + 1e-6:
            best = (extent, bb, (a + b) / 2)
    if best is None:
        bb = _bbox(lip)
        return bb, (z_lo + z_hi) / 2
    return best[1], best[2]


def _lid_mates(ctx: _Context) -> Clause:
    from build123d import Vector

    lid = ctx.lid_assembled
    if lid is None:
        return Clause("lid_mates", True, 0, f"{_NOTHING}: no lid")
    v = _intersection_volume(ctx.base, lid)
    if v > _VOLUME_TOL_MM3:
        side = _cbrt(v)
        return Clause(
            "lid_mates", False, -_nm(side),
            f"base ∩ lid (assembled) = {v:.4f} mm³ (margin = -cbrt, {side:.3f} mm)",
        )
    words = ["base ∩ lid = 0"]
    # The seam: from the rim at the middle of each wall, straight up, the lid
    # must be there. A lid floating above the rim intersects nothing and has
    # a lip in the cavity all the same; only this ray sees the gap.
    rim = ctx.base_bb.max.Z
    bb = ctx.base_bb
    half_wall = _mm(ctx.spec.wall_nm) / 2
    seam = 0.0
    seam_where = ""
    for name, x, y in (
        ("front", (bb.min.X + bb.max.X) / 2, bb.min.Y + half_wall),
        ("back", (bb.min.X + bb.max.X) / 2, bb.max.Y - half_wall),
        ("left", bb.min.X + half_wall, (bb.min.Y + bb.max.Y) / 2),
        ("right", bb.max.X - half_wall, (bb.min.Y + bb.max.Y) / 2),
    ):
        hits = _ray_hits(lid, Vector(x, y, rim - 1e-3), Vector(0, 0, 1))
        if not hits:
            return Clause(
                "lid_mates", False, UNEVALUATED_NM,
                f"no lid above the rim at the {name} wall ({x:.2f}, {y:.2f}, "
                f"{rim:.3f} mm)",
            )
        gap = hits[0][0] - 1e-3
        if gap > seam:
            seam, seam_where = gap, name
    seam_margin = _SEAM_TOL_MM - seam
    if seam > _SEAM_TOL_MM:
        return Clause(
            "lid_mates", False, _nm(seam_margin),
            f"seam open by {_fmt(seam)} at the {seam_where} wall (lid underside "
            f"above the rim at {rim:.3f} mm; allowed {_SEAM_TOL_MM:.3f} mm)",
        )
    words.append(f"seam closed (rim-to-lid {_fmt(seam)} at worst)")
    if ctx.spec.lid not in _LIP_STYLES:
        words.append(f"{ctx.spec.lid} lid: no lip gap to check")
        return Clause("lid_mates", True, 0, "; ".join(words))
    lip = _split_z(lid, rim - 1e-3, below=True)
    if lip is None:
        return Clause(
            "lid_mates", False, UNEVALUATED_NM,
            f"{ctx.spec.lid} lid has no geometry below the rim at z={rim:.3f} mm",
        )
    lip_bb = _bbox(lip)
    lb, z_probe = _widest_band(lip, lip_bb.min.Z, rim - 1e-3)
    cx, cy = (lb.min.X + lb.max.X) / 2, (lb.min.Y + lb.max.Y) / 2
    gaps: dict[str, float] = {}
    for name, d, lip_edge, coord in (
        ("-x", Vector(-1, 0, 0), lb.min.X, "X"),
        ("+x", Vector(1, 0, 0), lb.max.X, "X"),
        ("-y", Vector(0, -1, 0), lb.min.Y, "Y"),
        ("+y", Vector(0, 1, 0), lb.max.Y, "Y"),
    ):
        hits = _ray_hits(ctx.base, Vector(cx, cy, z_probe), d)
        if not hits:
            return Clause(
                "lid_mates", False, UNEVALUATED_NM,
                f"no cavity wall found in direction {name} at z={z_probe:.3f} mm",
            )
        wall = getattr(hits[0][1], coord)
        gaps[name] = abs(wall - lip_edge)
    params = ctx.model.params_mm or {}
    slack = float(params.get("lip_slack", _mm(rules.LIP_SLACK_NM)))
    allowed = 2 * slack
    if ctx.spec.lid == "snap":
        # The ring sits a catch further in; only the hooks reach the wall.
        allowed += _mm(rules.SNAP_CATCH_NM)
    lo, hi = min(gaps.values()), max(gaps.values())
    margin = min(lo, allowed - hi)
    words.append(
        "lip-to-cavity gap " + ", ".join(f"{k} {g:.3f}" for k, g in gaps.items())
        + f" mm (widest lip band vs cavity walls hit by rays at z={z_probe:.3f} mm), "
        f"allowed [0, {allowed:.3f}] mm"
    )
    return Clause("lid_mates", margin >= -1e-6, _nm(margin), "; ".join(words))


def _face_samples(face: Any) -> list[Any]:
    """Points on ``face`` at fixed (u, v) parameters, skipping any that fall in
    a hole of a trimmed face."""
    pts = []
    for u, v in _WALL_SAMPLES:
        try:
            p = face.position_at(u, v)
        except Exception:
            continue
        if face.is_inside(p, tolerance=1e-4):
            pts.append(p)
    return pts


@dataclass(frozen=True)
class _WallSample:
    thickness: float
    required: float
    point: Any
    where: str

    @property
    def margin(self) -> float:
        return self.thickness - self.required


def _wall_samples(
    ctx: _Context, name: str, shape: Any, *, z_only: bool
) -> list[_WallSample]:
    """One :class:`_WallSample` per sampled outer-face point.

    Outer faces are found by a ray test -- a ray leaving the face along its
    outward normal that never meets the part again is on the outside -- and
    the thickness is the distance the opposite ray travels through the
    material before it exits. ``z_only`` restricts to Z-facing faces (the lid
    plate; its lip ring is a feature governed by ``LIP_WIDTH_NM``, not a wall).
    """
    faces = sorted(shape.faces(), key=lambda f: -f.area)
    if len(faces) > _MAX_WALL_FACES:
        ctx.warnings.append(
            f"min_wall: {name} has {len(faces)} faces; only the {_MAX_WALL_FACES} "
            "largest were sampled"
        )
        faces = faces[:_MAX_WALL_FACES]
    wall = _mm(ctx.spec.wall_nm) - _WALL_TOL_MM
    printable = _mm(MIN_WALL_NM) - _WALL_TOL_MM
    boss_floor = _mm(rules.STANDOFF_MIN_FLOOR_NM) - _WALL_TOL_MM
    boss_r = ctx.boss_radius_mm()
    bosses = (
        [(_mm(x), _mm(y)) for _, x, y in ctx.model.standoffs] if name == "base" else []
    )
    bed_z = _bbox(shape).min.Z
    out = []
    for face in faces:
        for p in _face_samples(face):
            try:
                n = face.normal_at(p)
            except Exception:
                continue
            if z_only and abs(n.Z) < 0.9:
                continue
            if _ray_hits(shape, p, n):
                continue  # something beyond: a cavity face, a bore, an opening side
            hits = _ray_hits(shape, p, -n)
            if not hits:
                continue
            t = hits[0][0]
            required, where = wall, name
            # A ray that exits into a void narrower than a boss and re-enters
            # crossed a boss wall or a rib around a bore, not the case wall.
            narrow_void = len(hits) >= 2 and hits[1][0] - t < 2 * boss_r
            if narrow_void:
                required, where = printable, f"{name} wall around a bore"
            elif abs(n.Z) > 0.9 and any(
                math.hypot(p.X - bx, p.Y - by) <= boss_r for bx, by in bosses
            ):
                # A vertical ray under a standoff crosses the floor beneath its
                # blind insert bore: STANDOFF_MIN_FLOOR_NM governs there.
                required, where = boss_floor, f"{name} floor under a standoff bore"
            elif n.Z < -0.9 and bed_z + _BED_Z_MM < p.Z:
                # Ceiling of a pocket opening toward the bed (a screw-head
                # pocket): the plate above it needs the printable minimum.
                required, where = printable, f"{name} plate over a pocket"
            out.append(_WallSample(t, required, p, where))
    return out


def _min_wall(ctx: _Context) -> Clause:
    samples = _wall_samples(ctx, "base", ctx.base, z_only=False)
    if ctx.lid is not None:
        samples += _wall_samples(ctx, "lid", ctx.lid, z_only=True)
    if not samples:
        return Clause(
            "min_wall", False, UNEVALUATED_NM,
            "no outer-face sample produced a thickness",
        )
    worst = min(samples, key=lambda s: s.margin)
    detail = (
        f"thinnest of {len(samples)} outer-face ray samples: {_fmt(worst.thickness)} "
        f"on the {worst.where} at {_fmt_pt(worst.point)} vs {_fmt(worst.required)} "
        f"required (spec wall {_mm(ctx.spec.wall_nm):.3f} mm, tolerance "
        f"{_WALL_TOL_MM:.3f} mm)"
    )
    return Clause("min_wall", worst.margin >= -1e-6, _nm(worst.margin), detail)


def _overhang_deg(nz: float) -> float:
    """Overhang angle from vertical of a down-facing unit normal: 0 for a
    wall, 90 for a ceiling."""
    return math.degrees(math.asin(min(1.0, -nz)))


def _overhang_part(
    ctx: _Context, name: str, shape: Any
) -> tuple[float | None, str, list[str]]:
    """Worst overhang angle from vertical (degrees) among down-facing,
    off-bed faces of ``shape`` in its printed orientation, where it is, and
    the long bridges that were exempted."""
    from build123d import GeomType

    max_bridge = _mm(rules.VENT_MAX_BRIDGE_NM)
    worst: float | None = None
    where = ""
    bridges: list[str] = []
    bed_z = _bbox(shape).min.Z  # the part rests on its lowest point

    def note(angle: float, place: str) -> None:
        nonlocal worst, where
        if worst is None or angle > worst:
            worst, where = angle, place

    for face in shape.faces():
        fb = _bbox(face)
        if bed_z + _BED_Z_MM >= fb.max.Z:
            continue  # on the bed
        if face.geom_type == GeomType.PLANE:
            n = face.normal_at()
            if n.Z >= -1e-6:
                continue
            angle = _overhang_deg(n.Z)
            if angle < _BRIDGE_DEG:
                note(angle, f"{name} planar face at {_fmt_pt(face.center())}")
                continue
            # Flat and down-facing. A step narrower than a nozzle is nothing; a
            # short span, or one whose normal ray meets the part again (an
            # enclosed opening), is a bridge; an open-ended long ledge is a
            # 90-degree overhang. The width of a ring-shaped step is its area
            # over half its perimeter, which the bounding box cannot see.
            span = max(fb.size.X, fb.size.Y)
            perimeter = sum(e.length for e in face.edges())
            width = 2 * face.area / perimeter if perimeter > 0 else span
            centre = face.center()
            if width <= _STEP_MM:
                continue
            if span <= max_bridge or _ray_hits(shape, centre, n):
                if span > max_bridge:
                    bridges.append(
                        f"{name} {span:.1f} mm flat bridge at {_fmt_pt(centre)}"
                    )
                continue
            note(
                angle, f"{name} {span:.1f} mm open ledge underside at {_fmt_pt(centre)}"
            )
            continue
        if face.geom_type == GeomType.CYLINDER:
            axis = face.axis_of_rotation
            if axis is not None and abs(axis.direction.Z) < 1e-3:
                # A horizontal round opening prints as an arch: a bridge.
                span = 2 * float(face.radius)
                if span > max_bridge:
                    at = _fmt_pt(face.center())
                    bridges.append(f"{name} {span:.1f} mm round opening at {at}")
                continue
        verts, tris = face.tessellate(_TESS_TOL_MM, _TESS_ANG)
        kind = face.geom_type.name.lower()
        for a, b, c in tris:
            pa, pb, pc = verts[a], verts[b], verts[c]
            if max(pa.Z, pb.Z, pc.Z) <= bed_z + _BED_Z_MM:
                continue
            n = (pb - pa).cross(pc - pa)
            if n.length < 1e-12:
                continue
            n = n.normalized()
            centroid = (pa + pb + pc) / 3
            # Score the surface, not the facet: a chord across a curved face
            # leans further than the face itself (a 45-degree chamfer swept
            # round a 1 mm corner is a cone whose facets read 47 degrees), so
            # the facet only locates the sample and the B-rep answers the angle.
            try:
                surface = face.normal_at(centroid)
                n = surface if n.dot(surface) >= 0 else -surface
            except Exception:
                pass
            if n.Z >= -1e-6:
                continue
            note(_overhang_deg(n.Z), f"{name} {kind} face at {_fmt_pt(centroid)}")
    return worst, where, bridges


def _overhang(ctx: _Context) -> Clause:
    limit = float(rules.OVERHANG_LIMIT_DEG)
    worst: float | None = None
    where = ""
    for name, shape in _parts(ctx):
        w, loc, bridges = _overhang_part(ctx, name, shape)
        for b in bridges:
            ctx.warnings.append(
                f"overhang: {b} exceeds the {_mm(rules.VENT_MAX_BRIDGE_NM):.1f} mm "
                "bridge rule and may sag"
            )
        if w is not None and (worst is None or w > worst):
            worst, where = w, loc
    unit = "margin in degrees x 1e6"
    if worst is None:
        return Clause(
            "overhang", True, int(round(limit * 1e6)),
            "no down-facing face off the bed in any part's printed orientation "
            f"({unit})",
        )
    margin_deg = limit - worst
    detail = (
        f"worst overhang {worst:.1f} deg from vertical on the {where} vs {limit:.0f} "
        "deg limit (planar and tessellated faces, faces on the bed excluded, short "
        f"or enclosed flat bridges exempt; {unit})"
    )
    return Clause(
        "overhang", margin_deg >= -_ANGLE_TOL_DEG, int(round(margin_deg * 1e6)), detail
    )


def _rect_distance(
    px: float, py: float, x0: float, y0: float, x1: float, y1: float
) -> float:
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return math.hypot(dx, dy)


def _slots(shape: Any) -> list[tuple[float, float, float, float]]:
    """Slot-shaped openings in ``shape``'s planar faces, as XY rectangles in
    the shape's own frame: short side no wider than 1.5x ``VENT_SLOT_NM``,
    long side at least twice the short side."""
    from build123d import GeomType

    max_short = 1.5 * _mm(rules.VENT_SLOT_NM)
    out = []
    for face in shape.faces():
        if face.geom_type != GeomType.PLANE:
            continue
        for wire in face.inner_wires():
            wb = _bbox(wire)
            dims = sorted([wb.size.X, wb.size.Y, wb.size.Z])
            short, long_ = dims[1], dims[2]  # the smallest is the in-plane zero
            if short > 0 and short <= max_short and long_ >= 2 * short:
                out.append((wb.min.X, wb.min.Y, wb.max.X, wb.max.Y))
    return out


def _vent_keepout(ctx: _Context) -> Clause:
    if not ctx.spec.vents:
        return Clause("vent_keepout", True, 0, f"{_NOTHING}: spec has no vents")
    if not ctx.model.standoffs:
        return Clause("vent_keepout", True, 0, f"{_NOTHING}: no standoffs")
    keepout = _mm(rules.VENT_BOSS_KEEPOUT_NM)
    boss_r = ctx.boss_radius_mm()
    bosses = [(ref, _mm(x), _mm(y)) for ref, x, y in ctx.model.standoffs]
    worst: float | None = None
    where = ""
    count = 0
    frames: list[tuple[str, Any, Any]] = [("base", ctx.base, None)]
    if ctx.lid is not None:
        frames.append(("lid", ctx.lid, ctx.model.lid_assembled.inverse()))
    for name, shape, to_frame in frames:
        slots = _slots(shape)
        count += len(slots)
        for ref, bx, by in bosses:
            if to_frame is not None:
                bx, by, _ = _moved_point(to_frame, bx, by, 0.0)
            for x0, y0, x1, y1 in slots:
                clear = _rect_distance(bx, by, x0, y0, x1, y1) - boss_r
                if worst is None or clear < worst:
                    worst = clear
                    where = (
                        f"{name} slot at ({x0:.1f}..{x1:.1f}, {y0:.1f}..{y1:.1f}) "
                        f"vs boss {ref}"
                    )
    if count == 0 or worst is None:
        ctx.warnings.append(
            "vent_keepout: spec asks for vents but no slot-shaped opening was found "
            "in the geometry"
        )
        return Clause(
            "vent_keepout", True, 0,
            f"{_NOTHING}: no slot-shaped openings found (see warnings)",
        )
    detail = (
        f"{count} slot(s) found from planar-face inner wires; closest edge-to-boss-"
        f"edge clearance {_fmt(worst)} ({where}) vs {_fmt(keepout)} keep-out"
    )
    return Clause(
        "vent_keepout", worst >= keepout - 1e-6, _nm(worst - keepout), detail
    )


_EVALUATORS: dict[str, Callable[[_Context], Clause]] = {
    "valid_topology": _valid_topology,
    "positive_volume": _positive_volume,
    "solid_count": _solid_count,
    "bbox": _bbox_clause,
    "board_clash": _board_clash,
    "headroom": _headroom,
    "underside": _underside,
    "standoff_concentric": _standoff_concentric,
    "cutout_admits_plug": _cutout_admits_plug,
    "lid_mates": _lid_mates,
    "min_wall": _min_wall,
    "overhang": _overhang,
    "vent_keepout": _vent_keepout,
}
assert tuple(_EVALUATORS) == CLAUSES


def verify_model(
    model: EnclosureModel, spec: EnclosureSpec, envelope: BoardEnvelope
) -> KernelReport:
    """Evaluate every clause in :data:`CLAUSES`, in that order, and return
    the report. Never raises for a failing clause (call
    :meth:`KernelReport.raise_for_failures`); raises
    :class:`~.errors.KernelUnavailable` without build123d."""
    require_kernel()
    ctx = _Context(model, spec, envelope)
    clauses = []
    for name in CLAUSES:
        try:
            clause = _EVALUATORS[name](ctx)
        except Exception as exc:  # a kernel failure is a failed clause, never a pass
            clause = Clause(
                name, False, UNEVALUATED_NM,
                f"could not be evaluated: {type(exc).__name__}: {exc}",
            )
        clauses.append(clause)
    return KernelReport(tuple(clauses), tuple(ctx.warnings))

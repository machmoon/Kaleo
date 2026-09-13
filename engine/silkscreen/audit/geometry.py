"""Read a ``.kicad_pcb`` into a flat, absolute geometry model for checking.

This is deliberately a *second* reader, independent of :mod:`silkscreen.kicad`.
That module exists to feed the placer: it returns footprint-local courtyard
boxes and pad offsets measured from a courtyard corner in the solver's Y-up
frame, because that is what the packer consumes. A checker wants the opposite
-- every pad, courtyard and track already resolved to absolute board
coordinates -- and, more importantly, a checker written in terms of the code
that produced the board shares that code's blind spots. The rule the repo
already applies to its placement tests ("compute expected geometry with
independent math") applies with more force to a tool whose whole job is to
catch geometry that is wrong.

Everything here is **integer nanometres in KiCad's own frame**: origin
top-left, X right, Y **down**. No Y flip happens anywhere in this package
except once inside the SVG renderer's viewBox, because SVG shares KiCad's
handedness and a second flip would put every marker in the wrong place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from ..units import NM_PER_MM

__all__ = [
    "Rect",
    "Seg",
    "AuditPad",
    "AuditPart",
    "AuditBoard",
    "load_audit_board",
    "seg_seg_distance_nm",
    "seg_rect_distance_nm",
]


def _nm(value: float | None) -> int:
    return int(round(float(value or 0.0) * NM_PER_MM))


@dataclass(frozen=True)
class Rect:
    """An axis-aligned box in board coordinates, nanometres."""

    x0: int
    y0: int
    x1: int
    y1: int

    @staticmethod
    def around(points: list[tuple[int, int]]) -> Rect | None:
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return Rect(min(xs), min(ys), max(xs), max(ys))

    @property
    def width_nm(self) -> int:
        return self.x1 - self.x0

    @property
    def height_nm(self) -> int:
        return self.y1 - self.y0

    @property
    def centre(self) -> tuple[int, int]:
        return ((self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2)

    def grown(self, by_nm: int) -> Rect:
        return Rect(self.x0 - by_nm, self.y0 - by_nm, self.x1 + by_nm, self.y1 + by_nm)

    def union(self, other: Rect) -> Rect:
        return Rect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def intersection(self, other: Rect) -> Rect | None:
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x0 >= x1 or y0 >= y1:
            return None
        return Rect(x0, y0, x1, y1)

    def contains(self, other: Rect) -> bool:
        return (
            self.x0 <= other.x0
            and self.y0 <= other.y0
            and self.x1 >= other.x1
            and self.y1 >= other.y1
        )

    def gap_to(self, other: Rect) -> int:
        """Edge-to-edge separation. 0 when they touch, negative when they overlap.

        The negative case is the useful one: it is how deep the overlap goes,
        which is what a clearance violation needs to report.
        """
        dx = max(other.x0 - self.x1, self.x0 - other.x1)
        dy = max(other.y0 - self.y1, self.y0 - other.y1)
        if dx >= 0 and dy >= 0:
            return int(round(math.hypot(dx, dy)))
        if dx >= 0:
            return dx
        if dy >= 0:
            return dy
        return max(dx, dy)


@dataclass(frozen=True)
class Seg:
    """A straight run of copper or silkscreen, absolute, nanometres."""

    x0: int
    y0: int
    x1: int
    y1: int
    width_nm: int = 0
    layer: str = ""
    net: str = ""

    @property
    def bbox(self) -> Rect:
        radius = (self.width_nm + 1) // 2
        return Rect(
            min(self.x0, self.x1) - radius,
            min(self.y0, self.y1) - radius,
            max(self.x0, self.x1) + radius,
            max(self.y0, self.y1) + radius,
        )

    @property
    def midpoint(self) -> tuple[int, int]:
        return ((self.x0 + self.x1) // 2, (self.y0 + self.y1) // 2)

    @property
    def length_nm(self) -> int:
        return int(round(math.hypot(self.x1 - self.x0, self.y1 - self.y0)))

    @property
    def side(self) -> str:
        """``F``, ``B`` or ``*`` -- which side of the board this is on."""
        return self.layer.split(".")[0] if "." in self.layer else "*"


@dataclass(frozen=True)
class AuditPad:
    ref: str
    number: str
    net: str
    rect: Rect
    layers: tuple[str, ...] = ()
    through_hole: bool = False

    @property
    def centre(self) -> tuple[int, int]:
        return self.rect.centre

    @property
    def name(self) -> str:
        return f"{self.ref}.{self.number}"

    def shares_copper_with(self, other: AuditPad) -> bool:
        """True when both pads have copper on at least one common layer."""
        if self.through_hole or other.through_hole:
            return True
        mine = {lay for lay in self.layers if lay.endswith(".Cu")}
        theirs = {lay for lay in other.layers if lay.endswith(".Cu")}
        if any(lay.startswith("*") for lay in self.layers + other.layers):
            return True
        return bool(mine & theirs)

    @property
    def side(self) -> str:
        for lay in self.layers:
            if lay.endswith(".Cu") and "." in lay:
                return lay.split(".")[0]
        return "F"


@dataclass(frozen=True)
class AuditPart:
    ref: str
    value: str
    lib_id: str
    x_nm: int
    y_nm: int
    angle: float
    side: str
    pads: tuple[AuditPad, ...]
    courtyard: Rect | None
    silk: tuple[Seg, ...] = ()

    @property
    def extent(self) -> Rect:
        """Courtyard if the footprint has one, else the pad bounding box.

        A footprint with no courtyard is reported separately as a blind spot;
        falling back here keeps such a part visible on the render instead of
        silently absent from it.
        """
        if self.courtyard is not None:
            return self.courtyard
        boxes = [p.rect for p in self.pads]
        if not boxes:
            return Rect(self.x_nm, self.y_nm, self.x_nm, self.y_nm)
        out = boxes[0]
        for box in boxes[1:]:
            out = out.union(box)
        return out

    @property
    def centre(self) -> tuple[int, int]:
        return self.extent.centre

    @property
    def is_ic(self) -> bool:
        """Multi-pin active part -- what a decoupling rule applies to."""
        return len(self.pads) >= 4 or self.ref.upper().startswith("U")


@dataclass
class AuditBoard:
    parts: list[AuditPart] = field(default_factory=list)
    tracks: list[Seg] = field(default_factory=list)
    vias: list[tuple[int, int, int, str]] = field(default_factory=list)
    #: The Edge.Cuts bounding box, or None when the board has no outline at
    #: all -- itself a finding, since edge rules mean nothing without one.
    outline: Rect | None = None
    net_names: list[str] = field(default_factory=list)
    source: Path | None = None

    def pads(self) -> list[AuditPad]:
        return [pad for part in self.parts for pad in part.pads]

    def pads_by_net(self) -> dict[str, list[AuditPad]]:
        out: dict[str, list[AuditPad]] = {}
        for pad in self.pads():
            if pad.net:
                out.setdefault(pad.net, []).append(pad)
        return out

    def part_by_ref(self, ref: str) -> AuditPart | None:
        for part in self.parts:
            if part.ref == ref:
                return part
        return None

    @property
    def extent(self) -> Rect:
        """Everything the board occupies, outline included."""
        boxes: list[Rect] = [p.extent for p in self.parts]
        boxes += [t.bbox for t in self.tracks]
        if self.outline is not None:
            boxes.append(self.outline)
        if not boxes:
            return Rect(0, 0, NM_PER_MM, NM_PER_MM)
        out = boxes[0]
        for box in boxes[1:]:
            out = out.union(box)
        return out


def _rotate(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """Footprint-local point to board frame.

    KiCad turns a footprint counter-clockwise on screen and screen Y points
    down, so the board-frame image of a local point is a mathematical rotation
    by ``-angle``. Written out here rather than imported so that a sign error
    in the placer's copy of this cannot hide from the checker.
    """
    if not angle_deg:
        return x, y
    r = math.radians(angle_deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def _absolute_xy(
    x: float,
    y: float,
    offset_x: float = 0,
    offset_y: float = 0,
    angle: float = 0,
) -> tuple[int, int]:
    x, y = _rotate(x, y, angle)
    return int(round(offset_x + x)), int(round(offset_y + y))


def _point_xy(
    point: object,
    offset_x: float = 0,
    offset_y: float = 0,
    angle: float = 0,
) -> tuple[int, int]:
    return _absolute_xy(
        _nm(getattr(point, "X", None)),
        _nm(getattr(point, "Y", None)),
        offset_x,
        offset_y,
        angle,
    )


def _ccw_delta(start: float, end: float) -> float:
    return (end - start) % math.tau


def _arc_geometry(
    points: list[tuple[int, int]],
) -> tuple[float, float, float, float, float] | None:
    """Circle and signed sweep through an arc's start, mid and end points."""
    if len(points) != 3:
        return None
    (ax, ay), (bx, by), (cx, cy) = points
    determinant = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-9:
        return None

    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    centre_x = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / determinant
    centre_y = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / determinant
    radius = math.hypot(ax - centre_x, ay - centre_y)
    start = math.atan2(ay - centre_y, ax - centre_x)
    middle = math.atan2(by - centre_y, bx - centre_x)
    end = math.atan2(cy - centre_y, cx - centre_x)
    ccw_sweep = _ccw_delta(start, end)
    if _ccw_delta(start, middle) <= ccw_sweep + 1e-9:
        sweep = ccw_sweep
    else:
        sweep = -_ccw_delta(end, start)
    return centre_x, centre_y, radius, start, sweep


def _angle_on_arc(angle: float, start: float, sweep: float) -> bool:
    if sweep >= 0:
        return _ccw_delta(start, angle) <= sweep + 1e-9
    return _ccw_delta(angle, start) <= -sweep + 1e-9


def _arc_bound_points(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    geometry = _arc_geometry(points)
    if geometry is None:
        return points
    centre_x, centre_y, radius, start, sweep = geometry
    out = list(points)
    for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
        if _angle_on_arc(angle, start, sweep):
            out.append(
                (
                    int(round(centre_x + radius * math.cos(angle))),
                    int(round(centre_y + radius * math.sin(angle))),
                )
            )
    return out


def _arc_path(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    geometry = _arc_geometry(points)
    if geometry is None:
        return points
    centre_x, centre_y, radius, start, sweep = geometry
    steps = max(1, math.ceil(abs(sweep) / math.radians(5)))
    return [
        (
            int(round(centre_x + radius * math.cos(start + sweep * i / steps))),
            int(round(centre_y + radius * math.sin(start + sweep * i / steps))),
        )
        for i in range(steps + 1)
    ]


def _bezier_point(
    points: list[tuple[int, int]], t: float
) -> tuple[float, float]:
    mt = 1.0 - t
    weights = (mt**3, 3 * mt * mt * t, 3 * mt * t * t, t**3)
    return (
        sum(weight * point[0] for weight, point in zip(weights, points, strict=True)),
        sum(weight * point[1] for weight, point in zip(weights, points, strict=True)),
    )


def _bezier_extrema(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(points) != 4:
        return points
    candidates = {0.0, 1.0}
    for axis in (0, 1):
        p0, p1, p2, p3 = (point[axis] for point in points)
        a = -p0 + 3 * p1 - 3 * p2 + p3
        b = 3 * p0 - 6 * p1 + 3 * p2
        c = -3 * p0 + 3 * p1
        qa, qb, qc = 3 * a, 2 * b, c
        if abs(qa) < 1e-9:
            if abs(qb) >= 1e-9:
                root = -qc / qb
                if 0 < root < 1:
                    candidates.add(root)
            continue
        discriminant = qb * qb - 4 * qa * qc
        if discriminant < 0:
            continue
        root_term = math.sqrt(discriminant)
        for root in ((-qb - root_term) / (2 * qa), (-qb + root_term) / (2 * qa)):
            if 0 < root < 1:
                candidates.add(root)
    return [
        (int(round(x)), int(round(y)))
        for x, y in (_bezier_point(points, t) for t in sorted(candidates))
    ]


def _graphic_points(
    item: object,
    *,
    offset_x: float = 0,
    offset_y: float = 0,
    angle: float = 0,
    path: bool = False,
) -> list[tuple[int, int]]:
    """Absolute points that bound, or trace, one KiCad graphic primitive."""
    kind = type(item).__name__.lower()

    def point(value: object) -> tuple[int, int]:
        return _point_xy(value, offset_x, offset_y, angle)

    if kind.endswith("line"):
        return [point(item.start), point(item.end)]
    if kind.endswith("rect"):
        sx, sy = _nm(item.start.X), _nm(item.start.Y)
        ex, ey = _nm(item.end.X), _nm(item.end.Y)
        corners = [
            _absolute_xy(x, y, offset_x, offset_y, angle)
            for x, y in ((sx, sy), (ex, sy), (ex, ey), (sx, ey))
        ]
        return corners + [corners[0]] if path else corners
    if kind.endswith("circle"):
        centre, edge = point(item.center), point(item.end)
        radius = math.hypot(edge[0] - centre[0], edge[1] - centre[1])
        if not path:
            return [
                (
                    int(round(centre[0] + dx * radius)),
                    int(round(centre[1] + dy * radius)),
                )
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
            ]
        return [
            (
                int(round(centre[0] + radius * math.cos(math.tau * i / 72))),
                int(round(centre[1] + radius * math.sin(math.tau * i / 72))),
            )
            for i in range(73)
        ]
    if kind.endswith("arc"):
        points = [point(item.start), point(item.mid), point(item.end)]
        return _arc_path(points) if path else _arc_bound_points(points)
    if kind.endswith(("poly", "curve")):
        points = [point(value) for value in (item.coordinates or [])]
        if kind.endswith("curve"):
            if path and len(points) == 4:
                return [
                    (int(round(x)), int(round(y)))
                    for x, y in (_bezier_point(points, i / 32) for i in range(33))
                ]
            return _bezier_extrema(points)
        if path and len(points) > 1:
            return points + [points[0]]
        return points
    return []


def _graphic_width_nm(item: object) -> int:
    stroke = getattr(item, "stroke", None)
    if stroke is not None and getattr(stroke, "width", None) is not None:
        return _nm(stroke.width)
    return _nm(getattr(item, "width", None))


def _fp_graphic_segments(
    fp_x: float, fp_y: float, angle: float, item: object
) -> list[Seg]:
    points = _graphic_points(
        item, offset_x=fp_x, offset_y=fp_y, angle=angle, path=True
    )
    width = _graphic_width_nm(item)
    layer = str(getattr(item, "layer", "") or "")
    return [
        Seg(x0, y0, x1, y1, width_nm=width, layer=layer)
        for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False)
    ]


def _pad_rect(fp_x: float, fp_y: float, angle: float, pad) -> Rect:
    """Absolute bounding box of a pad, exact at 90 degree steps.

    Between the cardinal angles a rotated rectangle's bounding box is larger
    than the pad; that direction is the safe one for a clearance checker (it
    can over-report a gap, never miss one), and no footprint this project
    generates is placed off-cardinal.
    """
    hw = _nm(pad.size.X) / 2.0
    hh = _nm(pad.size.Y) / 2.0
    px, py = _nm(pad.position.X), _nm(pad.position.Y)
    local_angle = float(getattr(pad.position, "angle", None) or 0.0)
    total = angle + local_angle
    centre_x, centre_y = _rotate(px, py, angle)
    corners = []
    for sx in (-hw, hw):
        for sy in (-hh, hh):
            cx, cy = _rotate(sx, sy, total)
            corners.append(
                (
                    int(round(fp_x + centre_x + cx)),
                    int(round(fp_y + centre_y + cy)),
                )
            )
    box = Rect.around(corners)
    assert box is not None
    return box


def _reference_of(fp) -> str:
    """Reference designator, across the three ways KiCad has stored it."""
    props = getattr(fp, "properties", None)
    if isinstance(props, dict):
        ref = props.get("Reference")
        if ref:
            return str(ref)
    elif props:
        for prop in props:
            if getattr(prop, "key", None) == "Reference":
                return str(getattr(prop, "value", "") or "")
    for item in getattr(fp, "graphicItems", []) or []:
        if getattr(item, "type", None) == "reference":
            return str(getattr(item, "text", "") or "")
    return ""


def _value_of(fp) -> str:
    props = getattr(fp, "properties", None)
    if isinstance(props, dict):
        return str(props.get("Value", "") or "")
    for prop in props or []:
        if getattr(prop, "key", None) == "Value":
            return str(getattr(prop, "value", "") or "")
    for item in getattr(fp, "graphicItems", []) or []:
        if getattr(item, "type", None) == "value":
            return str(getattr(item, "text", "") or "")
    return ""


def load_audit_board(path: str | Path) -> AuditBoard:
    """Parse ``path`` into absolute geometry. Raises if the file is missing."""
    from kiutils.board import Board  # imported here: the CLI is the only caller

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such board file: {path}")
    raw = Board.from_file(str(path))

    net_by_number = {n.number: (n.name or "") for n in (raw.nets or [])}

    parts: list[AuditPart] = []
    for fp in raw.footprints or []:
        fx, fy = _nm(fp.position.X), _nm(fp.position.Y)
        angle = float(getattr(fp.position, "angle", None) or 0.0)
        side = "B" if str(getattr(fp, "layer", "F.Cu")).startswith("B") else "F"
        ref = _reference_of(fp) or str(getattr(fp, "entryName", "") or "")

        pads: list[AuditPad] = []
        for pad in fp.pads or []:
            layers = tuple(str(lay) for lay in (pad.layers or []))
            pads.append(
                AuditPad(
                    ref=ref,
                    number=str(pad.number),
                    net=str(getattr(pad.net, "name", "") or ""),
                    rect=_pad_rect(fx, fy, angle, pad),
                    layers=layers,
                    through_hole=str(pad.type) in ("thru_hole", "np_thru_hole"),
                )
            )

        courtyard_pts: list[tuple[int, int]] = []
        silk: list[Seg] = []
        for item in fp.graphicItems or []:
            layer = str(getattr(item, "layer", "") or "")
            if layer.endswith(".CrtYd"):
                courtyard_pts.extend(
                    _graphic_points(item, offset_x=fx, offset_y=fy, angle=angle)
                )
            elif layer.endswith(".SilkS"):
                silk.extend(_fp_graphic_segments(fx, fy, angle, item))

        parts.append(
            AuditPart(
                ref=ref,
                value=_value_of(fp),
                lib_id=str(getattr(fp, "libId", "") or ""),
                x_nm=fx,
                y_nm=fy,
                angle=angle,
                side=side,
                pads=tuple(pads),
                courtyard=Rect.around(courtyard_pts),
                silk=tuple(silk),
            )
        )

    tracks: list[Seg] = []
    vias: list[tuple[int, int, int, str]] = []
    for item in raw.traceItems or []:
        kind = type(item).__name__
        if kind == "Segment":
            tracks.append(
                Seg(
                    x0=_nm(item.start.X),
                    y0=_nm(item.start.Y),
                    x1=_nm(item.end.X),
                    y1=_nm(item.end.Y),
                    width_nm=_nm(item.width),
                    layer=str(item.layer),
                    net=net_by_number.get(int(item.net or 0), ""),
                )
            )
        elif kind == "Via":
            vias.append(
                (
                    _nm(item.position.X),
                    _nm(item.position.Y),
                    _nm(item.size),
                    net_by_number.get(int(item.net or 0), ""),
                )
            )

    edge_pts: list[tuple[int, int]] = []
    for item in raw.graphicItems or []:
        if not str(getattr(item, "layer", "")).startswith("Edge.Cuts"):
            continue
        edge_pts.extend(_graphic_points(item))

    return AuditBoard(
        parts=parts,
        tracks=tracks,
        vias=vias,
        outline=Rect.around(edge_pts),
        net_names=[name for name in net_by_number.values() if name],
        source=path,
    )


def _point_seg_distance(px: float, py: float, seg: Seg) -> float:
    vx, vy = seg.x1 - seg.x0, seg.y1 - seg.y0
    wx, wy = px - seg.x0, py - seg.y0
    denom = vx * vx + vy * vy
    if denom == 0:
        return math.hypot(wx, wy)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    return math.hypot(px - (seg.x0 + t * vx), py - (seg.y0 + t * vy))


def seg_seg_distance_nm(a: Seg, b: Seg) -> int:
    """Copper-edge separation between two tracks, accounting for width."""
    if _segments_cross(a, b):
        centre = 0.0
    else:
        centre = min(
            _point_seg_distance(a.x0, a.y0, b),
            _point_seg_distance(a.x1, a.y1, b),
            _point_seg_distance(b.x0, b.y0, a),
            _point_seg_distance(b.x1, b.y1, a),
        )
    return int(round(centre - a.width_nm / 2 - b.width_nm / 2))


def _orient(ax, ay, bx, by, cx, cy) -> int:
    val = (by - ay) * (cx - bx) - (bx - ax) * (cy - by)
    return (val > 0) - (val < 0)


def _segments_cross(a: Seg, b: Seg) -> bool:
    o1 = _orient(a.x0, a.y0, a.x1, a.y1, b.x0, b.y0)
    o2 = _orient(a.x0, a.y0, a.x1, a.y1, b.x1, b.y1)
    o3 = _orient(b.x0, b.y0, b.x1, b.y1, a.x0, a.y0)
    o4 = _orient(b.x0, b.y0, b.x1, b.y1, a.x1, a.y1)
    return o1 != o2 and o3 != o4


def seg_rect_distance_nm(seg: Seg, rect: Rect) -> int:
    """Separation between a track's copper edge and a pad rectangle.

    A segment that crosses the rectangle entirely has both endpoints outside
    it and both far from the corners, so nearest-endpoint distance alone
    reports a comfortable gap for a track running straight over the pad. The
    crossing test is what makes this a distance rather than a near-miss.
    """
    edges = [
        Seg(rect.x0, rect.y0, rect.x1, rect.y0),
        Seg(rect.x1, rect.y0, rect.x1, rect.y1),
        Seg(rect.x1, rect.y1, rect.x0, rect.y1),
        Seg(rect.x0, rect.y1, rect.x0, rect.y0),
    ]
    inside = (
        rect.x0 <= seg.x0 <= rect.x1 and rect.y0 <= seg.y0 <= rect.y1
    ) or (rect.x0 <= seg.x1 <= rect.x1 and rect.y0 <= seg.y1 <= rect.y1)
    if inside or any(_segments_cross(seg, edge) for edge in edges):
        centre = 0.0
    else:
        centre = min(
            min(
                _point_seg_distance(seg.x0, seg.y0, edge),
                _point_seg_distance(seg.x1, seg.y1, edge),
                _point_seg_distance(edge.x0, edge.y0, seg),
                _point_seg_distance(edge.x1, edge.y1, seg),
            )
            for edge in edges
        )
    return int(round(centre - seg.width_nm / 2))

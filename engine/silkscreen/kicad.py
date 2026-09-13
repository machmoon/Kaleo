"""KiCad board interop, without a KiCad install and without touching the GUI.

The original pipeline placed components by driving KiCad with ``pyautogui``:
sleep six seconds, click screen coordinate (1600, 590), paste a pre-placed board
off the clipboard. That is not an integration -- it depends on one monitor
resolution, one window position, and a committed board file that no code ever
generated.

This module reads and writes ``.kicad_pcb`` files directly through ``kiutils``,
which is pure Python. No ``pcbnew``, no DLL directory, no Windows-only paths, and
nothing that breaks when a window moves.

Coordinate systems
------------------
KiCad's Y axis points **down**; the placer's Y axis points **up**. The original
code mixed the two (it took ``bbox.GetBottom()`` as a bottom-left corner), which
mirrored every layout vertically. :func:`apply_placements` does the flip in one
place, explicitly.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from kiutils.board import Board

from .packing import Layer, Net, Part, Placement, Wire
from .units import NM_PER_MM

__all__ = [
    "FootprintInfo",
    "load_board",
    "extract_parts",
    "extract_wires",
    "extract_nets",
    "footprint_ref",
    "apply_placements",
    "set_board_outline",
    "to_parts",
    "save_board",
    "is_power_net",
    "POWER_NET_PATTERNS",
]

#: Nets whose name matches any of these are treated as power/ground and excluded
#: from the wirelength objective. Expanding a 50-pad ground net into a clique
#: yields 1225 "connections" that swamp every signal net -- which is what the
#: original did, collapsing the whole board into a single placement group.
POWER_NET_PATTERNS = (
    "gnd", "agnd", "dgnd", "pgnd", "vss", "avss", "dvss", "earth",
    "vcc", "vdd", "avdd", "dvdd", "vbus", "vin", "vout", "vref",
)

#: Nets with more pads than this are also treated as global rails even if the
#: name does not match, on the theory that no signal net fans out that far.
DEFAULT_MAX_NET_FANOUT = 6

#: Matches supply-rail net names that are spelled as a voltage rather than a
#: word: "+3V3", "5V", "-12V", "1V8". These are rails, not signals.
_VOLTAGE_RAIL_RE = re.compile(r"^[+-]?\d+v\d*$")

_MM_TO_NM = NM_PER_MM


def footprint_ref(fp) -> str:
    """Reference designator of a kiutils footprint.

    ``Footprint.properties`` is a plain ``dict`` in current kiutils and a list of
    property objects in older releases; KiCad 7 and earlier stored the reference
    as an ``fp_text reference`` graphic item instead. Handle all three.
    """
    props = getattr(fp, "properties", None)
    if isinstance(props, dict):
        ref = props.get("Reference")
        if ref:
            return str(ref)
    elif props:
        for prop in props:
            if getattr(prop, "key", None) == "Reference":
                value = getattr(prop, "value", "")
                if value:
                    return str(value)
    for item in getattr(fp, "graphicItems", []) or []:
        if getattr(item, "type", None) == "reference":
            text = getattr(item, "text", "")
            if text:
                return str(text)
    return ""


@dataclass(frozen=True)
class FootprintInfo:
    """A footprint's reference, courtyard extent, and pad-to-net mapping.

    ``min_x_nm``/``min_y_nm``/``max_x_nm``/``max_y_nm`` are the courtyard bounding
    box **in footprint-local coordinates**, i.e. relative to the footprint's
    anchor. This distinction is load-bearing: KiCad stores a footprint's position
    as its *anchor*, but the placer works in bounding-box corners, and in a real
    footprint the courtyard is centred on the anchor (so ``min_x`` is about
    ``-width/2``, not 0). Writing a bbox corner straight into ``fp.position``
    displaces every part by half its own size.
    """

    ref: str
    width_nm: int
    height_nm: int
    #: Pin offsets from the courtyard's bottom-left, in packer (Y-up) space.
    pad_offsets: dict[str, tuple[int, int]]
    pad_nets: dict[str, str]
    #: Courtyard bbox in footprint-local coordinates (KiCad frame, Y down).
    min_x_nm: int = 0
    min_y_nm: int = 0
    max_x_nm: int = 0
    max_y_nm: int = 0
    library_id: str = ""
    #: Which side of the board the footprint sits on. The solver enforces
    #: no-overlap per side -- a part on the back may sit under one on the front
    #: -- so calling every footprint top-side is not a cosmetic error: it makes
    #: the placer reserve a separate slot for parts that never collided.
    side: Layer = Layer.TOP

    def local_bbox_after_rotation(self, rotated: bool) -> tuple[int, int]:
        """Local bbox top-left corner, accounting for a 90 degree rotation.

        KiCad's ``angle`` rotates a footprint counter-clockwise on screen, and
        screen Y points down, so a local point ``(x, y)`` maps to ``(y, -x)``.
        Applying that to the courtyard corners gives a new local bbox whose
        top-left is ``(min_y, -max_x)``.
        """
        if not rotated:
            return self.min_x_nm, self.min_y_nm
        return self.min_y_nm, -self.max_x_nm


def _mm(value: float) -> int:
    return int(round(value * _MM_TO_NM))


def _placer_ref(fp) -> str:
    """The name the placer knows a footprint by.

    ``extract_parts`` and :func:`apply_placements` must agree on this, or a
    footprint gets a slot in the solve under one name and is looked up under
    another on the way back -- it keeps its original position while the solver
    reserves empty space for it.
    """
    return footprint_ref(fp) or (getattr(fp, "entryName", "") or "")


def _rotate_mm(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """Map a footprint-local point into the board frame.

    KiCad's ``angle`` turns a footprint counter-clockwise *on screen*, and screen
    Y points down, so the board-frame image of a local point is a mathematical
    rotation by ``-angle``. At 90 degrees this is ``(x, y) -> (y, -x)``, matching
    :meth:`FootprintInfo.local_bbox_after_rotation`.
    """
    if not angle_deg:
        return x, y
    r = math.radians(angle_deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def _fp_angle(fp) -> float:
    """A footprint's existing placement angle in degrees, 0 if unset."""
    position = getattr(fp, "position", None)
    return float(getattr(position, "angle", None) or 0.0)


def _footprint_side(fp) -> Layer:
    """Which side of the board a footprint sits on.

    KiCad names the side in the footprint's own ``layer``: ``F.Cu`` for the
    front, ``B.Cu`` for the back. A flipped footprint already has its geometry
    stored mirrored, so the courtyard extents need no further correction --
    only the side itself has to be carried through.
    """
    layer = getattr(fp, "layer", "") or ""
    return Layer.BOTTOM if str(layer).startswith("B.") else Layer.TOP


def load_board(path: str | Path) -> Board:
    """Read a ``.kicad_pcb``. Raises ``FileNotFoundError`` if it is missing."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such board file: {path}")
    return Board.from_file(str(path))


#: A circle whose derived radius exceeds this (in mm) is not a courtyard arc,
#: it is three nearly collinear points the circumcentre could not resolve. Ten
#: metres is far larger than any board, so treat such an arc as the segment it
#: visually is rather than reserving a keep-out the size of the implied circle.
_MAX_ARC_RADIUS_MM = 1e4


def _region_corners(
    x0: float, y0: float, x1: float, y1: float, angle_deg: float
) -> list[tuple[float, float]]:
    """All four corners of an axis-aligned region, in the board frame.

    Two opposite corners bound a region only while it stays axis aligned. Turn
    just those two by 45 degrees and the box they span collapses -- for a
    square, to zero height -- so any shape whose stored pair describes a
    *region* has to be expanded to four corners before it is rotated.
    """
    return [_rotate_mm(x, y, angle_deg) for x in (x0, x1) for y in (y0, y1)]


def _circumcentre(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float
) -> tuple[float, float] | None:
    """Centre of the circle through three points, or ``None`` if collinear."""
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    return (
        (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d,
        (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d,
    )


def _arc_points(start, mid, end, angle_deg: float) -> list[tuple[float, float]]:
    """Board-frame points that bound one ``fp_arc``, in mm.

    Three samples are not a bound. Wherever the sweep crosses a cardinal
    direction the arc reaches further than any of ``start``/``mid``/``end``: a
    270 degree arc of radius 1 measured from its samples alone comes out 0.29 mm
    short on two sides, and even the 180 degree arc in the tests reaches 0.125 mm
    past its own endpoints. So recover the circle the three points lie on and add
    whichever of its four extreme points the arc actually sweeps through.

    This runs *after* rotation, in the board frame, because which point of a
    curve is the topmost one depends on the frame you ask in.
    """
    pts = [_rotate_mm(pt.X, pt.Y, angle_deg) for pt in (start, mid, end)]
    (sx, sy), (mx, my), (ex, ey) = pts
    centre = _circumcentre(sx, sy, mx, my, ex, ey)
    if centre is None:
        return pts  # Collinear: the "arc" is a straight segment.
    cx, cy = centre
    radius = math.hypot(sx - cx, sy - cy)
    if radius > _MAX_ARC_RADIUS_MM:
        return pts

    # Which way round does start -> mid -> end go? Whichever direction puts
    # ``mid`` between the endpoints; the swept range is then that interval.
    def ccw(frm: float, to: float) -> float:
        return (to - frm) % math.tau

    a_s = math.atan2(sy - cy, sx - cx)
    a_m = math.atan2(my - cy, mx - cx)
    a_e = math.atan2(ey - cy, ex - cx)
    if ccw(a_s, a_m) <= ccw(a_s, a_e):
        low, span = a_s, ccw(a_s, a_e)
    else:
        low, span = a_e, ccw(a_e, a_s)

    for quarter in range(4):
        theta = quarter * math.pi / 2
        if ccw(low, theta) <= span:
            pts.append((cx + radius * math.cos(theta), cy + radius * math.sin(theta)))
    return pts


def _courtyard_points(item, angle_deg: float = 0.0) -> list[tuple[float, float]]:
    """Every point that bounds one courtyard graphic, in board-frame mm.

    The footprint's angle is applied here rather than by the caller because a
    curve's extreme points depend on the frame they are measured in: the
    rightmost point of a rotated circle is not the image of the rightmost point
    of the unrotated one. Bounding the sampled points first and rotating that box
    afterwards under-reserves space, and the solver packs a neighbour into the
    difference.

    Shape matters for the same reason -- reading the wrong attributes does not
    fail, it reports a part smaller than it is:

    * ``fp_poly`` keeps its vertices in ``coordinates``. Reading only
      ``start``/``end`` finds nothing, so a footprint whose courtyard is drawn
      as a polygon looks like it has no courtyard at all and falls back to the
      bare pad box -- no clearance whatsoever.
    * ``fp_circle``'s ``end`` is a point *on the circumference*, not a corner.
      Treating the pair as a bbox gives a half-width, zero-height box. Rotating
      the centre and taking the radius box is exact at any angle, because a
      circle is its own image under rotation.
    * ``fp_arc``'s bulge is in ``mid``; its endpoints alone miss it entirely,
      and past a quarter turn of sweep ``mid`` is not enough either.
    * ``fp_rect``'s ``start``/``end`` are opposite corners of a *region*, while
      an ``fp_line``'s are the drawn segment itself. Only the first needs its
      other two corners before it can be turned.
    """
    coords = getattr(item, "coordinates", None)
    if coords:
        # ``fp_poly`` vertices. An ``fp_curve``'s Bezier stays inside the hull
        # of its control points, which are stored the same way.
        return [_rotate_mm(pt.X, pt.Y, angle_deg) for pt in coords]

    start = getattr(item, "start", None)
    mid = getattr(item, "mid", None)
    end = getattr(item, "end", None)
    center = getattr(item, "center", None)

    if center is not None and start is None:
        radius = math.hypot(end.X - center.X, end.Y - center.Y) if end else 0.0
        cx, cy = _rotate_mm(center.X, center.Y, angle_deg)
        return _region_corners(cx - radius, cy - radius, cx + radius, cy + radius, 0.0)

    if mid is not None and start is not None and end is not None:
        return _arc_points(start, mid, end, angle_deg)

    if type(item).__name__ == "FpRect" and start is not None and end is not None:
        return _region_corners(start.X, start.Y, end.X, end.Y, angle_deg)

    return [
        _rotate_mm(pt.X, pt.Y, angle_deg)
        for pt in (start, mid, end, getattr(item, "position", None))
        if pt is not None
    ]


def _courtyard_extent(
    fp, angle_deg: float = 0.0
) -> tuple[float, float, float, float] | None:
    """Bounding box of the footprint's courtyard, in board-frame mm.

    Returns ``(min_x, min_y, max_x, max_y)`` relative to the anchor, already
    turned by the footprint's existing placement angle, or ``None`` if the
    footprint has no courtyard layer.
    """
    pts: list[tuple[float, float]] = []
    for item in fp.graphicItems:
        layer = getattr(item, "layer", "") or ""
        if "CrtYd" not in layer:
            continue
        pts += _courtyard_points(item, angle_deg)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _pad_extent(fp, angle_deg: float = 0.0) -> tuple[float, float, float, float]:
    """Bounding box of all pads, in board-frame mm. Fallback when no courtyard."""
    pts: list[tuple[float, float]] = []
    for pad in fp.pads:
        px, py = pad.position.X, pad.position.Y
        hw = (pad.size.X or 0) / 2
        hh = (pad.size.Y or 0) / 2
        # A pad carries its own angle relative to the footprint; the footprint's
        # angle then turns the whole thing.
        total = angle_deg + float(getattr(pad.position, "angle", None) or 0.0)
        for cx in (-hw, hw):
            for cy in (-hh, hh):
                ox, oy = _rotate_mm(cx, cy, total)
                rx, ry = _rotate_mm(px, py, angle_deg)
                pts.append((rx + ox, ry + oy))
    if not pts:
        # Degenerate footprint (e.g. a fiducial with no pads). Give it a
        # nominal 1 mm square so it still gets a slot rather than crashing.
        return -0.5, -0.5, 0.5, 0.5
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def extract_parts(
    board: Board, *, courtyard_margin_mm: float = 0.0
) -> list[FootprintInfo]:
    """Describe every footprint on ``board`` in placer terms.

    Sizes come from the courtyard layer where present, falling back to the pad
    bounding box. Pad offsets are converted into the placer's Y-up frame,
    measured from the part's bottom-left corner.

    A footprint that is **already rotated** on the input board is measured as it
    actually sits. Its courtyard and pads are stored unrotated in the file, so
    reading them literally models a part turned 90 degrees with its width and
    height the wrong way round -- the solver reserves a box of the wrong shape,
    and because :func:`apply_placements` leaves the existing angle in place, the
    written board really does overlap.
    """
    infos: list[FootprintInfo] = []
    for fp in board.footprints:
        ref = _placer_ref(fp)

        angle = _fp_angle(fp)
        extent = _courtyard_extent(fp, angle) or _pad_extent(fp, angle)
        min_x, min_y, max_x, max_y = extent
        min_x -= courtyard_margin_mm
        min_y -= courtyard_margin_mm
        max_x += courtyard_margin_mm
        max_y += courtyard_margin_mm

        width_nm = max(_mm(max_x - min_x), 1)
        height_nm = max(_mm(max_y - min_y), 1)

        pad_offsets: dict[str, tuple[int, int]] = {}
        pad_nets: dict[str, str] = {}
        for pad in fp.pads:
            name = str(pad.number)
            # Local mm -> offset from bottom-left, flipping Y (KiCad Y is down).
            pad_x, pad_y = _rotate_mm(pad.position.X, pad.position.Y, angle)
            off_x = _mm(pad_x - min_x)
            off_y = _mm(max_y - pad_y)
            pad_offsets[name] = (off_x, off_y)
            if pad.net is not None and pad.net.name:
                pad_nets[name] = pad.net.name

        infos.append(
            FootprintInfo(
                ref=ref,
                width_nm=width_nm,
                height_nm=height_nm,
                pad_offsets=pad_offsets,
                pad_nets=pad_nets,
                min_x_nm=_mm(min_x),
                min_y_nm=_mm(min_y),
                max_x_nm=_mm(max_x),
                max_y_nm=_mm(max_y),
                library_id=getattr(fp, "libraryNickname", "") or "",
                side=_footprint_side(fp),
            )
        )
    return infos


def is_power_net(name: str, pad_count: int, max_fanout: int) -> bool:
    """True if ``name`` should be excluded from the wirelength objective."""
    bare = name.lower().rsplit("/", 1)[-1]
    # Split on separators so "VINT" and "VREFBUF_OUT" are not mistaken for rails
    # by a naive prefix match, while "VCC_3V3" and "+3V3" still are.
    tokens = [t for t in re.split(r"[^a-z0-9]+", bare) if t]
    if any(t in POWER_NET_PATTERNS or _VOLTAGE_RAIL_RE.match(t) for t in tokens):
        return True
    return pad_count > max_fanout


def extract_nets(
    infos: list[FootprintInfo],
    *,
    power_weight: float = 0.25,
    max_net_fanout: int = DEFAULT_MAX_NET_FANOUT,
) -> list[Net]:
    """Turn shared nets into HPWL nets for the placer.

    Power rails stay in the objective at a reduced weight rather than being
    dropped. Dropping them entirely is what leaves a decoupling capacitor with
    no objective term at all -- its only connections to the IC it bypasses are
    VCC and GND -- so it drifts to wherever the packer finds room. HPWL costs one
    bounding box per net regardless of pad count, so keeping a 50-pad ground net
    is cheap in a way a pairwise clique never was.
    """
    by_net: dict[str, list[tuple[int, str]]] = {}
    for idx, info in enumerate(infos):
        for pad_name, net_name in info.pad_nets.items():
            by_net.setdefault(net_name, []).append((idx, pad_name))

    out: list[Net] = []
    for net_name, members in by_net.items():
        # Collapse pads on the same part: HPWL over one part's own pads is not
        # a placement signal.
        seen: dict[int, tuple[int, int]] = {}
        for part_idx, pad_name in members:
            if part_idx not in seen:
                seen[part_idx] = infos[part_idx].pad_offsets.get(pad_name, (0, 0))
        if len(seen) < 2:
            continue
        weight = (
            power_weight
            if is_power_net(net_name, len(members), max_net_fanout)
            else 1.0
        )
        out.append(
            Net(
                terminals=tuple(seen.items()),
                name=net_name,
                weight=weight,
            )
        )
    return out


def extract_wires(
    infos: list[FootprintInfo],
    *,
    max_net_fanout: int = DEFAULT_MAX_NET_FANOUT,
) -> list[Wire]:
    """Turn shared nets into placer wires.

    Two fixes over the original:

    * Power and ground nets are skipped. The original expanded every net into a
      full clique, so a ground net with *p* pads contributed ``p*(p-1)/2`` edges
      and dominated the objective.
    * Remaining multi-pad nets are connected as a **star** to the net's first
      pad rather than a clique, which keeps the edge count linear in pad count
      instead of quadratic.
    """
    by_net: dict[str, list[tuple[int, str]]] = {}
    for idx, info in enumerate(infos):
        for pad_name, net_name in info.pad_nets.items():
            by_net.setdefault(net_name, []).append((idx, pad_name))

    wires: list[Wire] = []
    for net_name, members in by_net.items():
        if len(members) < 2:
            continue
        if is_power_net(net_name, len(members), max_net_fanout):
            continue
        hub_idx, hub_pad = members[0]
        hub_off = infos[hub_idx].pad_offsets.get(hub_pad, (0, 0))
        for other_idx, other_pad in members[1:]:
            if other_idx == hub_idx:
                continue
            other_off = infos[other_idx].pad_offsets.get(other_pad, (0, 0))
            wires.append(
                Wire(
                    source=hub_idx,
                    dest=other_idx,
                    offset_source=hub_off,
                    offset_dest=other_off,
                )
            )
    return wires


def to_parts(
    infos: list[FootprintInfo],
    *,
    edge_refs: set[str] | None = None,
    rotatable_refs: set[str] | None = None,
) -> list[Part]:
    """Convert footprint descriptions into placer inputs."""
    edge_refs = set(edge_refs or ())
    rotatable_refs = set(rotatable_refs or ())

    # A placement is matched back to its footprint by ref and nothing else, so a
    # ref shared by two footprints -- or missing altogether -- is not a cosmetic
    # problem. Both placements land on whichever footprint the lookup happens to
    # keep, and the other one silently stays where it was while the solver holds
    # empty space for it. That reads as a successful placement: the moved count
    # still equals the part count.
    blank = sum(1 for info in infos if not info.ref)
    if blank:
        raise ValueError(
            f"{blank} footprint(s) on this board have no reference designator "
            "and no library name; placements cannot be matched back to them."
        )
    duplicated = sorted(r for r, n in Counter(i.ref for i in infos).items() if n > 1)
    if duplicated:
        raise ValueError(
            f"duplicate reference designators on this board: {duplicated}. "
            "Two footprints sharing a ref would collapse onto one position and "
            "strand the other. Annotate the board in KiCad first."
        )

    # Silently ignoring a ref that matches nothing turns a typo into a missing
    # constraint with no signal at all.
    known = {info.ref for info in infos}
    for label, given in (("edge_refs", edge_refs), ("rotatable_refs", rotatable_refs)):
        unknown = given - known
        if unknown:
            raise ValueError(
                f"{label} names refs not on this board: {sorted(unknown)}. "
                f"Available: {sorted(known)}"
            )
    return [
        Part(
            width_nm=info.width_nm,
            height_nm=info.height_nm,
            ref=info.ref,
            must_be_on_edge=info.ref in edge_refs,
            allow_rotation=info.ref in rotatable_refs,
            layer=info.side,
        )
        for info in infos
    ]


def apply_placements(
    board: Board,
    infos: list[FootprintInfo],
    placements: list[Placement],
    board_height_nm: int,
    *,
    origin_mm: tuple[float, float] = (0.0, 0.0),
) -> int:
    """Move footprints on ``board`` to their solved positions.

    Two conversions happen here, and only here.

    **Axis.** The placer works bottom-left origin with Y up; KiCad works top-left
    with Y down::

        bbox_top = board_height - (y + part_height)

    **Frame.** The placer produces the courtyard's bounding-box corner, but
    ``fp.position`` is the footprint's *anchor*, and the courtyard is centred on
    the anchor rather than starting at it. Writing the bbox corner directly would
    displace every footprint by half its own size -- producing overlaps on the
    written board even though the solver's answer was overlap-free::

        anchor = bbox_corner - local_bbox_corner

    Returns the number of footprints actually moved. Refs present in
    ``placements`` but absent from the board are skipped and counted out, rather
    than silently shifting every later index -- the original desynchronised its
    size and reference lists when a footprint was missing, corrupting the whole
    layout.
    """
    by_ref = {info.ref: info for info in infos}
    fp_by_ref = {}
    for fp in board.footprints:
        # Must be the same resolution ``extract_parts`` used, fallback included:
        # matching on the bare reference here strands every footprint that was
        # sized and packed under its library name.
        ref = _placer_ref(fp)
        if ref:
            fp_by_ref[ref] = fp

    moved = 0
    for placement in placements:
        info = by_ref.get(placement.ref)
        fp = fp_by_ref.get(placement.ref)
        if info is None or fp is None:
            continue

        # Moving a footprint across sides means mirroring every pad and graphic
        # it owns, which this writer does not do. Writing the position while
        # ignoring the side would produce a board whose geometry silently
        # contradicts the solve it came from, so refuse instead.
        if placement.layer is not info.side:
            raise ValueError(
                f"{placement.ref} was solved onto the {placement.layer} side but "
                f"sits on the {info.side} side of the board; flipping a footprint "
                "across sides is not supported."
            )

        height_nm = info.width_nm if placement.rotated else info.height_nm
        bbox_top_nm = board_height_nm - (placement.y_nm + height_nm)

        local_x_nm, local_y_nm = info.local_bbox_after_rotation(placement.rotated)

        fp.position.X = origin_mm[0] + (placement.x_nm - local_x_nm) / _MM_TO_NM
        fp.position.Y = origin_mm[1] + (bbox_top_nm - local_y_nm) / _MM_TO_NM
        if placement.rotated:
            current = fp.position.angle or 0
            fp.position.angle = (current + 90) % 360
        moved += 1

    return moved


def set_board_outline(
    board: Board,
    width_nm: int,
    height_nm: int,
    *,
    origin_mm: tuple[float, float] = (0.0, 0.0),
    margin_nm: int = 0,
    replace: bool = True,
) -> None:
    """Draw a rectangular ``Edge.Cuts`` outline around the placed area.

    Without this the written file has no board outline at all, which matters for
    more than looks: ``Edge.Cuts`` is what KiCad measures board-edge clearance
    against, and it is the only representation of the boundary that
    :class:`~silkscreen.packing.Part`'s ``must_be_on_edge`` was solved against.
    A file with parts pinned to an edge that does not exist cannot be
    meaningfully design-rule checked.

    Args:
        board: Board to modify in place.
        width_nm, height_nm: Placed area, as reported by :func:`~silkscreen.pack`.
        origin_mm: Top-left corner, matching the value passed to
            :func:`apply_placements`.
        margin_nm: Extra space between the placed area and the cut line.
        replace: Remove any existing ``Edge.Cuts`` graphics first. Leaving a
            stale outline behind is worse than having none.
    """
    from kiutils.items.common import Position
    from kiutils.items.gritems import GrLine

    if replace:
        board.graphicItems = [
            g for g in board.graphicItems
            if getattr(g, "layer", None) != "Edge.Cuts"
        ]

    m = margin_nm / _MM_TO_NM
    x0 = origin_mm[0] - m
    y0 = origin_mm[1] - m
    x1 = origin_mm[0] + width_nm / _MM_TO_NM + m
    y1 = origin_mm[1] + height_nm / _MM_TO_NM + m

    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        board.graphicItems.append(
            GrLine(
                start=Position(X=sx, Y=sy),
                end=Position(X=ex, Y=ey),
                layer="Edge.Cuts",
                width=0.05,
            )
        )


def save_board(board: Board, path: str | Path) -> Path:
    """Write ``board`` to ``path``, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    board.to_file(str(path))
    return path

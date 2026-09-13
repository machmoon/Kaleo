"""Multi-view PNG snapshots of an :class:`~.cad.EnclosureModel`.

A dependency-free software renderer: tessellate each solid with OCCT, project
the triangles with numpy, paint them back-to-front with Pillow. No OpenGL, no
VTK, no OpenSCAD binary. The packet copies earthtojake/text-to-cad's
``snapshot-review.md``: iso, the *opposite* iso (so every face is in at least
one image), top ortho, front ortho, plus an XZ section through the tallest
part -- the one view that shows whether the board sits in the cavity.

Snapshots are **advisory** (docs/ai-cad-plan.md v2, decision 17): the kernel
clauses are the gate, these exist so a critic model can catch "numerically
plausible, structurally wrong" and so a human can look.

How it draws: the assembled case is exploded -- the lid, in its assembled
orientation, is lifted by 1.5x the base height -- with the board keep-out
inside. Orthographic cameras (top looks down -Z with the front edge at the
bottom of the image and the lid left out so the cavity shows; front looks
along +Y; the two isos sit 30 degrees above the front-right and back-left
corners). Triangles are sorted by mean camera depth (painter's algorithm) and
flat-shaded with one Lambert light; there is no perspective and no
hidden-surface test beyond the sort, which is exactly enough for convex-ish
boxes and cheap enough to run on every repair round.
Colours are this module's own palette: base grey, lid a lighter grey, board
green, parts gold. Work is bounded by the tessellation tolerance: a solid
that tessellates to more than :data:`MAX_TRIANGLES` is re-meshed four times
coarser. Output is byte-deterministic for the same model.

Only the kernel-free names import without build123d, numpy and Pillow; the
three are imported inside the functions that need them.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

from .cad import EnclosureModel, require_kernel

__all__ = ["VIEWS", "MAX_TRIANGLES", "render_packet", "render_grid"]

VIEWS: tuple[str, ...] = ("iso", "iso_opposite", "top", "front", "section")

#: Above this many triangles a solid is re-tessellated four times coarser.
MAX_TRIANGLES: int = 40_000

#: Linear / angular tessellation tolerance (mm, radians).
_TESS_TOL_MM: float = 0.25
_TESS_ANG: float = 0.4

#: The render palette, 0..255.
_BACKGROUND = (244, 242, 238)
_BASE = (140, 145, 153)
_LID = (190, 194, 200)
_BOARD = (0, 115, 51)
_PART = (212, 176, 56)
_INK = (40, 40, 44)

#: Lid lift, as a multiple of the base height.
_EXPLODE = 1.5
#: Fraction of the image the projected scene fills.
_FILL = 0.86
#: Camera-space light direction (x right, y up, z toward the viewer).
_LIGHT = (0.35, 0.45, 0.82)
_ISO_ELEVATION_DEG = 30.0


# --- meshing --------------------------------------------------------------------

def _mesh(shape: Any) -> tuple[Any, Any]:
    """``(vertices (N,3) float64, triangles (M,3) int64)`` for ``shape``, bounded
    by :data:`MAX_TRIANGLES`.

    Reads OCCT's per-face triangulation directly (``BRep_Tool.Triangulation``
    with the face location applied) rather than through
    ``Shape.tessellate``, whose per-triangle iterator costs tens of
    milliseconds a face; the mesh itself is the same one.
    """
    import numpy as np
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_Orientation
    from OCP.TopLoc import TopLoc_Location

    def read(tolerance: float, angular: float) -> tuple[Any, Any, int]:
        shape.mesh(tolerance, angular)
        verts: list[tuple[float, float, float]] = []
        tris: list[tuple[int, int, int]] = []
        for face in shape.faces():
            loc = TopLoc_Location()
            poly = BRep_Tool.Triangulation_s(face.wrapped, loc)
            if poly is None:
                continue
            trsf = loc.Transformation()
            reverse = face.wrapped.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
            offset = len(verts) - 1
            for i in range(1, poly.NbNodes() + 1):
                pt = poly.Node(i).Transformed(trsf)
                verts.append((pt.X(), pt.Y(), pt.Z()))
            for i in range(1, poly.NbTriangles() + 1):
                a, b, c = poly.Triangle(i).Get()
                if reverse:
                    b, c = c, b
                tris.append((a + offset, b + offset, c + offset))
        return verts, tris, len(tris)

    verts, tris, count = read(_TESS_TOL_MM, _TESS_ANG)
    if count > MAX_TRIANGLES:
        BRepTools.Clean_s(shape.wrapped)  # OCCT caches the mesh; drop it
        verts, tris, count = read(4 * _TESS_TOL_MM, 2 * _TESS_ANG)
    if count == 0:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    return np.array(verts, dtype=np.float64), np.array(tris, dtype=np.int64)


def _slab_band(board: Any) -> tuple[float, float] | None:
    """Z range of the substrate: the two largest horizontal planar faces of
    the keep-out are the slab's bottom and top."""
    from build123d import GeomType

    flats = [
        f for f in board.faces()
        if f.geom_type == GeomType.PLANE and abs(f.normal_at().Z) > 0.99
    ]
    flats.sort(key=lambda f: -f.area)
    if len(flats) < 2:
        return None
    z = sorted(f.center().Z for f in flats[:2])
    return z[0], z[1]


def _tallest_part_y(board: Any) -> float:
    """Y of the highest up-facing planar face of the keep-out (the tallest
    top-side part's top), falling back to the keep-out's centre."""
    from build123d import GeomType

    tops = [
        f for f in board.faces()
        if f.geom_type == GeomType.PLANE and f.normal_at().Z > 0.99
    ]
    if not tops:
        return board.bounding_box().center().Y
    top = max(tops, key=lambda f: f.center().Z)
    return float(top.center().Y)


def _keep_behind(shape: Any, y: float) -> Any | None:
    """The part of ``shape`` with ``Y >= y`` (the half the front camera sees
    the cut face of), or None when nothing remains."""
    from build123d import Keep, Plane, split

    plane = Plane(origin=(0, y, 0), x_dir=(1, 0, 0), z_dir=(0, 1, 0))
    try:
        kept = split(shape, bisect_by=plane, keep=Keep.TOP)
    except Exception:
        return None
    if kept is None or not kept.solids():
        return None
    return kept


def _scene(
    model: EnclosureModel, *, section_y: float | None, with_lid: bool = True
) -> list[tuple[Any, Any, Any]]:
    """``[(vertices, triangles, per-triangle colours (M,3))]`` for the exploded
    assembly, optionally cut at ``section_y`` and optionally without the lid."""
    import numpy as np
    from build123d import Location

    base = model.base
    base_h = base.bounding_box().size.Z
    shapes: list[tuple[Any, tuple[int, int, int] | None]] = [(base, _BASE)]
    if with_lid and model.lid is not None and model.lid_assembled is not None:
        lid = model.lid.moved(model.lid_assembled).moved(
            Location((0, 0, _EXPLODE * base_h))
        )
        shapes.append((lid, _LID))
    band = _slab_band(model.board)
    shapes.append((model.board, None))  # coloured per triangle below

    out = []
    for shape, colour in shapes:
        if section_y is not None:
            shape = _keep_behind(shape, section_y)
            if shape is None:
                continue
        v, t = _mesh(shape)
        if len(t) == 0:
            continue
        if colour is not None:
            colours = np.tile(np.array(colour, dtype=np.float64), (len(t), 1))
        else:
            colours = np.tile(np.array(_PART, dtype=np.float64), (len(t), 1))
            if band is not None:
                z = v[:, 2][t]  # (M,3) vertex heights per triangle
                lo, hi = band[0] - 1e-3, band[1] + 1e-3
                on_slab = (z >= lo).all(axis=1) & (z <= hi).all(axis=1)
                colours[on_slab] = np.array(_BOARD, dtype=np.float64)
        out.append((v, t, colours))
    return out


# --- cameras ----------------------------------------------------------------------

def _camera(view: str) -> Any:
    """Rows are the camera's x (right), y (up), z (toward viewer) axes in
    world coordinates, so ``R @ p`` is the camera-space point."""
    import numpy as np

    if view == "top":
        return np.eye(3)
    if view in ("front", "section"):
        return np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float64)
    az = {"iso": -45.0, "iso_opposite": 135.0}[view]
    el = math.radians(_ISO_ELEVATION_DEG)
    az = math.radians(az)
    toward = np.array(
        [math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)]
    )
    z_world = np.array([0.0, 0.0, 1.0])
    up = z_world - toward * float(z_world @ toward)
    up /= np.linalg.norm(up)
    right = np.cross(up, toward)
    return np.stack([right, up, toward])


# --- painting ----------------------------------------------------------------------

def _paint(scene: list[tuple[Any, Any, Any]], view: str, size: tuple[int, int]) -> Any:
    import numpy as np
    from PIL import Image, ImageDraw

    width, height = size
    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    if scene:
        rot = _camera(view)
        cams = [(rot @ v.T).T for v, _, _ in scene]
        allpts = np.concatenate(cams)
        lo, hi = allpts.min(axis=0), allpts.max(axis=0)
        extent = np.maximum(hi[:2] - lo[:2], 1e-9)
        scale = _FILL * min(width / extent[0], height / extent[1])
        centre = (lo[:2] + hi[:2]) / 2
        light = np.array(_LIGHT) / np.linalg.norm(_LIGHT)

        tri_xy = []
        tri_depth = []
        tri_rgb = []
        for cam, (_, t, colours) in zip(cams, scene, strict=True):
            p0, p1, p2 = cam[t[:, 0]], cam[t[:, 1]], cam[t[:, 2]]
            n = np.cross(p1 - p0, p2 - p0)
            norm = np.linalg.norm(n, axis=1)
            keep = norm > 1e-12
            n = n[keep] / norm[keep, None]
            shade = 0.35 + 0.65 * np.abs(n @ light)
            rgb = np.clip(colours[keep] * shade[:, None], 0, 255)
            pts = np.stack([p0[keep], p1[keep], p2[keep]], axis=1)  # (M,3,3)
            xy = np.empty(pts.shape[:2] + (2,))
            xy[..., 0] = width / 2 + (pts[..., 0] - centre[0]) * scale
            xy[..., 1] = height / 2 - (pts[..., 1] - centre[1]) * scale
            tri_xy.append(xy)
            tri_depth.append(pts[..., 2].mean(axis=1))
            tri_rgb.append(rgb)
        xy = np.concatenate(tri_xy)
        depth = np.concatenate(tri_depth)
        rgb = np.concatenate(tri_rgb).round().astype(int)
        order = np.argsort(np.round(depth, 6), kind="stable")  # far first
        for i in order:
            colour = (int(rgb[i, 0]), int(rgb[i, 1]), int(rgb[i, 2]))
            poly = [(float(xy[i, k, 0]), float(xy[i, k, 1])) for k in range(3)]
            draw.polygon(poly, fill=colour, outline=colour)
    label = view if view != "section" else "section (XZ through tallest part)"
    draw.text((8, 6), label, fill=_INK)
    return image


def _render_view(model: EnclosureModel, view: str, size: tuple[int, int]) -> Any:
    section_y = _tallest_part_y(model.board) if view == "section" else None
    # The lifted lid would hide the whole cavity from above; the top view is
    # the one that shows the board in the base, so it leaves the lid out (the
    # two isos show the lid's outer face).
    scene = _scene(model, section_y=section_y, with_lid=(view != "top"))
    return _paint(scene, view, size)


def _png_bytes(image: Any) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# --- public API ------------------------------------------------------------------

def render_packet(
    model: EnclosureModel, out_dir: str | Path, *, size: tuple[int, int] = (800, 600)
) -> tuple[Path, ...]:
    """Write one PNG per :data:`VIEWS` into ``out_dir`` and return the paths
    in that order. The assembled case is drawn exploded (lid lifted) with the
    board keep-out inside it, base grey, lid translucent-ish lighter grey,
    board green, parts gold -- the module palette."""
    require_kernel()
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for view in VIEWS:
        path = directory / f"{view}.png"
        path.write_bytes(_png_bytes(_render_view(model, view, size)))
        paths.append(path)
    return tuple(paths)


def render_grid(model: EnclosureModel, *, size: tuple[int, int] = (1200, 900)) -> bytes:
    """One 2x2 PNG (iso, iso_opposite, top, section) with view labels, the
    single image a critic model is shown."""
    require_kernel()
    from PIL import Image

    width, height = size
    cell = (width // 2, height // 2)
    grid = Image.new("RGB", (width, height), _BACKGROUND)
    for i, view in enumerate(("iso", "iso_opposite", "top", "section")):
        tile = _render_view(model, view, cell)
        grid.paste(tile, ((i % 2) * cell[0], (i // 2) * cell[1]))
    return _png_bytes(grid)

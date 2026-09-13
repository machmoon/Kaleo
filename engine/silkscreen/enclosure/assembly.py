"""The board seated in its case: one labelled STEP an engineer can open.

Everything else in this package builds and checks the *case* around a
synthetic keep-out solid -- courtyard boxes extruded to heights from a table
(``board_shape.py``). This module closes the loop with the real thing: KiCad's
own 3D export of the board, with the library STEP model of every part on it,
placed into the case at the position the case was designed for, exported as
one assembly, and then *measured* there.

Why that is worth a module. The keep-out is a claim ("this part is 1.8 mm
tall, this is where it sits"); the KiCad export is the manufacturer's model.
Where the two disagree the lid presses on a part and nothing in the existing
kernel report can see it, because the kernel only ever sees the claim. The
``part_height_claim`` clause below is the disagreement, measured, with a
signed margin.

Two binaries, two rules
-----------------------
``kicad-cli pcb export step`` produces the board model. It is not vendored,
not emulated and has no fallback: without it :func:`export_board_step` raises
:class:`~.errors.BoardModelUnavailable` naming the exact command it tried.
A case verified against an invented board would be worse than a case verified
against no board, which is the ``KernelUnavailable`` rule applied to the
second binary this feature can need.

**FreeCAD is not used here at all**, and that is a finding rather than an
omission. build123d's ``import_step`` reads the kicad-cli STEP directly --
assembly hierarchy, labels and all six solids of the demo board in 0.25 s --
so seating the board is one OCCT in one process, with no subprocess and no
temporary file. FreeCAD's only job in this package is the *independent*
re-reading of a finished STEP in :mod:`~.freecad_check`, and even that is
strictly out-of-process: FreeCAD and build123d each link their own OCCT, and
loading two of them into one interpreter is a segfault waiting to happen.
Nothing in this package may ever ``import FreeCAD``; ``test_enclosure_assembly.py``
pins that by searching the package source.

Frames
------
Three frames meet here and the mapping is a pure translation, which is the
whole reason this is cheap:

* **KiCad** (``BoardEnvelope``): X right, Y **down**, mm.
* **kicad-cli STEP**: X unchanged, Y **negated**, Z zero at the underside of
  the board body. Read off KiCad's own exporter --
  ``pcbnew/exporters/step/step_pcb_model.cpp``, where every coordinate goes
  through ``gp_Pnt( pcbIUScale.IUTomm( aKiCoords.x - aOrigin.x ),
  -pcbIUScale.IUTomm( aKiCoords.y - aOrigin.y ), aZposition )`` in
  ``makeWireFromChain``, and ``getBoardBodyZPlacement`` sets ``aZPos = bottom``.
  Measured on a generated board to confirm it: outline X -2..14.2 / Y -2..17.85
  in KiCad came back as X -2..14.2 / Y -17.85..2, Z 0..1.51.
* **assembled** (``cad.py``): X as KiCad, Y flipped so ``front`` is KiCad
  max-Y, Z up from the outside of the floor.

So the STEP frame and the assembled frame are the *same handedness* already,
and seating is a translation whose Z term puts the board's underside on the
standoff tops. That is TurboCase's rule, in TurboCase's order: in
``turbocase/scad.py::generate`` the whole case is emitted under
``scale([1, -1, 1])`` (the same Y flip) and the board is drawn at
``translate([0, 0, floor_height + standoff_height]) pcb();`` -- underside on
the standoff top, floor thickness plus standoff height above the origin,
which is exactly ``_Dims.board_bottom = wall + standoff_h`` here. TurboCase
also draws the board **only in preview** (``if (show_pcb && $preview)``); this
module keeps that too, as a separate labelled child that is never fused into
base or lid. The case that gets printed is unchanged by the assembly.

Verification
------------
:func:`verify_assembly` returns a :class:`~.kernel.KernelReport` -- the same
``Clause`` objects, the same signed-margin-in-nanometres convention, the same
"a clause that cannot be evaluated fails with a reason" rule. It is a second
*list* of clauses (:data:`ASSEMBLY_CLAUSES`), deliberately not a second
report type and not a second measurement library: the volume, ray and prism
helpers come from :mod:`~.kernel` by import, because two implementations of
"is this thing inside that thing" is precisely how a checker ends up sharing
the blind spot of the code it checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..units import to_mm
from . import rules
from .board_shape import BoardEnvelope
from .cad import EnclosureModel, require_kernel
from .errors import AssemblyError, BoardModelUnavailable
from .ir import EnclosureSpec

# These names are private to this *module family*, not to ``kernel``: the
# alternative is a second implementation of every measurement, which is the
# failure docs/ai-cad-plan.md keeps warning about (a checker written in terms
# of a second reading of the same geometry shares nothing with the first).
from .kernel import (  # noqa: PLC2701 - shared measurement helpers, see above
    _NOTHING,
    _VOLUME_TOL_MM3,
    UNEVALUATED_NM,
    Clause,
    KernelReport,
    _bbox,
    _cbrt,
    _Context,
    _fmt,
    _intersection_volume,
    _mm,
    _nm,
    _plug_prisms,
    _ray_hits,
    _solids,
)

__all__ = [
    "ASSEMBLY_CLAUSES",
    "Assembly",
    "KICAD_CLI_ENV",
    "KICAD_CLI_MISSING",
    "build_assembly",
    "export_assembly",
    "export_board_step",
    "import_board_step",
    "kicad_cli",
    "seat_offset_mm",
    "verify_assembly",
]

#: Environment variable naming the ``kicad-cli`` binary, for a machine where
#: it is not on ``PATH`` (macOS installs it inside the app bundle).
KICAD_CLI_ENV: str = "SILKSCREEN_KICAD_CLI"

#: Where ``kicad-cli`` lives when it is not on ``PATH``. Checked in order.
KICAD_CLI_CANDIDATES: tuple[str, ...] = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
)

#: The one sentence said when the board model cannot be produced. It names the
#: command, the way ``cad.KERNEL_MISSING`` names the extra.
KICAD_CLI_MISSING: str = (
    "kicad-cli was not found; the board model comes from `kicad-cli pcb export "
    f"step` -- install KiCad, or point {KICAD_CLI_ENV} at the binary"
)

#: How far the board's underside may sit off the standoff tops and still count
#: as seated: a tenth of a 0.2 mm layer, the tolerance ``kernel.py`` uses for
#: the lid seam, for the same reason (a print cannot resolve less).
SEAT_TOL_NM: int = 10_000

#: Registration tolerance between the imported board body and the outline the
#: envelope measured, matching ``_Context.kicad_to_model_mm``'s own 0.01 mm
#: check on the keep-out.
REGISTRATION_TOL_NM: int = 10_000

#: One "millimetre" per defect, the ``kernel.py`` boolean-clause convention.
_DEFECT_NM: int = 1_000_000

#: Seconds ``kicad-cli`` gets. The demo board exported in 0.4 s; a minute is
#: a hung binary, not a slow one.
DEFAULT_EXPORT_TIMEOUT_S: float = 60.0

ASSEMBLY_CLAUSES: tuple[str, ...] = (
    "board_imported",
    "board_registration",
    "board_seated",
    "seated_clash",
    "seated_headroom",
    "part_height_claim",
    "cutout_clear_of_board",
)


# ------------------------------------------------------------ the board model


def kicad_cli() -> str | None:
    """The ``kicad-cli`` to use, or ``None`` when there is none.

    ``SILKSCREEN_KICAD_CLI`` wins, then ``PATH``, then the per-OS install
    locations in :data:`KICAD_CLI_CANDIDATES`.
    """
    override = os.environ.get(KICAD_CLI_ENV, "").strip()
    if override:
        return override if Path(override).exists() else None
    found = shutil.which("kicad-cli")
    if found:
        return found
    for candidate in KICAD_CLI_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def export_board_step(
    pcb_path: str | Path,
    out_path: str | Path,
    *,
    cli: str | None = None,
    timeout_s: float = DEFAULT_EXPORT_TIMEOUT_S,
) -> Path:
    """Run ``kicad-cli pcb export step`` on ``pcb_path``, writing ``out_path``.

    Raises :class:`~.errors.BoardModelUnavailable` -- naming the exact command
    -- when the binary is absent, times out, exits non-zero, or writes nothing.
    It never returns a path to a file that is not there: an agent handed an
    empty board model concludes the case is empty.
    """
    binary = cli or kicad_cli()
    out_path = Path(out_path)
    if binary is None:
        raise BoardModelUnavailable(KICAD_CLI_MISSING)
    command = [
        binary, "pcb", "export", "step", "--force", "-o", str(out_path), str(pcb_path),
    ]
    shown = " ".join(command)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise BoardModelUnavailable(f"{shown}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BoardModelUnavailable(
            f"{shown}: no answer in {timeout_s:g} s"
        ) from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else "no output"
        raise BoardModelUnavailable(f"{shown}: exit {done.returncode}: {detail}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise BoardModelUnavailable(f"{shown}: exit 0 but {out_path} is empty")
    return out_path


def import_board_step(path: str | Path) -> Any:
    """Read a board STEP into a build123d ``Compound``.

    OCCT reads its own dialect: this is ``import_step``, in this process, with
    no FreeCAD anywhere near it (see the module docstring). Raises
    :class:`~.errors.AssemblyError` when the file holds no solid -- a STEP
    that parses to nothing is the quiet zero this package refuses.
    """
    require_kernel()
    from build123d import import_step as _import_step

    path = Path(path)
    try:
        shape = _import_step(path)
    except Exception as exc:  # OCCT raises its own zoo
        raise AssemblyError(f"{path}: could not be read as STEP: {exc}") from exc
    if shape is None or not _solids(shape):
        raise AssemblyError(f"{path}: parsed as STEP but holds no solid")
    return shape


# ------------------------------------------------------------------- seating


def seat_offset_mm(model: EnclosureModel, envelope: BoardEnvelope) -> tuple[
    float, float, float
]:
    """The translation from the kicad-cli STEP frame to the assembled frame.

    Pure translation, because both frames already have Y running the same way
    (module docstring). The X and Y terms come from the substrate box the
    emitter itself recorded in ``model.board_boxes[0]`` rather than from a
    re-derivation of ``wall + clearance``: the emitter is the authority on
    where it put the board, and a second copy of that arithmetic here would
    agree with the first while both were wrong.

    The Z term seats the board's **underside** on the standoff tops --
    TurboCase's ``translate([0, 0, floor_height + standoff_height]) pcb();``.
    """
    if not model.board_boxes or model.board_boxes[0][0] != "board":
        raise AssemblyError(
            "the enclosure model carries no substrate box to seat against "
            "(model.board_boxes[0] should be the 'board' entry)"
        )
    _ref, _side, x0_nm, y0_nm, z0_nm, _x1, _y1, _z1 = model.board_boxes[0]
    return (
        to_mm(x0_nm) - to_mm(envelope.x_min_nm),
        to_mm(y0_nm) + to_mm(envelope.y_max_nm),
        to_mm(z0_nm),
    )


@dataclass(frozen=True)
class Assembly:
    """A case with its real board seated in it.

    ``board`` is the imported STEP moved into the assembled frame; ``pcb`` is
    the substrate solid inside it and ``components`` the rest, each with its
    label from the STEP (KiCad names them by footprint, e.g. ``SOT-223`` --
    *not* by reference designator, which is why parts are matched to the
    envelope by position in :func:`verify_assembly` and not by name).
    """

    model: EnclosureModel
    envelope: BoardEnvelope
    board: Any
    pcb: Any
    pcb_label: str
    components: tuple[tuple[str, Any], ...]
    offset_mm: tuple[float, float, float]
    source: Path | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _xy_area(shape: Any) -> float:
    bb = _bbox(shape)
    return float(bb.size.X * bb.size.Y)


def _bbox_key(shape: Any) -> tuple[int, ...]:
    """A shape's bounding box rounded to the micron, as a dict key."""
    bb = _bbox(shape)
    return tuple(
        int(round(v * 1000))
        for v in (bb.min.X, bb.min.Y, bb.min.Z, bb.max.X, bb.max.Y, bb.max.Z)
    )


def _labelled_solids(shape: Any) -> list[tuple[str, Any]]:
    """Every solid in ``shape``, in the file's own global frame, with the
    label of the assembly node it came from.

    Solids come from ``shape.solids()`` rather than from the ``children``
    tree, and this is load-bearing: a node fetched through ``.children``
    reports a *global* bounding box but hands ``_bool_op`` a wrapped shape
    that has lost the placement, so an intersection against a child silently
    measures the part sitting at the origin -- measured on a kicad-cli export
    of the demo board, where the SOT-223 clashed with the case by 8.97 mm³
    (a quarter of its own volume) with the board correctly seated. Every
    measurement here is therefore taken on flat solids, and labels are
    recovered by matching each solid's box to the node's.
    """
    by_box: dict[tuple[int, ...], str] = {}

    def walk(node: Any) -> None:
        for child in getattr(node, "children", ()) or ():
            if _solids(child):
                by_box.setdefault(_bbox_key(child), str(child.label or ""))
            walk(child)

    walk(shape)
    out = []
    for solid in _solids(shape):
        out.append((by_box.get(_bbox_key(solid), "") or "part", solid))
    return out


def build_assembly(
    model: EnclosureModel, envelope: BoardEnvelope, board_step: str | Path
) -> Assembly:
    """Seat the board STEP at ``board_step`` in ``model``'s case.

    The substrate is picked as the widest-footprint solid in the file and
    corroborated against KiCad's own ``<name>_PCB`` product name; a mismatch
    is a warning on the assembly, never a silent choice.
    """
    b = require_kernel()
    shape = import_board_step(board_step)
    dx, dy, dz = seat_offset_mm(model, envelope)
    seat = b.Location((dx, dy, dz))

    warnings: list[str] = []
    # Flat solids, each moved on its own: see ``_labelled_solids`` for why the
    # assembly tree is not carried through the seat.
    parts = [(label, solid.moved(seat)) for label, solid in _labelled_solids(shape)]
    placed = b.Compound(children=[solid for _label, solid in parts], label="board")
    pcb_label, pcb = max(parts, key=lambda item: _xy_area(item[1]))
    if not pcb_label.endswith("_PCB"):
        warnings.append(
            f"the widest solid in the board STEP is labelled {pcb_label!r}, not "
            "'<name>_PCB' as kicad-cli names the substrate; it is being taken "
            "as the board body anyway"
        )
    components = tuple(
        (label, solid) for label, solid in parts if solid is not pcb
    )
    if not components:
        warnings.append(
            "the board STEP holds no component solid: every footprint's "
            "3D model was missing, so part clauses have nothing real to check"
        )
    return Assembly(
        model=model,
        envelope=envelope,
        board=placed,
        pcb=pcb,
        pcb_label=pcb_label,
        components=components,
        offset_mm=(dx, dy, dz),
        source=Path(board_step),
        warnings=tuple(warnings),
    )


def export_assembly(
    assembly: Assembly, directory: str | Path, stem: str = "assembly"
) -> Path:
    """Write ``<stem>.step``: one labelled assembly, ``base`` / ``lid`` /
    ``board``.

    The board is a sibling of the case parts, never fused into them
    (TurboCase's preview-only board). Labels ride out through
    ``STEPCAFControl_Writer`` with ``SetNameMode(True)``, which is what
    build123d's ``exporters3d.export_step`` sets up.
    """
    b = require_kernel()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"{stem}.step"
    model = assembly.model
    children = [b.Part(model.base.wrapped, label="base")]
    if model.lid is not None:
        children.append(
            b.Part(model.lid.moved(model.lid_assembled).wrapped, label="lid")
        )
    board_parts = [b.Part(assembly.pcb.wrapped, label=assembly.pcb_label or "pcb")]
    board_parts += [
        b.Part(solid.wrapped, label=label) for label, solid in assembly.components
    ]
    children.append(b.Compound(label="board", children=board_parts))
    b.export_step(b.Compound(label=stem, children=children), out)
    if not out.exists() or out.stat().st_size == 0:
        raise AssemblyError(f"STEP export wrote nothing to {out}")
    return out


# -------------------------------------------------------------------- clauses


@dataclass
class _AsmContext:
    """What every assembly clause reads. Wraps a :class:`kernel._Context` so
    the plug prisms and the frame map are the kernel's, not a second copy."""

    assembly: Assembly
    spec: EnclosureSpec
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.kernel_ctx = _Context(
            self.assembly.model, self.spec, self.assembly.envelope
        )

    @property
    def base(self) -> Any:
        return self.assembly.model.base

    @property
    def lid(self) -> Any:
        return self.kernel_ctx.lid_assembled

    @property
    def pcb_bb(self) -> Any:
        return _bbox(self.assembly.pcb)


def _board_imported(ctx: _AsmContext) -> Clause:
    solids = _solids(ctx.assembly.board)
    volumes = [float(s.volume) for s in solids]
    bad = [v for v in volumes if v <= _VOLUME_TOL_MM3]
    detail = (
        f"{len(solids)} solid(s) from {ctx.assembly.source}; "
        f"smallest {min(volumes):.4f} mm³, board body "
        f"{float(ctx.assembly.pcb.volume):.3f} mm³, "
        f"{len(ctx.assembly.components)} component solid(s)"
    )
    if bad:
        return Clause(
            "board_imported", False, -_DEFECT_NM * len(bad),
            f"{len(bad)} solid(s) with no volume; {detail}",
        )
    return Clause("board_imported", True, _nm(_cbrt(min(volumes))), detail)


def _board_registration(ctx: _AsmContext) -> Clause:
    """The seated board body lands where the envelope says the board is.

    This is the transform's own receipt. It is measured on the imported
    substrate against the outline the envelope read out of the same
    ``.kicad_pcb``, so a sign error in the frame map shows up as tens of
    millimetres rather than as a subtly wrong case.
    """
    env = ctx.assembly.envelope
    _ref, _side, x0, y0, _z0, x1, y1, _z1 = ctx.assembly.model.board_boxes[0]
    bb = ctx.pcb_bb
    want = ((_mm(x0), _mm(y0)), (_mm(x1), _mm(y1)))
    got = ((bb.min.X, bb.min.Y), (bb.max.X, bb.max.Y))
    deltas = {
        "x_min": got[0][0] - want[0][0],
        "y_min": got[0][1] - want[0][1],
        "x_max": got[1][0] - want[1][0],
        "y_max": got[1][1] - want[1][1],
    }
    where, worst = max(deltas.items(), key=lambda kv: abs(kv[1]))
    tol = _mm(REGISTRATION_TOL_NM)
    # The substrate's own thickness is a separate fact and deliberately not a
    # clause: KiCad's STEP board body is the dielectric between the outer
    # copper layers, while ``(general (thickness))`` is the finished stack, so
    # they differ by the copper and mask (0.09 mm on the demo board). Seating
    # is on the imported underside, so the difference can only show up as
    # extra headroom -- which ``seated_headroom`` measures for real.
    step_thickness = bb.size.Z
    claimed = _mm(env.thickness_nm)
    if abs(step_thickness - claimed) > tol:
        ctx.warnings.append(
            f"board body is {step_thickness:.3f} mm thick in the STEP and "
            f"{claimed:.3f} mm in the board file's stack-up; the board is seated "
            "on its imported underside, so the difference reads as headroom"
        )
    detail = (
        f"seated board body {bb.min.X:.3f}..{bb.max.X:.3f} x "
        f"{bb.min.Y:.3f}..{bb.max.Y:.3f} mm vs the emitter's substrate box "
        f"{want[0][0]:.3f}..{want[1][0]:.3f} x {want[0][1]:.3f}..{want[1][1]:.3f} mm; "
        f"worst edge {where} off by {worst:+.4f} mm vs {_fmt(tol)} tolerance"
    )
    return Clause(
        "board_registration", abs(worst) <= tol, _nm(tol - abs(worst)), detail
    )


def _board_seated(ctx: _AsmContext) -> Clause:
    """The board rests on the standoffs: nothing under it, and no daylight.

    A downward ray from just above the board's underside at each standoff
    centre must meet case material at the underside itself. A gap means the
    board is hanging on nothing there; the negative case (material *through*
    the board) is ``seated_clash``'s.
    """
    from build123d import Vector

    standoffs = ctx.assembly.model.standoffs
    if not standoffs:
        return Clause("board_seated", True, 0, f"{_NOTHING}: no standoffs")
    bb = ctx.pcb_bb
    start = 0.5  # the ray starts this far above the underside
    tol = _mm(SEAT_TOL_NM)
    gaps: list[tuple[float, str]] = []
    missed: list[str] = []
    for ref, x_nm, y_nm in standoffs:
        x, y = _mm(x_nm), _mm(y_nm)
        origin = Vector(x, y, bb.min.Z + start)
        hits = _ray_hits(ctx.base, origin, Vector(0, 0, -1))
        if not hits:
            missed.append(ref)
            continue
        gaps.append((hits[0][0] - start, ref))
    if missed and not gaps:
        return Clause(
            "board_seated", False, UNEVALUATED_NM,
            "no case material under any standoff centre: "
            + ", ".join(sorted(missed)),
        )
    if missed:
        ctx.warnings.append(
            "board_seated: no case material under standoff(s) "
            + ", ".join(sorted(missed))
        )
    gap, ref = max(gaps, key=lambda g: abs(g[0]))
    detail = (
        f"board underside at z={bb.min.Z:.3f} mm; worst standoff {ref} supports it "
        f"{gap:+.4f} mm away ({len(gaps)} standoff(s) probed by downward ray) vs "
        f"{_fmt(tol)} tolerance"
    )
    return Clause("board_seated", abs(gap) <= tol, _nm(tol - abs(gap)), detail)


def _seated_clash(ctx: _AsmContext) -> Clause:
    """No case material inside the real board -- the same measurement
    ``kernel._board_clash`` makes against the keep-out, made against the
    manufacturer's models instead."""
    board = ctx.assembly.board
    v_base = _intersection_volume(ctx.base, board)
    v_lid = 0.0
    if ctx.lid is not None:
        v_lid = _intersection_volume(ctx.lid, board)
    total = v_base + v_lid
    if total > _VOLUME_TOL_MM3:
        side = _cbrt(total)
        return Clause(
            "seated_clash", False, -_nm(side),
            f"the case is inside the seated board: base ∩ board {v_base:.4f} mm³, "
            f"lid ∩ board {v_lid:.4f} mm³ (margin = -cbrt of the total, "
            f"{side:.3f} mm)",
        )
    clearance = float(ctx.base.distance(board))
    words = f"base ∩ board = 0; base-to-board clearance {_fmt(clearance)}"
    if ctx.lid is not None:
        lid_clear = float(ctx.lid.distance(board))
        words += f"; lid ∩ board = 0; lid-to-board clearance {_fmt(lid_clear)}"
        clearance = min(clearance, lid_clear)
    return Clause("seated_clash", True, _nm(clearance), words)


def _component_probe(solid: Any) -> tuple[float, float, float, float, float]:
    """``(x0, y0, x1, y1, top_z)`` of one seated component."""
    bb = _bbox(solid)
    return bb.min.X, bb.min.Y, bb.max.X, bb.max.Y, bb.max.Z


def _seated_headroom(ctx: _AsmContext) -> Clause:
    """Lid material over each real top-side part, measured over that part's
    own footprint -- ``kernel._headroom``'s method, applied to the imported
    solids rather than to the keep-out prisms."""
    from build123d import Align, Box, Location

    if ctx.lid is None:
        return Clause("seated_headroom", True, 0, f"{_NOTHING}: no lid (open top)")
    if not ctx.assembly.components:
        return Clause(
            "seated_headroom", True, 0,
            f"{_NOTHING}: the board STEP holds no component solid",
        )
    want = _mm(rules.HEADROOM_NM)
    slab_top = ctx.pcb_bb.max.Z
    lid = ctx.lid
    lid_bb = _bbox(lid)
    worst: tuple[float, str, float, float] | None = None
    probed = 0
    for label, solid in ctx.assembly.components:
        x0, y0, x1, y1, top = _component_probe(solid)
        if top <= slab_top + 1e-6:   # a bottom-side part: ``underside``'s business
            continue
        probed += 1
        height = max(lid_bb.max.Z - top, 0.0) + 1.0
        probe = Box(
            x1 - x0, y1 - y0, height, align=(Align.MIN, Align.MIN, Align.MIN)
        ).moved(Location((x0, y0, top - 1e-3)))
        above = lid.intersect(probe)
        hit = [item for item in (above or []) if item.solids()]
        underside = min(_bbox(item).min.Z for item in hit) if hit else lid_bb.max.Z
        gap = underside - top
        if worst is None or gap < worst[0]:
            worst = (gap, label, underside, top)
    if worst is None:
        return Clause(
            "seated_headroom", True, 0,
            f"{_NOTHING}: no component solid stands above the board surface",
        )
    gap, label, underside, top = worst
    detail = (
        f"lid underside over {label} at {underside:.3f} mm, its top at {top:.3f} mm: "
        f"{_fmt(gap)} headroom vs {_fmt(want)} required (worst of {probed} seated "
        "top-side part(s), each probed over its own footprint)"
    )
    return Clause("seated_headroom", gap >= want - 1e-6, _nm(gap - want), detail)


def _part_height_claim(ctx: _AsmContext) -> Clause:
    """The height table told the truth about every part.

    Each imported component is matched to a keep-out box by XY centre -- the
    STEP labels parts by footprint (``C_0603_1608Metric``), never by reference
    designator, so a name match is not available and a position match is the
    honest one. The margin is the shortest ``keep-out top - real top`` found:
    negative means a real part stands above the box the case was built around,
    which is exactly the bug that makes a lid press on a component while every
    existing kernel clause passes.
    """
    boxes = [
        bx for bx in ctx.assembly.model.board_boxes if bx[1] in ("top", "bottom")
    ]
    if not ctx.assembly.components:
        return Clause(
            "part_height_claim", True, 0,
            f"{_NOTHING}: the board STEP holds no component solid",
        )
    if not boxes:
        return Clause(
            "part_height_claim", True, 0,
            f"{_NOTHING}: the board carries no part keep-out box",
        )
    slab_top = ctx.pcb_bb.max.Z
    worst: tuple[float, str, str] | None = None
    unmatched: list[str] = []
    matched = 0
    for label, solid in ctx.assembly.components:
        x0, y0, x1, y1, top = _component_probe(solid)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        bottom = _bbox(solid).min.Z
        up = top > slab_top + 1e-6
        hit = None
        for ref, side, bx0, by0, bz0, bx1, by1, bz1 in boxes:
            if side != ("top" if up else "bottom"):
                continue
            if _mm(bx0) <= cx <= _mm(bx1) and _mm(by0) <= cy <= _mm(by1):
                hit = (ref, _mm(bz1) if up else _mm(bz0))
                break
        if hit is None:
            unmatched.append(label)
            continue
        matched += 1
        ref, limit = hit
        slack = (limit - top) if up else (bottom - limit)
        if worst is None or slack < worst[0]:
            worst = (slack, label, ref)
    if unmatched:
        names = ", ".join(sorted(set(unmatched)))
        return Clause(
            "part_height_claim", False, -_DEFECT_NM * len(unmatched),
            f"{len(unmatched)} seated part(s) sit over no keep-out box at all "
            f"({names}); the case was built without them",
        )
    if worst is None:
        return Clause(
            "part_height_claim", True, 0, f"{_NOTHING}: nothing matched a keep-out box"
        )
    slack, label, ref = worst
    detail = (
        f"{matched} seated part(s) matched to keep-out boxes by XY centre; tightest "
        f"is {label} under the box for {ref}, with {slack:+.3f} mm of the claimed "
        "envelope left above it"
    )
    return Clause("part_height_claim", slack >= -1e-6, _nm(slack), detail)


def _cutout_clear_of_board(ctx: _AsmContext) -> Clause:
    """Every connector opening still admits its plug with the board seated.

    The prisms are ``kernel._plug_prisms``' -- built from the spec, the plug
    envelopes and the part rectangles, never from the openings the emitter
    drew -- and the only thing added here is the seated board as a third
    obstacle. A plug that clears the walls and then hits the board is a case
    that cannot be plugged in.
    """
    prisms = _plug_prisms(ctx.kernel_ctx)
    if not prisms:
        return Clause(
            "cutout_clear_of_board", True, 0, f"{_NOTHING}: no side cutouts"
        )
    board = ctx.assembly.board
    worst: tuple[float, str] | None = None
    for cid, prism in prisms:
        blocked = _intersection_volume(prism, board)
        if worst is None or -blocked < worst[0]:
            worst = (-blocked, cid)
    blocked, cid = -worst[0], worst[1]
    if blocked > _VOLUME_TOL_MM3:
        side = _cbrt(blocked)
        return Clause(
            "cutout_clear_of_board", False, -_nm(side),
            f"the seated board blocks the {cid!r} opening by {blocked:.4f} mm³ "
            f"(margin = -cbrt, {side:.3f} mm)",
        )
    clear = min(float(prism.distance(board)) for _cid, prism in prisms)
    return Clause(
        "cutout_clear_of_board", True, _nm(clear),
        f"{len(prisms)} plug prism(s) clear of the seated board; closest "
        f"{_fmt(clear)}",
    )


_EVALUATORS = {
    "board_imported": _board_imported,
    "board_registration": _board_registration,
    "board_seated": _board_seated,
    "seated_clash": _seated_clash,
    "seated_headroom": _seated_headroom,
    "part_height_claim": _part_height_claim,
    "cutout_clear_of_board": _cutout_clear_of_board,
}
assert tuple(_EVALUATORS) == ASSEMBLY_CLAUSES


def verify_assembly(assembly: Assembly, spec: EnclosureSpec) -> KernelReport:
    """Evaluate every clause in :data:`ASSEMBLY_CLAUSES`, in that order.

    Never raises for a failing clause (call
    :meth:`~.kernel.KernelReport.raise_for_failures`); a clause that raises
    inside the kernel fails with :data:`~.kernel.UNEVALUATED_NM` and the
    exception in its detail, exactly as :func:`~.kernel.verify_model` does.
    """
    require_kernel()
    ctx = _AsmContext(assembly, spec, warnings=list(assembly.warnings))
    clauses = []
    for name in ASSEMBLY_CLAUSES:
        try:
            clause = _EVALUATORS[name](ctx)
        except Exception as exc:  # a kernel failure is a failed clause, never a pass
            clause = Clause(
                name, False, UNEVALUATED_NM,
                f"could not be evaluated: {type(exc).__name__}: {exc}",
            )
        clauses.append(clause)
    return KernelReport(tuple(clauses), tuple(ctx.warnings))

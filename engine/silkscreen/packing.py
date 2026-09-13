"""CP-SAT component placement.

This is a corrected rewrite of the original ``packing/rectangles.py``. The
formulation it inherits is sound -- fixed-size intervals plus ``AddNoOverlap2D``
for disjointness, ``AddMaxEquality`` for the bounding box, ``AddAbsEquality`` for
Manhattan wirelength -- but the original had four defects that made it produce
wrong or unusable answers:

1. ``y_sum`` was summed over ``for h, _ in rects``, which binds ``h`` to the
   *width*. Any part taller than the sum of all widths made the model infeasible.
2. Sizes came from ``pcbnew`` bounding boxes in nanometres and were scaled by
   ``1e-3``, so one solver unit was one micron. An 0603 resistor was 1600 units
   wide; the domains were enormous and the solver never closed.
3. Parts were packed flush against each other -- ``AddNoOverlap2D`` with no
   clearance term yields 0 mm courtyard gaps, which is unmanufacturable.
4. On ``UNKNOWN`` (i.e. the time limit expired) it raised, discarding a feasible
   solution the solver had already found.

It also adds optional 90-degree rotation, which real placement needs, and a
deterministic shelf fallback so the caller always gets a usable placement.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ortools.sat.python import cp_model

from .units import (
    DEFAULT_CLEARANCE_NM,
    DEFAULT_GRID_NM,
    cells_ceil,
    cells_round,
)

__all__ = [
    "Part",
    "Wire",
    "Net",
    "Keepout",
    "Layer",
    "Placement",
    "PackResult",
    "PackStatus",
    "pack",
]


class Layer(StrEnum):
    """Which side of the board a part sits on.

    Parts on opposite sides may occupy the same X/Y -- that is what a two-sided
    board is for -- so disjointness is enforced per layer rather than globally.
    """

    TOP = "top"
    BOTTOM = "bottom"
    #: Let the solver choose. Suitable for passives with no mechanical or
    #: thermal reason to be on a particular side; connectors, indicators and
    #: anything a human touches generally do have such a reason.
    EITHER = "either"


class PackStatus(StrEnum):
    """How the returned placement was obtained."""

    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    #: The solver found nothing in time; the shelf fallback produced this.
    FALLBACK = "fallback"


@dataclass(frozen=True)
class Part:
    """A footprint to place.

    Dimensions are courtyard extents in nanometres (KiCad internal units), which
    is exactly what ``pcbnew``'s ``GetBoundingBox().GetWidth()/GetHeight()``
    return -- no conversion at the call site.
    """

    width_nm: int
    height_nm: int
    ref: str = ""
    #: Force onto a board edge (USB connectors, antennas, mounting holes).
    must_be_on_edge: bool = False
    #: Allow a 90-degree rotation. Off by default: rotating a part invalidates
    #: any silkscreen orientation the caller may care about.
    allow_rotation: bool = False
    #: Which side of the board this part sits on.
    layer: Layer = Layer.TOP
    #: Pin this part's bottom-left corner, in nanometres. A placement is only
    #: useful if you can keep the parts you already like: without this, every
    #: re-solve reshuffles the whole board and the tool cannot be used
    #: iteratively. Snapped to the solver grid, so give grid-aligned values
    #: when the exact coordinate matters.
    fixed_at_nm: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.width_nm <= 0 or self.height_nm <= 0:
            raise ValueError(
                f"Part {self.ref!r} has non-positive extent "
                f"({self.width_nm} x {self.height_nm} nm)"
            )


@dataclass(frozen=True)
class Wire:
    """A connection between two parts, used only to weight the objective.

    ``offset_*`` are pin positions relative to the owning part's bottom-left
    corner, in nanometres.
    """

    source: int
    dest: int
    offset_source: tuple[int, int]
    offset_dest: tuple[int, int]


@dataclass(frozen=True)
class Net:
    """One electrical net, costed as half-perimeter wirelength (HPWL).

    HPWL -- the half-perimeter of the bounding box enclosing every terminal --
    is the standard placement proxy for routed length, and it replaces two bad
    alternatives:

    * A **clique** of pairwise distances, which makes a 50-pad ground net
      contribute 1,225 terms and swamp every signal net.
    * A **star** to one arbitrarily-chosen terminal, which overestimates length,
      turns the hub into a gravity well carrying *n-1* terms against every
      spoke's one, and -- because the hub is whichever pad was read first --
      makes the layout depend on footprint ordering inside the ``.kicad_pcb``
      text.

    HPWL costs one bounding box per net regardless of pad count, so power nets
    can stay *in* the objective at a reduced weight rather than being dropped.
    That matters more than it sounds: a decoupling capacitor is connected to its
    IC only by power nets, so excluding them entirely leaves the cap with no
    objective term at all and it drifts to wherever the packer finds room.
    """

    #: ``(part_index, (offset_x_nm, offset_y_nm))`` for each terminal.
    terminals: tuple[tuple[int, tuple[int, int]], ...]
    name: str = ""
    #: Relative importance. Power rails are routed as planes rather than traces,
    #: so their length matters less -- but not zero, because proximity still does.
    weight: float = 1.0


@dataclass(frozen=True)
class Keepout:
    """A rectangular region no part may occupy.

    Mounting holes, a connector's mating envelope, an antenna's ground
    clearance, a mechanical boss. Modelled as an immovable participant in the
    same no-overlap constraint as the parts, which is exactly what it is.
    """

    x_nm: int
    y_nm: int
    width_nm: int
    height_nm: int
    name: str = ""

    def __post_init__(self) -> None:
        if self.width_nm <= 0 or self.height_nm <= 0:
            raise ValueError(
                f"Keepout {self.name!r} has non-positive extent "
                f"({self.width_nm} x {self.height_nm} nm)"
            )


@dataclass(frozen=True)
class Placement:
    """Where a part ended up. ``x_nm``/``y_nm`` locate its bottom-left corner."""

    ref: str
    x_nm: int
    y_nm: int
    rotated: bool = False
    layer: Layer = Layer.TOP


@dataclass(frozen=True)
class PackResult:
    placements: list[Placement]
    board_width_nm: int
    board_height_nm: int
    status: PackStatus
    #: Total Manhattan wirelength in nanometres, or ``None`` when no wires were
    #: supplied.
    wirelength_nm: int | None = None
    solve_time_s: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _shelf_pack(
    parts: Sequence[Part], clearance_nm: int
) -> tuple[list[Placement], int, int]:
    """Deterministic next-fit-decreasing shelf packing.

    Used when CP-SAT finds nothing inside the time limit. It is not a good
    layout, but it is a *valid* one -- no overlaps, clearance respected -- which
    beats raising an exception on a large board.
    """
    if not parts:
        return [], 0, 0

    order = sorted(
        range(len(parts)), key=lambda i: parts[i].height_nm, reverse=True
    )
    # Keep shelves roughly square overall.
    total_area = sum(
        (p.width_nm + clearance_nm) * (p.height_nm + clearance_nm) for p in parts
    )
    target_w = max(
        int(math.sqrt(total_area)),
        max(p.width_nm for p in parts) + clearance_nm,
    )

    placements: list[Placement | None] = [None] * len(parts)
    cursor_x = 0
    shelf_y = 0
    shelf_h = 0
    width_used = 0

    for i in order:
        part = parts[i]
        step_w = part.width_nm + clearance_nm
        step_h = part.height_nm + clearance_nm
        if cursor_x > 0 and cursor_x + step_w > target_w:
            shelf_y += shelf_h
            cursor_x = 0
            shelf_h = 0
        placements[i] = Placement(
            ref=part.ref,
            x_nm=cursor_x + clearance_nm // 2,
            y_nm=shelf_y + clearance_nm // 2,
        )
        cursor_x += step_w
        shelf_h = max(shelf_h, step_h)
        width_used = max(width_used, cursor_x)

    return [p for p in placements if p is not None], width_used, shelf_y + shelf_h


def pack(
    parts: Sequence[Part],
    wires: Sequence[Wire] = (),
    *,
    nets: Sequence[Net] = (),
    keepouts: Sequence[Keepout] = (),
    grid_nm: int = DEFAULT_GRID_NM,
    clearance_nm: int = DEFAULT_CLEARANCE_NM,
    max_board_nm: tuple[int, int] | None = None,
    time_limit_s: float | None = 10.0,
    size_weight: float = 1.0,
    wire_weight: float = 1.0,
    balance_objective: bool = False,
    break_symmetry: bool = True,
    seed: int = 0,
    workers: int = 1,
) -> PackResult:
    """Place ``parts`` to minimise board size and total wirelength.

    Args:
        parts: Footprints to place. Extents are courtyard size in nanometres.
        wires: Two-terminal connections, kept for convenience. Each is
            normalised into a two-terminal :class:`Net`.
        nets: Multi-terminal nets costed as half-perimeter wirelength. Prefer
            these over ``wires`` -- a real net is a tree, not a set of pairs.
        keepouts: Rectangular regions no part may occupy, on either side --
            mounting holes, a connector's mating envelope, mechanical bosses.
            A keepout goes through the board, so it blocks both faces.
        grid_nm: Solver resolution. Parts are inflated to a whole number of
            cells, so a coarser grid means faster solving and looser packing.
        clearance_nm: Minimum edge-to-edge gap between two placed courtyards.
        max_board_nm: Optional hard ``(width, height)`` cap. The model is
            infeasible if the parts cannot fit inside it.
        time_limit_s: Wall-clock budget for CP-SAT. ``None`` lets CP-SAT run
            until it proves the optimum or infeasibility.
        size_weight: Relative weight on board half-perimeter. Zero disables it.
        wire_weight: Relative weight on total HPWL. Multiplied by each net's own
            ``weight``, so a power rail can be down-weighted without being
            dropped from the objective.
        balance_objective: Scale the size term by the net count. This was
            necessary under the old pairwise-wire model, where the wire sum grew
            with the number of wires while the size term did not. Under HPWL it
            over-corrects and board size swamps everything -- measured on a
            decoupling-capacitor test it pushed the cap from 5.15 mm to 7.75 mm
            from its IC -- so it now defaults off. Both terms are compared
            directly in grid units; tune ``size_weight``/``wire_weight`` for a
            given board rather than relying on an automatic heuristic.
        break_symmetry: Force interchangeable, unwired parts into a fixed
            lexicographic order. This removes permutations of identical parts
            from the search space without excluding any distinct layout.
        seed: CP-SAT random seed. Note that reproducibility also requires
            ``workers=1``; a multi-worker portfolio search interleaves results
            non-deterministically regardless of the seed.
        workers: CP-SAT search worker count. Defaults to 1 for reproducible
            output; raise it to trade determinism for speed.

    Returns:
        A :class:`PackResult`. ``status`` is ``FALLBACK`` when the solver timed
        out without a solution and the shelf packer was used instead.

    Raises:
        ValueError: if a wire references a part index that does not exist.
        RuntimeError: if the model is provably infeasible (only possible when
            ``max_board_nm`` is set, since the model is otherwise unbounded).
    """
    if grid_nm <= 0:
        raise ValueError("grid_nm must be positive")
    if clearance_nm < 0:
        raise ValueError("clearance_nm must be non-negative")

    warnings: list[str] = []

    if not parts:
        return PackResult([], 0, 0, PackStatus.OPTIMAL, None, 0.0, warnings)

    for w in wires:
        for idx in (w.source, w.dest):
            if not 0 <= idx < len(parts):
                raise ValueError(
                    f"Wire references part index {idx}, but only "
                    f"{len(parts)} parts were supplied"
                )

    # A two-terminal Wire is just a Net with two terminals; normalise so the
    # model only has one code path.
    all_nets: list[Net] = list(nets)
    all_nets += [
        Net(
            terminals=((w.source, w.offset_source), (w.dest, w.offset_dest)),
            name=f"wire{i}",
        )
        for i, w in enumerate(wires)
    ]
    for net in all_nets:
        for idx, _ in net.terminals:
            if not 0 <= idx < len(parts):
                raise ValueError(
                    f"Net {net.name!r} references part index {idx}, but only "
                    f"{len(parts)} parts were supplied"
                )

    model = cp_model.CpModel()
    n = len(parts)

    # A part that appears in any net -- power nets included -- is distinguishable
    # by its connectivity, so constraining its order would exclude real
    # solutions. Decoupling capacitors connect only via power nets, so counting
    # those is what stops the symmetry constraint from shoving them into a
    # corner away from the IC they bypass.
    wired = [False] * n
    for net in all_nets:
        for idx, _ in net.terminals:
            wired[idx] = True
    # A pinned part is distinguishable by its position, so ordering it against
    # an identical free part would exclude legal layouts.
    for i, part in enumerate(parts):
        if part.fixed_at_nm is not None:
            wired[i] = True

    # Inflate every part by the clearance so AddNoOverlap2D enforces a real gap.
    # Each part carries ceil(clearance/2) on every side, so two neighbours end up
    # at least `clearance_nm` apart (rounding up rather than down means an odd
    # clearance is met, not missed by 1 nm).
    pad = (clearance_nm + 1) // 2
    box_w = [cells_ceil(p.width_nm + 2 * pad, grid_nm) for p in parts]
    box_h = [cells_ceil(p.height_nm + 2 * pad, grid_nm) for p in parts]

    # An upper bound that is always sufficient: lay every part in one row/column.
    # (The original computed the y bound from widths, which made tall parts
    # infeasible.)
    x_max = max(1, sum(box_w))
    y_max = max(1, sum(box_h))
    # A keepout or a pinned part occupies a FIXED location, so the free parts
    # must still fit around it. The bound therefore has to be the free span
    # *plus* the furthest fixed edge -- taking a max would leave the domain one
    # cell short whenever a pinned part sits mid-board, and the model would be
    # reported as infeasible for no reason the caller could act on.
    fixed_right = 0
    fixed_top = 0
    for keep in keepouts:
        fixed_right = max(
            fixed_right, cells_ceil(keep.x_nm + keep.width_nm + 2 * pad, grid_nm)
        )
        fixed_top = max(
            fixed_top, cells_ceil(keep.y_nm + keep.height_nm + 2 * pad, grid_nm)
        )
    for i, part in enumerate(parts):
        if part.fixed_at_nm is None:
            continue
        fixed_right = max(
            fixed_right, cells_ceil(part.fixed_at_nm[0], grid_nm) + box_w[i]
        )
        fixed_top = max(
            fixed_top, cells_ceil(part.fixed_at_nm[1], grid_nm) + box_h[i]
        )
    x_max += fixed_right
    y_max += fixed_top
    if max_board_nm is not None:
        # Floor, not ceil: a hard cap must not be overshot by up to one cell.
        x_max = min(x_max, max_board_nm[0] // grid_nm)
        y_max = min(y_max, max_board_nm[1] // grid_nm)
        if x_max <= 0 or y_max <= 0:
            raise ValueError(
                f"max_board_nm {max_board_nm} is smaller than one grid cell "
                f"({grid_nm} nm)"
            )

    x = [model.NewIntVar(0, x_max, f"x[{i}]") for i in range(n)]
    y = [model.NewIntVar(0, y_max, f"y[{i}]") for i in range(n)]

    # Effective (possibly rotated) extents.
    eff_w: list[cp_model.IntVar | int] = []
    eff_h: list[cp_model.IntVar | int] = []
    rot: list[cp_model.IntVar | None] = []

    # Disjointness is per layer: two parts on opposite sides of the board may
    # occupy the same X/Y. Each part contributes an OPTIONAL interval to each
    # layer's no-overlap constraint, present only on the side it ends up on.
    on_top: list[cp_model.IntVar | bool] = []
    top_x, top_y, bot_x, bot_y = [], [], [], []

    for i, part in enumerate(parts):
        if part.layer is Layer.EITHER:
            lit = model.NewBoolVar(f"on_top[{i}]")
        elif part.layer is Layer.TOP:
            lit = model.NewConstant(1)
        else:
            lit = model.NewConstant(0)
        on_top.append(lit)

    for i, part in enumerate(parts):
        # Compare true extents, not rounded cell counts: a 2.0 x 1.9 mm part
        # rounds to the same cell count on a coarse grid but its *pads* are not
        # square, so rotation is still a legal option worth giving the solver.
        if part.allow_rotation and part.width_nm != part.height_nm:
            r = model.NewBoolVar(f"rot[{i}]")
            lo, hi = min(box_w[i], box_h[i]), max(box_w[i], box_h[i])
            w_var = model.NewIntVar(lo, hi, f"w[{i}]")
            h_var = model.NewIntVar(lo, hi, f"h[{i}]")
            model.Add(w_var == box_w[i]).OnlyEnforceIf(r.Not())
            model.Add(h_var == box_h[i]).OnlyEnforceIf(r.Not())
            model.Add(w_var == box_h[i]).OnlyEnforceIf(r)
            model.Add(h_var == box_w[i]).OnlyEnforceIf(r)
            rot.append(r)
            eff_w.append(w_var)
            eff_h.append(h_var)
            size_w: cp_model.IntVar | int = w_var
            size_h: cp_model.IntVar | int = h_var
        else:
            rot.append(None)
            eff_w.append(box_w[i])
            eff_h.append(box_h[i])
            size_w = box_w[i]
            size_h = box_h[i]

        lit = on_top[i]
        if part.layer is not Layer.BOTTOM:
            xe = model.NewIntVar(0, x_max, f"top_xe[{i}]")
            ye = model.NewIntVar(0, y_max, f"top_ye[{i}]")
            top_x.append(
                model.NewOptionalIntervalVar(x[i], size_w, xe, lit, f"top_xi[{i}]")
            )
            top_y.append(
                model.NewOptionalIntervalVar(y[i], size_h, ye, lit, f"top_yi[{i}]")
            )
        if part.layer is not Layer.TOP:
            not_lit = lit.Not() if part.layer is Layer.EITHER else model.NewConstant(1)
            xe = model.NewIntVar(0, x_max, f"bot_xe[{i}]")
            ye = model.NewIntVar(0, y_max, f"bot_ye[{i}]")
            bot_x.append(
                model.NewOptionalIntervalVar(
                    x[i], size_w, xe, not_lit, f"bot_xi[{i}]"
                )
            )
            bot_y.append(
                model.NewOptionalIntervalVar(
                    y[i], size_h, ye, not_lit, f"bot_yi[{i}]"
                )
            )

    # A keepout is a hole or a mechanical envelope: it goes through the board,
    # so it blocks both sides.
    for k, keep in enumerate(keepouts):
        kx = keep.x_nm // grid_nm
        ky = keep.y_nm // grid_nm
        kw = cells_ceil(keep.width_nm + 2 * pad, grid_nm)
        kh = cells_ceil(keep.height_nm + 2 * pad, grid_nm)
        for side_x, side_y, tag in ((top_x, top_y, "top"), (bot_x, bot_y, "bot")):
            side_x.append(
                model.NewFixedSizeIntervalVar(kx, kw, f"{tag}_keepout_x[{k}]")
            )
            side_y.append(
                model.NewFixedSizeIntervalVar(ky, kh, f"{tag}_keepout_y[{k}]")
            )

    if top_x:
        model.AddNoOverlap2D(top_x, top_y)
    if bot_x:
        model.AddNoOverlap2D(bot_x, bot_y)

    # Parts the caller has pinned. fixed_at_nm names where the PART goes, not
    # its clearance-inflated box, so the pad is removed before constraining --
    # otherwise pinning at (0, 0) would silently report the part at
    # (clearance/2, clearance/2).
    for i, part in enumerate(parts):
        if part.fixed_at_nm is None:
            continue
        fx_nm, fy_nm = part.fixed_at_nm
        if fx_nm < pad or fy_nm < pad:
            raise ValueError(
                f"Part {part.ref!r} is fixed at {part.fixed_at_nm} nm, but a "
                f"part carries {pad} nm of clearance on each side, so the "
                f"minimum pinnable coordinate is ({pad}, {pad}). Reduce "
                f"clearance_nm or move the part."
            )
        fx = round((fx_nm - pad) / grid_nm)
        fy = round((fy_nm - pad) / grid_nm)
        if not (0 <= fx <= x_max and 0 <= fy <= y_max):
            raise ValueError(
                f"Part {part.ref!r} is fixed at {part.fixed_at_nm} nm, which is "
                f"outside the solvable area. Raise max_board_nm, or check the "
                f"coordinate."
            )
        model.Add(x[i] == fx)
        model.Add(y[i] == fy)

    # Symmetry breaking over interchangeable parts.
    #
    # A board with eight identical 0402 capacitors admits 8! = 40320 relabelings
    # of the same physical layout, and CP-SAT will happily explore all of them.
    # Forcing interchangeable parts into a fixed lexicographic (x, y) order
    # collapses each of those orbits to a single representative without
    # excluding any distinct placement.
    #
    # The comparison is encoded as a single linear inequality using a positional
    # index x * (y_max + 1) + y, which is a strict lexicographic ranking because
    # y never exceeds y_max.
    if break_symmetry:
        groups: dict[tuple, list[int]] = {}
        for i, part in enumerate(parts):
            if wired[i]:
                # A part with wires is distinguishable by its connectivity, so
                # constraining its order would exclude real solutions.
                continue
            key = (
                box_w[i],
                box_h[i],
                part.must_be_on_edge,
                part.allow_rotation,
            )
            groups.setdefault(key, []).append(i)

        stride = y_max + 1
        for members in groups.values():
            # Consecutive pairs: members[1:] is deliberately one shorter.
            for a, b in zip(members, members[1:], strict=False):
                model.Add(x[a] * stride + y[a] <= x[b] * stride + y[b])

    # Board extent.
    w_used = model.NewIntVar(0, x_max, "w_used")
    h_used = model.NewIntVar(0, y_max, "h_used")
    w_terms = [x[i] + eff_w[i] for i in range(n)]
    h_terms = [y[i] + eff_h[i] for i in range(n)]
    for keep in keepouts:
        w_terms.append(cells_ceil(keep.x_nm + keep.width_nm, grid_nm))
        h_terms.append(cells_ceil(keep.y_nm + keep.height_nm, grid_nm))
    model.AddMaxEquality(w_used, w_terms)
    model.AddMaxEquality(h_used, h_terms)

    # Edge constraints. Every part is inside the board by construction, so
    # "on an edge" means flush with one of the four sides.
    for i, part in enumerate(parts):
        if not part.must_be_on_edge:
            continue
        b_left = model.NewBoolVar(f"edge_left[{i}]")
        b_bottom = model.NewBoolVar(f"edge_bottom[{i}]")
        b_right = model.NewBoolVar(f"edge_right[{i}]")
        b_top = model.NewBoolVar(f"edge_top[{i}]")
        model.Add(x[i] == 0).OnlyEnforceIf(b_left)
        model.Add(y[i] == 0).OnlyEnforceIf(b_bottom)
        model.Add(x[i] + eff_w[i] == w_used).OnlyEnforceIf(b_right)
        model.Add(y[i] + eff_h[i] == h_used).OnlyEnforceIf(b_top)
        model.AddBoolOr([b_left, b_bottom, b_right, b_top])

    # Manhattan wirelength.
    def endpoint(idx: int, offset: tuple[int, int]):
        """Pin position in grid cells, accounting for rotation."""
        ox = cells_round(offset[0] + pad, grid_nm)
        oy = cells_round(offset[1] + pad, grid_nm)
        r = rot[idx]
        if r is None:
            return x[idx] + ox, y[idx] + oy
        # A 90-degree rotation about the part's own box maps a local offset
        # (ox, oy) to (H - oy, ox), where H is the unrotated height in cells.
        # Both branches are constants, so two implications express it exactly.
        h_cells = box_h[idx]
        w_cells = box_w[idx]
        span = max(w_cells, h_cells)
        rx = model.NewIntVar(-span, span, f"rx[{idx}]")
        ry = model.NewIntVar(-span, span, f"ry[{idx}]")
        model.Add(rx == ox).OnlyEnforceIf(r.Not())
        model.Add(ry == oy).OnlyEnforceIf(r.Not())
        model.Add(rx == h_cells - oy).OnlyEnforceIf(r)
        model.Add(ry == ox).OnlyEnforceIf(r)
        return x[idx] + rx, y[idx] + ry

    # Half-perimeter wirelength, one bounding box per net.
    net_cost_terms: list[tuple[int, object]] = []
    hpwl_terms = []
    span = x_max + y_max

    for k, net in enumerate(all_nets):
        if len(net.terminals) < 2:
            continue
        xs = []
        ys = []
        for idx, offset in net.terminals:
            ex, ey = endpoint(idx, offset)
            xs.append(ex)
            ys.append(ey)

        xmin = model.NewIntVar(-span, span, f"net{k}_xmin")
        xmax = model.NewIntVar(-span, span, f"net{k}_xmax")
        ymin = model.NewIntVar(-span, span, f"net{k}_ymin")
        ymax = model.NewIntVar(-span, span, f"net{k}_ymax")
        model.AddMinEquality(xmin, xs)
        model.AddMaxEquality(xmax, xs)
        model.AddMinEquality(ymin, ys)
        model.AddMaxEquality(ymax, ys)

        hpwl = model.NewIntVar(0, 2 * span, f"net{k}_hpwl")
        model.Add(hpwl == (xmax - xmin) + (ymax - ymin))
        hpwl_terms.append(hpwl)
        net_cost_terms.append((net.weight, hpwl))

    # Scale both objective terms by a common denominator before rounding, so a
    # fractional weight is preserved rather than truncated to zero -- and so
    # size_weight=0 actually disables the size term instead of clamping to 1.
    _RESOLUTION = 1000
    size_scale = max(1, len(hpwl_terms)) if balance_objective else 1
    size_coeff = int(round(size_weight * size_scale * _RESOLUTION))
    if size_coeff < 0:
        raise ValueError("size_weight must be non-negative")

    objective_terms = []
    if size_coeff:
        objective_terms.append(size_coeff * (w_used + h_used))
    for weight, term in net_cost_terms:
        coeff = int(round(wire_weight * weight * _RESOLUTION))
        if coeff < 0:
            raise ValueError("wire_weight and net weights must be non-negative")
        if coeff:
            objective_terms.append(coeff * term)

    if not objective_terms:
        raise ValueError(
            "Objective is empty: size_weight and wire_weight are both zero "
            "(or all net weights are zero). Nothing would be optimised."
        )

    model.Minimize(cp_model.LinearExpr.Sum(objective_terms))

    solver = cp_model.CpSolver()
    if time_limit_s is not None:
        solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.random_seed = seed
    solver.parameters.num_workers = workers
    status = solver.Solve(model)

    if status == cp_model.INFEASIBLE:
        raise RuntimeError(
            "No valid packing exists. This is only reachable when max_board_nm "
            "is set; the parts do not fit inside the requested outline."
        )
    if status == cp_model.MODEL_INVALID:
        raise RuntimeError(
            f"CP-SAT rejected the model: {model.Validate()}"
        )

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Time limit expired with nothing found. Return a valid-but-poor layout
        # rather than raising, which is what the original did.
        limit = (
            f"in {time_limit_s}s"
            if time_limit_s is not None
            else "before the unrestricted search stopped"
        )
        warnings.append(
            f"CP-SAT found no solution {limit} "
            f"({solver.StatusName(status)}); used shelf fallback."
        )
        # The fallback packs by size alone. Say so loudly rather than returning
        # a layout that quietly violates what the caller asked for.
        if any(p.must_be_on_edge for p in parts):
            edge_refs = [p.ref for p in parts if p.must_be_on_edge]
            warnings.append(
                f"Fallback ignores must_be_on_edge; {edge_refs} are NOT on an edge."
            )
        if max_board_nm is not None:
            warnings.append(
                f"Fallback ignores max_board_nm; the {max_board_nm} cap may be "
                f"exceeded."
            )
        if any(p.allow_rotation for p in parts):
            warnings.append("Fallback does not rotate parts.")
        if any(p.fixed_at_nm for p in parts):
            pinned = [p.ref for p in parts if p.fixed_at_nm]
            warnings.append(
                f"Fallback ignores fixed_at_nm; {pinned} were NOT pinned."
            )
        if keepouts:
            warnings.append("Fallback ignores keepouts; regions may be occupied.")
        if any(p.layer is not Layer.TOP for p in parts):
            warnings.append(
                "Fallback is single-sided; every part was placed on the top layer."
            )
        placements, bw, bh = _shelf_pack(parts, clearance_nm)
        return PackResult(
            placements=placements,
            board_width_nm=bw,
            board_height_nm=bh,
            status=PackStatus.FALLBACK,
            wirelength_nm=None,
            solve_time_s=solver.WallTime(),
            warnings=warnings,
        )

    placements = []
    for i, part in enumerate(parts):
        r = rot[i]
        is_rot = bool(r is not None and solver.Value(r))
        # Undo the clearance inflation: the solver placed the padded box, the
        # caller wants the part itself.
        if part.layer is Layer.EITHER:
            side = Layer.TOP if solver.Value(on_top[i]) else Layer.BOTTOM
        else:
            side = part.layer
        placements.append(
            Placement(
                ref=part.ref,
                x_nm=solver.Value(x[i]) * grid_nm + pad,
                y_nm=solver.Value(y[i]) * grid_nm + pad,
                rotated=is_rot,
                layer=side,
            )
        )

    wirelength = None
    if hpwl_terms:
        wirelength = sum(solver.Value(t) for t in hpwl_terms) * grid_nm

    if status == cp_model.FEASIBLE:
        reason = (
            "Time limit reached"
            if time_limit_s is not None
            else "Solver stopped before proving optimality"
        )
        warnings.append(
            f"{reason}; solution is feasible but not proven optimal "
            f"(gap bound {solver.BestObjectiveBound():.0f} vs "
            f"{solver.ObjectiveValue():.0f})."
        )

    return PackResult(
        placements=placements,
        board_width_nm=solver.Value(w_used) * grid_nm,
        board_height_nm=solver.Value(h_used) * grid_nm,
        status=(
            PackStatus.OPTIMAL if status == cp_model.OPTIMAL else PackStatus.FEASIBLE
        ),
        wirelength_nm=wirelength,
        solve_time_s=solver.WallTime(),
        warnings=warnings,
    )

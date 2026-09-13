"""Copper routing: turn placed pads into tracks and vias.

Before this module the pipeline emitted a board whose pads carried net numbers
and whose copper was empty. KiCad opens such a file and draws a ratsnest --
thin lines showing what *should* connect -- which looks like a routed board at
a glance and is not one. Nothing in the project routed anything; placement was
the end of the line.

This is a two-layer grid maze router: A* over a uniform lattice with an
explicit via cost, nets routed one at a time, each net grown from its first
terminal outward so later terminals connect to the nearest point of the tree
already laid rather than back to the first pad.

**It is not a competitive autorouter and does not pretend to be.** A uniform
grid does not land on the pins of a fine-pitch package, and the two ways that
bites are answered separately. A keep-out rounded outward to whole lattice
cells seals pads that are not actually crowded, so the pad keep-out is exact
(``span`` in :func:`route`). A pad centre that misses the lattice by less than
a grid step can still have every node around it owned by a neighbour, so such a
pad gets an *escape*: a short piece of real copper from its exact centre out to
a node it may legally own, after which the maze search is unchanged. That is a
fanout, and the prior art is named where it is implemented. What no escape can
do is invent a channel that does not exist, so the contract below is unchanged.
The order-dependence a sequential router suffers is answered with a bounded
rip-up-and-retry pass:
when a net is blocked by copper an earlier net laid, the router lifts the
offending nets, routes the blocked one, and re-routes what it lifted --
deterministically, inside the same expansion budget, a bounded number of
times. Pads are never ripped, and a board can still be genuinely out of
channels. So the contract stays honesty, not completeness: every net it
cannot route is named
in :attr:`RouteResult.unrouted` and left as ratsnest for a human to finish. A
router that silently dropped a connection would be worse than no router --
that is the same bug class :mod:`silkscreen.kicad` guards against, where the
run reports success and the board is wrong.

**Coordinate frame.** This module works entirely in the solver's Y-up frame,
like :mod:`silkscreen.packing`. The flip to KiCad's Y-down frame happens once,
in the emitter, alongside the flip already applied to footprint anchors.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from .packing import Layer
from .units import mm

__all__ = [
    "RoutePad",
    "Track",
    "Via",
    "RouteResult",
    "route",
    "DEFAULT_MAX_RIPUPS",
    "DEFAULT_ROUTE_GRID_NM",
    "DEFAULT_TRACK_WIDTH_NM",
    "DEFAULT_ROUTE_CLEARANCE_NM",
    "DEFAULT_EDGE_CLEARANCE_NM",
]

#: Routing lattice pitch. 0.25 mm divides the 0.5 mm pitch of the finest
#: package these footprints generate (LQFP), so adjacent pins land on distinct
#: nodes instead of collapsing onto one.
DEFAULT_ROUTE_GRID_NM = mm(0.25)

#: Track width. 0.2 mm carries a few hundred milliamps on 1 oz copper and is
#: above every mainstream fab's minimum, so a board using it is orderable.
DEFAULT_TRACK_WIDTH_NM = mm(0.2)

#: Copper-to-copper clearance the router keeps. Deliberately equal to the
#: track width so the two together fit inside twice the grid pitch.
DEFAULT_ROUTE_CLEARANCE_NM = mm(0.2)

#: Copper-to-board-edge clearance. A separate rule from copper-to-copper, with
#: a separate physical cause: the outline is cut by a routing bit, and what has
#: to be kept clear is not another net but the kerf and the tolerance on where
#: that cut actually lands.
#:
#: 0.5 mm is KiCad's own board-setup default, and this is the number KiCad's
#: DRC grades an emitted board against, because the file carries no design
#: settings block: ``#define DEFAULT_COPPEREDGECLEARANCE   0.5     // clearance
#: between copper items and edge cuts`` in ``include/board_design_settings.h``
#: (registered as ``rules.min_copper_edge_clearance`` in
#: ``pcbnew/board_design_settings.cpp``). Measured on a generated LQFP-64
#: board, ``kicad-cli pcb drc`` reported the constraint verbatim as "board
#: setup constraints edge clearance 0.5000 mm".
#:
#: It has a fabricator's reason as well as a tool's. JLCPCB's published
#: capability is "Copper clearance from routed board edges: >= 0.2 mm" against
#: a "Dimension tolerance for routed board edges: +/-0.2 mm (regular
#: precision)" -- so copper laid at exactly the stated 0.2 mm minimum is inside
#: the cut on a worst-case pass. KiCad's 0.5 mm is that minimum plus that
#: tolerance plus a little, which is why it is a defensible default rather than
#: a number copied off a screenshot.
DEFAULT_EDGE_CLEARANCE_NM = mm(0.5)

#: Via pad and hole. KiCad 8's own default board constraints are a 0.5 mm
#: minimum diameter and a 0.3 mm minimum hole, and the emitted board carries
#: no design-settings block, so a reader gets those defaults and every via
#: we wrote at 0.4/0.2 came back as a DRC error. 0.6/0.3 clears them and is
#: a standard low-cost fab offering rather than a fine-pitch upcharge.
DEFAULT_VIA_DIAMETER_NM = mm(0.6)
DEFAULT_VIA_DRILL_NM = mm(0.3)

#: What a layer change costs, in grid steps. High enough that the router keeps
#: a net on one layer when it can, low enough that it will hop to get through.
_VIA_COST_STEPS = 12

#: Refuse rather than grind: a lattice this size means the grid is far too fine
#: for the board, and searching it would take minutes for a worse result.
_MAX_NODES = 4_000_000

#: Total A* node expansions one :func:`route` call may spend, across all nets.
#:
#: _MAX_NODES bounds the size of the lattice; this bounds the work done on it,
#: which is a different thing. An unroutable net is the expensive case: A* only
#: returns None once it has exhausted everything reachable, and it pays that in
#: full for every net that fails. Measured before this existed, a 250x200 mm
#: board with four blocked nets sat in route() for 76 seconds, and nothing
#: stopped it -- the node guard does not fire until roughly 350 mm square.
#:
#: A count, not a clock. This module promises that the same design routes the
#: same way twice, and test_routing_is_deterministic holds it to that; a
#: wall-clock cutoff would make the copper depend on how busy the machine was.
#:
#: Sized from measurement rather than guessed. Boards that route successfully
#: spend 72 expansions (12x12 mm, 6 nets) to 379k (250x200 mm, 20 nets, far
#: larger than anything this pipeline generates); the search runs at roughly
#: 35k expansions a second. 400k therefore clears every board measured that
#: could finish, leaves about three orders of magnitude of headroom over a
#: realistic one, and caps the pathological case near ten seconds.
DEFAULT_MAX_EXPANSIONS = 400_000

#: The most any single net may spend out of that. Without it one hopeless net
#: drains the whole budget on its own and every net behind it is reported
#: unrouted having never been tried -- honest, but needlessly bad copper. The
#: largest single search on a board that did finish measured 125,626, so this
#: sits above every success seen and well below the total.
DEFAULT_MAX_EXPANSIONS_PER_NET = 150_000

#: How many rip-up rounds one :func:`route` call may spend, across all nets.
#: Each round is one blocked net probing for the copper in its way, lifting
#: it, and re-routing it. The expansion budget already bounds the *work*; this
#: bounds the *churn*, so a capacity-starved board settles instead of two nets
#: trading one channel until the budget dies.
DEFAULT_MAX_RIPUPS = 8

#: The most times any single net may be the one doing the ripping. Two is
#: enough to escape the measured order traps; more mostly re-fails the same
#: geometry with less budget.
_MAX_RIPS_PER_NET = 2

#: What standing on another net's committed copper costs in a rip-up probe,
#: in grid steps per node. High enough that the probe prefers a clean detour
#: of many millimetres over crossing a channel, low enough that it still
#: finds the crossing when no detour exists.
_RIP_COST_STEPS = 25

#: How far a pad-escape stub may reach, in grid steps of Manhattan distance.
#:
#: A pad whose centre does not land on the lattice can have every node near it
#: sealed by a neighbour's clearance -- a phase problem, not a placement one --
#: and the answer is to leave the pad on a short segment of real copper and
#: join the lattice further out. That is a *fanout*, and it is FreeRouting's
#: name for it too: ``BatchFanout.fanout_pass`` in
#: ``src_v19/main/java/app/freerouting/autoroute/BatchFanout.java`` routes one
#: escape per SMD pin, as its own pass, before the batch autorouter runs.
#:
#: Sized from measurement, not guessed. On the LQFP-32 board this repo's own
#: emitter builds, the nearest node no neighbouring pad's clearance owns is
#: 1.225 mm out along the pad's own row and 0.125 mm across it -- 5.4 grid
#: steps of Manhattan distance at the 0.25 mm default. Eight leaves headroom
#: for a coarser lattice without letting a "stub" become a route: the escape
#: is straight copper that no maze search checked for a better way round, so
#: it must stay short enough that the search still owns the routing decisions.
_MAX_ESCAPE_STEPS = 8

#: The two layers this router uses, in the order it prefers them.
_LAYERS: tuple[Layer, ...] = (Layer.TOP, Layer.BOTTOM)

#: Marks a node no net may use, where two nets' pad clearances overlap.
_CONTESTED = "\x00contested"


@dataclass(frozen=True)
class RoutePad:
    """One pad to route to, in absolute Y-up board coordinates."""

    net: str
    x_nm: int
    y_nm: int
    w_nm: int
    h_nm: int
    layer: Layer = Layer.TOP
    #: A plated through-hole pad exists on *every* copper layer, whichever
    #: side its part sits on. ``layer`` is then only the side the part was
    #: placed on: the keep-out is reserved on both layers, the pad is reachable
    #: from both, and no via may land inside its annulus. Leaving this False
    #: for a hole was a real short -- the router kept the back layer free and
    #: laid a foreign net straight through the barrel.
    through_hole: bool = False
    #: For error messages only; never used in geometry.
    ref: str = ""
    number: str = ""

    def copper_layers(self, layers: tuple[Layer, ...]) -> tuple[Layer, ...]:
        """The routing layers this pad's copper occupies, out of ``layers``."""
        if self.through_hole:
            return layers
        own = Layer.TOP if self.layer is not Layer.BOTTOM else Layer.BOTTOM
        return (own,) if own in layers else ()


@dataclass(frozen=True)
class Track:
    """A straight copper segment on one layer, in the Y-up frame."""

    start_x_nm: int
    start_y_nm: int
    end_x_nm: int
    end_y_nm: int
    layer: Layer
    net: str
    width_nm: int

    @property
    def length_nm(self) -> int:
        return abs(self.end_x_nm - self.start_x_nm) + abs(
            self.end_y_nm - self.start_y_nm
        )


@dataclass(frozen=True)
class Via:
    """A through via joining the two copper layers."""

    x_nm: int
    y_nm: int
    net: str
    diameter_nm: int
    drill_nm: int


@dataclass
class RouteResult:
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    #: Nets fully connected by the tracks above.
    routed: list[str] = field(default_factory=list)
    #: Nets left as ratsnest, each with the reason it could not be finished.
    unrouted: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    track_width_nm: int = DEFAULT_TRACK_WIDTH_NM

    @property
    def routed_length_nm(self) -> int:
        return sum(t.length_nm for t in self.tracks)

    @property
    def completion(self) -> float:
        """Fraction of routable nets that came out fully connected."""
        total = len(self.routed) + len(self.unrouted)
        return 1.0 if total == 0 else len(self.routed) / total

    def summary(self) -> str:
        return (
            f"{len(self.routed)}/{len(self.routed) + len(self.unrouted)} nets routed, "
            f"{len(self.tracks)} tracks, {len(self.vias)} vias, "
            f"{self.routed_length_nm / 1_000_000:.1f} mm of copper"
        )


def _disc(radius_nm: int, grid_nm: int) -> tuple[tuple[int, int], ...]:
    """Grid offsets strictly inside ``radius_nm`` of the origin node.

    Strictly inside, not within: a neighbour at exactly the required clearance
    is legal, and rounding it out would cost real routing channels.
    """
    reach = int(radius_nm // grid_nm)
    offsets = [
        (dx, dy)
        for dx in range(-reach, reach + 1)
        for dy in range(-reach, reach + 1)
        if math.hypot(dx * grid_nm, dy * grid_nm) < radius_nm
    ]
    return tuple(sorted(offsets))


@dataclass(frozen=True)
class _Stub:
    """One pad's escape onto the lattice: real copper, off the lattice.

    Two axis-aligned legs from the pad's exact centre to the lattice node the
    maze search will use as that pad's port. Either leg may be zero length, so
    this covers the straight case too.

    The pad centre, not a rounded node, is where it starts, which is how a
    shape-based router anchors a track: ``SOLID::Anchor`` in
    ``pcbnew/router/pns_solid.cpp`` returns ``m_pos`` -- the pad's own
    position -- and PNS begins the trace there, never at a quantised point.

    ``width_nm`` may be narrower than the routing track. That is FreeRouting's
    neckdown: ``Pin.get_trace_neckdown_halfwidth`` in
    ``src_v19/main/java/app/freerouting/board/Pin.java`` returns
    ``max(0.5 * get_min_width(layer) - 1, 1)`` and
    ``MazeSearchAlgo.expand_to_room_doors`` applies it as
    ``half_width = min(half_width, neckdown_half_width)`` when the door is a
    start pin, so a trace entering or leaving a pad narrower than itself is
    thinned rather than refused.
    """

    net: str
    layer: Layer
    #: Pad centre, corner, port node -- in order, in absolute Y-up nm.
    points: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]
    width_nm: int
    #: For the failure messages only; never used in geometry.
    ref: str = ""
    number: str = ""

    def legs(self) -> tuple[tuple[int, int, int, int], ...]:
        """The two legs as ``(x0, y0, x1, y1)``, in nm."""
        a, b, c = self.points
        return ((a[0], a[1], b[0], b[1]), (b[0], b[1], c[0], c[1]))

    @property
    def length_nm(self) -> int:
        a, b, c = self.points
        return (
            abs(b[0] - a[0]) + abs(b[1] - a[1])
            + abs(c[0] - b[0]) + abs(c[1] - b[1])
        )


def _box(
    ax: int, ay: int, bx: int, by: int
) -> tuple[int, int, int, int]:
    """An axis-aligned segment (or point) as ``(lo_x, hi_x, lo_y, hi_y)``."""
    return (min(ax, bx), max(ax, bx), min(ay, by), max(ay, by))


def _far_enough(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int], need_nm: int
) -> bool:
    """Are two axis-aligned boxes at least ``need_nm`` apart, centre geometry?

    Both arguments are ``(lo_x, hi_x, lo_y, hi_y)``, which is a segment, a
    point and a pad rectangle alike -- one formula for all three, which is why
    escapes are restricted to axis-aligned legs: for axis-aligned geometry the
    per-axis overhangs give the *exact* distance, and an approximation is
    precisely how an off-lattice segment passes a clearance test and still
    shorts two pads in copper.

    Integers throughout, comparing squares rather than taking a root, so the
    decision cannot turn on a float rounding mode -- the reasoning the pad
    keep-out's own ``span`` states inside :func:`route`. The comparison is
    ``>=``: a neighbour at exactly the required clearance is legal, the rule
    :func:`_disc` already states for copper.
    """
    ex = max(0, b[0] - a[1], a[0] - b[1])
    ey = max(0, b[2] - a[3], a[2] - b[3])
    return ex * ex + ey * ey >= need_nm * need_nm


def _terminal_counts(pads: list[RoutePad]) -> dict[str, int]:
    """How many pads each named net has. Unnetted pads are obstacles, not nets."""
    counts: dict[str, int] = {}
    for pad in pads:
        if pad.net:
            counts[pad.net] = counts.get(pad.net, 0) + 1
    return counts


def route(
    pads: list[RoutePad],
    *,
    min_x_nm: int,
    min_y_nm: int,
    max_x_nm: int,
    max_y_nm: int,
    grid_nm: int = DEFAULT_ROUTE_GRID_NM,
    track_width_nm: int = DEFAULT_TRACK_WIDTH_NM,
    clearance_nm: int = DEFAULT_ROUTE_CLEARANCE_NM,
    edge_clearance_nm: int = DEFAULT_EDGE_CLEARANCE_NM,
    via_diameter_nm: int = DEFAULT_VIA_DIAMETER_NM,
    via_drill_nm: int = DEFAULT_VIA_DRILL_NM,
    two_layer: bool = True,
    max_expansions: int = DEFAULT_MAX_EXPANSIONS,
    max_expansions_per_net: int = DEFAULT_MAX_EXPANSIONS_PER_NET,
    max_ripups: int = DEFAULT_MAX_RIPUPS,
) -> RouteResult:
    """Route every multi-terminal net over the placed pads.

    Args:
        pads: Every pad on the board, absolute, in the solver's Y-up frame.
            Pads whose net is empty are obstacles and nothing else.
        min_x_nm..max_y_nm: The rectangle copper may occupy, normally the board
            outline. Nodes outside it are unusable, so no track leaves the
            board.
        edge_clearance_nm: How far the router's own copper stays *inside* that
            rectangle, measured from the copper's edge to the rectangle's line.
            Zero is not the pre-2026-09-08 behaviour and cannot be: it still
            keeps half a track (and half a via barrel) in, so copper touches
            the outline at worst rather than crossing it, which is what the
            old lattice did. Pads are not moved by this rule -- an edge
            connector is a legitimate design -- but a pad it makes unreachable
            is named, never dropped.
        two_layer: Allow the back copper layer and vias. With this off the
            router is single-layer and will leave more nets unrouted, which is
            the honest outcome rather than a crossing short.
        max_expansions: Total search effort for the whole call. Nets still
            unrouted when it runs out are named like any other failure rather
            than silently skipped, so the result says the board was not
            finished instead of implying there was nothing left to do.
        max_expansions_per_net: The most any one net may take out of that, so a
            single hopeless net cannot starve the ones behind it.
        max_ripups: Rip-up-and-retry rounds allowed after the first pass. Zero
            disables the pass entirely, which is the pre-rip-up router.

    Returns:
        A :class:`RouteResult` whose ``unrouted`` names every net that did not
        come out fully connected, with the reason. Callers must surface that;
        a partially routed board reported as routed is the failure this whole
        module is written to avoid.
    """
    result = RouteResult(track_width_nm=track_width_nm)

    # Every net that would have been routed had the run got that far. Refusing
    # to route is still a result about these nets, and a refusal that named
    # none of them reported 0/0 -- which ``completion`` reads as 100% and
    # ``summary`` prints as "0/0 nets routed" on a board with no copper at all.
    # Naming them here is the same contract the per-net failures keep.
    def refuse(reason: str) -> RouteResult:
        for net, count in _terminal_counts(pads).items():
            if count >= 2:
                result.unrouted[net] = reason
        result.warnings.append(reason)
        return result

    if max_x_nm <= min_x_nm or max_y_nm <= min_y_nm:
        return refuse("board area is empty; nothing routed")

    nx = int((max_x_nm - min_x_nm) // grid_nm) + 1
    ny = int((max_y_nm - min_y_nm) // grid_nm) + 1
    layers = _LAYERS if two_layer else (Layer.TOP,)
    if nx * ny * len(layers) > _MAX_NODES:
        return refuse(
            f"routing grid would be {nx}x{ny} nodes, over the {_MAX_NODES} node "
            f"budget; skipped routing (raise grid_nm to route this board)"
        )

    def node_x(i: int) -> int:
        return min_x_nm + i * grid_nm

    def node_y(j: int) -> int:
        return min_y_nm + j * grid_nm

    # ---- the board edge --------------------------------------------------
    # The rectangle above is ``Edge.Cuts`` itself -- board.py draws the outline
    # on exactly these four lines -- and until now the lattice ran right out to
    # it. A track on the outermost column therefore had its *centreline* on the
    # board edge and half its width off the board, and the router had no rule
    # that could notice: every clearance in this module is copper-to-copper,
    # and the edge is not copper.
    #
    # Latent rather than harmless, and it stayed invisible only because the
    # router was bad. Measured on a generated LQFP-64 board, 2026-09-08:
    # ``kicad-cli pcb drc --severity-error`` reported ten
    # ``copper_edge_clearance`` violations, three of them at "actual 0.0000 mm"
    # -- copper across the outline. The escape work that landed the same day
    # completes four times as many nets, so the outer columns get used for the
    # first time, which is what brought it out. A board that routes well is
    # exactly the board that reaches its own edge.
    #
    # The rule is KiCad's ``EDGE_CLEARANCE_CONSTRAINT``. Two things about how
    # KiCad measures it are load-bearing here and were checked against its
    # source rather than assumed: it is measured from the ``Edge.Cuts``
    # *centreline*, because ``DRC_TEST_PROVIDER_EDGE_CLEARANCE::Run`` in
    # ``pcbnew/drc/drc_test_provider_edge_clearance.cpp`` does
    # ``if( item->IsOnLayer( Edge_Cuts ) ) stroke.SetWidth( 0 );`` before
    # taking ``edge->GetEffectiveShape( Edge_Cuts )``; and it is compared
    # against the copper item's own shape, ``itemShape->Collide( shape.get(),
    # std::max( 0, minClearance - m_epsilon ), &actual, &pos )`` in
    # ``testAgainstEdge`` -- so the quantity is edge-of-copper to centreline,
    # which is the quantity computed below.
    #
    # The shape of the fix is FreeRouting's. There the outline is a first-class
    # obstacle item, ``BoardOutline`` in
    # ``src_v19/main/java/app/freerouting/board/BoardOutline.java`` (whose
    # ``is_obstacle`` is true for every item that is not another outline), and
    # ``ShapeSearchTree.calculate_tree_shapes(BoardOutline)`` inserts it on
    # *every* layer as the outline polyline offset by
    # ``half_width + clearance_compensation_value( clearance_class_no(), 0 )``
    # -- the boundary inflated by its own clearance class, on all layers,
    # before any net is routed. This is that, in a raster: the inflation is
    # done once, exactly, in integers, and the nodes it swallows are simply not
    # nodes any more. Two radii, because a barrel is wider than a track and the
    # neighbour's shape sets the spacing -- the same rule ``via_disc`` states
    # for copper.
    #
    # Exact, never rounded outward. Rounding a keep-out out to whole cells is
    # what sealed the pads on fine-pitch parts (see ``span`` below); the bands
    # here are the largest index range whose node genuinely keeps the distance,
    # by integer ceil and floor of the real inequality, with no float anywhere.
    def band(need_nm: int) -> tuple[int, int, int, int]:
        """Index bounds ``(i_lo, i_hi, j_lo, j_hi)`` at least ``need_nm`` in.

        Node ``i`` is in when ``node_x(i) - min_x_nm >= need_nm`` and
        ``max_x_nm - node_x(i) >= need_nm``; substituting
        ``node_x(i) = min_x_nm + i * grid_nm`` those become
        ``i >= need_nm / grid_nm`` and
        ``i <= (max_x_nm - min_x_nm - need_nm) / grid_nm``, so the bounds are a
        ceil and a floor of exact integer ratios. ``-(-a // b)`` ceils.
        """
        return (
            max(0, -(-need_nm // grid_nm)),
            min(nx - 1, (max_x_nm - min_x_nm - need_nm) // grid_nm),
            max(0, -(-need_nm // grid_nm)),
            min(ny - 1, (max_y_nm - min_y_nm - need_nm) // grid_nm),
        )

    # A track's centreline may sit one half-width closer than its copper may;
    # a barrel is its own radius wide and pierces every layer.
    track_i_lo, track_i_hi, track_j_lo, track_j_hi = band(
        edge_clearance_nm + track_width_nm // 2
    )
    via_i_lo, via_i_hi, via_j_lo, via_j_hi = band(
        edge_clearance_nm + via_diameter_nm // 2
    )

    def track_room(i: int, j: int) -> bool:
        """May a track of ours stand on this node without crowding the edge?"""
        return track_i_lo <= i <= track_i_hi and track_j_lo <= j <= track_j_hi

    def via_room(i: int, j: int) -> bool:
        """May a via barrel stand here? Wider, so a stricter band."""
        return via_i_lo <= i <= via_i_hi and via_j_lo <= j <= via_j_hi

    def edge_gap(box: tuple[int, int, int, int]) -> int:
        """Nearest distance from an axis-aligned box to the rectangle's sides.

        Negative when the box crosses a side. ``box`` is
        ``(lo_x, hi_x, lo_y, hi_y)``, the shape :func:`_box` produces, so one
        formula serves a pad rectangle and an escape leg alike -- and it is the
        same edge-of-copper-to-centreline quantity KiCad's DRC measures.
        """
        return min(
            box[0] - min_x_nm, max_x_nm - box[1],
            box[2] - min_y_nm, max_y_nm - box[3],
        )

    if track_i_lo > track_i_hi or track_j_lo > track_j_hi:
        return refuse(
            f"the routable rectangle is "
            f"{(max_x_nm - min_x_nm) / 1_000_000:.2f} x "
            f"{(max_y_nm - min_y_nm) / 1_000_000:.2f} mm, which leaves no "
            f"lattice node at all once the "
            f"{edge_clearance_nm / 1_000_000:.2f} mm copper-to-board-edge "
            f"clearance and half a "
            f"{track_width_nm / 1_000_000:.2f} mm track are kept off every "
            f"side; nothing routed"
        )

    # ---- obstacles -------------------------------------------------------
    # A node is reserved for at most one net. Reserved-for-me is free; anything
    # else is a wall. Two nets' pad clearances overlapping leaves the node
    # contested, and no net may cross it.
    reserved: dict[Layer, dict[tuple[int, int], str]] = {
        layer: {} for layer in layers
    }
    pad_margin = clearance_nm + track_width_nm // 2

    def reserve(layer: Layer, key: tuple[int, int], net: str) -> None:
        if layer not in reserved:
            return
        held = reserved[layer].get(key)
        if held is None:
            reserved[layer][key] = net
        elif held != net:
            reserved[layer][key] = _CONTESTED

    def span(pad: RoutePad, extra_nm: int) -> tuple[range, range]:
        """Lattice nodes whose centre lies inside the pad grown by ``extra_nm``.

        The keep-out is the pad rectangle grown by ``extra_nm`` on each axis and
        a node is in it only if the node's own centre is *strictly* inside --
        the rule :func:`_disc` already states for copper ("a neighbour at
        exactly the required clearance is legal, and rounding it out would cost
        real routing channels"), applied to pads too.

        This used to round the keep-out outward to whole lattice cells
        (``floor``/``ceil``), which is up to one grid step -- 0.25 mm -- of
        clearance nobody asked for, in every direction, around every pad. On a
        0.5 mm-pitch package that is fatal rather than merely wasteful: an
        LQFP's neighbouring pads then own the node one step off *this* pad's
        centre on all four sides, every port is sealed, and the net is reported
        "blocked by pad clearance" when the board has a perfectly good escape
        lane straight out along the pad's own row. Measured on a 32-pin LQFP
        board built by this repo's own emitter: 19 of 54 pads sealed, 2 of 22
        nets routed.

        Real routers keep pad clearance exact for this reason and do not
        rasterise it at all. KiCad's push-and-shove router builds a pad's
        keep-out as the pad shape grown by "clearance plus half the trace
        width" and nothing else -- ``BuildHullForPrimitiveShape`` in
        ``pcbnew/router/pns_utils.cpp`` (``cl = aClearance + (
        aWalkaroundThickness + 1 ) / 2``; ``SH_RECT`` becomes
        ``OctagonalHull( rect->GetPosition(), rect->GetSize(), cl, 0 )``),
        which is exactly ``pad_margin`` here, per axis, unrounded. FreeRouting
        does the same by enlarging the item's own convex shape by the clearance
        and testing exact overlap -- ``ShapeSearchTree.overlaps_with_clearance``
        and ``clearanceCompensationValue`` in
        ``src/main/java/app/freerouting/board/searchtree/ShapeSearchTree.java``.
        Neither ever rounds a keep-out outward to a cell boundary. A raster
        cannot be exact, but it can stop being conservative in the one
        direction that costs escape lanes, and that is what this does.

        Integer arithmetic throughout, doubled so an odd pad dimension needs no
        division: node ``i`` is blocked on X when
        ``2*|node_x(i) - pad.x_nm| < pad.w_nm + 2*extra_nm``. Floats here would
        put the seal/unseal decision on a rounding mode, which is the unit
        confusion :mod:`silkscreen.units` exists to keep out of the pipeline.

        One thing the outward rounding *did* buy, and which is kept explicitly:
        a keep-out narrower than a grid step can fall between two nodes and mark
        nothing at all, which does not merely cost a channel -- it makes the pad
        invisible, and copper steps straight over it. So a band that comes out
        empty is widened to the single nearest node instead. Both bands are then
        always non-empty, and since every move is one step along one axis, any
        move crossing the keep-out has to land on a node inside both bands,
        which is marked. At the default 0.25 mm grid this never fires: the
        smallest keep-out is ``2 * pad_margin`` = 0.6 mm wide before the pad
        itself is counted. It exists for a caller that hands in a coarse grid,
        which :func:`route` allows and even recommends for a large board.
        """

        def bounds(centre_nm: int, size_nm: int, origin_nm: int, count: int) -> range:
            width = size_nm + 2 * extra_nm
            offset = 2 * (centre_nm - origin_nm)
            step = 2 * grid_nm
            # Smallest index strictly above the low edge, largest strictly
            # below the high edge. ``//`` floors and ``-(-a // b)`` ceils, so an
            # index landing exactly on an edge is excluded either way -- that
            # node sits at exactly the required clearance, which is legal.
            lo = (offset - width) // step + 1
            hi = -((offset + width) // -step) - 1
            if lo > hi:
                # Narrower than the lattice can resolve: round it to the nearest
                # node rather than lose it. Half-up in integers, so it does not
                # depend on a float rounding mode.
                lo = hi = (offset + grid_nm) // step
            return range(max(0, lo), min(count - 1, hi) + 1)

        return (
            bounds(pad.x_nm, pad.w_nm, min_x_nm, nx),
            bounds(pad.y_nm, pad.h_nm, min_y_nm, ny),
        )

    # Nodes where a via barrel would sit inside a plated hole's annulus. A
    # via there is a drill through a drill, whatever net owns either, so the
    # search may not change layer on any of them. Foreign nets are already
    # walled off by the keep-out on both layers; this is what stops a net
    # dropping a via into its *own* header pin.
    holes: set[tuple[int, int]] = set()

    # The pad keep-out above is sized for a *track*: half a track plus the
    # clearance. A via is not a track -- the barrel is three times as wide and
    # it pierces every layer -- so a via centre one lattice step outside that
    # halo still sits deep inside its own clearance of the pad. Nothing else
    # catches it: `via_fits` checks committed copper and other vias, never the
    # pads. On a 0.5 mm-pitch package that is a via on a foreign pad, which
    # KiCad reports as `hole_clearance` and `solder_mask_bridge` and the fab
    # builds as a short. Vias therefore get their own, wider map, keyed by
    # node alone rather than by layer, because a barrel is on every layer.
    via_margin = clearance_nm + via_diameter_nm // 2
    via_keepout: dict[tuple[int, int], str] = {}

    def keep_via_out(key: tuple[int, int], net: str) -> None:
        held = via_keepout.get(key)
        if held is None:
            via_keepout[key] = net
        elif held != net:
            via_keepout[key] = _CONTESTED

    for pad in sorted(pads, key=lambda p: (p.net, p.ref, p.number, p.x_nm, p.y_nm)):
        i_range, j_range = span(pad, pad_margin)
        # A through-hole pad is copper on every layer, so its keep-out is
        # too; an SMD pad reserves only the side it sits on.
        for layer in pad.copper_layers(layers):
            for i in i_range:
                for j in j_range:
                    reserve(layer, (i, j), pad.net or _CONTESTED)
        if pad.copper_layers(layers):
            i_range, j_range = span(pad, via_margin)
            for i in i_range:
                for j in j_range:
                    keep_via_out((i, j), pad.net or _CONTESTED)
        if pad.through_hole:
            i_range, j_range = span(pad, via_margin)
            for i in i_range:
                for j in j_range:
                    holes.add((i, j))

    # ---- ports and pad escapes -------------------------------------------
    # Each pad's port is normally the lattice node nearest its centre, which
    # lies inside its own copper. Forcing it back to the pad's own net undoes
    # any contest written above: a track ending on the pad it belongs to is not
    # a violation, whatever else crowds that node.
    #
    # That is true only while the nearest node is a node the pad is *allowed*
    # to own. A pad centre that does not land on the lattice can have its
    # nearest node sit inside a neighbouring pad's clearance instead, and
    # forcing it anyway put the start of a track 0.125 mm from a foreign pad
    # against a 0.2 mm rule -- measured on the LQFP-32 board this repo's own
    # emitter builds, and reported by KiCad's own DRC as a `clearance`
    # violation on net NET1. Worse, on a 0.5 mm-pitch row the nodes *around*
    # that one belong to the neighbours too, so the net could not leave the pad
    # at all: 11 of 44 netted pads sealed, 13 of 22 nets unrouted, and the
    # reason given ("blocked by pad clearance") pointed at a placement that was
    # fine. The lattice was simply 0.125 mm out of phase with the pad row.
    #
    # So a pad that cannot use its nearest node gets an *escape*: a short piece
    # of real copper from its exact centre out to a node it may own, after
    # which the maze search is unchanged. This is a fanout, and the name is
    # FreeRouting's: ``BatchFanout.fanout_pass`` routes one escape per SMD pin
    # as its own pass before the batch autorouter, and ``isPinEscaped`` calls a
    # pin escaped when it has a contact "with no clearance violations" --
    # which is the definition used here too, and the metric this change is
    # measured by.
    #
    # A port is a *group* of nodes, one per layer the pad's copper is on: an
    # SMD pad is one node, a plated hole is the same (i, j) on both layers.
    # Reaching any node of the group reaches the pad, and the whole group
    # then joins the net's tree -- the hole itself is the layer change, so the
    # router never has to buy a via to cross at a header pin.
    ports: dict[str, list[_Port]] = {}
    stubs: dict[_Node, _Stub] = {}
    #: Nets a pad could not escape from, and why. Kept apart from the port
    #: bookkeeping because the reason has to survive to the result: a pad with
    #: no legal way onto the lattice is a different fact from a net with one
    #: pad or a channel blocked by an earlier net, and it sends a person to a
    #: different fix (move the part, or coarsen the lattice off the pad pitch).
    escape_failures: dict[str, str] = {}

    def node_ok(pad: RoutePad, i: int, j: int) -> bool:
        """May ``pad`` own the node ``(i, j)`` outright?

        Read straight off the exact pad rasterisation above rather than
        recomputed: ``reserved`` already holds, per layer, the net whose pad
        clearance covers each node, ``_CONTESTED`` where two disagree, and
        nothing where the node is free. Same-net is legal because same-net
        copper owes itself no clearance.
        """
        if not (0 <= i < nx and 0 <= j < ny):
            return False
        # The board edge is checked here as well as in the search, because this
        # is where a *port* is chosen: a port outside the band would be a
        # track's endpoint sitting on the outline, which is the violation this
        # whole rule exists to stop, and the search would never see it as a
        # move it had to make.
        if not track_room(i, j):
            return False
        return all(
            reserved[layer].get((i, j)) in (None, pad.net)
            for layer in pad.copper_layers(layers)
        )

    def stub_clears(stub: _Stub) -> bool:
        """Does this escape keep clearance from every pad and every other one?

        Exact, not rasterised. The legs are axis-aligned and every pad is an
        axis-aligned rectangle, so :func:`_far_enough` compares the true
        distance, in integers. Checking a lattice approximation instead is
        precisely how an off-grid segment passes a grid-based clearance test
        and still shorts two pads in copper.

        Only pads and other escapes are checked, and that is sufficient rather
        than lazy: escapes are chosen before any net is routed, so no other
        copper exists yet, and what comes afterwards is kept off the escape by
        the keep-outs installed below. It is the same ordering FreeRouting
        uses -- ``BatchFanout.fanout_board`` runs its passes before
        ``BatchAutorouter`` lays a single net.
        """
        for x0, y0, x1, y1 in stub.legs():
            if x0 == x1 and y0 == y1:
                continue
            leg = _box(x0, y0, x1, y1)
            # An escape is copper, so it owes the board edge exactly what a
            # lattice track owes it. Checking only the lattice would have left
            # the one piece of copper in this module that does not lie on the
            # lattice free to run out over the outline -- the same reasoning
            # ``stub_keepout`` states for copper-to-copper. Measured against
            # the leg's own extent grown by the half-width it reserves, not
            # the narrower width it is drawn at, so the check cannot be
            # loosened by a neckdown.
            if edge_gap(leg) < edge_clearance_nm + track_width_nm // 2:
                return False
            # Reserved as a full-width track even where it is drawn narrower.
            # The neckdown removes copper, never adds it, so this can only be
            # conservative -- and it keeps one half-width in every clearance
            # sum, which is what makes an escape's own port node provably safe
            # from a neighbouring escape's keep-out below.
            need = clearance_nm + track_width_nm // 2
            for other in pads:
                if other.net and other.net == stub.net:
                    continue
                if stub.layer not in other.copper_layers(layers):
                    continue
                box = (
                    other.x_nm - other.w_nm // 2, other.x_nm + other.w_nm // 2,
                    other.y_nm - other.h_nm // 2, other.y_nm + other.h_nm // 2,
                )
                if not _far_enough(leg, box, need):
                    return False
            for laid in stubs.values():
                if laid.net == stub.net or laid.layer is not stub.layer:
                    continue
                pair = need + track_width_nm // 2
                for a0, b0, a1, b1 in laid.legs():
                    if not _far_enough(leg, _box(a0, b0, a1, b1), pair):
                        return False
        return True

    def escape(
        pad: RoutePad, i0: int, j0: int
    ) -> tuple[_Stub, int, int] | None:
        """The shortest legal escape from ``pad``, with the node it lands on.

        Candidates are every node within ``_MAX_ESCAPE_STEPS`` of Manhattan
        distance, each tried with two L-shaped stubs -- along X first, then
        along Y, and the other way round. Both legs axis-aligned, because that
        is what makes :func:`stub_clears` exact, and because a diagonal escape
        measured 2.0 mm on the LQFP where the L measured 1.2 mm: leaving a pad
        along its own axis is the shape that fits between its neighbours.

        This spends no search budget, and that is a statement about the work
        rather than an exemption from it: the budget counts A* node expansions
        because the *search* is the unbounded thing, and this is not a search.
        It is a fixed sweep of at most ``2 * _MAX_ESCAPE_STEPS**2 + 2 *
        _MAX_ESCAPE_STEPS + 1`` nodes times two orientations times the pads,
        run once per board before any net is routed -- the same kind of bounded
        pre-pass as the pad rasterisation above, and like it, charged nowhere
        because it cannot run long. Measured on the LQFP-48 stress board (96
        pads, 50 of them escaping): the whole route went from 1.6 s to 8.1 s,
        of which the escapes are a small part and the extra A* is the rest,
        since four times as many nets now route at all.

        Deterministic by construction: candidates are generated in a fixed
        order and the first legal one wins, with no dependence on dict
        iteration or on how much budget was left. The order is stub length
        first, then the pad's own long axis (a 1.5 x 0.3 mm pad escapes along
        its length, which keeps the first leg inside its own copper), then the
        node indices.
        """
        # Which leg to run first, expressed as a rank so it is a tie-break and
        # never overrides a shorter stub: along the pad's own long axis, since
        # a 1.5 x 0.3 mm pad's escape wants to travel inside its own copper
        # before it crosses anything.
        first_x_rank = 0 if pad.w_nm >= pad.h_nm else 1
        width = min(track_width_nm, pad.w_nm, pad.h_nm)
        candidates: list[tuple[int, int, int, int, bool]] = []
        for di in range(-_MAX_ESCAPE_STEPS, _MAX_ESCAPE_STEPS + 1):
            for dj in range(-_MAX_ESCAPE_STEPS, _MAX_ESCAPE_STEPS + 1):
                if abs(di) + abs(dj) > _MAX_ESCAPE_STEPS:
                    continue
                i, j = i0 + di, j0 + dj
                if not node_ok(pad, i, j):
                    continue
                reach = abs(node_x(i) - pad.x_nm) + abs(node_y(j) - pad.y_nm)
                for first_x in (True, False):
                    rank = first_x_rank if first_x else 1 - first_x_rank
                    candidates.append((reach, rank, i, j, first_x))
        for _, _, i, j, first_x in sorted(candidates):
            x, y = node_x(i), node_y(j)
            stub = _Stub(
                net=pad.net,
                layer=pad.copper_layers(layers)[0],
                points=(
                    (pad.x_nm, pad.y_nm),
                    (x, pad.y_nm) if first_x else (pad.x_nm, y),
                    (x, y),
                ),
                width_nm=width,
                ref=pad.ref,
                number=pad.number,
            )
            if stub_clears(stub):
                return stub, i, j
        return None

    def pad_box(pad: RoutePad) -> tuple[int, int, int, int]:
        """The pad's own copper rectangle, as ``(lo_x, hi_x, lo_y, hi_y)``."""
        return (
            pad.x_nm - pad.w_nm // 2, pad.x_nm + pad.w_nm // 2,
            pad.y_nm - pad.h_nm // 2, pad.y_nm + pad.h_nm // 2,
        )

    def edge_only_blocks(pad: RoutePad, i0: int, j0: int) -> int:
        """Nodes within escape reach that only the board-edge band forbids.

        Diagnosis, not geometry: it runs solely on a failure, and it exists so
        the reason given names the rule that actually did the blocking. A pad
        told "a neighbouring pad's clearance" when what stopped it was the
        board edge sends a person to move the wrong part.
        """
        count = 0
        for di in range(-_MAX_ESCAPE_STEPS, _MAX_ESCAPE_STEPS + 1):
            for dj in range(-_MAX_ESCAPE_STEPS, _MAX_ESCAPE_STEPS + 1):
                if abs(di) + abs(dj) > _MAX_ESCAPE_STEPS:
                    continue
                i, j = i0 + di, j0 + dj
                if not (0 <= i < nx and 0 <= j < ny) or track_room(i, j):
                    continue
                if all(
                    reserved[layer].get((i, j)) in (None, pad.net)
                    for layer in pad.copper_layers(layers)
                ):
                    count += 1
        return count

    for pad in pads:
        if not pad.net:
            continue
        on = pad.copper_layers(layers)
        if not on:
            continue
        i = min(nx - 1, max(0, int(round((pad.x_nm - min_x_nm) / grid_nm))))
        j = min(ny - 1, max(0, int(round((pad.y_nm - min_y_nm) / grid_nm))))
        if not node_ok(pad, i, j):
            # A plated hole is copper on every layer, so its escape would have
            # to be drawn on every layer -- two pieces of copper, one of which
            # the route may never touch. Refusing in words beats inventing
            # that, and a hole is not the fine-pitch case: it is round, large,
            # and its own annulus already reaches past a lattice step.
            found = None if pad.through_hole else escape(pad, i, j)
            if found is None:
                # Three reasons, kept apart because they send a person to
                # three different fixes. A pad sitting inside the
                # copper-to-edge band is a *placement* fact and a legitimate
                # one -- an edge connector is a real design -- so it is named
                # with its measured gap and the rule it is inside, and the net
                # is refused rather than quietly dropped: routing to it would
                # put copper over the outline, and routing round it would
                # report a net connected with one pin dead.
                gap = edge_gap(pad_box(pad))
                edge_blocked = edge_only_blocks(pad, i, j)
                if gap < edge_clearance_nm:
                    reason = (
                        f"pad {pad.ref}.{pad.number} sits "
                        f"{gap / 1_000_000:.3f} mm from the board edge, inside "
                        f"the {edge_clearance_nm / 1_000_000:.2f} mm "
                        f"copper-to-edge clearance; no track may reach it "
                        f"without laying copper the outline cut would take. "
                        f"Move the part inward, or lower edge_clearance_nm."
                    )
                elif edge_blocked:
                    reason = (
                        f"pad {pad.ref}.{pad.number} has no legal way onto the "
                        f"routing lattice: every node within "
                        f"{_MAX_ESCAPE_STEPS} grid steps that it could "
                        f"otherwise have used lies inside the "
                        f"{edge_clearance_nm / 1_000_000:.2f} mm "
                        f"copper-to-board-edge clearance ({edge_blocked} such "
                        f"node(s)). Move the part inward, or lower "
                        f"edge_clearance_nm."
                    )
                else:
                    reason = (
                        f"pad {pad.ref}.{pad.number} has no legal way onto the "
                        f"routing lattice: the node nearest its centre lies "
                        f"inside a neighbouring pad's clearance, and no node "
                        f"within {_MAX_ESCAPE_STEPS} grid steps can be reached "
                        f"by a stub that keeps clearance"
                        + (" (a plated hole is never given one)"
                           if pad.through_hole else "")
                    )
                # setdefault, not assignment: several pads of one net can
                # fail, and the first is the one to name -- a reason that
                # changed depending on how many other pads also failed would
                # read as a different bug each time.
                escape_failures.setdefault(pad.net, reason)
                continue
            stub, i, j = found
            stubs[(stub.layer, i, j)] = stub
        group = tuple((layer, i, j) for layer in on)
        for node in group:
            reserved[node[0]][(i, j)] = pad.net
        known = ports.setdefault(pad.net, [])
        # Two pads collapsing onto the same node are one terminal, not two;
        # a hole landing on an SMD pad's node widens that terminal to both
        # layers rather than adding a second one.
        for index, existing in enumerate(known):
            if any(node in existing for node in group):
                known[index] = existing + tuple(
                    node for node in group if node not in existing
                )
                break
        else:
            known.append(group)

    track_disc = _disc(track_width_nm + clearance_nm, grid_nm)
    via_disc = _disc(via_diameter_nm // 2 + clearance_nm + track_width_nm // 2, grid_nm)
    #: Barrel to barrel. Every disc here is a centre-to-centre distance, so the
    #: radius is *both* neighbours' half-widths plus the clearance -- and two
    #: vias are the one pairing where neither half-width is a track's.
    #: ``via_disc`` is via-to-track (0.3 + 0.2 + 0.1 = 0.6 mm at the defaults)
    #: and was being used for via-to-via as well, which needs 0.8 mm; two
    #: barrels 0.75 mm apart therefore passed the search with a 0.15 mm gap
    #: against a 0.2 mm rule. Found on the ``VIA_BOARD`` fixture in
    #: engine/tests/test_routing.py, 2026-09-08 -- latent before then only
    #: because no placement had put two vias in that band.
    via_via_disc = _disc(via_diameter_nm + clearance_nm, grid_nm)

    # Where copper actually is, as opposed to where copper may not go. The
    # halo written into `reserved` is advisory: mark() will not overwrite a
    # node another net already owns, and a pad keep-out owns a lot of them, so
    # a committed track routinely fails to claim the clearance it needs and
    # the next net is free to run one grid step away. Checking the disc around
    # a candidate node against this map instead makes the clearance a property
    # of the search rather than of whatever the halo happened to win.
    copper: dict[Layer, dict[tuple[int, int], str]] = {
        layer: {} for layer in layers
    }
    # Vias again, separately. A barrel is wider than a track, so the spacing a
    # neighbour owes it is wider than track-to-track -- checking a candidate
    # node against the track radius alone let a GND track sit 0.159 mm from a
    # VIN via against a 0.2 mm rule, which is only visible once you ask KiCad.
    via_nodes: dict[Layer, dict[tuple[int, int], str]] = {
        layer: {} for layer in layers
    }

    # ---- what an escape forbids ------------------------------------------
    # An escape is copper that does not lie on the lattice, so the disc tests
    # cannot see it. Every other map in this module compares a node against
    # other *nodes*, which is exact only because all the copper it describes
    # runs node to node; a stub breaks that premise, and a stub checked with
    # the lattice discs is exactly the change that passes a grid clearance test
    # and shorts two pads in copper.
    #
    # So the exclusion is computed exactly, once, and stored as the nodes it
    # forbids -- node-keyed and checked directly rather than through a disc,
    # the shape ``via_keepout`` above already uses for the same reason. Two
    # radii because the neighbour's own half-width is part of the distance: a
    # foreign track owes the stub half a track plus the clearance, a foreign
    # barrel owes it half a via plus the clearance, and a barrel is on every
    # layer so its map is not keyed by one.
    stub_keepout: dict[Layer, dict[tuple[int, int], str]] = {
        layer: {} for layer in layers
    }
    stub_via_keepout: dict[tuple[int, int], str] = {}

    def forbid(
        where: dict[tuple[int, int], str], key: tuple[int, int], net: str
    ) -> None:
        held = where.get(key)
        if held is None:
            where[key] = net
        elif held != net:
            where[key] = _CONTESTED

    for stub in stubs.values():
        # Full track half-width for the stub itself, matching what
        # ``stub_clears`` reserved: an escape drawn narrower than the track
        # still holds a track's worth of room, which is what makes a
        # neighbouring escape's port provably clear of this keep-out.
        track_reach = track_width_nm // 2 + clearance_nm + track_width_nm // 2
        via_reach = via_diameter_nm // 2 + clearance_nm + track_width_nm // 2
        for x0, y0, x1, y1 in stub.legs():
            leg = _box(x0, y0, x1, y1)
            reach = max(track_reach, via_reach)
            i_lo = max(0, (leg[0] - reach - min_x_nm) // grid_nm)
            i_hi = min(nx - 1, (leg[1] + reach - min_x_nm) // grid_nm)
            j_lo = max(0, (leg[2] - reach - min_y_nm) // grid_nm)
            j_hi = min(ny - 1, (leg[3] + reach - min_y_nm) // grid_nm)
            for i in range(int(i_lo), int(i_hi) + 1):
                for j in range(int(j_lo), int(j_hi) + 1):
                    node = _box(node_x(i), node_y(j), node_x(i), node_y(j))
                    if not _far_enough(leg, node, track_reach):
                        forbid(stub_keepout[stub.layer], (i, j), stub.net)
                    if not _far_enough(leg, node, via_reach):
                        forbid(stub_via_keepout, (i, j), stub.net)
    # ---- net order -------------------------------------------------------
    # Shortest and simplest first. Sequential routers are order-dependent and
    # this order is a heuristic, not an optimum -- but it is deterministic,
    # which matters more here: the same design must route the same way twice.
    def net_key(net: str) -> tuple[int, int, str]:
        nodes = [node for group in ports[net] for node in group]
        extent = max(n[1] for n in nodes) - min(n[1] for n in nodes) + (
            max(n[2] for n in nodes) - min(n[2] for n in nodes)
        )
        return (len(ports[net]), extent, net)

    # A net one of whose pads could not escape is not routed round that pad --
    # it is not routed at all. Dropping the pad and connecting the rest is the
    # silent-disconnection bug this whole module exists to avoid: the run would
    # report the net routed and the board would come back with one pin dead.
    routable = sorted(
        (n for n, p in ports.items() if len(p) >= 2 and n not in escape_failures),
        key=net_key,
    )
    result.unrouted.update(escape_failures)
    pads_per_net = _terminal_counts(pads)
    for net, groups in sorted(ports.items()):
        if net in escape_failures:
            continue
        if len(groups) < 2:
            # One reachable terminal is not a connection to make -- but there
            # are two quite different ways to arrive at one, and they send a
            # person to opposite fixes. A net with a single pad has nothing to
            # connect and no grid would change that; a net whose several pads
            # all landed on one lattice node really is a grid too coarse for
            # the footprint. Saying the second when the first is true was a
            # measured lie: on engine/tests/fixtures/ref.kicad_pcb, 48 of the
            # 54 nets carry exactly one pad and every one of them was reported
            # as a footprint the grid could not resolve.
            count = pads_per_net.get(net, 0)
            result.unrouted[net] = (
                "only one pad carries this net, so there is nothing to "
                "connect it to"
                if count < 2
                else f"its {count} pads collapsed onto {len(groups)} distinct "
                f"grid node(s); the routing grid is too coarse for this "
                f"footprint"
            )

    # The reservation map as it stands before any copper: pads, contested
    # nodes and ports. Rip-up needs this twice over -- it is what separates
    # "blocked by a pad" (immovable) from "blocked by an earlier net's copper"
    # (liftable), and it is the base state surviving routes are replayed onto
    # after a rip.
    base_reserved = {layer: dict(nodes) for layer, nodes in reserved.items()}

    committed: dict[str, list[list[_Node]]] = {}
    commit_order: list[str] = []

    def commit_net(net: str, paths: list[list[_Node]]) -> None:
        committed[net] = paths
        commit_order.append(net)
        for path in paths:
            _commit(
                path,
                net=net,
                reserved=reserved,
                copper=copper,
                via_nodes=via_nodes,
                track_disc=track_disc,
                via_disc=via_disc,
                nx=nx,
                ny=ny,
            )

    def rip(nets: list[str]) -> None:
        """Lift ``nets`` and rebuild the maps by replaying the survivors.

        Replaying onto the base state is deliberately dumber than un-marking
        the ripped nets' nodes: clearance halos overlap, and subtracting one
        net's halo can subtract a node another net still needs. A replay
        cannot get that wrong.
        """
        for net in nets:
            del committed[net]
            commit_order.remove(net)
        order = list(commit_order)
        commit_order.clear()
        for layer in layers:
            reserved[layer].clear()
            reserved[layer].update(base_reserved[layer])
            copper[layer].clear()
            via_nodes[layer].clear()
        survivors = {net: committed.pop(net) for net in order}
        for net in order:
            commit_net(net, survivors[net])

    def attempt(net: str, rip_cost: int | None = None):
        return _route_net(
            ports[net],
            net=net,
            nx=nx,
            ny=ny,
            layers=layers,
            reserved=reserved,
            copper=copper,
            via_nodes=via_nodes,
            holes=holes,
            via_keepout=via_keepout,
            stub_keepout=stub_keepout,
            stub_via_keepout=stub_via_keepout,
            track_room=track_room,
            via_room=via_room,
            track_disc=track_disc,
            via_disc=via_disc,
            via_via_disc=via_via_disc,
            via_cost=_VIA_COST_STEPS,
            budget=budget,
            rip_cost=rip_cost,
            base_reserved=base_reserved,
        )

    budget = _Budget(max_expansions, max_expansions_per_net)
    #: First-pass failures rip-up can act on: blockages, not budget deaths.
    blocked: list[str] = []
    for net in routable:
        if budget.spent:
            result.unrouted[net] = (
                f"search budget of {max_expansions} node expansions ran out "
                f"before this net was reached"
            )
            continue
        paths, failure, retryable = attempt(net)
        if failure is not None:
            result.unrouted[net] = failure
            if retryable:
                blocked.append(net)
            continue
        commit_net(net, paths)

    # ---- rip-up and retry ------------------------------------------------
    # A sequential router is order-dependent: the shortest-first heuristic
    # above can lay an early net straight through the only channel a later
    # net has, while the early net had an alternative. Each round takes one
    # blocked net, probes for the cheapest path pretending committed copper
    # were liftable (pads stay hard), lifts the nets that path crosses,
    # routes the blocked net honestly, and re-routes what it lifted. A lifted
    # net that cannot be re-routed is named like any other failure and may
    # rip in its own turn, bounded per net and in rounds.
    attempts: dict[str, int] = {}
    queue = sorted(blocked, key=net_key)
    ripups = 0
    while queue and ripups < max_ripups and not budget.spent:
        net = queue.pop(0)
        if attempts.get(net, 0) >= _MAX_RIPS_PER_NET:
            continue
        attempts[net] = attempts.get(net, 0) + 1
        ripups += 1

        probe_paths, failure, _ = attempt(net, rip_cost=_RIP_COST_STEPS)
        if failure is not None:
            # Even with every other net's copper lifted there is no path --
            # or the budget died looking. Saying which matters: the first is
            # a fact about the board, the second about how hard we looked.
            if "budget" not in failure:
                failure = (
                    f"no clear path to one of its {len(ports[net])} pads "
                    f"even with every other net's copper lifted; blocked by "
                    f"pad clearance or the board shape"
                )
            result.unrouted[net] = failure
            continue
        victims = _crossed_nets(
            probe_paths,
            net,
            layers=layers,
            reserved=reserved,
            copper=copper,
            via_nodes=via_nodes,
            track_disc=track_disc,
            via_disc=via_disc,
            via_via_disc=via_via_disc,
            committed=committed,
        )
        saved = {v: committed[v] for v in victims}
        if victims:
            rip(victims)

        paths, failure, _ = attempt(net)
        if failure is not None:
            # Put the victims back exactly as they were; nothing else was
            # committed while they were out, so their old paths still hold.
            for v in victims:
                commit_net(v, saved[v])
            result.unrouted[net] = failure
            continue
        commit_net(net, paths)
        result.unrouted.pop(net, None)

        for v in sorted(victims, key=net_key):
            vpaths, vfailure, vretryable = attempt(v)
            if vfailure is None:
                commit_net(v, vpaths)
                continue
            result.unrouted[v] = (
                f"ripped up to free a channel for {net} and could not be "
                f"re-routed: {vfailure}"
            )
            if vretryable and attempts.get(v, 0) < _MAX_RIPS_PER_NET:
                queue.append(v)
        queue.sort(key=net_key)

    # ---- emit ------------------------------------------------------------
    for net in commit_order:
        result.routed.append(net)
        # The escapes first, so a reader of the track list meets a net where it
        # leaves its pad. Only for nets that actually routed: a stub emitted
        # for an unrouted net would be copper hanging off a pad connected to
        # nothing, which is exactly the "looks routed where you happen to look"
        # failure this module refuses to produce.
        for _node, stub in sorted(
            stubs.items(), key=lambda kv: (kv[0][0].value, kv[0][1], kv[0][2])
        ):
            if stub.net != net:
                continue
            for x0, y0, x1, y1 in stub.legs():
                if (x0, y0) == (x1, y1):
                    continue
                result.tracks.append(
                    Track(
                        start_x_nm=x0, start_y_nm=y0,
                        end_x_nm=x1, end_y_nm=y1,
                        layer=stub.layer, net=net, width_nm=stub.width_nm,
                    )
                )
        for path in committed[net]:
            result.tracks.extend(
                _to_tracks(path, net, node_x, node_y, track_width_nm)
            )
            result.vias.extend(
                _to_vias(path, net, node_x, node_y, via_diameter_nm, via_drill_nm)
            )

    if result.unrouted:
        result.warnings.append(
            f"{len(result.unrouted)} of "
            f"{len(result.unrouted) + len(result.routed)} nets left unrouted: "
            + ", ".join(sorted(result.unrouted))
        )
    return result


_Node = tuple[Layer, int, int]
#: One terminal: every node a pad's copper touches. A single node for an SMD
#: pad, the same (i, j) on both layers for a plated hole.
_Port = tuple[_Node, ...]


@dataclass
class _Budget:
    """Search effort left, for the whole call and for the net in hand.

    Two caps because they answer different failures. The total is what stops a
    board with forty hopeless nets costing forty times one with a single
    hopeless net. The per-net share is what stops the first hopeless net
    spending everything and leaving the rest reported unrouted without ever
    having been tried.
    """

    remaining: int
    per_net: int
    net_remaining: int = 0

    @property
    def spent(self) -> bool:
        return self.remaining <= 0

    def start_net(self) -> None:
        self.net_remaining = min(self.per_net, self.remaining)

    def take(self) -> bool:
        """Charge one node expansion. False once either cap is reached."""
        self.remaining -= 1
        self.net_remaining -= 1
        return self.remaining > 0 and self.net_remaining > 0


def _route_net(
    terminals: list[_Port],
    *,
    net: str,
    nx: int,
    ny: int,
    layers: tuple[Layer, ...],
    reserved: dict[Layer, dict[tuple[int, int], str]],
    copper: dict[Layer, dict[tuple[int, int], str]],
    via_nodes: dict[Layer, dict[tuple[int, int], str]],
    holes: set[tuple[int, int]],
    via_keepout: dict[tuple[int, int], str],
    stub_keepout: dict[Layer, dict[tuple[int, int], str]],
    stub_via_keepout: dict[tuple[int, int], str],
    track_room: Callable[[int, int], bool],
    via_room: Callable[[int, int], bool],
    track_disc: tuple[tuple[int, int], ...],
    via_disc: tuple[tuple[int, int], ...],
    via_via_disc: tuple[tuple[int, int], ...],
    via_cost: int,
    budget: _Budget,
    rip_cost: int | None = None,
    base_reserved: dict[Layer, dict[tuple[int, int], str]] | None = None,
) -> tuple[list[list[_Node]], str | None, bool]:
    """Grow one net's tree, terminal by terminal.

    Returns ``(paths, None, False)``, or ``([], reason, retryable)`` if any
    terminal could not be reached -- ``retryable`` is True only when the
    failure was a blockage, the one thing rip-up can act on. A net is
    all-or-nothing on purpose: half a net's tracks laid down is a board that
    looks routed in the places you happen to look at.

    A terminal is reached when any node of its group is; the whole group then
    joins the tree, so a plated hole touched on the front is a source on the
    back from then on, with no via bought for it.

    With ``rip_cost`` set this is the rip-up probe: committed foreign copper
    becomes passable at that many extra steps per node, while anything in
    ``base_reserved`` -- pads, contested nodes, ports -- stays as hard as
    ever. The paths it returns cross other nets and must never be committed;
    they exist to say *which* nets are in the way.
    """
    budget.start_net()
    tree: set[_Node] = set(terminals[0])
    paths: list[list[_Node]] = []

    def absorb(path: list[_Node]) -> None:
        tree.update(path)
        for group in terminals:
            if any(node in tree for node in group):
                tree.update(group)

    for target in terminals[1:]:
        if any(node in tree for node in target):
            continue
        path = _astar(
            sources=tree,
            goal=target,
            net=net,
            nx=nx,
            ny=ny,
            layers=layers,
            reserved=reserved,
            copper=copper,
            via_nodes=via_nodes,
            holes=holes,
            via_keepout=via_keepout,
            stub_keepout=stub_keepout,
            stub_via_keepout=stub_via_keepout,
            track_room=track_room,
            via_room=via_room,
            track_disc=track_disc,
            via_disc=via_disc,
            via_via_disc=via_via_disc,
            via_cost=via_cost,
            budget=budget,
            # A path may run along copper this net already owns; that is a
            # T-junction, which is exactly what a multi-pin net wants.
            owned=tree,
            rip_cost=rip_cost,
            base_reserved=base_reserved,
        )
        if path is None:
            # Distinguish the two: "blocked" is a fact about the board, "ran
            # out of budget" is a fact about how hard we looked. Reporting the
            # second as the first would send someone rearranging a board that
            # routes fine given more search.
            if budget.spent or budget.net_remaining <= 0:
                return [], (
                    "search budget ran out while looking for a path to one of "
                    f"its {len(terminals)} pads"
                ), False
            return [], (
                f"no clear path to one of its {len(terminals)} pads; "
                f"the channel is blocked by earlier nets or by pad clearance"
            ), True
        paths.append(path)
        absorb(path)
    return paths, None, False


def _astar(
    *,
    sources: set[_Node],
    goal: _Port,
    net: str,
    nx: int,
    ny: int,
    layers: tuple[Layer, ...],
    reserved: dict[Layer, dict[tuple[int, int], str]],
    copper: dict[Layer, dict[tuple[int, int], str]],
    via_nodes: dict[Layer, dict[tuple[int, int], str]],
    holes: set[tuple[int, int]],
    via_keepout: dict[tuple[int, int], str],
    stub_keepout: dict[Layer, dict[tuple[int, int], str]],
    stub_via_keepout: dict[tuple[int, int], str],
    track_room: Callable[[int, int], bool],
    via_room: Callable[[int, int], bool],
    track_disc: tuple[tuple[int, int], ...],
    via_disc: tuple[tuple[int, int], ...],
    via_via_disc: tuple[tuple[int, int], ...],
    via_cost: int,
    budget: _Budget,
    owned: set[_Node],
    rip_cost: int | None = None,
    base_reserved: dict[Layer, dict[tuple[int, int], str]] | None = None,
) -> list[_Node] | None:
    """Shortest path from any node in ``sources`` to any node of ``goal``.

    Costs are in grid steps; a layer change costs ``via_cost`` of them. The
    heuristic is Manhattan distance in steps, which never overestimates because
    every move costs at least one step and a via costs more.

    Clearance is enforced here, against ``copper``, rather than by trusting the
    halo an earlier net wrote into ``reserved``. The halo cannot claim a node
    another net already owns -- and pad keep-outs own a great many -- so relying
    on it let a track and a foreign via end up one grid step apart, which is a
    short, and two tracks 0.05 mm apart against a 0.2 mm rule. Both were real:
    KiCad's own DRC found them on a seven-part board that the suite passed.
    """
    # Every node of a goal group shares one (i, j), so one heuristic serves.
    _, gi, gj = goal[0]
    goals = set(goal)

    def _clear(
        where: dict[Layer, dict[tuple[int, int], str]],
        layer: Layer,
        i: int,
        j: int,
        disc: tuple[tuple[int, int], ...],
    ) -> bool:
        here = where[layer]
        if not here:
            return True
        for dx, dy in disc:
            held = here.get((i + dx, j + dy))
            if held is not None and held != net:
                return False
        return True

    def clear_of_foreign(layer: Layer, i: int, j: int) -> bool:
        """Enough room here for a track of ours, given everything already laid.

        Two radii, because the neighbour's shape sets the spacing: track to
        track needs the track disc, but a via barrel is wider and a track
        beside one owes it the via disc.
        """
        return _clear(copper, layer, i, j, track_disc) and _clear(
            via_nodes, layer, i, j, via_disc
        )

    #: Rip-up probe mode: committed copper is passable at a penalty, pads and
    #: everything else in the pre-copper base state stay walls.
    soft = rip_cost is not None

    def passable(node: _Node) -> bool:
        layer, i, j = node
        if not (0 <= i < nx and 0 <= j < ny):
            return False
        # Before ``owned``, and in probe mode too. The board edge is not
        # copper anybody laid, so no rip-up can lift it and no source node may
        # be grandfathered through it -- the same standing a plated hole's
        # annulus has below. Putting it above the ``owned`` shortcut also
        # means a port that somehow reached the tree outside the band cannot
        # seed a path from there.
        if not track_room(i, j):
            return False
        if node in owned:
            return True
        # An escape is checked in both modes, including the rip-up probe, and
        # deliberately so: a stub is a pad's own way onto the lattice, it is
        # chosen before any net is routed, and no rip-up can lift it. A probe
        # allowed to walk through one would find a channel the strict route can
        # never take -- the same reason a plated hole's annulus stays a wall in
        # probe mode.
        held = stub_keepout[layer].get((i, j))
        if held is not None and held != net:
            return False
        if soft:
            held = base_reserved[layer].get((i, j))
            return held is None or held == net
        held = reserved[layer].get((i, j))
        if held is not None and held != net:
            return False
        return clear_of_foreign(layer, i, j)

    def penalty(node: _Node) -> int:
        """Extra steps for standing where committed copper is, probe mode only."""
        if not soft:
            return 0
        layer, i, j = node
        if node in owned:
            return 0
        held = reserved[layer].get((i, j))
        if held is not None and held != net:
            return rip_cost
        if not clear_of_foreign(layer, i, j):
            return rip_cost
        return 0

    def via_blocked(i: int, j: int) -> bool:
        """Base state no rip-up can clear: a plated hole, or a pad's copper.

        Kept apart from :func:`via_fits`, whose other half -- committed tracks
        and vias -- *is* liftable, so the probe may pay to cross it.
        """
        if (i, j) in holes:
            return True
        # A barrel is wider than a track, so it owes the board edge more room
        # than the node it stands on owes as a track. Base state: the outline
        # is where it is, and the probe may not pretend otherwise.
        if not via_room(i, j):
            return True
        held = stub_via_keepout.get((i, j))
        if held is not None and held != net:
            return True
        held = via_keepout.get((i, j))
        return held is not None and held != net

    def via_fits(i: int, j: int) -> bool:
        """A barrel is wider than a track and pierces both layers.

        So it has to clear foreign copper on the side the path never touches,
        at the via's own radius rather than the track's -- and it can never
        sit inside a plated hole's annulus, this net's or anyone's.

        Two radii, because a disc radius is *both* neighbours' half-widths plus
        the clearance and the neighbour is not always a track: ``via_disc``
        against committed tracks, the wider ``via_via_disc`` against another
        barrel. Using the track radius for both let two barrels of different
        nets stand 0.75 mm apart -- a 0.15 mm gap against a 0.2 mm rule.
        """
        if (i, j) in holes:
            return False
        if not via_room(i, j):
            return False
        held = stub_via_keepout.get((i, j))
        if held is not None and held != net:
            return False
        held = via_keepout.get((i, j))
        if held is not None and held != net:
            return False
        return all(
            _clear(copper, layer, i, j, via_disc)
            and _clear(via_nodes, layer, i, j, via_via_disc)
            for layer in layers
        )

    def h(i: int, j: int) -> int:
        return abs(i - gi) + abs(j - gj)

    open_heap: list[tuple[int, int, int, _Node]] = []
    best: dict[_Node, int] = {}
    came: dict[_Node, _Node] = {}
    tie = 0
    for src in sorted(sources, key=lambda n: (n[0].value, n[1], n[2])):
        if not passable(src):
            continue
        best[src] = 0
        tie += 1
        heapq.heappush(open_heap, (h(src[1], src[2]), tie, 0, src))
    if not open_heap:
        return None

    while open_heap:
        _, _, cost, node = heapq.heappop(open_heap)
        if cost > best.get(node, cost):
            continue
        if not budget.take():
            return None
        if node in goals:
            path = [node]
            while path[-1] in came:
                path.append(came[path[-1]])
            path.reverse()
            return path
        layer, i, j = node
        moves: list[tuple[_Node, int]] = [
            ((layer, i + 1, j), 1),
            ((layer, i - 1, j), 1),
            ((layer, i, j + 1), 1),
            ((layer, i, j - 1), 1),
        ]
        if len(layers) > 1:
            fits = via_fits(i, j)
            # The probe may pay to drill through liftable copper, but a
            # plated hole's annulus is base state no rip can clear: it stays
            # a wall in probe mode too, or the probe finds a channel the
            # strict route can never take.
            if fits or (soft and not via_blocked(i, j)):
                for other in layers:
                    if other is not layer:
                        moves.append(
                            ((other, i, j), via_cost + (0 if fits else rip_cost))
                        )
        for nxt, step in moves:
            if not passable(nxt):
                continue
            new_cost = cost + step + penalty(nxt)
            if new_cost >= best.get(nxt, new_cost + 1):
                continue
            best[nxt] = new_cost
            came[nxt] = node
            tie += 1
            heapq.heappush(
                open_heap, (new_cost + h(nxt[1], nxt[2]), tie, new_cost, nxt)
            )
    return None


def _crossed_nets(
    paths: list[list[_Node]],
    net: str,
    *,
    layers: tuple[Layer, ...],
    reserved: dict[Layer, dict[tuple[int, int], str]],
    copper: dict[Layer, dict[tuple[int, int], str]],
    via_nodes: dict[Layer, dict[tuple[int, int], str]],
    track_disc: tuple[tuple[int, int], ...],
    via_disc: tuple[tuple[int, int], ...],
    via_via_disc: tuple[tuple[int, int], ...],
    committed: dict[str, list[list[_Node]]],
) -> list[str]:
    """The committed nets whose copper stands in the way of ``paths``.

    Mirrors exactly the checks the strict search applies -- reserved nodes,
    the track disc against copper, the via disc against barrels, and both
    layers at a layer change -- so ripping every net named here is sufficient
    for the strict route to see the channel the probe saw. Anything found
    that is not in ``committed`` is a pad reservation and not liftable, so it
    is not a victim. Sorted, because the rip order must not depend on dict
    iteration.
    """
    victims: set[str] = set()

    def collect(
        where: dict[Layer, dict[tuple[int, int], str]],
        layer: Layer,
        i: int,
        j: int,
        disc: tuple[tuple[int, int], ...],
    ) -> None:
        grid = where[layer]
        for dx, dy in disc:
            owner = grid.get((i + dx, j + dy))
            if owner is not None and owner != net and owner in committed:
                victims.add(owner)

    for path in paths:
        for index, (layer, i, j) in enumerate(path):
            held = reserved[layer].get((i, j))
            if held is not None and held != net and held in committed:
                victims.add(held)
            collect(copper, layer, i, j, track_disc)
            collect(via_nodes, layer, i, j, via_disc)
            changes_layer = (index and path[index - 1][0] is not layer) or (
                index + 1 < len(path) and path[index + 1][0] is not layer
            )
            if changes_layer:
                for other in layers:
                    collect(copper, other, i, j, via_disc)
                    collect(via_nodes, other, i, j, via_via_disc)
    return sorted(victims)


def _commit(
    path: list[_Node],
    *,
    net: str,
    reserved: dict[Layer, dict[tuple[int, int], str]],
    copper: dict[Layer, dict[tuple[int, int], str]],
    via_nodes: dict[Layer, dict[tuple[int, int], str]],
    track_disc: tuple[tuple[int, int], ...],
    via_disc: tuple[tuple[int, int], ...],
    nx: int,
    ny: int,
) -> None:
    """Reserve the copper a routed path occupies, plus its clearance halo.

    The halo is what keeps the next net from being laid one grid step away
    from this one. Without it the router would produce tracks that pass DRC
    node-by-node and short in the real world.
    """

    def mark(layer: Layer, i: int, j: int) -> None:
        if 0 <= i < nx and 0 <= j < ny and reserved[layer].get((i, j)) is None:
            reserved[layer][(i, j)] = net

    for index, (layer, i, j) in enumerate(path):
        reserved[layer][(i, j)] = net
        copper[layer][(i, j)] = net
        for dx, dy in track_disc:
            mark(layer, i + dx, j + dy)
        changes_layer = (index and path[index - 1][0] is not layer) or (
            index + 1 < len(path) and path[index + 1][0] is not layer
        )
        if changes_layer:
            # A via is a barrel through both layers: it has to clear copper on
            # the side the path never touches, too.
            for other in reserved:
                reserved[other][(i, j)] = net
                copper[other][(i, j)] = net
                via_nodes[other][(i, j)] = net
                for dx, dy in via_disc:
                    mark(other, i + dx, j + dy)


def _to_tracks(
    path: list[_Node], net: str, node_x, node_y, width_nm: int
) -> list[Track]:
    """Collapse a node path into the fewest straight segments that draw it.

    One ``segment`` per grid step would be electrically identical and
    unreadable -- a 40 mm track as 160 objects that a human cannot select or
    drag. Splitting only at layer changes and corners gives the same copper in
    the shape someone can edit.
    """
    tracks: list[Track] = []

    def segment(start: _Node, end: _Node) -> None:
        if (start[1], start[2]) == (end[1], end[2]):
            return
        tracks.append(
            Track(
                start_x_nm=node_x(start[1]),
                start_y_nm=node_y(start[2]),
                end_x_nm=node_x(end[1]),
                end_y_nm=node_y(end[2]),
                layer=start[0],
                net=net,
                width_nm=width_nm,
            )
        )

    runs: list[list[_Node]] = [[path[0]]]
    for node in path[1:]:
        if node[0] is runs[-1][-1][0]:
            runs[-1].append(node)
        else:
            runs.append([node])

    for run in runs:
        start = run[0]
        heading: tuple[int, int] | None = None
        for prev, node in zip(run, run[1:], strict=False):
            step = (_sign(node[1] - prev[1]), _sign(node[2] - prev[2]))
            if heading is not None and step != heading:
                segment(start, prev)
                start = prev
            heading = step
        segment(start, run[-1])
    return tracks


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _to_vias(
    path: list[_Node], net: str, node_x, node_y, diameter_nm: int, drill_nm: int
) -> list[Via]:
    """One via wherever the path changes layer."""
    vias: list[Via] = []
    for index in range(1, len(path)):
        if path[index][0] is not path[index - 1][0]:
            _, i, j = path[index]
            vias.append(
                Via(
                    x_nm=node_x(i),
                    y_nm=node_y(j),
                    net=net,
                    diameter_nm=diameter_nm,
                    drill_nm=drill_nm,
                )
            )
    return vias

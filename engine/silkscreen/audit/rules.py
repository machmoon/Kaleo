"""Deterministic checks. Everything in here is proven, or it does not belong.

A rule may only report what it measured in the board file, and it must put the
measurement in the finding's ``evidence``. No rule guesses, and no rule reports
a problem it cannot locate -- an unlocated geometry finding is a bug in the
rule, not a property of the board.

The split this package is built around lives here: these functions are the
trustworthy half of the review. The model layer next door is the half that
argues. When the two disagree, this one wins, because it can show its working.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from ..units import mm
from .effort import EffortProfile
from .findings import Finding, Origin, Severity, fmt_mm
from .geometry import (
    AuditBoard,
    AuditPad,
    Rect,
    Seg,
    seg_rect_distance_nm,
    seg_seg_distance_nm,
)

__all__ = ["Rule", "RULES", "run_rules", "rules_for", "GROUPS"]

#: Net-name shapes that mean supply or ground. Deliberately its own copy
#: rather than the placer's: the placer's list exists to *down-weight* power
#: nets in the objective, and widening it there changes placement. Widening it
#: here only changes which pins a decoupling rule looks at.
_GROUND_RE = re.compile(r"^[+-]?(gnd|vss|agnd|dgnd|pgnd|ground|0v)", re.I)
_SUPPLY_RE = re.compile(
    r"^[+-]?(vcc|vdd|avdd|dvdd|vbat|vbus|vin|vout|vref|"
    r"\d+v\d*|\d+ ?v\d*)", re.I
)

#: Below this a track is not manufacturable at any normal fab class.
_MIN_TRACK_NM = mm(0.13)
#: A supply net carrying current through a signal-width track.
_MIN_POWER_TRACK_NM = mm(0.35)
#: Two connected tracks meeting more sharply than this trap etchant.
_ACUTE_ANGLE_DEG = 45.0

GROUPS = ("geometry", "connectivity", "clearance", "practice", "manufacturing")


def is_ground(net: str) -> bool:
    return bool(_GROUND_RE.match(net.strip()))


def is_supply(net: str) -> bool:
    net = net.strip()
    return bool(_SUPPLY_RE.match(net)) and not is_ground(net)


@dataclass(frozen=True)
class Rule:
    name: str
    group: str
    summary: str
    check: Callable[[AuditBoard, EffortProfile], list[Finding]]


def _f(rule: Rule | str, **kwargs) -> Finding:
    name = rule if isinstance(rule, str) else rule.name
    kwargs.setdefault("origin", Origin.PROVEN)
    kwargs.setdefault("source", f"rule:{name}")
    return Finding(rule=name, **kwargs)


def _via_rect(via: tuple[int, int, int, str]) -> Rect:
    x, y, size, _net = via
    radius = (size + 1) // 2
    return Rect(x - radius, y - radius, x + radius, y + radius)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


def _courtyard_overlap(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    out: list[Finding] = []
    parts = board.parts
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if a.side != b.side:
                continue  # opposite sides of the laminate never collide
            overlap = a.extent.intersection(b.extent)
            if overlap is None:
                continue
            area_mm2 = (overlap.width_nm / 1e6) * (overlap.height_nm / 1e6)
            out.append(
                _f(
                    "courtyard-overlap",
                    severity=Severity.BLOCKER,
                    title=f"{a.ref} and {b.ref} overlap",
                    detail=(
                        f"The courtyards of {a.ref} and {b.ref} intersect over "
                        f"{fmt_mm(overlap.width_nm)} x {fmt_mm(overlap.height_nm)}. "
                        "Two parts cannot occupy the same laminate: this board "
                        "cannot be assembled as placed."
                    ),
                    refs=(a.ref, b.ref),
                    extent=overlap,
                    evidence=f"overlap {area_mm2:.3f} mm^2, required 0",
                    fix=f"Move {b.ref} clear of {a.ref} and re-run placement.",
                )
            )
    return out


def _no_courtyard(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    """A part with no courtyard is a hole in every other geometry rule."""
    out = []
    for part in board.parts:
        if part.courtyard is None:
            out.append(
                _f(
                    "missing-courtyard",
                    severity=Severity.MARGINAL,
                    title=f"{part.ref} has no courtyard",
                    detail=(
                        f"{part.ref} ({part.lib_id or 'unknown footprint'}) draws "
                        "nothing on its courtyard layer, so overlap and edge "
                        "checks fell back to its pad bounding box. Those checks "
                        "are weaker for this part than for the rest of the board."
                    ),
                    refs=(part.ref,),
                    extent=part.extent,
                    evidence="0 courtyard graphics on F.CrtYd/B.CrtYd",
                    fix="Use a footprint that defines a courtyard.",
                )
            )
    return out


def _duplicate_refs(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    seen: dict[str, list] = {}
    for part in board.parts:
        seen.setdefault(part.ref or "", []).append(part)
    out = []
    for ref, parts in seen.items():
        if not ref:
            out.append(
                _f(
                    "missing-designator",
                    severity=Severity.MARGINAL,
                    title=f"{len(parts)} footprint(s) have no reference designator",
                    detail=(
                        "A footprint with no designator cannot be matched to a "
                        "BOM line or to a pick-and-place row."
                    ),
                    extent=parts[0].extent,
                    evidence=f"{len(parts)} footprint(s) with an empty Reference",
                    fix="Give every footprint a unique reference designator.",
                )
            )
        elif len(parts) > 1:
            out.append(
                _f(
                    "duplicate-designator",
                    severity=Severity.BLOCKER,
                    title=f"{ref} is used by {len(parts)} footprints",
                    detail=(
                        f"{len(parts)} footprints share the designator {ref}. "
                        "Assembly cannot tell which part goes where, and the "
                        "schematic and board no longer describe one circuit."
                    ),
                    refs=(ref,),
                    extent=parts[0].extent.union(parts[1].extent),
                    evidence=f"{len(parts)} footprints named {ref}",
                    fix="Renumber the duplicates.",
                )
            )
    return out


def _board_edge(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    if board.outline is None:
        return [
            _f(
                "no-board-outline",
                severity=Severity.BLOCKER,
                title="The board has no outline",
                detail=(
                    "Nothing is drawn on Edge.Cuts, so the board has no "
                    "boundary. A fab house cannot profile it, and every "
                    "edge-clearance check in this review is vacuous."
                ),
                extent=board.extent,
                evidence="0 graphics on Edge.Cuts",
                fix="Draw an Edge.Cuts outline around the placement.",
            )
        ]
    out = []
    inner = board.outline.grown(-profile.edge_margin_nm)
    for part in board.parts:
        extent = part.extent
        if not board.outline.contains(extent):
            out.append(
                _f(
                    "part-off-board",
                    severity=Severity.BLOCKER,
                    title=f"{part.ref} extends past the board edge",
                    detail=(
                        f"{part.ref}'s courtyard crosses Edge.Cuts. The part "
                        "would be routed through when the board is profiled."
                    ),
                    refs=(part.ref,),
                    extent=extent,
                    evidence=(
                        f"courtyard {fmt_mm(extent.x0)},{fmt_mm(extent.y0)} to "
                        f"{fmt_mm(extent.x1)},{fmt_mm(extent.y1)} vs outline "
                        f"{fmt_mm(board.outline.x0)},{fmt_mm(board.outline.y0)} to "
                        f"{fmt_mm(board.outline.x1)},{fmt_mm(board.outline.y1)}"
                    ),
                    fix=f"Move {part.ref} inside the outline or enlarge the board.",
                )
            )
        elif not inner.contains(extent):
            gap = min(
                extent.x0 - board.outline.x0,
                extent.y0 - board.outline.y0,
                board.outline.x1 - extent.x1,
                board.outline.y1 - extent.y1,
            )
            out.append(
                _f(
                    "part-near-edge",
                    severity=Severity.MARGINAL,
                    title=f"{part.ref} sits {fmt_mm(gap)} from the board edge",
                    detail=(
                        f"{part.ref} is inside the outline but within the "
                        f"{fmt_mm(profile.edge_margin_nm)} keep-out. Depaneling "
                        "stress and routing tolerance both live in that band."
                    ),
                    refs=(part.ref,),
                    extent=extent,
                    evidence=(
                        f"gap {fmt_mm(gap)}, keep-out "
                        f"{fmt_mm(profile.edge_margin_nm)}"
                    ),
                    fix=f"Pull {part.ref} further in from the edge.",
                )
            )
    return out


# --------------------------------------------------------------------------
# connectivity
# --------------------------------------------------------------------------


def _touching(a: AuditPad | Seg, b: Seg) -> bool:
    """Does a track terminate on this pad, or meet this other track?"""
    if isinstance(a, Seg):
        if a.side != b.side and "*" not in (a.side, b.side):
            return False
        return seg_seg_distance_nm(a, b) <= 0
    if not any(lay.endswith(".Cu") or lay.startswith("*") for lay in a.layers):
        return False
    if not a.through_hole and a.side != b.side:
        return False
    return seg_rect_distance_nm(b, a.rect) <= 0


class _DisjointSet:
    """Union-find, so an island count is a fact rather than a heuristic."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self._parent[rj] = ri


def _net_connectivity(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    """Union-find over pads, tracks and vias, one net at a time.

    A net whose pads fall into more than one island is not connected, whatever
    the file's net numbers claim. This is the check that catches "the run said
    routed" -- the failure the router's own reporting is least able to see.
    """
    out: list[Finding] = []
    by_net = board.pads_by_net()
    tracks_by_net: dict[str, list[Seg]] = {}
    for track in board.tracks:
        tracks_by_net.setdefault(track.net, []).append(track)
    vias_by_net: dict[str, list[tuple[int, int, int, str]]] = {}
    for via in board.vias:
        vias_by_net.setdefault(via[3], []).append(via)

    for net, pads in sorted(by_net.items()):
        if len(pads) < 2:
            out.append(
                _f(
                    "single-pad-net",
                    severity=Severity.MARGINAL,
                    title=f"net {net} reaches only one pad",
                    detail=(
                        f"Net {net} appears on {pads[0].name} and nowhere else. "
                        "A net with one terminal connects nothing; the signal it "
                        "was meant to carry has no other end."
                    ),
                    refs=(pads[0].ref,),
                    nets=(net,),
                    extent=pads[0].rect,
                    evidence=f"1 pad on net {net}",
                    fix=f"Connect {net} to its other endpoint, or delete it.",
                )
            )
            continue

        tracks = tracks_by_net.get(net, [])
        vias = vias_by_net.get(net, [])
        if not tracks:
            centre = Rect.around([p.rect.centre for p in pads])
            out.append(
                _f(
                    "unrouted-net",
                    severity=Severity.BLOCKER,
                    title=f"net {net} has no copper",
                    detail=(
                        f"Net {net} joins {len(pads)} pads "
                        f"({', '.join(p.name for p in pads[:6])}) and carries no "
                        "track at all. KiCad draws this as a ratsnest line; a "
                        "fab house etches nothing."
                    ),
                    refs=tuple(dict.fromkeys(p.ref for p in pads)),
                    nets=(net,),
                    extent=centre,
                    evidence=f"{len(pads)} pads, 0 track segments on net {net}",
                    fix=f"Route {net}.",
                )
            )
            continue

        # Islands: pads and tracks are nodes; a track that touches a pad or
        # another track merges their sets. A via joins every same-net node its
        # barrel lands on, including an SMD pad directly beneath it.
        nodes: list = list(pads) + list(tracks)
        sets = _DisjointSet(len(nodes))
        for ti in range(len(pads), len(nodes)):
            track = nodes[ti]
            for pi, pad in enumerate(pads):
                if _touching(pad, track):
                    sets.union(pi, ti)
            for tj in range(ti + 1, len(nodes)):
                if _touching(nodes[tj], track):
                    sets.union(ti, tj)
        for via in vias:
            box = _via_rect(via)
            hit = [
                i
                for i, pad in enumerate(pads)
                if pad.rect.gap_to(box) <= 0
            ]
            hit.extend(
                i
                for i in range(len(pads), len(nodes))
                if seg_rect_distance_nm(nodes[i], box) <= 0
            )
            for other in hit[1:]:
                sets.union(hit[0], other)

        islands: dict[int, list[int]] = {}
        for pi in range(len(pads)):
            islands.setdefault(sets.find(pi), []).append(pi)
        if len(islands) > 1:
            groups = [
                ", ".join(pads[i].name for i in sorted(members))
                for members in islands.values()
            ]
            spots = Rect.around([p.rect.centre for p in pads])
            out.append(
                _f(
                    "net-not-connected",
                    severity=Severity.BLOCKER,
                    title=f"net {net} is in {len(islands)} disconnected pieces",
                    detail=(
                        f"Net {net} carries copper, but its pads do not all reach "
                        "each other through it. Islands: "
                        + " | ".join(groups[:4])
                        + ". A board reported as routed with a split net comes "
                        "back with a track that ends in bare laminate."
                    ),
                    refs=tuple(dict.fromkeys(p.ref for p in pads)),
                    nets=(net,),
                    extent=spots,
                    evidence=(
                        f"{len(islands)} islands over {len(pads)} pads and "
                        f"{len(tracks)} segments"
                    ),
                    fix=f"Finish routing {net} so every pad is on one island.",
                )
            )
    return out


# --------------------------------------------------------------------------
# clearance
# --------------------------------------------------------------------------


def _pad_clearance(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    pads = board.pads()
    limit = profile.clearance_nm
    out: list[Finding] = []
    for i, a in enumerate(pads):
        for b in pads[i + 1 :]:
            if a.net and b.net and a.net == b.net:
                continue
            if not a.shares_copper_with(b):
                continue
            gap = a.rect.gap_to(b.rect)
            if gap >= limit:
                continue
            shorted = gap <= 0
            # Two pads of one footprint at their nominal spacing are the land
            # pattern, not a layout mistake: nobody fixes a SOT-223's 0.1 mm
            # pad gap by moving something. Overlapping pads on one footprint
            # stay reported, because that is a broken footprint.
            if a.ref == b.ref and not shorted:
                continue
            out.append(
                _f(
                    "pad-clearance",
                    severity=Severity.BLOCKER if shorted else Severity.MARGINAL,
                    title=(
                        f"{a.name} and {b.name} "
                        + ("touch" if shorted else f"are {fmt_mm(gap)} apart")
                    ),
                    detail=(
                        f"{a.name} (net {a.net or 'none'}) and {b.name} "
                        f"(net {b.net or 'none'}) are on different nets and "
                        + (
                            "their copper overlaps: this is a short."
                            if shorted
                            else "closer than the clearance rule allows. Solder "
                            "bridging on assembly is likely."
                        )
                    ),
                    refs=tuple(dict.fromkeys((a.ref, b.ref))),
                    nets=tuple(n for n in dict.fromkeys((a.net, b.net)) if n),
                    extent=a.rect.union(b.rect),
                    evidence=f"gap {fmt_mm(gap)}, clearance {fmt_mm(limit)}",
                    fix="Increase the spacing between these parts or their pads.",
                )
            )
    return out


def _track_clearance(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    limit = profile.clearance_nm
    out: list[Finding] = []
    pads = board.pads()

    for track in board.tracks:
        for pad in pads:
            if pad.net and track.net and pad.net == track.net:
                continue
            if not pad.through_hole and pad.side != track.side:
                continue
            gap = seg_rect_distance_nm(track, pad.rect)
            if gap >= limit:
                continue
            out.append(
                _f(
                    "track-pad-clearance",
                    severity=Severity.BLOCKER if gap <= 0 else Severity.MARGINAL,
                    title=(
                        f"track on {track.net or 'no net'} runs {fmt_mm(gap)} "
                        f"from {pad.name}"
                    ),
                    detail=(
                        f"A {track.layer} track carrying {track.net or 'no net'} "
                        f"passes {pad.name} (net {pad.net or 'none'}) at "
                        f"{fmt_mm(gap)}. "
                        + (
                            "The copper intersects: these two nets are shorted."
                            if gap <= 0
                            else "Etch tolerance eats gaps this size."
                        )
                    ),
                    refs=(pad.ref,),
                    nets=tuple(n for n in dict.fromkeys((track.net, pad.net)) if n),
                    extent=track.bbox.union(pad.rect),
                    point=track.midpoint,
                    evidence=f"gap {fmt_mm(gap)}, clearance {fmt_mm(limit)}",
                    fix="Reroute the track around the pad.",
                )
            )

    for via in board.vias:
        vx, vy, _size, net = via
        box = _via_rect(via)
        for pad in pads:
            if net and pad.net and net == pad.net:
                continue
            gap = box.gap_to(pad.rect)
            if gap >= limit:
                continue
            out.append(
                _f(
                    "via-pad-clearance",
                    severity=Severity.BLOCKER if gap <= 0 else Severity.MARGINAL,
                    title=f"via on {net or 'no net'} is {fmt_mm(gap)} from {pad.name}",
                    detail=(
                        f"A via carrying {net or 'no net'} passes {pad.name} "
                        f"(net {pad.net or 'none'}) at {fmt_mm(gap)}. "
                        + (
                            "Their copper intersects: these two nets are shorted."
                            if gap <= 0
                            else "Etch tolerance eats gaps this size."
                        )
                    ),
                    refs=(pad.ref,),
                    nets=tuple(n for n in dict.fromkeys((net, pad.net)) if n),
                    extent=box.union(pad.rect),
                    point=(vx, vy),
                    evidence=f"gap {fmt_mm(gap)}, clearance {fmt_mm(limit)}",
                    fix="Move the via or the pad to restore clearance.",
                )
            )
        for track in board.tracks:
            if net and track.net and net == track.net:
                continue
            gap = seg_rect_distance_nm(track, box)
            if gap >= limit:
                continue
            out.append(
                _f(
                    "via-track-clearance",
                    severity=Severity.BLOCKER if gap <= 0 else Severity.MARGINAL,
                    title=(
                        f"via on {net or 'no net'} is {fmt_mm(gap)} from "
                        f"track {track.net or 'with no net'}"
                    ),
                    detail=(
                        "A through via crosses both copper layers and passes a "
                        f"foreign {track.layer} track at {fmt_mm(gap)}. "
                        + (
                            "Their copper intersects: these two nets are shorted."
                            if gap <= 0
                            else "Etch tolerance eats gaps this size."
                        )
                    ),
                    nets=tuple(n for n in dict.fromkeys((net, track.net)) if n),
                    extent=box.union(track.bbox),
                    point=(vx, vy),
                    evidence=f"gap {fmt_mm(gap)}, clearance {fmt_mm(limit)}",
                    fix="Move the via or reroute the track.",
                )
            )

    for i, a in enumerate(board.vias):
        a_box = _via_rect(a)
        for b in board.vias[i + 1 :]:
            if a[3] and b[3] and a[3] == b[3]:
                continue
            b_box = _via_rect(b)
            gap = a_box.gap_to(b_box)
            if gap >= limit:
                continue
            out.append(
                _f(
                    "via-via-clearance",
                    severity=Severity.BLOCKER if gap <= 0 else Severity.MARGINAL,
                    title=(
                        f"vias {a[3] or 'with no net'} and {b[3] or 'with no net'} "
                        f"are {fmt_mm(gap)} apart"
                    ),
                    detail=(
                        "Two vias on different nets are closer than the clearance "
                        "rule allows. Their annular copper spans both layers."
                    ),
                    nets=tuple(n for n in dict.fromkeys((a[3], b[3])) if n),
                    extent=a_box.union(b_box),
                    point=(a[0], a[1]),
                    evidence=f"gap {fmt_mm(gap)}, clearance {fmt_mm(limit)}",
                    fix="Move one of the vias to restore clearance.",
                )
            )

    for i, a in enumerate(board.tracks):
        for b in board.tracks[i + 1 :]:
            if a.net and b.net and a.net == b.net:
                continue
            if a.side != b.side:
                continue
            gap = seg_seg_distance_nm(a, b)
            if gap >= limit:
                continue
            mx, my = a.midpoint
            out.append(
                _f(
                    "track-track-clearance",
                    severity=Severity.BLOCKER if gap <= 0 else Severity.MARGINAL,
                    title=(
                        f"tracks {a.net or 'no net'} and {b.net or 'no net'} "
                        + ("cross" if gap <= 0 else f"pass at {fmt_mm(gap)}")
                    ),
                    detail=(
                        f"Two {a.layer} tracks on different nets "
                        + (
                            "intersect. This is a short in copper."
                            if gap <= 0
                            else "run closer than the clearance rule allows."
                        )
                    ),
                    nets=tuple(n for n in dict.fromkeys((a.net, b.net)) if n),
                    extent=a.bbox.union(b.bbox),
                    point=(mx, my),
                    evidence=f"gap {fmt_mm(gap)}, clearance {fmt_mm(limit)}",
                    fix="Reroute one of the two nets, or move it to the other layer.",
                )
            )
    return out


def _silk_over_pad(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    """Silkscreen ink on solderable copper, one finding per pair of parts.

    Several silk segments crossing one pad is one problem, not three, and the
    fix is always the same edit to the same footprint.
    """
    pads = board.pads()
    hits: dict[tuple[str, str], dict] = {}
    for part in board.parts:
        for seg in part.silk:
            for pad in pads:
                if pad.side != seg.side and not pad.through_hole:
                    continue
                gap = seg_rect_distance_nm(seg, pad.rect)
                if gap > 0:
                    continue
                entry = hits.setdefault(
                    (part.ref, pad.ref),
                    {"pads": {}, "extent": seg.bbox, "worst": 0},
                )
                entry["pads"][pad.name] = pad
                entry["extent"] = entry["extent"].union(seg.bbox).union(pad.rect)
                entry["worst"] = max(entry["worst"], -gap)

    out: list[Finding] = []
    for (silk_ref, pad_ref), entry in sorted(hits.items()):
        names = sorted(entry["pads"])
        own = " its own" if silk_ref == pad_ref else f" {pad_ref}'s"
        out.append(
            _f(
                "silkscreen-over-pad",
                severity=Severity.MARGINAL,
                title=(
                    f"{silk_ref}'s silkscreen covers{own} "
                    f"{'pad' if len(names) == 1 else 'pads'} {', '.join(names)}"
                ),
                detail=(
                    f"Silkscreen belonging to {silk_ref} crosses the solderable "
                    f"area of {len(names)} pad(s). Ink on a pad resists solder; "
                    "most fabs clip it silently, so the board that arrives does "
                    "not match the artwork that was approved."
                ),
                refs=tuple(dict.fromkeys((silk_ref, pad_ref))),
                extent=entry["extent"],
                point=list(entry["pads"].values())[0].rect.centre,
                evidence=(
                    f"{len(names)} pad(s) overlapped, deepest "
                    f"{fmt_mm(entry['worst'])} into the pad"
                ),
                fix=f"Trim {silk_ref}'s outline clear of the pads.",
            )
        )
    return out


# --------------------------------------------------------------------------
# practice
# --------------------------------------------------------------------------


def _decoupling(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    """Every supply pin of every IC wants a cap to ground, close by.

    Rule-checkable in full: which pins are supplies comes from the net names,
    which parts are capacitors comes from the designator, and "close by" is a
    distance. What it cannot say is whether the *value* is right -- that is
    left to the model layer, which is the division this package exists for.
    """
    out: list[Finding] = []
    caps = [
        part
        for part in board.parts
        if part.ref.upper().startswith("C") and len(part.pads) == 2
    ]
    for part in board.parts:
        if not part.is_ic:
            continue
        for pad in part.pads:
            if not is_supply(pad.net):
                continue
            candidates = []
            for cap in caps:
                nets = {p.net for p in cap.pads}
                if pad.net not in nets or not any(is_ground(n) for n in nets):
                    continue
                near = min(
                    int(round(math.hypot(cp.centre[0] - pad.centre[0],
                                         cp.centre[1] - pad.centre[1])))
                    for cp in cap.pads
                    if cp.net == pad.net
                )
                candidates.append((near, cap))
            if not candidates:
                out.append(
                    _f(
                        "no-decoupling",
                        severity=Severity.MARGINAL,
                        title=f"{pad.name} ({pad.net}) has no decoupling capacitor",
                        detail=(
                            f"No capacitor on this board has one leg on {pad.net} "
                            f"and the other on a ground net, so {part.ref}'s "
                            f"supply pin {pad.number} is fed straight off the "
                            "plane. Switching current then has to come the long "
                            "way round, which shows up as supply ringing and, on "
                            "an MCU, as spurious resets."
                        ),
                        refs=(part.ref,),
                        nets=(pad.net,),
                        extent=pad.rect,
                        evidence=f"0 capacitors between {pad.net} and ground",
                        fix=(
                            f"Add a 100nF capacitor from {pad.net} to ground "
                            f"beside {part.ref} pin {pad.number}."
                        ),
                    )
                )
                continue
            nearest, cap = min(candidates, key=lambda c: c[0])
            if nearest > profile.decoupling_max_nm:
                out.append(
                    _f(
                        "decoupling-too-far",
                        severity=Severity.MARGINAL,
                        title=(
                            f"{cap.ref} decouples {pad.name} from "
                            f"{fmt_mm(nearest)} away"
                        ),
                        detail=(
                            f"{cap.ref} is the nearest capacitor between "
                            f"{pad.net} and ground, and it sits {fmt_mm(nearest)} "
                            f"from {part.ref} pin {pad.number}. The loop area is "
                            "what decoupling controls; at this distance the cap "
                            "is decoupling the trace, not the pin."
                        ),
                        refs=(part.ref, cap.ref),
                        nets=(pad.net,),
                        extent=pad.rect.union(cap.extent),
                        point=cap.centre,
                        evidence=(
                            f"{fmt_mm(nearest)} pin-to-pad, limit "
                            f"{fmt_mm(profile.decoupling_max_nm)}"
                        ),
                        fix=f"Move {cap.ref} beside {part.ref} pin {pad.number}.",
                    )
                )
    return out


def _copper_off_board(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    if board.outline is None:
        return []
    out = []
    for track in board.tracks:
        if board.outline.contains(track.bbox):
            continue
        out.append(
            _f(
                "copper-off-board",
                severity=Severity.BLOCKER,
                title=f"track on {track.net or 'no net'} leaves the board",
                detail=(
                    "This track crosses Edge.Cuts. Profiling the board cuts "
                    "the copper, and the exposed edge is a short waiting for "
                    "the panel's neighbour."
                ),
                nets=(track.net,) if track.net else (),
                extent=track.bbox,
                point=track.midpoint,
                evidence=(
                    f"track bbox {fmt_mm(track.bbox.x0)},{fmt_mm(track.bbox.y0)} "
                    f"to {fmt_mm(track.bbox.x1)},{fmt_mm(track.bbox.y1)} outside "
                    "the outline"
                ),
                fix="Reroute inside the outline.",
            )
        )
    for via in board.vias:
        box = _via_rect(via)
        if board.outline.contains(box):
            continue
        vx, vy, _size, net = via
        out.append(
            _f(
                "copper-off-board",
                severity=Severity.BLOCKER,
                title=f"via on {net or 'no net'} leaves the board",
                detail=(
                    "This via crosses Edge.Cuts. Profiling the board cuts its "
                    "annular copper and leaves an exposed board edge."
                ),
                nets=(net,) if net else (),
                extent=box,
                point=(vx, vy),
                evidence=(
                    f"via bbox {fmt_mm(box.x0)},{fmt_mm(box.y0)} to "
                    f"{fmt_mm(box.x1)},{fmt_mm(box.y1)} outside the outline"
                ),
                fix="Move the via inside the outline.",
            )
        )
    return out


def _track_widths(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    """One finding per net, not one per segment.

    A net routed too thin is thin along its whole length, and a report that
    repeats the same sentence twelve times with twelve different badges buries
    the eleven other things wrong with the board. The finding names how many
    segments are affected and points at the thinnest of them.
    """
    thin: dict[str, list[Seg]] = {}
    weak: dict[str, list[Seg]] = {}
    for track in board.tracks:
        if not track.width_nm:
            continue
        if track.width_nm < _MIN_TRACK_NM:
            thin.setdefault(track.net, []).append(track)
        elif (
            (is_supply(track.net) or is_ground(track.net))
            and track.width_nm < _MIN_POWER_TRACK_NM
        ):
            weak.setdefault(track.net, []).append(track)

    def span(segs: list[Seg]) -> Rect:
        box = segs[0].bbox
        for seg in segs[1:]:
            box = box.union(seg.bbox)
        return box

    out: list[Finding] = []
    for net, segs in sorted(thin.items()):
        worst = min(segs, key=lambda s: s.width_nm)
        out.append(
            _f(
                "track-too-thin",
                severity=Severity.BLOCKER,
                title=(
                    f"{net or 'unnamed net'} routed at "
                    f"{fmt_mm(worst.width_nm)}"
                ),
                detail=(
                    f"{len(segs)} segment(s) on this net are below the process "
                    "floor. A standard 1oz etch does not hold copper this "
                    "narrow: it comes back necked, or open."
                ),
                nets=(net,) if net else (),
                extent=span(segs),
                point=worst.midpoint,
                evidence=(
                    f"{len(segs)} segment(s), thinnest {fmt_mm(worst.width_nm)}, "
                    f"floor {fmt_mm(_MIN_TRACK_NM)}"
                ),
                fix=f"Widen {net or 'this net'} to at least {fmt_mm(_MIN_TRACK_NM)}.",
            )
        )
    for net, segs in sorted(weak.items()):
        worst = min(segs, key=lambda s: s.width_nm)
        total = sum(1 for t in board.tracks if t.net == net)
        out.append(
            _f(
                "power-track-thin",
                severity=Severity.MARGINAL,
                title=f"{net} routed at signal width ({fmt_mm(worst.width_nm)})",
                detail=(
                    f"{len(segs)} of {total} segment(s) on {net} are narrower "
                    "than a supply net should be. Current density and IR drop "
                    "both scale with width, and this is the net the rest of the "
                    "board leans on."
                ),
                nets=(net,),
                extent=span(segs),
                point=worst.midpoint,
                evidence=(
                    f"{len(segs)}/{total} segment(s), thinnest "
                    f"{fmt_mm(worst.width_nm)}, suggested "
                    f"{fmt_mm(_MIN_POWER_TRACK_NM)}"
                ),
                fix=f"Widen {net} to at least {fmt_mm(_MIN_POWER_TRACK_NM)}.",
            )
        )
    return out


# --------------------------------------------------------------------------
# manufacturing (deep only)
# --------------------------------------------------------------------------


def _dangling_copper(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    """A track end that lands on nothing: a stub, an antenna, or a miss."""
    pads = board.pads()
    out = []
    for track in board.tracks:
        for end in ((track.x0, track.y0), (track.x1, track.y1)):
            probe = Seg(end[0], end[1], end[0], end[1], layer=track.layer)
            on_pad = any(
                (pad.through_hole or pad.side == track.side)
                and seg_rect_distance_nm(probe, pad.rect) <= 0
                for pad in pads
            )
            if on_pad:
                continue
            on_track = any(
                other is not track
                and other.side == track.side
                and seg_seg_distance_nm(probe, other) <= 0
                for other in board.tracks
            )
            if on_track:
                continue
            on_via = any(
                abs(vx - end[0]) <= size // 2 and abs(vy - end[1]) <= size // 2
                for vx, vy, size, _n in board.vias
            )
            if on_via:
                continue
            out.append(
                _f(
                    "dangling-track",
                    severity=Severity.MARGINAL,
                    title=f"track on {track.net or 'no net'} ends on nothing",
                    detail=(
                        "This track terminates in bare laminate: no pad, no via, "
                        "no other segment. Either the route is unfinished or the "
                        "stub is an unintended antenna."
                    ),
                    nets=(track.net,) if track.net else (),
                    extent=Rect(end[0], end[1], end[0], end[1]).grown(mm(0.4)),
                    point=end,
                    evidence=(
                        f"endpoint {fmt_mm(end[0])},{fmt_mm(end[1])} touches "
                        "0 pads, 0 vias, 0 other segments"
                    ),
                    fix="Finish the connection or delete the stub.",
                )
            )
    return out


def _acute_angles(board: AuditBoard, profile: EffortProfile) -> list[Finding]:
    """Acid traps: two segments meeting at a sharp interior angle."""
    out = []
    tracks = board.tracks
    for i, a in enumerate(tracks):
        for b in tracks[i + 1 :]:
            if a.net != b.net or a.side != b.side:
                continue
            shared = None
            for pa in ((a.x0, a.y0), (a.x1, a.y1)):
                for pb in ((b.x0, b.y0), (b.x1, b.y1)):
                    if pa == pb:
                        shared = pa
            if shared is None:
                continue
            other_a = (a.x1, a.y1) if shared == (a.x0, a.y0) else (a.x0, a.y0)
            other_b = (b.x1, b.y1) if shared == (b.x0, b.y0) else (b.x0, b.y0)
            v1 = (other_a[0] - shared[0], other_a[1] - shared[1])
            v2 = (other_b[0] - shared[0], other_b[1] - shared[1])
            n1 = math.hypot(*v1)
            n2 = math.hypot(*v2)
            if n1 == 0 or n2 == 0:
                continue
            cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
            angle = math.degrees(math.acos(cos))
            if angle >= _ACUTE_ANGLE_DEG:
                continue
            out.append(
                _f(
                    "acute-track-angle",
                    severity=Severity.NOTE,
                    title=f"{angle:.0f} degree corner on {a.net or 'no net'}",
                    detail=(
                        "Two segments of this net meet at an acute angle. The "
                        "inside of the corner traps etchant, which undercuts the "
                        "copper there over time in the bath."
                    ),
                    nets=(a.net,) if a.net else (),
                    extent=Rect(shared[0], shared[1], shared[0], shared[1]).grown(
                        mm(0.5)
                    ),
                    point=shared,
                    evidence=f"interior angle {angle:.1f} deg, floor "
                    f"{_ACUTE_ANGLE_DEG:.0f} deg",
                    fix="Break the corner into two obtuse bends.",
                )
            )
    return out


RULES: tuple[Rule, ...] = (
    Rule("courtyard-overlap", "geometry",
         "parts whose courtyards intersect", _courtyard_overlap),
    Rule("missing-courtyard", "geometry",
         "footprints with no courtyard, weakening every other check",
         _no_courtyard),
    Rule("designators", "geometry",
         "duplicate or missing reference designators", _duplicate_refs),
    Rule("board-edge", "geometry",
         "parts outside or too near the board outline", _board_edge),
    Rule("connectivity", "connectivity",
         "nets that are unrouted, split, or reach one pad", _net_connectivity),
    Rule("pad-clearance", "clearance",
         "pads of different nets closer than the clearance rule",
         _pad_clearance),
    Rule("track-clearance", "clearance",
         "tracks and vias too close to foreign copper", _track_clearance),
    Rule("silkscreen-over-pad", "clearance",
         "silkscreen ink on solderable area", _silk_over_pad),
    Rule("decoupling", "practice",
         "supply pins with no nearby capacitor to ground", _decoupling),
    Rule("copper-off-board", "practice",
         "tracks crossing the board outline", _copper_off_board),
    Rule("track-widths", "practice",
         "unmanufacturable or under-sized tracks", _track_widths),
    Rule("dangling-copper", "manufacturing",
         "track ends that touch nothing", _dangling_copper),
    Rule("acute-angles", "manufacturing",
         "acid-trap corners", _acute_angles),
)


def rules_for(profile: EffortProfile) -> tuple[Rule, ...]:
    return tuple(rule for rule in RULES if rule.group in profile.groups)


def run_rules(
    board: AuditBoard, profile: EffortProfile
) -> tuple[list[Finding], list[str]]:
    """Run every rule the profile enables. Returns findings and rule names.

    A rule that raises is reported as a finding of its own rather than taking
    the review down: a checker that crashed on one board must not make the
    other twelve checks unavailable, and it must not look like a clean bill.
    """
    findings: list[Finding] = []
    ran: list[str] = []
    for rule in rules_for(profile):
        try:
            findings.extend(rule.check(board, profile))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            findings.append(
                _f(
                    "checker-failed",
                    severity=Severity.NOTE,
                    title=f"the {rule.name} check could not run",
                    detail=(
                        f"{type(exc).__name__}: {exc}. This board was not checked "
                        f"for {rule.summary}."
                    ),
                    evidence=f"rule {rule.name} raised {type(exc).__name__}",
                    fix="Report this board as a checker bug.",
                )
            )
        ran.append(rule.name)
    return findings, ran

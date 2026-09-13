"""Order preparation, and the manufacturability gate that stands in front of it.

Ordering is where a design tool stops being a toy. A wrong board is no longer a
bad drawing: it is scrap copper, a week of lead time and someone's money. So
this module is written the way the rest of the engine is written -- ``netlist``
refuses an inconsistent circuit, ``footprints`` refuses to invent a land
pattern -- and it refuses to call a board orderable when it is not.

The refusal that matters most today is ``unrouted-nets``. A
Placement is not a finished board. Until :func:`~silkscreen.board.route_board`
has laid copper, every net reaching two or more pads is still open, and a board
fabricated in that state arrives electrically dead -- it looks exactly like the
finished article and does nothing whatsoever. So the gate reads the router's own
verdict on the board and blocks on whatever is still open. There is deliberately
no flag to suppress it: a net the router could not finish is a net without
copper, whoever is in a hurry.

Nothing here contacts a fabricator, prices a board, or spends money. The output
is a manifest and a zip for a **human** to read and submit.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, fields
from enum import StrEnum

from .board import BoardResult
from .netlist import CircuitSpec
from .packing import Layer, PackStatus
from .units import NM_PER_MM, mm, to_mm

__all__ = [
    "OrderIssueSeverity",
    "OrderIssue",
    "SurfaceFinish",
    "SolderMaskColour",
    "OrderOptions",
    "OrderPreflight",
    "preflight",
    "board_summary",
    "order_manifest",
    "package_zip",
]


class OrderIssueSeverity(StrEnum):
    """How hard a finding pushes back on the order."""

    #: Must not be ordered. One of these makes the board un-orderable, period.
    BLOCKER = "blocker"
    #: Orderable, but the buyer should know before paying.
    WARNING = "warning"
    NOTE = "note"


#: Sort order for issues. Keyed by the enum *value* so a plain string severity
#: (``"blocker"``) ranks the same as the member -- ``Enum.__hash__`` hashes the
#: member name, so a member-keyed dict would miss on the string.
_SEVERITY_RANK: dict[str, int] = {
    OrderIssueSeverity.BLOCKER.value: 0,
    OrderIssueSeverity.WARNING.value: 1,
    OrderIssueSeverity.NOTE.value: 2,
}


@dataclass(frozen=True)
class OrderIssue:
    """One reason an order should be stopped, questioned, or merely noted."""

    code: str
    severity: OrderIssueSeverity
    title: str
    detail: str
    parts: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        """JSON-safe form, for the manifest."""
        return {
            "code": self.code,
            "severity": str(self.severity),
            "title": self.title,
            "detail": self.detail,
            "parts": list(self.parts),
        }


class SurfaceFinish(StrEnum):
    HASL = "hasl"
    LEAD_FREE_HASL = "lead_free_hasl"
    ENIG = "enig"


class SolderMaskColour(StrEnum):
    GREEN = "green"
    RED = "red"
    BLUE = "blue"
    BLACK = "black"
    WHITE = "white"
    YELLOW = "yellow"


_LAYER_COUNTS = (1, 2, 4, 6)
_THICKNESSES_MM = (0.6, 0.8, 1.0, 1.2, 1.6, 2.0)
_ASSEMBLY_SIDES = ("top", "bottom", "both")
_MAX_PANEL_REPEATS = 10


def _check_int(name: str, value: object, *, minimum: int, maximum: int) -> None:
    """Integer range check that does not accept a bool.

    ``bool`` is an ``int`` subclass, so ``quantity=True`` would otherwise sail
    through as a quantity of one and ship a board nobody asked for.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}, got {value}"
        )


@dataclass(frozen=True)
class OrderOptions:
    """What the buyer is asking the fab for.

    Every field is validated on construction: an option a fab would reject is
    caught here rather than at the checkout page of a website, or -- worse --
    accepted by a fab that quietly substitutes something else.
    """

    quantity: int = 5
    layers: int = 2
    thickness_mm: float = 1.6
    surface_finish: SurfaceFinish = SurfaceFinish.LEAD_FREE_HASL
    mask_colour: SolderMaskColour = SolderMaskColour.GREEN
    silkscreen_white: bool = True
    #: SMT assembly by the fab or contract manufacturer, rather than bare boards.
    assembly: bool = False
    assembly_side: str = "top"
    panel_columns: int = 1
    panel_rows: int = 1

    def __post_init__(self) -> None:
        _check_int("quantity", self.quantity, minimum=1, maximum=1_000_000)
        if self.layers not in _LAYER_COUNTS:
            raise ValueError(
                f"layers must be one of {list(_LAYER_COUNTS)}, got {self.layers!r}"
            )
        if (
            isinstance(self.thickness_mm, bool)
            or not isinstance(self.thickness_mm, (int, float))
            or float(self.thickness_mm) not in _THICKNESSES_MM
        ):
            raise ValueError(
                f"thickness_mm must be one of {list(_THICKNESSES_MM)}, "
                f"got {self.thickness_mm!r}"
            )
        if self.assembly_side not in _ASSEMBLY_SIDES:
            raise ValueError(
                f"assembly_side must be one of {list(_ASSEMBLY_SIDES)}, "
                f"got {self.assembly_side!r}"
            )
        _check_int(
            "panel_columns", self.panel_columns, minimum=1, maximum=_MAX_PANEL_REPEATS
        )
        _check_int(
            "panel_rows", self.panel_rows, minimum=1, maximum=_MAX_PANEL_REPEATS
        )

    def assembled_sides(self) -> tuple[str, ...]:
        """The board sides ``assembly_side`` actually names."""
        if self.assembly_side == "both":
            return (Layer.TOP.value, Layer.BOTTOM.value)
        return (self.assembly_side,)


@dataclass(frozen=True)
class OrderPreflight:
    """The verdict on a board, as a list of findings rather than a bare bool."""

    issues: tuple[OrderIssue, ...]

    @property
    def blockers(self) -> tuple[OrderIssue, ...]:
        return tuple(
            i for i in self.issues if i.severity == OrderIssueSeverity.BLOCKER
        )

    @property
    def orderable(self) -> bool:
        """True only when nothing blocks. Derived, so it cannot disagree."""
        return not self.blockers


#: Below this on either side, many fabs surcharge or refuse outright.
_MIN_BOARD_NM = mm(10)

#: How many net names an issue detail spells out before it starts counting.
_MAX_LISTED_NETS = 8

#: Placement statuses that mean CP-SAT actually optimised the layout.
#: ``PackStatus.FEASIBLE`` belongs here: it is the normal outcome on a real
#: board (the time limit expires before optimality is *proven*, not before a
#: real solution is found), so warning on it would cry wolf on every order.
_SOLVED_STATUSES = frozenset(
    {PackStatus.OPTIMAL.value, PackStatus.FEASIBLE.value}
)


def _pad_net_counts(board: BoardResult) -> dict[str, int]:
    """How many pads carry each net name, across every placed footprint."""
    counts: dict[str, int] = {}
    for part in board.parts:
        for pad in part.footprint.pads:
            if pad.net:
                counts[pad.net] = counts.get(pad.net, 0) + 1
    return counts


def _side_counts(board: BoardResult) -> dict[str, int]:
    """Parts per board side.

    ``Layer.EITHER`` survives as its own key rather than being folded into
    "top": an undecided side is not the same claim as a decided one, and the
    assembler is the last person who should be guessing which it is.
    """
    counts = {Layer.TOP.value: 0, Layer.BOTTOM.value: 0}
    for part in board.parts:
        name = str(getattr(part.layer, "value", part.layer))
        counts[name] = counts.get(name, 0) + 1
    return counts


def _net_names(board: BoardResult, pad_counts: dict[str, int]) -> tuple[str, ...]:
    """Every net name on the board: declared, or found on a pad, or both."""
    declared = {name for name in board.nets if name}
    return tuple(sorted(declared | set(pad_counts)))


def _nets_needing_copper(
    board: BoardResult, spec: CircuitSpec | None = None
) -> tuple[str, ...]:
    """Nets joining two or more pads, which is to say nets that need routing.

    Pads are the physical truth, so they lead. A ``spec`` widens the answer:
    a connection the model asked for that never reached a pad still needs
    copper, and staying silent about it would be the flattering answer rather
    than the true one.
    """
    names = {name for name, count in _pad_net_counts(board).items() if count >= 2}
    if spec is not None:
        names |= {
            conn.net
            for conn in spec.connections
            if conn.net and len(set(conn.endpoints)) >= 2
        }
    return tuple(sorted(names))


def _mm3(value_nm: int) -> float:
    """Nanometres to millimetres for display. Geometry stays integer nm."""
    return round(to_mm(value_nm), 3)


def _summarise_nets(names: Sequence[str]) -> str:
    """First few net names, then a count -- a 200-net detail helps nobody."""
    shown = ", ".join(names[:_MAX_LISTED_NETS])
    remainder = len(names) - _MAX_LISTED_NETS
    return f"{shown}, and {remainder} more" if remainder > 0 else shown


def _finalise(issues: Sequence[OrderIssue]) -> tuple[OrderIssue, ...]:
    """Deduplicate by (code, parts), then sort by severity and code."""
    seen: set[tuple[str, tuple[str, ...]]] = set()
    unique: list[OrderIssue] = []
    for issue in issues:
        key = (issue.code, issue.parts)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    unique.sort(
        key=lambda i: (
            _SEVERITY_RANK.get(str(i.severity), len(_SEVERITY_RANK)),
            i.code,
        )
    )
    return tuple(unique)


def preflight(
    board: BoardResult,
    *,
    spec: CircuitSpec | None = None,
    options: OrderOptions | None = None,
) -> OrderPreflight:
    """Decide whether ``board`` may be ordered, and say why not.

    Conservative by construction: every check reports what is true of the board
    as it stands, and none of them can be waived by an argument. A board that
    cannot work is never described as orderable.
    """
    if options is None:
        options = OrderOptions()

    issues: list[OrderIssue] = []

    # A net the router finished no longer needs copper. The board carries that
    # verdict itself, so the gate reads the same field the router wrote rather
    # than trusting a caller to pass the good news along. Only fully routed
    # nets clear: anything the router left open keeps blocking, which is the
    # whole point of the gate.
    open_nets = tuple(
        net
        for net in _nets_needing_copper(board, spec)
        if net not in set(board.routed_nets)
    )
    if open_nets:
        issues.append(
            OrderIssue(
                code="unrouted-nets",
                severity=OrderIssueSeverity.BLOCKER,
                title=(
                    f"{len(open_nets)} net(s) have no copper connecting them"
                ),
                detail=(
                    f"{len(open_nets)} net(s) join two or more pads with no "
                    f"copper between them: {_summarise_nets(open_nets)}. "
                    f"Fabricated as it stands, the board would arrive "
                    f"electrically dead -- correct parts, correct outline, no "
                    f"circuit. Run the router over it before ordering, and "
                    f"check what it reports it could not finish."
                ),
            )
        )

    if not board.parts:
        issues.append(
            OrderIssue(
                code="no-parts",
                severity=OrderIssueSeverity.BLOCKER,
                title="The board has no placed parts",
                detail=(
                    "The placement contains zero footprints, so there is "
                    "nothing to fabricate and nothing to assemble."
                ),
            )
        )

    if board.width_nm <= 0 or board.height_nm <= 0:
        issues.append(
            OrderIssue(
                code="degenerate-outline",
                severity=OrderIssueSeverity.BLOCKER,
                title="Board outline has no area",
                detail=(
                    f"The outline measures {board.width_nm} x "
                    f"{board.height_nm} nm ({_mm3(board.width_nm)} x "
                    f"{_mm3(board.height_nm)} mm). A board needs a positive "
                    f"extent on both axes before anyone can cut it out."
                ),
            )
        )

    if board.width_nm < _MIN_BOARD_NM or board.height_nm < _MIN_BOARD_NM:
        issues.append(
            OrderIssue(
                code="tiny-board",
                severity=OrderIssueSeverity.WARNING,
                title="Board is under 10 mm on at least one side",
                detail=(
                    f"The outline is {_mm3(board.width_nm)} x "
                    f"{_mm3(board.height_nm)} mm. Many fabs have a minimum "
                    f"billable size and will either surcharge this, panelise "
                    f"it on your behalf, or decline the job."
                ),
            )
        )

    status = str(board.solver_status)
    if status not in _SOLVED_STATUSES:
        issues.append(
            OrderIssue(
                code="fallback-placement",
                severity=OrderIssueSeverity.WARNING,
                title=f"Placement status is {status!r}, not a solved layout",
                detail=(
                    f"The layout was not solved to the normal objective "
                    f"(board size plus wirelength). Status {status!r} means "
                    f"CP-SAT returned nothing usable in its time budget and a "
                    f"deterministic shelf packing was substituted: valid and "
                    f"non-overlapping, but larger and longer-wired than a "
                    f"solved board, and it ignores edge, rotation, keepout and "
                    f"pinning requests. Re-solve with a longer time limit "
                    f"before committing money to it."
                ),
            )
        )

    if board.warnings:
        issues.append(
            OrderIssue(
                code="solver-warnings",
                severity=OrderIssueSeverity.NOTE,
                title=f"The placer emitted {len(board.warnings)} warning(s)",
                detail=(
                    f"{len(board.warnings)} warning(s) were recorded while "
                    f"placing this board; the first is: "
                    f"{board.warnings[0]!r}. The full list stays on the "
                    f"BoardResult."
                ),
            )
        )

    sides = _side_counts(board)
    if options.assembly:
        empty = [side for side in options.assembled_sides() if not sides.get(side)]
        if empty:
            issues.append(
                OrderIssue(
                    code="assembly-without-bottom-parts",
                    severity=OrderIssueSeverity.NOTE,
                    title=(
                        f"Assembly requested for a side with no parts: "
                        f"{', '.join(empty)}"
                    ),
                    detail=(
                        f"assembly_side is {options.assembly_side!r}, but the "
                        f"board has no parts on: {', '.join(empty)}. Paying "
                        f"for a setup on an empty side buys nothing; narrow "
                        f"assembly_side to the side that carries parts."
                    ),
                )
            )

    bottom_refs = tuple(
        sorted(
            part.ref
            for part in board.parts
            if str(getattr(part.layer, "value", part.layer)) == Layer.BOTTOM.value
        )
    )
    if sides.get(Layer.TOP.value) and bottom_refs:
        issues.append(
            OrderIssue(
                code="two-sided-assembly-cost",
                severity=OrderIssueSeverity.NOTE,
                title="Parts are on both sides of the board",
                detail=(
                    f"{sides[Layer.TOP.value]} part(s) on top and "
                    f"{len(bottom_refs)} on the bottom. Double-sided assembly "
                    f"is two stencils, two reflow passes and two setup "
                    f"charges; it is normal, but it is not the cheap option."
                ),
                parts=bottom_refs,
            )
        )

    return OrderPreflight(issues=_finalise(issues))


def board_summary(board: BoardResult, options: OrderOptions) -> dict:
    """Geometry, part and net counts, and the panel maths, as a JSON-safe dict.

    All arithmetic is in integer nanometres; the conversion to millimetres
    happens once, at the boundary, for the benefit of the human reading it.

    ``net_count`` is every distinct net name known to the board -- declared on
    the :class:`~silkscreen.board.BoardResult` or found on a pad. The panel
    figures are the plain product of board size and repeat count: **panel gaps,
    rails, mouse-bites and V-score allowances are not modelled**, which the key
    names say out loud so nobody quotes them as a panel size.
    """
    pad_counts = _pad_net_counts(board)
    net_names = _net_names(board, pad_counts)
    multi_pad = [name for name in net_names if pad_counts.get(name, 0) >= 2]

    area_nm2 = board.width_nm * board.height_nm
    panel_width_nm = board.width_nm * options.panel_columns
    panel_height_nm = board.height_nm * options.panel_rows

    return {
        "width_mm": _mm3(board.width_nm),
        "height_mm": _mm3(board.height_nm),
        "area_mm2": round(area_nm2 / (NM_PER_MM * NM_PER_MM), 3),
        "part_count": len(board.parts),
        "parts_by_side": _side_counts(board),
        "net_count": len(net_names),
        "nets_with_two_or_more_pads": len(multi_pad),
        "solver_status": str(board.solver_status),
        "panel_columns": options.panel_columns,
        "panel_rows": options.panel_rows,
        "boards_per_panel": options.panel_columns * options.panel_rows,
        "panel_width_mm_no_gaps_or_rails": _mm3(panel_width_nm),
        "panel_height_mm_no_gaps_or_rails": _mm3(panel_height_nm),
    }


_DISCLAIMER = (
    "This manifest describes an intended order for a human to review and "
    "submit. Nothing has been purchased, no order has been placed and no "
    "fabricator has been contacted. Silkscreen does not transact."
)


def _options_dict(options: OrderOptions) -> dict:
    """Every field of :class:`OrderOptions`, JSON-safe.

    Read off ``dataclasses.fields`` rather than listed by hand, so a field
    added later cannot go missing from the order description.
    """
    out: dict = {}
    for spec in fields(options):
        value = getattr(options, spec.name)
        out[spec.name] = str(value) if isinstance(value, StrEnum) else value
    return out


def order_manifest(
    board: BoardResult, options: OrderOptions, pre: OrderPreflight
) -> dict:
    """The complete, JSON-safe description of the order being proposed.

    ``orderable`` is read straight off the preflight, where it is derived from
    the issue list rather than stored: the manifest has no way to claim a board
    is orderable while a blocker stands against it.
    """
    return {
        "generator": "silkscreen",
        "board": board_summary(board, options),
        "options": _options_dict(options),
        "issues": [issue.as_dict() for issue in pre.issues],
        "blocker_count": len(pre.blockers),
        "orderable": pre.orderable,
        "requires_human_approval": True,
        "disclaimer": _DISCLAIMER,
    }


MANIFEST_FILENAME = "order-manifest.json"

#: Every entry is stamped with the earliest timestamp the zip format allows, so
#: two runs over the same inputs produce byte-identical archives. A real
#: mtime would make the bytes -- and any hash taken over them -- differ every
#: time, which destroys the only cheap way to tell two packages apart.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def package_zip(files: Sequence[tuple[str, str]], manifest: dict) -> bytes:
    """Bundle ``(filename, text)`` pairs plus the manifest into zip bytes.

    Deterministic: entries sorted by name, fixed timestamps, and the file mode
    and creator pinned rather than taken from the host, so the bytes match
    across machines and not merely across runs.

    This does not refuse to package a blocked board, and should not: the
    manifest inside carries the verdict, and a human needs to be able to read
    the package that explains why they cannot order it yet.
    """
    payload: dict[str, str] = {}
    for name, text in files:
        if name == MANIFEST_FILENAME or name in payload:
            raise ValueError(
                f"duplicate entry {name!r} in the order package; zip would "
                f"store both copies and readers disagree about which wins"
            )
        payload[name] = text
    payload[MANIFEST_FILENAME] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(payload):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            # A hand-built ZipInfo defaults to ZIP_STORED and picks up the host
            # platform as its creator; both are set explicitly here.
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload[name].encode("utf-8"))
    return buffer.getvalue()

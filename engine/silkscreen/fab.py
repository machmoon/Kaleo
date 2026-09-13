"""Fabrication output: Gerber, Excellon, BOM and pick-and-place.

A ``.kicad_pcb`` is a design file, not an order. Nobody at a board house opens
one: what they take is a set of RS-274X Gerbers, a drill file, a bill of
materials and a pick-and-place list. Without those, "the pipeline produced a
board" stops one step short of a board anyone can actually buy.

This module renders a :class:`~silkscreen.board.BoardResult` straight into that
set, with no KiCad process and no plotting library, for the same reason the rest
of the engine has neither: the geometry is already exact, so re-deriving it
through a third tool can only lose precision.

Two coordinate frames meet here and only one of them is KiCad's.
:func:`~silkscreen.board.emit_kicad_pcb` flips Y on the way out because KiCad's
canvas is Y-down; **Gerber is Y-up**, the same handedness as the solver, so
nothing is flipped here. Instead every emitted coordinate is shifted by the
outline margin, which puts the board's bottom-left corner at exactly (0, 0):
some fabs reject a file containing negative coordinates outright.

Everything stays in integer nanometres until the final ``str()``. That is more
than house style here. Under ``%FSLAX46Y46*%`` with ``%MOMM*%`` a coordinate has
four integer and six decimal digits of a millimetre, so one Gerber coordinate
unit *is* one nanometre -- the engine's integer-nm values are the Gerber
coordinates, written verbatim, with no scaling step left to get wrong.

Output is a pure function of the board -- no timestamps, no UUIDs, no locale --
so regenerating an unchanged design produces byte-identical files.

Two limits are worth stating rather than discovering at a fab. Bottom-side
geometry is emitted at the same coordinates as the top, which is the Gerber
convention of one top-viewed frame shared by every layer; whether that agrees
with how KiCad renders a mirrored back-side footprint has not been verified
against a running KiCad, and it cannot bite today because nothing in the
pipeline enables ``two_sided`` placement. Pads are emitted as plain rectangles
where the ``.kicad_pcb`` writes ``roundrect``: standard Gerber has no roundrect
aperture, and the corners differ by a fraction of a millimetre.
"""

from __future__ import annotations

from dataclasses import dataclass

from .board import BoardResult, PlacedPart
from .footprints import silk_segments
from .packing import Layer
from .units import NM_PER_MM, mm

__all__ = [
    "GENERATOR",
    "FabLayer",
    "gerber_copper",
    "gerber_mask",
    "gerber_paste",
    "gerber_silkscreen",
    "gerber_outline",
    "excellon_drill",
    "bom_csv",
    "cpl_csv",
    "fab_files",
]

#: Name written into every file's generator attribute.
GENERATOR = "silkscreen"

#: The outline margin :func:`silkscreen.board.emit_kicad_pcb` draws around the
#: placement. The two must agree: this is the same physical rectangle, emitted
#: twice in two formats, so a change there is a change here.
_MARGIN_NM = mm(2.0)

#: Stroke widths: thin enough not to distort the geometry they trace, wide
#: enough to clear every fab's minimum-feature check.
_OUTLINE_WIDTH_NM = mm(0.1)
_SILK_WIDTH_NM = mm(0.12)

#: Soldermask expansion per side. 0.051 mm (2 mil) is the industry-default
#: opening enlargement: enough that a small registration error still leaves the
#: pad clear, small enough not to bridge the mask web between fine-pitch pads.
_MASK_EXPANSION_NM = 51_000

#: Aperture numbering starts at D10 because D00-D09 are reserved for Gerber's
#: own operation codes (D01 draw, D02 move, D03 flash).
_FIRST_APERTURE = 10


@dataclass(frozen=True)
class FabLayer:
    """One manufacturing file: its conventional name and its full text."""

    filename: str
    content: str


# ------------------------------------------------------------------ formatting


def _mm_text(nm: int) -> str:
    """Integer nanometres -> a trimmed decimal millimetre string.

    Done with integer division rather than ``nm / NM_PER_MM`` so an aperture
    size can never pick up a float artefact like ``0.8999999999999999`` -- which
    a strict Gerber reader is entitled to reject and a lax one will round.
    """
    sign = "-" if nm < 0 else ""
    whole, frac = divmod(abs(nm), NM_PER_MM)
    text = f"{whole}.{frac:06d}".rstrip("0").rstrip(".")
    return f"{sign}{text}"


def _mm4(nm: int) -> str:
    """Integer nanometres -> millimetres at exactly 4 decimals, rounded half-up.

    Four decimals is 0.1 um, finer than any pick-and-place tolerance and the
    resolution assembly importers expect. Kept in integers so the rounding is
    the same on every platform.
    """
    sign = "-" if nm < 0 else ""
    tenths_of_um = (abs(nm) + 50) // 100
    return f"{sign}{tenths_of_um // 10_000}.{tenths_of_um % 10_000:04d}"


#: Leading characters a spreadsheet reads as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_field(value: str) -> str:
    """Quote a CSV field only when it needs it, doubling embedded quotes.

    Component values and references in this file come from a model reading a
    vendor's datasheet, so they are untrusted text on a path that ends in
    someone opening the BOM in a spreadsheet. A cell beginning ``=``, ``+``,
    ``-`` or ``@`` is evaluated as a formula there, which turns "the value of
    C1" into code running on the buyer's machine. Prefixing an apostrophe is
    the standard neutralisation: the spreadsheet shows the text and runs
    nothing, and a plain CSV reader sees one extra leading character.
    """
    if value.startswith(_FORMULA_PREFIXES):
        value = "'" + value
    if any(char in value for char in ',"\n\r'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _csv_row(fields: list[str]) -> str:
    return ",".join(_csv_field(field) for field in fields)


def _natural_key(ref: str) -> tuple[str, int, str]:
    """Sort C2 before C10, which plain string ordering does not."""
    prefix = ref.rstrip("0123456789")
    digits = ref[len(prefix) :]
    return (prefix, int(digits) if digits else -1, ref)


# -------------------------------------------------------------------- geometry


def _is_bottom(part: PlacedPart) -> bool:
    """Whether a part sits on the underside.

    Tested against ``BOTTOM`` rather than ``TOP`` on purpose: a leftover
    :attr:`~silkscreen.packing.Layer.EITHER` means the solver never chose a
    side, and putting it on top keeps its copper in the output instead of
    dropping the pads from both sides where nobody would notice.
    """
    return part.layer is Layer.BOTTOM


def _anchor_nm(part: PlacedPart) -> tuple[int, int]:
    """The footprint anchor in shifted Gerber coordinates.

    ``PlacedPart.x_nm``/``y_nm`` locate the courtyard's bottom-left corner in
    the solver's Y-up frame, and ``courtyard_w_nm``/``courtyard_h_nm`` are half
    extents, so the anchor is one half extent in along each axis. This is the
    same expression :func:`silkscreen.board.emit_kicad_pcb` uses; it differs
    there only by the ``board.height_nm -`` that turns Y-up into KiCad's Y-down.
    """
    fp = part.footprint
    return (
        part.x_nm + fp.courtyard_w_nm + _MARGIN_NM,
        part.y_nm + fp.courtyard_h_nm + _MARGIN_NM,
    )


def _rotate_offset(dx_nm: int, dy_nm: int, rotated: bool) -> tuple[int, int]:
    """Apply a part's 90-degree rotation to an anchor-relative offset.

    KiCad is handed the angle and rotates the footprint itself. Gerber has no
    concept of a part -- only flashes and strokes at absolute coordinates -- so
    the rotation has to be baked into every coordinate before it is written.

    A counter-clockwise quarter turn in this Y-up frame is (x, y) -> (-y, x).
    That is the same rotation the placer applies in ``packing.endpoint``, where
    it reads as (ox, oy) -> (H - oy, ox) only because those offsets are measured
    from the box's corner rather than from its centre: shifting a corner-relative
    (ox, oy) to centre-relative (ox - W/2, oy - H/2), turning it to
    (H/2 - oy, ox - W/2), then re-cornering against the rotated H x W box by
    adding (H/2, W/2) gives exactly (H - oy, ox).
    """
    return (-dy_nm, dx_nm) if rotated else (dx_nm, dy_nm)


def _rotate_size(w_nm: int, h_nm: int, rotated: bool) -> tuple[int, int]:
    """A quarter turn swaps an axis-aligned rectangle's width and height."""
    return (h_nm, w_nm) if rotated else (w_nm, h_nm)


@dataclass(frozen=True)
class _Flash:
    """A pad reduced to what a Gerber aperture can say: a centred rectangle."""

    x_nm: int
    y_nm: int
    w_nm: int
    h_nm: int


def _flashes(
    board: BoardResult, *, bottom: bool, grow_nm: int = 0, paste: bool = False
) -> list[_Flash]:
    """Every pad on one side, in board coordinates, optionally grown per side.

    A plated through-hole pad is copper on *both* faces whichever side its
    part sits on (the contract :class:`~silkscreen.footprints.Pad` states and
    ``emit_kicad_pcb`` honours), so it is flashed on each side. With ``paste``
    set it is left out instead: a hole stencilled with solder paste is a
    reflow defect, not a joint.
    """
    out: list[_Flash] = []
    for part in board.parts:
        on_this_side = _is_bottom(part) == bottom
        anchor_x, anchor_y = _anchor_nm(part)
        for pad in part.footprint.pads:
            if pad.is_tht:
                if paste:
                    continue
            elif not on_this_side:
                continue
            off_x, off_y = _rotate_offset(pad.x_nm, pad.y_nm, part.rotated)
            width, height = _rotate_size(pad.w_nm, pad.h_nm, part.rotated)
            out.append(
                _Flash(
                    x_nm=anchor_x + off_x,
                    y_nm=anchor_y + off_y,
                    w_nm=width + 2 * grow_nm,
                    h_nm=height + 2 * grow_nm,
                )
            )
    return out


# ---------------------------------------------------------------- gerber writer


class _GerberFile:
    """Builder for one RS-274X layer.

    Apertures are assigned on first use and deduplicated by (shape, size), but
    the whole table has to appear before any command that selects from it, so
    the drawing commands are buffered and the header rendered last.
    """

    def __init__(self, file_function: str) -> None:
        self._file_function = file_function
        self._apertures: dict[tuple[str, int, int], int] = {}
        self._definitions: list[str] = []
        self._body: list[str] = []
        self._selected: int | None = None

    def _aperture(self, key: tuple[str, int, int], definition: str) -> int:
        code = self._apertures.get(key)
        if code is None:
            code = _FIRST_APERTURE + len(self._apertures)
            self._apertures[key] = code
            self._definitions.append(f"%ADD{code}{definition}*%")
        return code

    def rect(self, w_nm: int, h_nm: int) -> int:
        """A rectangular aperture, used to flash pads."""
        return self._aperture(
            ("R", w_nm, h_nm), f"R,{_mm_text(w_nm)}X{_mm_text(h_nm)}"
        )

    def circle(self, diameter_nm: int) -> int:
        """A round aperture, used as the pen for stroked outlines."""
        key = ("C", diameter_nm, diameter_nm)
        return self._aperture(key, f"C,{_mm_text(diameter_nm)}")

    def select(self, code: int) -> None:
        """Make ``code`` current, skipping a redundant reselect."""
        if self._selected != code:
            self._body.append(f"D{code}*")
            self._selected = code

    def flash(self, x_nm: int, y_nm: int) -> None:
        self._body.append(f"X{x_nm}Y{y_nm}D03*")

    def line(self, x0_nm: int, y0_nm: int, x1_nm: int, y1_nm: int) -> None:
        self._body.append(f"X{x0_nm}Y{y0_nm}D02*")
        self._body.append(f"X{x1_nm}Y{y1_nm}D01*")

    def render(self) -> str:
        # Deliberately no %TF.CreationDate: a timestamp is the one attribute
        # that would stop identical designs producing identical files.
        out = [
            "%FSLAX46Y46*%",
            "%MOMM*%",
            f"G04 Generated by {GENERATOR}*",
            "G04 Format 4.6 in mm makes one coordinate unit exactly 1 nm, so*",
            "G04 the engine's integer-nanometre geometry is written verbatim*",
            f"%TF.GenerationSoftware,{GENERATOR},{GENERATOR}*%",
            f"%TF.FileFunction,{self._file_function}*%",
            "%TF.FilePolarity,Positive*%",
            "%LPD*%",
        ]
        out += self._definitions
        out.append("G01*")
        out += self._body
        out.append("M02*")
        return "\n".join(out) + "\n"


def _rectangle(
    gerber: _GerberFile, x0_nm: int, y0_nm: int, x1_nm: int, y1_nm: int
) -> None:
    """Stroke a closed axis-aligned rectangle as four separate segments."""
    corners = [(x0_nm, y0_nm), (x1_nm, y0_nm), (x1_nm, y1_nm), (x0_nm, y1_nm)]
    for i in range(4):
        start_x, start_y = corners[i]
        end_x, end_y = corners[(i + 1) % 4]
        gerber.line(start_x, start_y, end_x, end_y)


def _flashed_layer(
    board: BoardResult,
    file_function: str,
    *,
    bottom: bool,
    grow_nm: int = 0,
    paste: bool = False,
) -> str:
    """Render one side's pads as flashes -- the shape copper, mask and paste share."""
    gerber = _GerberFile(file_function)
    for flash in _flashes(board, bottom=bottom, grow_nm=grow_nm, paste=paste):
        gerber.select(gerber.rect(flash.w_nm, flash.h_nm))
        gerber.flash(flash.x_nm, flash.y_nm)
    return gerber.render()


# ---------------------------------------------------------------------- layers


def gerber_copper(board: BoardResult, *, bottom: bool = False) -> str:
    """Copper for one side: every pad on that side, flashed, plus any traces.

    Pads are flashed, then any copper :func:`silkscreen.board.route_board` laid
    on this side is stroked over them, and every via is flashed as an annular
    ring -- a via pierces both layers, so it appears on each. A board that was
    never routed simply has no tracks and produces valid copper with no
    connections in it.

    ``L1``/``L2`` name the two copper layers of the stack-up that
    :func:`silkscreen.board.emit_kicad_pcb` declares (F.Cu and B.Cu, nothing
    between them).

    Pads are emitted as plain rectangles. KiCad writes them as rounded
    rectangles with a 0.25 corner ratio, which in Gerber needs an aperture
    macro; the square-cornered approximation is within the corner radius
    everywhere and is universally accepted, but it is an approximation.
    """
    function = "Copper,L2,Bot" if bottom else "Copper,L1,Top"
    gerber = _GerberFile(function)
    for flash in _flashes(board, bottom=bottom):
        gerber.select(gerber.rect(flash.w_nm, flash.h_nm))
        gerber.flash(flash.x_nm, flash.y_nm)
    side = Layer.BOTTOM if bottom else Layer.TOP
    for track in board.tracks:
        if track.layer is not side:
            continue
        gerber.select(gerber.circle(track.width_nm))
        gerber.line(
            track.start_x_nm + _MARGIN_NM,
            track.start_y_nm + _MARGIN_NM,
            track.end_x_nm + _MARGIN_NM,
            track.end_y_nm + _MARGIN_NM,
        )
    for via in board.vias:
        gerber.select(gerber.circle(via.diameter_nm))
        gerber.flash(via.x_nm + _MARGIN_NM, via.y_nm + _MARGIN_NM)
    return gerber.render()


def gerber_mask(
    board: BoardResult,
    *,
    bottom: bool = False,
    expansion_nm: int = _MASK_EXPANSION_NM,
) -> str:
    """Soldermask openings for one side, each pad grown by ``expansion_nm``.

    The expansion is per side, so a pad's opening is ``w + 2 * expansion_nm``
    wide. The file is a *positive* of the openings, which is the convention
    every fab's CAM step expects; it inverts it against the mask coat itself.
    """
    function = "Soldermask,Bot" if bottom else "Soldermask,Top"
    return _flashed_layer(board, function, bottom=bottom, grow_nm=expansion_nm)


def gerber_paste(board: BoardResult, *, bottom: bool = False) -> str:
    """Stencil apertures for one side, at 1:1 with the pads.

    No area reduction is applied. Reduction is a stencil-house decision that
    depends on foil thickness and the specific part, and guessing one here
    would silently change how much solder every joint gets.
    """
    function = "Paste,Bot" if bottom else "Paste,Top"
    return _flashed_layer(board, function, bottom=bottom, paste=True)


def gerber_silkscreen(board: BoardResult, *, bottom: bool = False) -> str:
    """Component body outlines for one side, stroked with a 0.12 mm pen.

    Parts with no body extents contribute nothing: a zero-size rectangle would
    be four zero-length strokes, which is four dots of ink on the board.
    """
    function = "Legend,Bot" if bottom else "Legend,Top"
    gerber = _GerberFile(function)
    for part in board.parts:
        if _is_bottom(part) != bottom:
            continue
        fp = part.footprint
        # The outline comes pre-clipped clear of the pads (see
        # footprints.silk_segments) and each endpoint goes through the same
        # rotation as the pads, so ink and copper cannot disagree about where
        # the pads are.
        segments = silk_segments(fp)
        if not segments:
            continue
        anchor_x, anchor_y = _anchor_nm(part)
        gerber.select(gerber.circle(_SILK_WIDTH_NM))
        for x0, y0, x1, y1 in segments:
            sx, sy = _rotate_offset(x0, y0, part.rotated)
            ex, ey = _rotate_offset(x1, y1, part.rotated)
            gerber.line(anchor_x + sx, anchor_y + sy, anchor_x + ex, anchor_y + ey)
    return gerber.render()


def gerber_outline(board: BoardResult) -> str:
    """The board profile: the rectangle ``emit_kicad_pcb`` puts on Edge.Cuts.

    ``NP`` marks the profile as non-plated -- a plain routed edge, not a
    plated-through castellation.
    """
    gerber = _GerberFile("Profile,NP")
    gerber.select(gerber.circle(_OUTLINE_WIDTH_NM))
    # emit_kicad_pcb spans (-margin, -margin) to (width + margin, height +
    # margin). The +margin shift every coordinate here carries slides that same
    # rectangle to (0, 0)..(width + 2*margin, height + 2*margin).
    _rectangle(
        gerber,
        0,
        0,
        board.width_nm + 2 * _MARGIN_NM,
        board.height_nm + 2 * _MARGIN_NM,
    )
    return gerber.render()


def _plated_holes(board: BoardResult) -> list[tuple[int, int, int]]:
    """Every plated hole as ``(drill_nm, x_nm, y_nm)`` in shifted coordinates.

    Through-hole pads first, in part order, then the vias the router placed.
    A pad's hole sits at the pad's centre, through the same anchor and
    quarter-turn arithmetic as its copper, so the drill cannot land beside
    the annular ring it is meant to pierce.
    """
    holes: list[tuple[int, int, int]] = []
    for part in board.parts:
        anchor_x, anchor_y = _anchor_nm(part)
        for pad in part.footprint.pads:
            if not pad.is_tht:
                continue
            off_x, off_y = _rotate_offset(pad.x_nm, pad.y_nm, part.rotated)
            holes.append((pad.drill_nm, anchor_x + off_x, anchor_y + off_y))
    for via in board.vias:
        holes.append((via.drill_nm, via.x_nm + _MARGIN_NM, via.y_nm + _MARGIN_NM))
    return holes


def excellon_drill(board: BoardResult) -> str:
    """Every plated hole in the board, as an Excellon drill program.

    Two things make holes here: a through-hole pad (``Pad.drill_nm > 0``, the
    header land patterns) and a via the router placed to change layers. Both
    are plated -- a pad's hole carries copper on every layer and a via exists
    to join two -- so this is the PTH program and there is no non-plated
    file: nothing in the engine emits a hole with no copper around it, and a
    fab handed an ``NPTH`` file containing vias would leave them unplated and
    the two layers unconnected, which is exactly the bug this docstring's
    predecessor shipped.

    A board with no holes produces a well-formed empty program rather than no
    file at all -- fabs expect the file, and an empty one is the honest
    answer. What would be dangerous is the reverse: claiming no holes on a
    board that has them.

    Coordinates carry the same ``+margin`` shift as the Gerbers, so the drill
    program and the copper share one origin.
    """
    lines = ["M48", f"; Generated by {GENERATOR}"]
    holes = _plated_holes(board)
    tools: dict[int, int] = {}
    for drill_nm, _, _ in holes:
        if drill_nm not in tools:
            tools[drill_nm] = len(tools) + 1
    if not tools:
        lines.append(
            "; No holes: every pad in this design is SMD and the board carries"
            " no vias."
        )
    lines.append("METRIC")
    for drill_nm, number in sorted(tools.items(), key=lambda item: item[1]):
        lines.append(f"T{number}C{_mm_text(drill_nm)}")
    lines.append("%")
    for drill_nm, number in sorted(tools.items(), key=lambda item: item[1]):
        lines.append(f"T{number}")
        for hole_nm, x_nm, y_nm in holes:
            if hole_nm != drill_nm:
                continue
            lines.append(f"X{_mm_text(x_nm)}Y{_mm_text(y_nm)}")
    lines.append("M30")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ assembly


def bom_csv(board: BoardResult) -> str:
    """Bill of materials, one row per distinct (value, package) pair.

    The column names are the ones JLCPCB, PCBWay and Altium all read, so the
    file can be uploaded without being reshaped first.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for part in board.parts:
        groups.setdefault((part.value, part.footprint.name), []).append(part.ref)

    rows = ["Comment,Designator,Footprint,Quantity"]
    for (value, footprint), refs in sorted(groups.items()):
        designators = ";".join(sorted(refs, key=_natural_key))
        rows.append(_csv_row([value, designators, footprint, str(len(refs))]))
    return "\n".join(rows) + "\n"


def cpl_csv(board: BoardResult) -> str:
    """Pick-and-place list: one anchor position per part, in millimetres.

    The coordinates are the same shifted, Y-up anchors the Gerbers use, so the
    assembler's origin lines up with the copper without anyone having to know
    about the outline margin.
    """
    rows = ["Designator,Mid X,Mid Y,Layer,Rotation"]
    for part in sorted(board.parts, key=lambda p: _natural_key(p.ref)):
        x_nm, y_nm = _anchor_nm(part)
        rows.append(
            _csv_row(
                [
                    part.ref,
                    _mm4(x_nm),
                    _mm4(y_nm),
                    "Bottom" if _is_bottom(part) else "Top",
                    "90" if part.rotated else "0",
                ]
            )
        )
    return "\n".join(rows) + "\n"


def fab_files(board: BoardResult) -> list[FabLayer]:
    """Every file a board house needs, in a fixed order.

    Both sides are always present. A side with no parts yields an empty but
    valid Gerber, which is what fabs expect: a missing file reads as an
    oversight and stalls the order, whereas an empty one reads as "no copper
    here" and passes CAM untouched.
    """
    return [
        FabLayer("silkscreen-F_Cu.GTL", gerber_copper(board)),
        FabLayer("silkscreen-B_Cu.GBL", gerber_copper(board, bottom=True)),
        FabLayer("silkscreen-F_Mask.GTS", gerber_mask(board)),
        FabLayer("silkscreen-B_Mask.GBS", gerber_mask(board, bottom=True)),
        FabLayer("silkscreen-F_Paste.GTP", gerber_paste(board)),
        FabLayer("silkscreen-B_Paste.GBP", gerber_paste(board, bottom=True)),
        FabLayer("silkscreen-F_Silkscreen.GTO", gerber_silkscreen(board)),
        FabLayer(
            "silkscreen-B_Silkscreen.GBO",
            gerber_silkscreen(board, bottom=True),
        ),
        FabLayer("silkscreen-Edge_Cuts.GKO", gerber_outline(board)),
        FabLayer("silkscreen-PTH.DRL", excellon_drill(board)),
        FabLayer("silkscreen-BOM.csv", bom_csv(board)),
        FabLayer("silkscreen-CPL.csv", cpl_csv(board)),
    ]

"""Parametric KiCad footprint generation.

To emit a board you need footprints, and a footprint is not just a symbol: it
is pads at real coordinates, a courtyard, and silkscreen. The project this
replaces sidestepped that entirely -- it pasted a board a human had already
drawn -- which is why it could never produce a design it had not been handed.

These generators build IPC-7351-shaped land patterns from the package
dimensions, so a board can be emitted without a KiCad installation and without
a footprint library on disk. Everything is nanometres (KiCad internal units).

Coverage is deliberately narrow and honest: two-terminal chip passives, SOT-23,
SOT-223, SOIC, LQFP, and the power-entry connectors, battery holders, switches
and test points in :data:`CONNECTOR_PACKAGES` / :data:`BATTERY_PACKAGES` /
:data:`SWITCH_PACKAGES` / :data:`TESTPOINT_PACKAGES`. That is enough for the
regulator/MCU/driver circuits this pipeline generates. Anything else raises
rather than guessing -- a wrong footprint is the single most common cause of a
dead first-spin board, and silently inventing one would be worse than refusing.

The connector generators exist because refusing was not what actually happened:
``board._footprint_for_device`` dispatched on pin count alone, so a two-pin
power connector was padded out and drawn as a SOIC-4 -- a surface-mount IC with
no hole a barrel plug could enter. Nothing raised, DRC passed, parity passed,
and the board simply had no power input. Every pattern below is therefore
copied from a named KiCad library footprint (pad centres, sizes and drills, all
recorded in the generator's docstring) rather than derived from a datasheet
reading, so "does this match a real part" is a question anyone can re-check
against the file named.

**Frame.** Pad offsets are in KiCad's footprint frame: the anchor at the
package centre, X right, **Y down** on screen, exactly as the emitter writes
them. So "pin 1 top-left" means ``x < 0, y < 0``, and a dual-row package
counts anticlockwise on screen from there. The generators were once written
Y-up and copied into the Y-down file unchanged, which mirrored every
multi-pin package: pin 1 landed bottom-left with pin 2 *above* it, an
arrangement no real SOIC, SOT or LQFP has in any rotation. On a SOT-223
that swaps pins 1 and 3 -- ground and input on an AMS1117. The chirality
test in ``test_footprints.py`` pins the winding independently of the
library, and the gated model test compares pad 1 with KiCad's own
footprint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .units import mm

__all__ = [
    "Pad",
    "Footprint",
    "fit_courtyard",
    "silk_segments",
    "pin1_mark",
    "cathode_mark",
    "PIN1_DOT_RADIUS_NM",
    "SILK_STROKE_NM",
    "SILK_PAD_CLEARANCE_NM",
    "chip_passive",
    "sot23",
    "sot223",
    "soic",
    "lqfp",
    "dual_row_header",
    "HEADER_PITCH_MM",
    "for_passive",
    "CHIP_SIZES",
    "UnsupportedPackage",
    "CONNECTOR_PACKAGES",
    "BATTERY_PACKAGES",
    "SWITCH_PACKAGES",
    "TESTPOINT_PACKAGES",
    "CONNECTOR_COURTYARD_EXCESS_MM",
    "connector",
    "battery_holder",
    "switch",
    "probe_point",
]


class UnsupportedPackage(ValueError):
    """No generator covers this package; refusing to invent a land pattern."""


@dataclass(frozen=True)
class Pad:
    """One pad. ``x``/``y`` are relative to the footprint anchor.

    ``drill_nm`` is what separates the two pad technologies, and it defaults to
    zero so every existing SMD generator keeps its meaning. Zero is an SMD pad:
    copper, paste and mask on the placed side only. Anything greater is a
    plated through-hole -- the drill is the finished hole diameter, the pad
    lands on every copper layer, and it gets no paste, because a hole that is
    stencilled with solder paste is a reflow defect, not a joint.
    """

    number: str
    x_nm: int
    y_nm: int
    w_nm: int
    h_nm: int
    net: str = ""
    drill_nm: int = 0

    @property
    def is_tht(self) -> bool:
        """Whether this pad is a plated through-hole rather than SMD."""
        return self.drill_nm > 0


@dataclass
class Footprint:
    """A generated land pattern, anchored at its centre."""

    name: str
    pads: list[Pad] = field(default_factory=list)
    #: Courtyard half-extents from the anchor.
    courtyard_w_nm: int = 0
    courtyard_h_nm: int = 0
    #: Body outline for silkscreen, half-extents.
    body_w_nm: int = 0
    body_h_nm: int = 0
    description: str = ""
    #: A two-terminal part whose orientation matters (a diode): pin 1 is the
    #: anode and pin 2 the cathode, the convention the schematic symbol
    #: draws, so the legend marks pad 2 and the 3D model turns to match.
    polarised: bool = False

    def pad_by_number(self, number: str) -> Pad | None:
        for pad in self.pads:
            if pad.number == number:
                return pad
        return None


#: Body size and land pattern for the common two-terminal chip packages.
#: (body_w, body_h, pad_w, pad_h, pad_centre_spacing) in mm.
CHIP_SIZES: dict[str, tuple[float, float, float, float, float]] = {
    "0402": (1.0, 0.5, 0.60, 0.65, 0.90),
    "0603": (1.6, 0.8, 0.90, 0.95, 1.55),
    "0805": (2.0, 1.25, 1.05, 1.40, 1.90),
    "1206": (3.2, 1.6, 1.15, 1.80, 3.00),
    "1210": (3.2, 2.5, 1.15, 2.70, 3.00),
}

#: Courtyard excess over the land pattern, per IPC-7351 nominal density.
_COURTYARD_EXCESS_MM = 0.25


def fit_courtyard(fp: Footprint, excess_mm: float = _COURTYARD_EXCESS_MM) -> None:
    """Size the courtyard to enclose every pad AND the body, plus excess.

    Deriving it from one representative pad is how a footprint ends up with
    pins outside its own courtyard -- which defeats the placer's clearance
    guarantee, because the placer only ever sees the courtyard.
    """
    half_w = fp.body_w_nm
    half_h = fp.body_h_nm
    for pad in fp.pads:
        half_w = max(half_w, abs(pad.x_nm) + pad.w_nm // 2)
        half_h = max(half_h, abs(pad.y_nm) + pad.h_nm // 2)
    fp.courtyard_w_nm = half_w + mm(excess_mm)
    fp.courtyard_h_nm = half_h + mm(excess_mm)


#: Pen width both emitters stroke silkscreen with.
SILK_STROKE_NM = mm(0.12)

#: Minimum gap between silkscreen ink and solderable copper. Ink on a pad
#: resists solder; most fabs clip it silently, so the shipped board stops
#: matching the approved artwork. 0.2 mm is the KLC/IPC convention.
SILK_PAD_CLEARANCE_NM = mm(0.2)

#: Clipped remnants shorter than this are dropped -- a speck of ink marks
#: nothing and just reads as debris on the legend.
_MIN_SILK_SEG_NM = mm(0.2)


def silk_segments(
    fp: Footprint,
    *,
    stroke_nm: int = SILK_STROKE_NM,
    clearance_nm: int = SILK_PAD_CLEARANCE_NM,
) -> list[tuple[int, int, int, int]]:
    """The body outline as strokes, clipped clear of every pad.

    Stroking the body rectangle directly puts ink on copper wherever the body
    edge meets a pad -- on a chip passive the pads sit under the body ends, on
    an LQFP the pad row starts exactly at the body edge -- so every emitted
    footprint used to overlap its own pads by half the pen width. Instead the
    four edges are cut wherever the stroked line would come within
    ``clearance_nm`` of a pad, and what survives is returned as
    ``(x0, y0, x1, y1)`` segments in footprint-local nanometres.

    Both emitters draw these same segments (KiCad s-expressions and the Gerber
    legend), transformed exactly as they transform pads, so the clipping stays
    valid in either frame. A part whose outline is swallowed entirely (an 0603
    body barely wider than its own pads) gets no outline, which is what real
    library footprints do too.
    """
    if not fp.body_w_nm or not fp.body_h_nm:
        return []
    margin = stroke_nm // 2 + clearance_nm
    bw, bh = fp.body_w_nm, fp.body_h_nm
    # (fixed axis, fixed coordinate, span start, span end)
    edges = [
        ("y", -bh, -bw, bw),  # top
        ("y", bh, -bw, bw),  # bottom
        ("x", -bw, -bh, bh),  # left
        ("x", bw, -bh, bh),  # right
    ]
    out: list[tuple[int, int, int, int]] = []
    for axis, fixed, lo, hi in edges:
        spans = [(lo, hi)]
        for pad in fp.pads:
            if axis == "y":
                near = abs(fixed - pad.y_nm) <= pad.h_nm // 2 + margin
                cut = (pad.x_nm - pad.w_nm // 2 - margin,
                       pad.x_nm + pad.w_nm // 2 + margin)
            else:
                near = abs(fixed - pad.x_nm) <= pad.w_nm // 2 + margin
                cut = (pad.y_nm - pad.h_nm // 2 - margin,
                       pad.y_nm + pad.h_nm // 2 + margin)
            if not near:
                continue
            spans = [
                piece
                for a, b in spans
                for piece in ((a, min(b, cut[0])), (max(a, cut[1]), b))
                if piece[1] > piece[0]
            ]
        for a, b in spans:
            if b - a < _MIN_SILK_SEG_NM:
                continue
            if axis == "y":
                out.append((a, fixed, b, fixed))
            else:
                out.append((fixed, a, fixed, b))
    return out


#: Radius of the pin-1 dot. KLC asks for a mark a fab can read at 1:1; a
#: 0.3 mm dot beside the pad is what the KiCad library draws.
PIN1_DOT_RADIUS_NM = mm(0.15)


def _outside_pad(pad: Pad, *, stroke_nm: int, clearance_nm: int, reach_nm: int) -> int:
    """X of a mark's centre just outboard of ``pad``, clear of its copper."""
    outward = -1 if pad.x_nm < 0 else 1
    reach = pad.w_nm // 2 + clearance_nm + stroke_nm // 2 + reach_nm
    return pad.x_nm + outward * reach


def pin1_mark(
    fp: Footprint,
    *,
    stroke_nm: int = SILK_STROKE_NM,
    clearance_nm: int = SILK_PAD_CLEARANCE_NM,
    radius_nm: int = PIN1_DOT_RADIUS_NM,
) -> tuple[int, int, int] | None:
    """A silkscreen dot ``(x, y, radius)`` beside pad 1, or None.

    The body outline is symmetric, so without this the legend says nothing
    about which way the part goes and the 3D model is the only cue -- and a
    model is not on the printed board. The dot sits outboard of pad 1, on the
    pad's own row, the same clearance from copper the outline keeps. A
    through-hole pattern already marks pin 1 with a square pad, and a
    two-terminal chip has no pin 1 to point at, so both answer None.
    """
    if len(fp.pads) < 3:
        return None
    pad = fp.pad_by_number("1")
    if pad is None or pad.is_tht:
        return None
    x = _outside_pad(
        pad, stroke_nm=stroke_nm, clearance_nm=clearance_nm, reach_nm=radius_nm
    )
    return (x, pad.y_nm, radius_nm)


def cathode_mark(
    fp: Footprint,
    *,
    stroke_nm: int = SILK_STROKE_NM,
    clearance_nm: int = SILK_PAD_CLEARANCE_NM,
) -> tuple[int, int, int, int] | None:
    """A silkscreen bar ``(x0, y0, x1, y1)`` beside the cathode pad, or None.

    Only a :attr:`Footprint.polarised` part gets one. The bar stands outboard
    of pad 2 (the cathode, see the attribute) and spans the pad's height, the
    band a fab reads as "stripe end here".
    """
    if not fp.polarised:
        return None
    pad = fp.pad_by_number("2")
    if pad is None:
        return None
    x = _outside_pad(pad, stroke_nm=stroke_nm, clearance_nm=clearance_nm, reach_nm=0)
    half = pad.h_nm // 2
    return (x, pad.y_nm - half, x, pad.y_nm + half)


def chip_passive(size: str = "0603", *, net1: str = "", net2: str = "") -> Footprint:
    """Two-terminal chip package (resistor, capacitor, inductor, diode)."""
    if size not in CHIP_SIZES:
        raise UnsupportedPackage(
            f"Unknown chip size {size!r}. Known: {sorted(CHIP_SIZES)}"
        )
    body_w, body_h, pad_w, pad_h, spacing = CHIP_SIZES[size]
    half = mm(spacing) // 2
    pads = [
        Pad("1", -half, 0, mm(pad_w), mm(pad_h), net1),
        Pad("2", half, 0, mm(pad_w), mm(pad_h), net2),
    ]
    fp = Footprint(
        name=f"C_{size}",
        pads=pads,
        body_w_nm=mm(body_w) // 2,
        body_h_nm=mm(body_h) // 2,
        description=f"{size} chip package",
    )
    fit_courtyard(fp)
    return fp


def sot23(nets: dict[str, str] | None = None) -> Footprint:
    """SOT-23-3. Pin 1 top-left, pin 2 below it, pin 3 on the right."""
    nets = nets or {}
    pw, ph = mm(1.0), mm(0.6)
    col = mm(1.0)
    row = mm(0.95)
    pads = [
        Pad("1", -col, -row, pw, ph, nets.get("1", "")),
        Pad("2", -col, row, pw, ph, nets.get("2", "")),
        Pad("3", col, 0, pw, ph, nets.get("3", "")),
    ]
    fp = Footprint(
        name="SOT-23",
        pads=pads,
        body_w_nm=mm(1.3) // 2,
        body_h_nm=mm(2.9) // 2,
        description="SOT-23-3",
    )
    fit_courtyard(fp)
    return fp


def sot223(nets: dict[str, str] | None = None) -> Footprint:
    """SOT-223-3, pin 1 top-left, with the tab as a second pad numbered 2.

    The tab is pin 2 electrically (the AMS1117 datasheet: tab = Vout = pin
    2), which is what KiCad's own ``SOT-223-3_TabPin2`` means and why the
    tab pad carries the *same number* as the middle pin. Numbering it "4"
    gave the board a pad no schematic pin exists for, and KiCad's parity
    check reported it as an orphan on every regulator board.

    Pad geometry follows the IPC-7351 SOT223 (TO-261) nominal land pattern:
    1.5mm x 1.8mm pads at 2.3mm pitch (0.5mm gap, well clear of the 0.2mm
    default netclass clearance) and a 3.5mm x 2.6mm tab pad. Known issue 11
    (CLAUDE.md) used a 2.2mm pad height at the same pitch, leaving only
    0.1mm between adjacent pads and tripping KiCad 8 DRC pad-to-pad
    clearance on every board carrying this part.
    """
    nets = nets or {}
    pw, ph = mm(1.5), mm(1.8)
    pitch = mm(2.3)
    col = mm(3.475)
    pads = [
        Pad("1", -col, -pitch, pw, ph, nets.get("1", "")),
        Pad("2", -col, 0, pw, ph, nets.get("2", "")),
        Pad("3", -col, pitch, pw, ph, nets.get("3", "")),
        Pad("2", col, 0, mm(3.5), mm(2.6), nets.get("2", "")),
    ]
    fp = Footprint(
        name="SOT-223-3_TabPin2",
        pads=pads,
        body_w_nm=mm(6.5) // 2,
        body_h_nm=mm(3.5) // 2,
        description="SOT-223-3, tab on pin 2",
    )
    fit_courtyard(fp)
    return fp


def soic(pin_count: int, *, pitch_mm: float = 1.27, body_w_mm: float = 3.9,
         nets: dict[str, str] | None = None) -> Footprint:
    """SOIC / SO dual-row package. Pin 1 top-left, counting anticlockwise
    on screen: down the left column, then up the right (Y-down frame)."""
    if pin_count < 4 or pin_count % 2:
        raise UnsupportedPackage(f"SOIC needs an even pin count >= 4, got {pin_count}")
    nets = nets or {}
    per_side = pin_count // 2
    pw, ph = mm(1.95), mm(0.6)
    col = mm(body_w_mm / 2 + 0.9)
    span = (per_side - 1) * mm(pitch_mm)
    pads: list[Pad] = []
    for i in range(per_side):
        y = -span // 2 + i * mm(pitch_mm)
        pads.append(Pad(str(i + 1), -col, y, pw, ph, nets.get(str(i + 1), "")))
    for i in range(per_side):
        y = span // 2 - i * mm(pitch_mm)
        n = str(per_side + i + 1)
        pads.append(Pad(n, col, y, pw, ph, nets.get(n, "")))
    fp = Footprint(
        name=f"SOIC-{pin_count}",
        pads=pads,
        body_w_nm=mm(body_w_mm) // 2,
        body_h_nm=span // 2 + mm(0.6),
        description=f"SOIC-{pin_count}, {pitch_mm}mm pitch",
    )
    fit_courtyard(fp)
    return fp


def lqfp(pin_count: int, *, pitch_mm: float = 0.5, body_mm: float = 7.0,
         nets: dict[str, str] | None = None) -> Footprint:
    """LQFP quad package. Pin 1 top-left, counting anticlockwise on screen
    (Y-down frame): down the left, along the bottom, up the right, back
    along the top."""
    if pin_count < 16 or pin_count % 4:
        raise UnsupportedPackage(
            f"LQFP needs a pin count >= 16 divisible by 4, got {pin_count}"
        )
    nets = nets or {}
    per_side = pin_count // 4
    pw, ph = mm(1.5), mm(0.3)
    offset = mm(body_mm / 2 + 0.75)
    span = (per_side - 1) * mm(pitch_mm)
    pads: list[Pad] = []
    n = 1

    def net(i: int) -> str:
        return nets.get(str(i), "")

    for i in range(per_side):  # left, top to bottom
        pads.append(Pad(str(n), -offset, -span // 2 + i * mm(pitch_mm), pw, ph, net(n)))
        n += 1
    for i in range(per_side):  # bottom, left to right
        pads.append(Pad(str(n), -span // 2 + i * mm(pitch_mm), offset, ph, pw, net(n)))
        n += 1
    for i in range(per_side):  # right, bottom to top
        pads.append(Pad(str(n), offset, span // 2 - i * mm(pitch_mm), pw, ph, net(n)))
        n += 1
    for i in range(per_side):  # top, right to left
        pads.append(Pad(str(n), span // 2 - i * mm(pitch_mm), -offset, ph, pw, net(n)))
        n += 1

    fp = Footprint(
        name=f"LQFP-{pin_count}",
        pads=pads,
        body_w_nm=mm(body_mm) // 2,
        body_h_nm=mm(body_mm) // 2,
        description=f"LQFP-{pin_count}, {pitch_mm}mm pitch, {body_mm}mm body",
    )
    fit_courtyard(fp)
    return fp


#: Standard 0.1 inch header geometry, in mm: pitch, finished hole, pad
#: diameter. 1.0 mm clears the 0.8 mm square post of a machined or stamped
#: 2.54 mm header with plating and tolerance to spare, and a 1.7 mm annulus
#: leaves ~0.35 mm of copper all round, which is what a hand-solderable joint
#: and every fab's minimum annular ring both want.
HEADER_PITCH_MM = 2.54
HEADER_DRILL_MM = 1.0
HEADER_PAD_MM = 1.7


def dual_row_header(
    pin_count: int,
    *,
    pitch_mm: float = HEADER_PITCH_MM,
    row_spacing_mm: float = 15.24,
    drill_mm: float = HEADER_DRILL_MM,
    pad_mm: float = HEADER_PAD_MM,
    body_w_mm: float | None = None,
    body_h_mm: float | None = None,
    nets: dict[str, str] | None = None,
) -> Footprint:
    """A two-row through-hole header, the land pattern a plug-in module sits on.

    Numbering follows the same convention :func:`soic` uses -- 1..n/2 down the
    left column, continuing back up the right -- because that is how this
    codebase already numbers a dual-row part, and a module's pin 1 marking is
    what the caller's pin map is written against.

    ``row_spacing_mm`` defaults to 15.24 mm (0.6 inch), the span an Arduino
    Nano and most 0.1 inch modules straddle. It is the distance between the two
    pad *columns*, not the body width.
    """
    if pin_count < 4 or pin_count % 2:
        raise UnsupportedPackage(
            f"A dual-row header needs an even pin count >= 4, got {pin_count}"
        )
    if drill_mm >= pad_mm:
        # A pad no wider than its own hole is an annular ring of zero: the
        # drill removes the entire land and the joint has nothing to wet.
        raise UnsupportedPackage(
            f"header drill {drill_mm}mm must be smaller than pad {pad_mm}mm"
        )
    nets = nets or {}
    per_side = pin_count // 2
    col = mm(row_spacing_mm) // 2
    span = (per_side - 1) * mm(pitch_mm)
    pw = ph = mm(pad_mm)
    drill = mm(drill_mm)
    pads: list[Pad] = []
    for i in range(per_side):
        y = -span // 2 + i * mm(pitch_mm)
        n = str(i + 1)
        pads.append(Pad(n, -col, y, pw, ph, nets.get(n, ""), drill))
    for i in range(per_side):
        y = span // 2 - i * mm(pitch_mm)
        n = str(per_side + i + 1)
        pads.append(Pad(n, col, y, pw, ph, nets.get(n, ""), drill))
    # The body defaults to the pads' own extent plus an edge margin rather than
    # to a guessed module outline: silk that claims a smaller body than the
    # part actually has is worse than silk that merely traces the connector.
    half_w = mm(body_w_mm) // 2 if body_w_mm else col + pw // 2
    half_h = mm(body_h_mm) // 2 if body_h_mm else span // 2 + ph // 2
    fp = Footprint(
        name=f"PinHeader_2x{per_side}_P{pitch_mm}mm",
        pads=pads,
        body_w_nm=half_w,
        body_h_nm=half_h,
        description=(
            f"2x{per_side} through-hole header, {pitch_mm}mm pitch, "
            f"{row_spacing_mm}mm row spacing"
        ),
    )
    fit_courtyard(fp)
    return fp


#: Default chip size per passive type. Electrolytics and power inductors want
#: something bigger than an 0603, so they are not all the same.
_PASSIVE_DEFAULT_SIZE = {
    "resistor": "0603",
    "capacitor": "0603",
    "inductor": "0805",
    "diode": "0603",
    "crystal": "1210",
}


def for_passive(passive_type: str, value: str = "", *, net1: str = "",
                net2: str = "") -> Footprint:
    """Choose a chip package for a passive, widening it for large values.

    A 22uF part does not fit an 0603, and silently emitting one would produce a
    board whose parts do not physically exist in that size.
    """
    size = _PASSIVE_DEFAULT_SIZE.get(passive_type)
    if size is None:
        raise UnsupportedPackage(
            f"No footprint rule for passive type {passive_type!r}"
        )
    if passive_type == "capacitor":
        farads = _parse_capacitance(value)
        if farads is not None:
            if farads >= 10e-6:
                size = "1206"
            elif farads >= 1e-6:
                size = "0805"
    fp = chip_passive(size, net1=net1, net2=net2)
    fp.name = f"{passive_type[:1].upper()}_{size}"
    fp.polarised = passive_type == "diode"
    return fp


_CAP_UNITS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}


def _parse_capacitance(value: str) -> float | None:
    """'100nF' -> 1e-7. Returns None when the value is not parseable."""
    if not value:
        return None
    text = value.strip().lower().replace("f", "")
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([pnuµm]?)$", text)
    if not match:
        return None
    number, unit = match.groups()
    try:
        return float(number) * _CAP_UNITS.get(unit, 1.0)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Connectors and battery holders
#
# Every pattern below is measured from a KiCad library footprint, named in the
# generator's docstring, at
# /Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/. The library
# anchors most through-hole connectors on *pad 1*; this module anchors on the
# package centre (see the module "Frame" note), so each generator recentres the
# library coordinates onto the middle of that footprint's F.Fab body outline and
# says which numbers it started from. Nothing here is invented: a land pattern
# that does not match a real part is the entire bug class this section removes.
# ---------------------------------------------------------------------------

#: Courtyard excess for connectors and battery holders, in mm. Wider than the
#: 0.25 mm IPC nominal that :func:`fit_courtyard` defaults to, because KLC asks
#: for 0.5 mm around a connector and every library footprint measured here uses
#: it -- a plug is inserted by hand, and the extra room is for the fingers and
#: the housing overhang, not for the copper.
CONNECTOR_COURTYARD_EXCESS_MM = 0.5


def _net(nets: dict[str, str] | None, number: str) -> str:
    return (nets or {}).get(number, "")


#: 2.1 x 5.5 mm DC barrel jack, from ``Connector_BarrelJack.pretty/
#: BarrelJack_CUI_PJ-102AH_Horizontal.kicad_mod``. Library pads (anchored on
#: pad 1): 1 at (0, 0), 2 at (0, 6), 3 at (4.7, 3), all 2.6 mm round with a
#: 1.6 mm drill. Its F.Fab body runs x -4.5..4.5, y -0.7..13.7, whose centre is
#: (0, 6.5) -- the offset subtracted below. Pin 1 is the centre tip (+), pin 2
#: the sleeve (GND) and pin 3 the normally-closed switch contact.
_BARREL_PADS_MM = (("1", 0.0, -6.5), ("2", 0.0, -0.5), ("3", 4.7, -3.5))
_BARREL_PAD_MM = 2.6
_BARREL_DRILL_MM = 1.6
_BARREL_BODY_MM = (4.5, 7.2)


def _barrel_jack(nets: dict[str, str] | None) -> Footprint:
    """DC barrel jack, 5.5 mm outer / 2.1 mm inner pin (CUI PJ-102AH)."""
    pads = [
        Pad(n, mm(x), mm(y), mm(_BARREL_PAD_MM), mm(_BARREL_PAD_MM),
            _net(nets, n), mm(_BARREL_DRILL_MM))
        for n, x, y in _BARREL_PADS_MM
    ]
    fp = Footprint(
        name="Barrel_Jack_5.5x2.1mm",
        pads=pads,
        body_w_nm=mm(_BARREL_BODY_MM[0]),
        body_h_nm=mm(_BARREL_BODY_MM[1]),
        description=(
            "DC barrel jack 5.5x2.1mm, horizontal (CUI PJ-102AH); "
            "1=tip/+, 2=sleeve/GND, 3=switch"
        ),
    )
    fit_courtyard(fp, CONNECTOR_COURTYARD_EXCESS_MM)
    return fp


#: 5.08 mm screw terminal block, from ``TerminalBlock_Phoenix.pretty/
#: TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal.kicad_mod``.
#: Library pads: 1 at (0, 0), 2 at (5.08, 0), 2.6 mm round, 1.3 mm drill.
#: F.Fab x -2.54..7.62, y -5.2..4.6 -> centre (2.54, -0.3).
_TB_PITCH_MM = 5.08
_TB_PAD_MM = 2.6
_TB_DRILL_MM = 1.3
_TB_BODY_MM = (5.08, 4.9)
_TB_PAD_Y_MM = 0.3


def _terminal_block_2p(nets: dict[str, str] | None) -> Footprint:
    """Two-way 5.08 mm screw terminal, the usual bare-wire power input."""
    half = mm(_TB_PITCH_MM) // 2
    pads = [
        Pad("1", -half, mm(_TB_PAD_Y_MM), mm(_TB_PAD_MM), mm(_TB_PAD_MM),
            _net(nets, "1"), mm(_TB_DRILL_MM)),
        Pad("2", half, mm(_TB_PAD_Y_MM), mm(_TB_PAD_MM), mm(_TB_PAD_MM),
            _net(nets, "2"), mm(_TB_DRILL_MM)),
    ]
    fp = Footprint(
        name="TerminalBlock_2P_5.08mm",
        pads=pads,
        body_w_nm=mm(_TB_BODY_MM[0]),
        body_h_nm=mm(_TB_BODY_MM[1]),
        description=(
            "2-way screw terminal block, 5.08mm pitch (Phoenix MKDS 1,5/2-5,08)"
        ),
    )
    fit_courtyard(fp, CONNECTOR_COURTYARD_EXCESS_MM)
    return fp


#: JST PH, from ``Connector_JST.pretty/JST_PH_S{n}B-PH-K_1x0{n}_P2.00mm_
#: Horizontal.kicad_mod``. Library pads: pin i at ((i-1)*2, 0), 1.2 x 1.75 mm
#: with a 0.75 mm drill. F.Fab x -1.95..(n-1)*2+1.95, y -1.35..6.25, so the
#: body centre is ((n-1), 2.45) -- the pad row sits *above* the body centre
#: because the housing extends away from the pins, which is why every pad's
#: recentred y is negative.
_JST_PITCH_MM = 2.0
_JST_PAD_MM = (1.2, 1.75)
_JST_DRILL_MM = 0.75
_JST_FAB_OVERHANG_MM = 1.95  # F.Fab beyond the outermost pad, along x
_JST_PAD_Y_MM = -2.45
_JST_BODY_H_MM = 3.8


def _jst_ph(pin_count: int, nets: dict[str, str] | None) -> Footprint:
    """JST PH 2.00 mm side-entry header, 2 to 4 ways."""
    pw, ph = mm(_JST_PAD_MM[0]), mm(_JST_PAD_MM[1])
    pitch = mm(_JST_PITCH_MM)
    span = (pin_count - 1) * pitch
    pads = [
        Pad(str(i + 1), -span // 2 + i * pitch, mm(_JST_PAD_Y_MM), pw, ph,
            _net(nets, str(i + 1)), mm(_JST_DRILL_MM))
        for i in range(pin_count)
    ]
    fp = Footprint(
        name=f"JST_PH_{pin_count}P",
        pads=pads,
        body_w_nm=span // 2 + mm(_JST_FAB_OVERHANG_MM),
        body_h_nm=mm(_JST_BODY_H_MM),
        description=(
            f"JST PH {pin_count}-way, 2.00mm pitch, horizontal "
            f"(JST S{pin_count}B-PH-K)"
        ),
    )
    fit_courtyard(fp, CONNECTOR_COURTYARD_EXCESS_MM)
    return fp


#: Single-row 2.54 mm pin header, from ``Connector_PinHeader_2.54mm.pretty/
#: PinHeader_1x{nn}_P2.54mm_Vertical.kicad_mod``. Library pads: pin i at
#: (0, (i-1)*2.54), 1.7 mm square, 1.0 mm drill -- the same hole and annulus
#: :data:`HEADER_DRILL_MM` / :data:`HEADER_PAD_MM` already state, and the
#: reason those two constants are reused here rather than restated. F.Fab
#: x -1.27..1.27, y -1.27..(n-1)*2.54+1.27.
_HEADER_FAB_OVERHANG_MM = 1.27


def _pin_header_1xn(pin_count: int, nets: dict[str, str] | None) -> Footprint:
    """Single-row 0.1 inch through-hole header. Pin 1 is the topmost pad.

    The column runs down the screen from pin 1, which is the frame's "pin 1
    top-left" rule for a one-column part: there is no second column to wind
    back along, so the whole numbering is that single downward run.
    """
    pitch = mm(HEADER_PITCH_MM)
    pad = mm(HEADER_PAD_MM)
    span = (pin_count - 1) * pitch
    pads = [
        Pad(str(i + 1), 0, -span // 2 + i * pitch, pad, pad,
            _net(nets, str(i + 1)), mm(HEADER_DRILL_MM))
        for i in range(pin_count)
    ]
    fp = Footprint(
        name=f"PinHeader_1x{pin_count:02d}_P2.54mm",
        pads=pads,
        body_w_nm=mm(_HEADER_FAB_OVERHANG_MM),
        body_h_nm=span // 2 + mm(_HEADER_FAB_OVERHANG_MM),
        description=f"1x{pin_count:02d} through-hole pin header, 2.54mm pitch",
    )
    fit_courtyard(fp, CONNECTOR_COURTYARD_EXCESS_MM)
    return fp


#: Power-only USB-C receptacle, from ``Connector_USB.pretty/
#: USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal.kicad_mod``. Already
#: anchored on the body centre, so these are the library's own numbers: six SMD
#: pads on one row at y = -3.08, listed left to right. F.Fab x -4.47..4.47,
#: y -3.4..3.4.
#:
#: The pad *names* are the library's and they are the ones a real 6-pin
#: power-only USB-C part has: A5/B5 are CC1/CC2, A9/B9 are VBUS and A12/B12 are
#: GND. There is deliberately no "A1"/"A4" here and no D+/D-: a 6-way
#: power-only receptacle does not break those out, and inventing pad names the
#: part does not have is the same lie as inventing pad positions.
_USB_C_PADS_MM = (
    ("B12", -2.75, 0.80),
    ("B9", -1.52, 0.76),
    ("A5", -0.50, 0.70),
    ("B5", 0.50, 0.70),
    ("A9", 1.52, 0.76),
    ("A12", 2.75, 0.80),
)
_USB_C_PAD_Y_MM = -3.08
_USB_C_PAD_H_MM = 1.2
_USB_C_BODY_MM = (4.47, 3.4)


def _usb_c_power(nets: dict[str, str] | None) -> Footprint:
    """Six-way power-only USB-C receptacle (GCT USB4125).

    The real part also has four through-hole shield tabs. They are **not**
    emitted: :class:`Pad` carries one round drill diameter and the tabs are
    slots (0.6 x 1.2 mm oval), so drawing them as round holes would put a
    plated hole where the mechanical shell does not fit. The consequence is
    honest and worth stating -- this land pattern solders the six signal pads
    only, and a board using it wants the shell tabs added by hand before it is
    fabricated.
    """
    pads = [
        Pad(number, mm(x), mm(_USB_C_PAD_Y_MM), mm(w), mm(_USB_C_PAD_H_MM),
            _net(nets, number))
        for number, x, w in _USB_C_PADS_MM
    ]
    fp = Footprint(
        name="USB_C_Receptacle_Power",
        pads=pads,
        body_w_nm=mm(_USB_C_BODY_MM[0]),
        body_h_nm=mm(_USB_C_BODY_MM[1]),
        description=(
            "USB-C receptacle, 6-way power only (GCT USB4125); "
            "A5/B5=CC1/CC2, A9/B9=VBUS, A12/B12=GND; shield tabs not drawn"
        ),
    )
    fit_courtyard(fp, CONNECTOR_COURTYARD_EXCESS_MM)
    return fp


#: CR2032 coin cell holder, from ``Battery.pretty/
#: BatteryHolder_Keystone_3002_1x2032.kicad_mod``. Library pads (already body
#: anchored): "1" twice, at (-12.8, 0) and (12.8, 0), 5.1 mm square SMD; "2"
#: once, at (0, 0), a 17.8 mm circle. F.Fab x -15.35..15.35, y -9.3..10.6, so
#: the body centre is (0, 0.65) and every pad's recentred y is -0.65.
#:
#: Two pads share the number "1" on purpose, exactly as the SOT-223 tab shares
#: "2": the holder's two spring clips are one net (the cell's + case rim), and
#: numbering the second one "3" would give the board a pad no schematic pin
#: exists for -- the orphan KiCad's parity check reports.
_CR2032_CLIP_X_MM = 12.8
_CR2032_CLIP_MM = 5.1
_CR2032_CUP_MM = 17.8
_CR2032_PAD_Y_MM = -0.65
_CR2032_BODY_MM = (15.35, 9.95)


def _battery_cr2032(nets: dict[str, str] | None) -> Footprint:
    """CR2032 coin-cell holder. Pin 1 = + (the clips), pin 2 = - (the cup)."""
    y = mm(_CR2032_PAD_Y_MM)
    clip = mm(_CR2032_CLIP_MM)
    plus, minus = _net(nets, "1"), _net(nets, "2")
    pads = [
        Pad("1", -mm(_CR2032_CLIP_X_MM), y, clip, clip, plus),
        Pad("2", 0, y, mm(_CR2032_CUP_MM), mm(_CR2032_CUP_MM), minus),
        Pad("1", mm(_CR2032_CLIP_X_MM), y, clip, clip, plus),
    ]
    fp = Footprint(
        name="BatteryHolder_CR2032",
        pads=pads,
        body_w_nm=mm(_CR2032_BODY_MM[0]),
        body_h_nm=mm(_CR2032_BODY_MM[1]),
        description="CR2032 coin cell holder, SMD (Keystone 3002); 1=+, 2=-",
    )
    fit_courtyard(fp, CONNECTOR_COURTYARD_EXCESS_MM)
    return fp


#: 1xAAA cell holder, from ``Battery.pretty/
#: BatteryHolder_Keystone_2466_1xAAA.kicad_mod``. Library pads: 1 at (0, 0),
#: 2 at (44.7, 0), 2.0 mm round with a 1.02 mm drill. F.Fab x -2.7..47.3,
#: y -6.5..6.5 -> centre (22.3, 0). The 44.7 mm span is the part: an AAA cell
#: is 44.5 mm long and the holder is longer still, so this footprint is bigger
#: than most boards this pipeline emits and the placer will size the outline
#: around it rather than the other way round.
_AAA_SPAN_MM = 44.7
_AAA_PAD_MM = 2.0
_AAA_DRILL_MM = 1.02
_AAA_BODY_MM = (25.0, 6.5)


def _battery_aaa(nets: dict[str, str] | None) -> Footprint:
    """Single AAA cell holder. Pin 1 = + , pin 2 = -."""
    half = mm(_AAA_SPAN_MM) // 2
    pad, drill = mm(_AAA_PAD_MM), mm(_AAA_DRILL_MM)
    pads = [
        Pad("1", -half, 0, pad, pad, _net(nets, "1"), drill),
        Pad("2", half, 0, pad, pad, _net(nets, "2"), drill),
    ]
    fp = Footprint(
        name="BatteryHolder_AAA_1x",
        pads=pads,
        body_w_nm=mm(_AAA_BODY_MM[0]),
        body_h_nm=mm(_AAA_BODY_MM[1]),
        description="1x AAA cell holder, through-hole (Keystone 2466); 1=+, 2=-",
    )
    fit_courtyard(fp, CONNECTOR_COURTYARD_EXCESS_MM)
    return fp


#: SMD tactile switch, from ``Button_Switch_SMD.pretty/
#: SW_SPST_TL3305A.kicad_mod``. Library pads (already body anchored, so these
#: are the library's own numbers): "1" at (-3.6, -1.5) and (3.6, -1.5), "2" at
#: (-3.6, 1.5) and (3.6, 1.5), all 1.6 x 1.4 mm SMD. F.Fab body x -2.25..2.25,
#: y -2.25..2.25; F.CrtYd x -4.65..4.65, y -2.5..2.5, which the default
#: courtyard excess reproduces exactly.
#:
#: Two pads per number, like the CR2032 clips and the SOT-223 tab: a tactile
#: switch's two contacts per pole are the one terminal inside the part, and
#: numbering the second pair "3"/"4" would give the board two pads no symbol
#: pin exists for -- the orphan KiCad's parity check reports.
#:
#: **The A of TL3305A is load-bearing.** A, B and C share this land pattern
#: exactly (checked pad for pad against both library files) and differ only in
#: actuator height: KiCad's own models measure z -0.05..3.8 for the A,
#: ..5.0 for the B and ..7.0 for the C. The A is drawn because it is the one
#: whose height is nearest what ``enclosure.heights`` believes a ``SW_SPST``
#: is (3.5 mm), and even that is 0.3 mm short -- see this module's note in the
#: report and ``TODO.txt``. Drawing the B or the C would put the lid 1.5 or
#: 3.5 mm into the button.
_TL3305_PADS_MM = (
    ("1", -3.6, -1.5),
    ("2", -3.6, 1.5),
    ("2", 3.6, 1.5),
    ("1", 3.6, -1.5),
)
_TL3305_PAD_MM = (1.6, 1.4)
_TL3305_BODY_MM = (2.25, 2.25)


def _switch_tl3305a(nets: dict[str, str] | None) -> Footprint:
    """SPST normally-open SMD tactile switch (E-Switch TL3305A).

    Pin 1 is the top-left pad, the frame's rule for every multi-pin part
    here; the pair on each row is one pole, so the count reads 1, 2 down the
    left column and back up the right exactly as :func:`soic` does.
    """
    pw, ph = mm(_TL3305_PAD_MM[0]), mm(_TL3305_PAD_MM[1])
    pads = [
        Pad(number, mm(x), mm(y), pw, ph, _net(nets, number))
        for number, x, y in _TL3305_PADS_MM
    ]
    fp = Footprint(
        name="SW_SPST_TL3305A",
        pads=pads,
        body_w_nm=mm(_TL3305_BODY_MM[0]),
        body_h_nm=mm(_TL3305_BODY_MM[1]),
        description=(
            "SPST normally-open SMD tactile switch (E-Switch TL3305A), "
            "6.2 x 4.5 mm, 3.8 mm actuator; two pads per pole"
        ),
    )
    fit_courtyard(fp)
    return fp


#: SMD test pad, from ``TestPoint.pretty/TestPoint_Pad_1.5x1.5mm.kicad_mod``:
#: one 1.5 mm square SMD pad at the origin, an F.SilkS box at +-0.95 and an
#: F.CrtYd box at +-1.25. The 0.3 mm excess below is what reproduces that
#: courtyard exactly; the default 0.25 would draw one 0.05 mm tighter than
#: KiCad's, and a courtyard smaller than the library's is the direction this
#: file must not err in, because the courtyard is all the placer sees.
_TESTPOINT_PAD_MM = 1.5
_TESTPOINT_SILK_MM = 0.95
_TESTPOINT_COURTYARD_EXCESS_MM = 0.3


def _test_pad(nets: dict[str, str] | None) -> Footprint:
    """A bare 1.5 mm square test pad. One pad, numbered 1.

    There is no pin-1 question and no winding to get wrong -- which is the
    whole reason a test point is cheap to add honestly. What it is *not* is a
    Keystone test post: those are a different part with a hole, and this is
    the flat pad a probe touches.
    """
    pad = mm(_TESTPOINT_PAD_MM)
    fp = Footprint(
        name="TestPoint_Pad_1.5x1.5mm",
        pads=[Pad("1", 0, 0, pad, pad, _net(nets, "1"))],
        body_w_nm=mm(_TESTPOINT_SILK_MM),
        body_h_nm=mm(_TESTPOINT_SILK_MM),
        description="1.5 x 1.5 mm SMD test pad",
    )
    fit_courtyard(fp, _TESTPOINT_COURTYARD_EXCESS_MM)
    return fp


#: Connector package name -> pin count. The names are also the emitted
#: :attr:`Footprint.name`, chosen so ``enclosure.rules.connector_class`` (a
#: longest-substring match) and ``enclosure.heights.height_for`` resolve them
#: with no change to those tables: an opening is cut for the plug because the
#: footprint is *called* what the plug is.
CONNECTOR_PACKAGES: dict[str, int] = {
    "Barrel_Jack_5.5x2.1mm": 3,
    "TerminalBlock_2P_5.08mm": 2,
    "JST_PH_2P": 2,
    "JST_PH_3P": 3,
    "JST_PH_4P": 4,
    "PinHeader_1x02_P2.54mm": 2,
    "PinHeader_1x03_P2.54mm": 3,
    "PinHeader_1x04_P2.54mm": 4,
    "PinHeader_1x05_P2.54mm": 5,
    "PinHeader_1x06_P2.54mm": 6,
    "PinHeader_1x08_P2.54mm": 8,
    "PinHeader_1x10_P2.54mm": 10,
    "USB_C_Receptacle_Power": 6,
}

#: Battery-holder package name -> pin count. Pin 1 is + and pin 2 is - on both.
BATTERY_PACKAGES: dict[str, int] = {
    "BatteryHolder_CR2032": 2,
    "BatteryHolder_AAA_1x": 2,
}

#: Switch package name -> pin count, counted as *distinct pad numbers* (the
#: CR2032 rule): the TL3305A has four pads and two poles.
SWITCH_PACKAGES: dict[str, int] = {
    "SW_SPST_TL3305A": 2,
}

#: Test-point package name -> pin count. One pad, one net, no polarity.
TESTPOINT_PACKAGES: dict[str, int] = {
    "TestPoint_Pad_1.5x1.5mm": 1,
}

#: Package name -> the builder that draws it. Kept beside the tables above so
#: an advertised package that nothing can draw is a KeyError here rather than a
#: board with a missing part; the test asserting the two agree is the guard.
_CONNECTOR_BUILDERS = {
    "Barrel_Jack_5.5x2.1mm": _barrel_jack,
    "TerminalBlock_2P_5.08mm": _terminal_block_2p,
    "JST_PH_2P": lambda nets: _jst_ph(2, nets),
    "JST_PH_3P": lambda nets: _jst_ph(3, nets),
    "JST_PH_4P": lambda nets: _jst_ph(4, nets),
    "PinHeader_1x02_P2.54mm": lambda nets: _pin_header_1xn(2, nets),
    "PinHeader_1x03_P2.54mm": lambda nets: _pin_header_1xn(3, nets),
    "PinHeader_1x04_P2.54mm": lambda nets: _pin_header_1xn(4, nets),
    "PinHeader_1x05_P2.54mm": lambda nets: _pin_header_1xn(5, nets),
    "PinHeader_1x06_P2.54mm": lambda nets: _pin_header_1xn(6, nets),
    "PinHeader_1x08_P2.54mm": lambda nets: _pin_header_1xn(8, nets),
    "PinHeader_1x10_P2.54mm": lambda nets: _pin_header_1xn(10, nets),
    "USB_C_Receptacle_Power": _usb_c_power,
}

_BATTERY_BUILDERS = {
    "BatteryHolder_CR2032": _battery_cr2032,
    "BatteryHolder_AAA_1x": _battery_aaa,
}

_SWITCH_BUILDERS = {
    "SW_SPST_TL3305A": _switch_tl3305a,
}

_TESTPOINT_BUILDERS = {
    "TestPoint_Pad_1.5x1.5mm": _test_pad,
}


def connector(package: str, nets: dict[str, str] | None = None) -> Footprint:
    """The land pattern for a named connector package.

    ``nets`` is keyed by *pad number* as this module writes it -- "1".."n" for
    every through-hole part here, and the USB-C receptacle's own "A5"/"B12"
    style names, because those are what the real part's pads are called.

    Raises :class:`UnsupportedPackage` (a ``ValueError``) for anything not in
    :data:`CONNECTOR_PACKAGES`. It is a ``ValueError`` rather than an error
    imported from ``board`` deliberately: this module must not import the one
    that imports it.
    """
    builder = _CONNECTOR_BUILDERS.get(package)
    if builder is None:
        raise UnsupportedPackage(
            f"Unknown connector package {package!r}. "
            f"Known: {sorted(CONNECTOR_PACKAGES)}"
        )
    return builder(nets)


def battery_holder(package: str, nets: dict[str, str] | None = None) -> Footprint:
    """The land pattern for a named battery holder. Pin 1 is +, pin 2 is -."""
    builder = _BATTERY_BUILDERS.get(package)
    if builder is None:
        raise UnsupportedPackage(
            f"Unknown battery package {package!r}. "
            f"Known: {sorted(BATTERY_PACKAGES)}"
        )
    return builder(nets)


def switch(package: str, nets: dict[str, str] | None = None) -> Footprint:
    """The land pattern for a named switch package.

    A separate entry point from :func:`connector` for the reason the battery
    holders have one: these are separate namespaces, and a caller that asks
    for a switch and is handed a connector has a bug this refusal names.
    """
    builder = _SWITCH_BUILDERS.get(package)
    if builder is None:
        raise UnsupportedPackage(
            f"Unknown switch package {package!r}. Known: {sorted(SWITCH_PACKAGES)}"
        )
    return builder(nets)


def probe_point(package: str, nets: dict[str, str] | None = None) -> Footprint:
    """The land pattern for a named test point.

    Named ``probe_point`` rather than ``test_point``, which is what it draws:
    pytest collects any importable callable whose name begins with ``test``,
    so ``from silkscreen.footprints import test_point`` in a test module made
    the entry point itself a failing test asking for a ``package`` fixture.
    The kind, the package names and the emitted ``Footprint.name`` all still
    say "test point"; only the Python identifier steps aside.
    """
    builder = _TESTPOINT_BUILDERS.get(package)
    if builder is None:
        raise UnsupportedPackage(
            f"Unknown test-point package {package!r}. "
            f"Known: {sorted(TESTPOINT_PACKAGES)}"
        )
    return builder(nets)

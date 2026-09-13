"""Emit a complete ``.kicad_sch`` from a validated circuit.

Until this module existed the pipeline had no schematic at all. It went from a
:class:`~silkscreen.netlist.CircuitSpec` straight to placed footprints, so the
one artifact a hardware person opens first -- the drawing that says what the
circuit *is* -- was the only one never produced. A board with no schematic
cannot be reviewed by a human, cannot be re-annotated, and cannot be handed to
anyone who did not watch it being generated.

The ``CircuitSpec`` already holds exactly what a schematic needs: devices with
named pins, two-terminal passives, and nets whose endpoints are pin-level. This
module renders that as KiCad 8 s-expressions.

Symbols are **generated, not looked up**, in the same spirit as
:mod:`silkscreen.footprints`: the file carries its own ``lib_symbols`` block,
so it opens on a machine with no KiCad symbol libraries installed and cannot
pick up a different part than the one it was drawn for. That includes the
power symbols: every ground and every supply rail the circuit names gets a
``(power)`` symbol of its own with one invisible ``power_in`` pin, which is
what makes KiCad treat it as a power port rather than as a part.

Connections are drawn as a short wire stub from each pin to a **net label**
(or, for a power net, to a power symbol) rather than as point-to-point wires.
That is ordinary KiCad practice, it is electrically identical, and it
sidesteps the one part of schematic drawing that has no good automatic answer
-- routing wires around symbols without crossing them ambiguously. The netlist
KiCad extracts from this file is the netlist the board was built from.

**A device is not always a chip.** ``Device.kind`` chooses the glyph: an
``ic`` is the rectangle with pins down both sides, a ``connector`` is the
conventional header -- every pin on one edge, numbered, with a contact square
each -- a ``battery`` is the alternating plates with its ``+`` and ``-``
drawn, a ``switch`` is the push button's actuator over its two contacts, and a
``testpoint`` is the little open circle on a stick. Until that dispatch
existed a power input opened as a surface-mount
rectangle, so the one thing a reviewer looks for first, where power enters the
board, was the one thing the drawing did not say.

**Layout.** ICs sit in a row across the upper part of the sheet, passives in a
grid beneath them, everything inside a drawable area that keeps clear of the
frame and of the title block. Cells are sized from the text they must hold --
the reference, the value, the longest net name on any pin -- so nothing is
drawn through anything else; a circuit that does not fit A4 is drawn on A3
(then A2) rather than off the page or on top of itself.

**Coordinate frames.** KiCad symbol libraries are Y-up; the schematic sheet is
Y-down. Everything that crosses that boundary goes through :func:`_pin_on_sheet`
and :func:`_stub_on_sheet`, which sit next to each other for that reason,
mirroring the rule :mod:`silkscreen.kicad` follows for the board. Nothing else
here may flip a sign -- the emitter reaching for ``SymbolPin.stub`` directly and
adding its symbol-local ``dy`` to a sheet coordinate is exactly how every
passive's net label ended up drawn on top of its own body. A passive may be
placed rotated by 180 degrees so its ground faces down and its rail faces up;
that rotation is composed inside the same two functions.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ids import stable_uuid
from .kicad import POWER_NET_PATTERNS, is_power_net
from .netlist import CircuitSpec, PassiveType
from .units import NM_PER_MM, mm

__all__ = [
    "SchematicResult",
    "PowerSymbol",
    "PowerFlag",
    "build_schematic",
    "emit_kicad_sch",
    "emit_kicad_pro",
    "write_schematic",
    "write_project",
]

#: KiCad's schematic grid. Pins land on it or they do not connect, so every
#: coordinate this module produces is quantised to it.
GRID_NM = mm(1.27)

#: Paper sizes the layout may use, smallest first, landscape. KiCad's default
#: is A4; a circuit that does not fit is drawn on the next size up rather than
#: past the border or on top of itself.
PAPERS: tuple[tuple[str, int, int], ...] = (
    ("A4", mm(297.0), mm(210.0)),
    ("A3", mm(420.0), mm(297.0)),
    ("A2", mm(594.0), mm(420.0)),
)

#: A4 landscape, KiCad's default sheet. Kept for callers that only ever
#: needed the default size.
PAGE_W_NM, PAGE_H_NM = PAPERS[0][1], PAPERS[0][2]

#: Distance kept from every page edge. The frame KiCad draws is 10 mm in;
#: twice that leaves the border, its zone letters and numbers untouched.
MARGIN_NM = mm(20.32)

#: KiCad's stock title block occupies the bottom-right corner, about 112 mm
#: wide and 37 mm tall, on every paper size. This is the corner kept clear of
#: it, measured from the right and bottom edges, with clearance to spare.
TITLE_BLOCK_W_NM = mm(132.08)
TITLE_BLOCK_H_NM = mm(48.26)

#: Length of the wire stub between a pin and its net label or power symbol.
_STUB_NM = mm(2.54)

_PIN_PITCH_NM = mm(2.54)
_PIN_LEN_NM = mm(2.54)

#: Passives get a shorter pin. Their connection point is 3.81 mm out and the
#: body half-height is 2.54 mm, so a 2.54 mm pin ends 1.27 mm *inside* the
#: rectangle it is supposed to touch. KiCad's own R symbol uses 1.27 here.
_PASSIVE_PIN_LEN_NM = mm(1.27)

#: Half-extents of the standard two-terminal passive body.
_PASSIVE_BODY_W_NM = mm(1.016)
_PASSIVE_BODY_H_NM = mm(2.54)
#: Pin connection points of a passive, measured from its anchor.
_PASSIVE_PIN_Y_NM = mm(3.81)

#: Text size of every label and field on the sheet. One value, so the sheet
#: does not read as three different hands.
TEXT_NM = mm(1.27)

#: Advance width allowed for one character at this sheet's font. Taken as the
#: font size itself, which is an upper bound for KiCad's stroke font: measured
#: off a plotted sheet, a narrow glyph runs about 0.94 mm and a wide one about
#: 1.24 mm. Guessing lower once already put two labels through each other, and
#: the cost of being generous is only sheet space.
_CHAR_W_NM = mm(1.27)

#: How far a power symbol reaches past the stub end it sits on: the 1.27 mm
#: lead, the bars or arrow, and the net name drawn beyond them.
_POWER_REACH_NM = mm(5.08)

#: Label text of the flag symbol, and how far the flag itself reaches past
#: the stub end before that text starts.
_FLAG_TEXT = "PWR_FLAG"
_FLAG_LEN_NM = mm(2.54)

#: Gap between neighbouring cells, and between the IC row and the passives.
_GAP_NM = mm(2.54)
_ROW_GAP_NM = mm(7.62)

_ICON_LIB = "silkscreen"

#: The date written to a title block when no ``today`` is given. Fixed rather
#: than ``date.today()`` so two runs of the same spec emit the same bytes --
#: a schematic that diffs on the calendar is one nobody can ``git diff``.
DEFAULT_DATE = _dt.date(2026, 1, 1)


def _f(nm: int) -> str:
    """Nanometres as the millimetre literal KiCad files carry."""
    return f"{nm / NM_PER_MM:.4f}".rstrip("0").rstrip(".") or "0"


def _snap(nm: int) -> int:
    """Quantise to the schematic grid, rounding to nearest."""
    return int(round(nm / GRID_NM)) * GRID_NM


def _snap_up(nm: int) -> int:
    """Quantise to the schematic grid, never rounding down."""
    return int(math.ceil(nm / GRID_NM)) * GRID_NM


def _esc(text: str) -> str:
    """Escape a string for an s-expression literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class SymbolPin:
    """One pin of a generated symbol, in symbol-local Y-up coordinates.

    ``x_nm``/``y_nm`` is the *connection point* -- the far end of the pin from
    the body, which is where a wire must meet it. ``angle`` is the direction
    the pin extends from that point toward the body, in KiCad's convention
    (0 right, 90 up, 180 left, 270 down). ``stub_nm`` is how long a wire
    leaves it before meeting its label or power symbol; IC pins carrying a
    rail get progressively longer stubs so their symbols step clear of each
    other.
    """

    number: str
    name: str
    x_nm: int
    y_nm: int
    angle: int
    stub_nm: int = _STUB_NM

    @property
    def stub(self) -> tuple[int, int]:
        """Unit direction a wire leaves this pin, away from the body.

        **Symbol-local, so Y-up like the rest of this class.** Consuming it in
        sheet coordinates without the flip sent every vertical stub back along
        its own pin and parked the net label on top of the body. Use
        :func:`_stub_on_sheet`, which owns the conversion.
        """
        return {0: (-1, 0), 90: (0, -1), 180: (1, 0), 270: (0, 1)}[self.angle]


@dataclass
class SymbolShape:
    """A generated schematic symbol: a body outline plus pins.

    ``graphics`` holds already-rendered s-expression lines for the body, so a
    resistor rectangle and a capacitor's two plates can share everything else.
    """

    lib_id: str
    pins: list[SymbolPin]
    graphics: list[str]
    hide_pin_numbers: bool = False
    #: Drawn length of every pin on this symbol.
    pin_len_nm: int = _PIN_LEN_NM
    #: Half-extents of the drawn symbol including its pins.
    extent_w_nm: int = 0
    extent_h_nm: int = 0


@dataclass
class PlacedSymbol:
    """A symbol instance on the sheet, anchored at ``x_nm``/``y_nm``."""

    ref: str
    value: str
    shape: SymbolShape
    footprint: str = ""
    #: Sheet coordinates, Y-**down**.
    x_nm: int = 0
    y_nm: int = 0
    #: 0 or 180. A passive is turned over when its ground is on pin 1 or its
    #: rail on pin 2, so ground hangs down and supply points up.
    rotation: int = 0
    #: ``{pin_number: net}``; a pin missing from this map is left unconnected.
    pin_nets: dict[str, str] = field(default_factory=dict)


@dataclass
class PowerSymbol:
    """A ground or rail symbol sitting on the end of one pin's stub."""

    ref: str
    net: str
    ground: bool
    #: Sheet coordinates of the symbol's single pin, Y-down.
    x_nm: int
    y_nm: int
    #: The part and pin this symbol is wired to.
    part_ref: str
    pin_number: str


@dataclass
class PowerFlag:
    """KiCad's ``PWR_FLAG``: the one declared source on a power net.

    Every generated ground and rail symbol carries a ``power_in`` pin, and
    ERC rightly asks what drives it. One flag per power net answers that, the
    way a hand-drawn KiCad sheet does; it sits on the same stub end as one of
    the net's power symbols and points sideways, away from the part.
    """

    ref: str
    net: str
    x_nm: int
    y_nm: int
    #: 0 points right, 180 points left.
    rotation: int
    part_ref: str
    pin_number: str


@dataclass
class SchematicResult:
    symbols: list[PlacedSymbol]
    nets: list[str]
    warnings: list[str] = field(default_factory=list)
    power: list[PowerSymbol] = field(default_factory=list)
    flags: list[PowerFlag] = field(default_factory=list)
    #: ``(ref, pin number, x, y)`` of every unconnected device pin, each drawn
    #: with a no-connect cross so it reads as deliberate rather than forgotten.
    no_connects: list[tuple[str, str, int, int]] = field(default_factory=list)
    #: KiCad paper name the layout was fitted to.
    paper: str = PAPERS[0][0]

    @property
    def refs(self) -> list[str]:
        return [s.ref for s in self.symbols]

    @property
    def page_nm(self) -> tuple[int, int]:
        """Width and height of the chosen paper."""
        return next((w, h) for name, w, h in PAPERS if name == self.paper)


# --------------------------------------------------------------------------
# Net classification
# --------------------------------------------------------------------------

#: The subset of :data:`~silkscreen.kicad.POWER_NET_PATTERNS` that is a
#: ground rather than a supply. A ground symbol hangs down, a rail points up,
#: so the two must be told apart.
_GROUND_TOKENS = frozenset(
    {"gnd", "agnd", "dgnd", "pgnd", "vss", "avss", "dvss", "earth"}
)
assert set(POWER_NET_PATTERNS) >= _GROUND_TOKENS


def _net_tokens(net: str) -> list[str]:
    """The same split :func:`~silkscreen.kicad.is_power_net` applies."""
    bare = net.lower().rsplit("/", 1)[-1]
    return [t for t in re.split(r"[^a-z0-9]+", bare) if t]


def net_class(net: str) -> str | None:
    """``"ground"``, ``"rail"`` or ``None`` for a signal.

    Delegates the power decision to :func:`~silkscreen.kicad.is_power_net` by
    *name only*: the board's fan-out rule is deliberately not applied here,
    because a wide signal net drawn as a supply rail would misread the circuit,
    and a schematic that misreads is worse than one with a few more labels.
    """
    if not is_power_net(net, 0, 1 << 30):
        return None
    if any(t in _GROUND_TOKENS for t in _net_tokens(net)):
        return "ground"
    return "rail"


# --------------------------------------------------------------------------
# Symbol generation
# --------------------------------------------------------------------------


def _stroke(width_nm: int = mm(0.254)) -> str:
    return f"(stroke (width {_f(width_nm)}) (type default))"


def _rect(x0: int, y0: int, x1: int, y1: int, *, fill: str = "none") -> str:
    return (
        f"(rectangle (start {_f(x0)} {_f(y0)}) (end {_f(x1)} {_f(y1)}) "
        f"{_stroke()} (fill (type {fill})))"
    )


def _circle(cx: int, cy: int, radius_nm: int, *, fill: str = "none") -> str:
    return (
        f"(circle (center {_f(cx)} {_f(cy)}) (radius {_f(radius_nm)}) "
        f"{_stroke()} (fill (type {fill})))"
    )


def _polyline(
    points: list[tuple[int, int]], *, width_nm: int = mm(0.254), fill: str = "none"
) -> str:
    pts = " ".join(f"(xy {_f(x)} {_f(y)})" for x, y in points)
    return f"(polyline (pts {pts}) {_stroke(width_nm)} (fill (type {fill})))"


def _humps(*, count: int, span_nm: int, radius_nm: int) -> list[tuple[int, int]]:
    """A chain of semicircular bumps up the Y axis, for the inductor body.

    Polyline-approximated rather than drawn with ``arc``: a polyline is one
    primitive with no centre/mid-point convention to get wrong, and at this size
    the flats are invisible.
    """
    points: list[tuple[int, int]] = []
    steps = 8
    for hump in range(count):
        y0 = -span_nm + (2 * span_nm * hump) // count
        y1 = -span_nm + (2 * span_nm * (hump + 1)) // count
        for step in range(steps + 1):
            if hump and step == 0:
                continue  # the previous hump already ended here
            t = step / steps
            angle = math.pi * t
            points.append(
                (
                    int(round(radius_nm * math.sin(angle))),
                    int(round(y0 + (y1 - y0) * t)),
                )
            )
    return points


#: Library symbol name per passive type, following KiCad's own designators.
#: Spelled out rather than derived from the first letter of the type name,
#: which collides: "capacitor" and "crystal" both start with C, and since one
#: definition is emitted per distinct ``lib_id``, every crystal on the sheet
#: would be drawn -- and read -- as a capacitor.
_SYMBOL_NAME: dict[PassiveType, str] = {
    PassiveType.RESISTOR: "R",
    PassiveType.CAPACITOR: "C",
    PassiveType.INDUCTOR: "L",
    PassiveType.DIODE: "D",
    PassiveType.CRYSTAL: "Y",
}


def _passive_shape(ptype: PassiveType) -> SymbolShape:
    """A two-terminal symbol, pin 1 at the top and pin 2 at the bottom.

    The glyphs differ per type because a schematic whose capacitors look like
    resistors is worse than no schematic: it reads as correct and is not.
    """
    w, h = _PASSIVE_BODY_W_NM, _PASSIVE_BODY_H_NM
    y = _PASSIVE_PIN_Y_NM
    graphics: list[str]

    if ptype is PassiveType.RESISTOR:
        graphics = [_rect(-w, -h, w, h)]
    elif ptype is PassiveType.INDUCTOR:
        # Four humps up the vertical axis, KiCad's own inductor. Sharing the
        # resistor's rectangle would draw a part that is not the one specified,
        # which is the failure the per-type glyphs exist to prevent.
        graphics = [_polyline(_humps(count=4, span_nm=h, radius_nm=w))]
    elif ptype is PassiveType.CAPACITOR:
        plate = mm(2.54)
        gap = mm(0.508)
        graphics = [
            _polyline([(-plate, gap), (plate, gap)], width_nm=mm(0.508)),
            _polyline([(-plate, -gap), (plate, -gap)], width_nm=mm(0.508)),
        ]
    elif ptype is PassiveType.DIODE:
        s = mm(1.27)
        graphics = [
            # Anode triangle on top (pin 1), cathode bar beneath it.
            _polyline([(-s, s), (s, s), (0, -s), (-s, s)]),
            _polyline([(-s, -s), (s, -s)], width_nm=mm(0.508)),
        ]
    else:  # crystal
        plate = mm(1.27)
        graphics = [
            _rect(-mm(0.508), -mm(1.27), mm(0.508), mm(1.27)),
            _polyline([(-plate, mm(1.778)), (plate, mm(1.778))], width_nm=mm(0.508)),
            _polyline([(-plate, -mm(1.778)), (plate, -mm(1.778))], width_nm=mm(0.508)),
        ]

    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{_SYMBOL_NAME[ptype]}",
        pins=[
            SymbolPin("1", "~", 0, y, 270),
            SymbolPin("2", "~", 0, -y, 90),
        ],
        graphics=graphics,
        hide_pin_numbers=True,
        pin_len_nm=_PASSIVE_PIN_LEN_NM,
        extent_w_nm=mm(2.54),
        extent_h_nm=y,
    )


#: Pin names KiCad conventionally draws on the left of an IC body regardless of
#: their number, because power and inputs entering from the right reads wrong.
_LEFT_TOKENS = ("vcc", "vdd", "vin", "vbus", "vss", "gnd", "en", "nrst", "reset")

#: Extra stub length per rail (or ground) pin below (above) the first on the
#: same IC side, so their power symbols step outward instead of stacking on one
#: x. Wide enough for a rail name of several characters centred on the stub.
_POWER_STAGGER_NM = mm(7.62)


def _stagger_stubs(
    side: list[tuple[str, str]], nets_by_name: dict[str, str]
) -> list[int]:
    """Stub length per row of one column of pins.

    Rails stagger outward from the top of the column and grounds from the
    bottom; everything else keeps the plain stub. Two power symbols one pin
    pitch apart on the same x draw their arrows and their net names through
    each other, so the stagger is what keeps a rail-and-ground pair legible.
    Shared by the IC and the connector, which are the two shapes with a column
    of sideways pins -- a connector's whole point is that power enters there,
    so it needs this at least as much as an IC does.
    """
    out = [_STUB_NM] * len(side)
    rail_row = ground_row = 0
    for i, item in enumerate(side):
        if net_class(nets_by_name.get(item[0], "")) == "rail":
            out[i] = _STUB_NM + rail_row * _POWER_STAGGER_NM
            rail_row += 1
    for i, item in reversed(list(enumerate(side))):
        if net_class(nets_by_name.get(item[0], "")) == "ground":
            out[i] = _STUB_NM + ground_row * _POWER_STAGGER_NM
            ground_row += 1
    return out


def _device_shape(
    name: str, pins: dict[str, str], nets_by_name: dict[str, str] | None = None
) -> SymbolShape:
    """A rectangular IC symbol with pins split down the two long sides.

    Ordering is by pin *number*, which is the only ordering the spec
    guarantees, with power and reset pins pulled to the left side so the symbol
    reads the way a person expects rather than the way the package numbers run.
    Within a side, pins on a supply rail go to the top rows and pins on a
    ground to the bottom rows: a rail symbol points up and a ground symbol
    hangs down, and this is what keeps them clear of the neighbouring pins'
    labels. ``nets_by_name`` maps pin name to net and decides that ordering.
    """
    nets_by_name = nets_by_name or {}
    items = sorted(pins.items(), key=lambda kv: (_pin_sort_key(kv[1]), kv[0]))
    left_names = [
        (pin_name, number)
        for pin_name, number in items
        if pin_name.lower().lstrip("+-/~").startswith(_LEFT_TOKENS)
    ]
    rest = [item for item in items if item not in left_names]
    # Top the left side up to half of *everything*, not half of the remainder:
    # adding to a side that is already over its share empties the other one.
    half = max(0, (len(items) + 1) // 2 - len(left_names))
    left = left_names + rest[:half]
    right = rest[half:]

    def band(item: tuple[str, str]) -> int:
        kind = net_class(nets_by_name.get(item[0], ""))
        return {"rail": 0, None: 1, "ground": 2}[kind]

    left.sort(key=band)
    right.sort(key=band)

    rows = max(len(left), len(right), 1)
    body_h = _snap((rows + 1) * _PIN_PITCH_NM // 2)
    body_w = _snap(max(mm(12.7), _widest(pins)))
    edge_x = body_w + _PIN_LEN_NM

    symbol_pins: list[SymbolPin] = []
    for side, x, angle in ((left, -edge_x, 0), (right, edge_x, 180)):
        for i, ((pin_name, number), stub) in enumerate(
            zip(side, _stagger_stubs(side, nets_by_name), strict=True)
        ):
            y = body_h - _PIN_PITCH_NM * (i + 1)
            symbol_pins.append(SymbolPin(str(number), pin_name, x, y, angle, stub))

    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{_sanitise(name)}",
        pins=symbol_pins,
        graphics=[_rect(-body_w, -body_h, body_w, body_h, fill="background")],
        extent_w_nm=edge_x,
        extent_h_nm=body_h,
    )


def _widest(pins: dict[str, str]) -> int:
    """Body half-width that keeps the longest pin name inside the rectangle.

    0.85 mm per character at KiCad's default 1.27 mm text is a deliberate
    over-estimate; a name spilling out of its own body is the sort of thing
    that makes a generated schematic look untrustworthy.
    """
    longest = max((len(n) for n in pins), default=0)
    return mm(0.85) * longest + mm(2.54)


# --------------------------------------------------------------------------
# Connector and battery glyphs
#
# Before these existed every device was drawn as the rectangle above, so a
# power input opened as a surface-mount chip: an engineer reading the sheet
# could not see where power entered the board, and neither could a reviewer
# asked to approve it. The geometry below is measured off KiCad's own
# ``Connector_Generic:Conn_01x0N`` and ``Device:Battery`` rather than invented,
# for the same reason ``footprints.py`` copies real land patterns -- a symbol
# that is nearly the conventional one reads as the conventional one and is not.
# --------------------------------------------------------------------------

#: Connection point of a connector pin, and how much of that is drawn as the
#: pin line: KiCad's generic connector puts the body edge at 1.27 mm.
_CONN_PIN_X_NM = mm(5.08)
_CONN_PIN_LEN_NM = mm(3.81)
#: Left edge of the connector body, where the contact rectangles start.
_CONN_BODY_LEFT_NM = mm(1.27)
#: Half-height of one contact rectangle -- the little square per pin that says
#: "receptacle" rather than "chip".
_CONN_CONTACT_H_NM = mm(0.127)
#: Padding above the top pin and below the bottom one.
_CONN_BODY_PAD_NM = mm(1.27)


def _connector_stubs(
    items: list[tuple[str, str]], nets_by_name: dict[str, str]
) -> list[int]:
    """Stub length per connector pin: the IC stagger, plus room for neighbours.

    An IC never needs the second half. Its pins are re-ordered so rails sit at
    the top of a side and grounds at the bottom, which leaves every power
    symbol drawing outward into empty sheet. A connector keeps pin-number
    order, so a ground can sit *between* two signal pins -- and a ground symbol
    reaches 2.54 mm down and prints its net name 3.81 mm down, straight through
    the label of the pin below it. (Measured on a plotted sheet, not deduced:
    the file is valid and ERC is clean either way, which is why only looking at
    it catches this.)

    So a power pin with a labelled signal neighbour is pushed out far enough
    that its own name clears the widest of those labels. Computed from the
    names rather than fixed, because a fixed step is only wide enough until
    someone calls a net ``MOTOR_ENABLE``.
    """
    stubs = _stagger_stubs(items, nets_by_name)
    for i, (pin_name, _) in enumerate(items):
        net = nets_by_name.get(pin_name, "")
        if net_class(net) is None:
            continue
        # A neighbouring power pin is fine: a rail draws up and a ground draws
        # down, so a pair of them diverges. Only plain labels are in the way.
        neighbours = [
            nets_by_name.get(items[j][0], "")
            for j in (i - 1, i + 1)
            if 0 <= j < len(items)
        ]
        widest = max(
            (len(n) * _CHAR_W_NM for n in neighbours if n and net_class(n) is None),
            default=0,
        )
        if widest:
            # The power symbol's own name is centred on the stub end, so half
            # of it reaches back toward the label being cleared.
            stubs[i] += _snap_up(widest + len(net) * _CHAR_W_NM // 2 + _GAP_NM)
    return stubs


def _connector_shape(
    name: str, pins: dict[str, str], nets_by_name: dict[str, str] | None = None
) -> SymbolShape:
    """A header/receptacle: every pin on one side, in pin-number order.

    Both properties are the point. **One side** is what distinguishes a
    connector from an IC at a glance -- wires enter a connector from the
    outside world, so they all leave the same edge. **Pin-number order**, top
    to bottom with 1 at the top, is what lets a person hold the physical part
    next to the sheet and count; the IC's readability re-ordering (power to
    the left, rails to the top) would be actively wrong here, because the
    numbers are the pin-out.

    The numbers are drawn, not hidden, and they are the numbers the spec gave,
    so they are the pad numbers the land pattern carries. A connector symbol
    whose numbering does not match its footprint is a ``net_conflict`` on
    every pin in KiCad's parity report.
    """
    nets_by_name = nets_by_name or {}
    items = sorted(pins.items(), key=lambda kv: (_pin_sort_key(kv[1]), kv[0]))
    rows = len(items)
    half_h = _PIN_PITCH_NM * (rows - 1) // 2 + _CONN_BODY_PAD_NM

    # The body grows to the right only. Its left edge is fixed at 1.27 mm so
    # the contacts and pins stay where KiCad draws them; pin names are printed
    # inside the body from that edge, and a name spilling out of its own body
    # is what makes a generated sheet look untrustworthy.
    longest = max((len(pin_name) for pin_name in pins), default=0)
    body_right = max(_CONN_BODY_LEFT_NM, mm(0.85) * longest + mm(0.508))

    graphics = [
        _rect(-_CONN_BODY_LEFT_NM, -half_h, body_right, half_h, fill="background")
    ]
    symbol_pins: list[SymbolPin] = []
    for i, ((pin_name, number), stub) in enumerate(
        zip(items, _connector_stubs(items, nets_by_name), strict=True)
    ):
        y = half_h - _CONN_BODY_PAD_NM - _PIN_PITCH_NM * i
        graphics.append(
            _rect(
                -_CONN_BODY_LEFT_NM,
                y + _CONN_CONTACT_H_NM,
                0,
                y - _CONN_CONTACT_H_NM,
                fill="none",
            )
        )
        symbol_pins.append(
            SymbolPin(str(number), pin_name, -_CONN_PIN_X_NM, y, 0, stub)
        )

    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{_sanitise(name)}",
        pins=symbol_pins,
        graphics=graphics,
        pin_len_nm=_CONN_PIN_LEN_NM,
        extent_w_nm=max(_CONN_PIN_X_NM, body_right),
        extent_h_nm=half_h,
    )


#: Connection point of each battery terminal, KiCad's ``Device:Battery``.
_BATTERY_PIN_Y_NM = mm(5.08)
_BATTERY_PIN_LEN_NM = mm(2.54)


def _battery_glyph() -> list[str]:
    """Two cells' worth of alternating long and short plates, plus polarity.

    Copied from KiCad's ``Device:Battery`` so it reads as the symbol every
    engineer already knows, with one addition: KiCad marks only the ``+``
    terminal and leaves the ``-`` implied by the short plate. A generated
    sheet has no draughtsman to ask, so the minus is drawn too -- a battery
    wired backwards is a destroyed board, and "unmistakable" is cheaper than
    a plate-length convention the reader has to remember.
    """
    long_half, short_half = mm(2.286), mm(1.524)
    plates: list[str] = []
    # Two cells: within each, the long thin plate is the positive side and the
    # short thick one the negative. The four ``(top, bottom, half-width)``
    # triples are KiCad's own, not rounded versions of them -- the long plates
    # are 0.254 thick and the short ones 0.508, which is what makes the pair
    # read as a cell rather than as two identical bars.
    for top, bottom, half in (
        (mm(1.778), mm(1.524), long_half),
        (mm(1.016), mm(0.508), short_half),
        (-mm(1.27), -mm(1.524), long_half),
        (-mm(2.032), -mm(2.54), short_half),
    ):
        plates.append(_rect(-half, top, half, bottom, fill="outline"))
    # Only the + terminal needs a lead: the drawn pin already reaches the
    # bottom plate. A zero-length polyline is not a shorter lead, it is a
    # malformed one.
    leads = [_polyline([(0, mm(1.778)), (0, _BATTERY_PIN_Y_NM - _BATTERY_PIN_LEN_NM)])]
    plus = [
        _polyline([(mm(0.762), mm(3.048)), (mm(1.778), mm(3.048))]),
        _polyline([(mm(1.27), mm(3.556)), (mm(1.27), mm(2.54))]),
    ]
    minus = [_polyline([(mm(0.762), -mm(3.048)), (mm(1.778), -mm(3.048))])]
    return plates + leads + plus + minus


def _battery_shape(name: str, pins: dict[str, str]) -> SymbolShape:
    """A cell: ``+`` on top (pin 1), ``-`` beneath it (pin 2).

    Which terminal is which comes from the pin *number*, not the pin name:
    the contract fixes pin 1 as ``+`` and pin 2 as ``-`` on both battery
    holders, and that is what the land pattern's pads carry. A model that
    names them ``VBAT``/``GND`` still gets the polarity right.

    The pin names are ``~`` -- KiCad's spelling for "no name", the same one
    the passives use. A vertical pin's name is drawn *inside* the body along
    the pin, which on a battery is straight across the plates; the polarity is
    carried by the drawn ``+`` and ``-`` instead, as it is on a real symbol,
    and the net on each terminal is named by its own label or power symbol.
    """
    items = sorted(pins.items(), key=lambda kv: (_pin_sort_key(kv[1]), kv[0]))
    (_, plus_number), (_, minus_number) = items
    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{_sanitise(name)}",
        pins=[
            SymbolPin(str(plus_number), "~", 0, _BATTERY_PIN_Y_NM, 270),
            SymbolPin(str(minus_number), "~", 0, -_BATTERY_PIN_Y_NM, 90),
        ],
        graphics=_battery_glyph(),
        hide_pin_numbers=True,
        pin_len_nm=_BATTERY_PIN_LEN_NM,
        extent_w_nm=mm(2.54),
        extent_h_nm=_BATTERY_PIN_Y_NM,
    )


# --------------------------------------------------------------------------
# Switch and test-point glyphs
#
# Both are copied from KiCad's own symbol library rather than invented, the
# rule the connector and battery glyphs above already follow:
# ``Switch.kicad_sym``'s ``SW_Push`` and ``Connector.kicad_sym``'s
# ``TestPoint``. Every number below is that symbol's own.
# --------------------------------------------------------------------------

#: ``Switch:SW_Push``: pins at +-5.08 mm, 2.54 mm long, so the body edge sits
#: at +-2.54; a contact circle of radius 0.508 at +-2.032; and the actuator
#: bar at y = 1.27 spanning -2.54..2.54 with its stem rising to 3.048.
_SW_PIN_X_NM = mm(5.08)
_SW_PIN_LEN_NM = mm(2.54)
_SW_CONTACT_X_NM = mm(2.032)
_SW_CONTACT_R_NM = mm(0.508)
_SW_BAR_Y_NM = mm(1.27)
_SW_BAR_HALF_NM = mm(2.54)
_SW_STEM_TOP_NM = mm(3.048)


def _switch_shape(name: str, pins: dict[str, str]) -> SymbolShape:
    """A normally-open push button: pin 1 left, pin 2 right.

    Which pin is which comes from the pin *number*, not the pin name, for the
    reason :func:`_battery_shape` gives: the land pattern's pads carry the
    numbers, and a model that calls the two poles ``A``/``B`` still gets a
    symbol whose numbering matches its footprint -- the thing KiCad's parity
    check reports as a ``net_conflict`` on every pin when it does not.

    A switch that is not two-terminal falls through to the connector shape in
    :func:`_shape_for_device` rather than being forced into this glyph: a
    drawing that says "SPST" over a three-pole part is a lie a reader acts on.
    """
    items = sorted(pins.items(), key=lambda kv: (_pin_sort_key(kv[1]), kv[0]))
    (_, left_number), (_, right_number) = items
    graphics = [
        _circle(-_SW_CONTACT_X_NM, 0, _SW_CONTACT_R_NM),
        _circle(_SW_CONTACT_X_NM, 0, _SW_CONTACT_R_NM),
        # The actuator: a bar across both contacts, held off them (this is the
        # *open* switch), with the stem a finger presses rising out of it.
        _polyline([(-_SW_BAR_HALF_NM, _SW_BAR_Y_NM), (_SW_BAR_HALF_NM, _SW_BAR_Y_NM)]),
        _polyline([(0, _SW_BAR_Y_NM), (0, _SW_STEM_TOP_NM)]),
    ]
    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{_sanitise(name)}",
        # ``~`` is KiCad's spelling for "no name", as on the passives: a
        # horizontal pin's name is drawn inside the body, straight through the
        # contacts, and the net on each side is named by its own label anyway.
        pins=[
            SymbolPin(str(left_number), "~", -_SW_PIN_X_NM, 0, 0),
            SymbolPin(str(right_number), "~", _SW_PIN_X_NM, 0, 180),
        ],
        graphics=graphics,
        hide_pin_numbers=True,
        pin_len_nm=_SW_PIN_LEN_NM,
        extent_w_nm=_SW_PIN_X_NM,
        extent_h_nm=_SW_STEM_TOP_NM,
    )


#: ``Connector:TestPoint``: one pin at the origin pointing up, 2.54 mm long,
#: with an open circle of radius 0.762 centred at 3.302 -- i.e. sitting just
#: past the end of the pin.
_TP_PIN_LEN_NM = mm(2.54)
_TP_CIRCLE_Y_NM = mm(3.302)
_TP_CIRCLE_R_NM = mm(0.762)


def _testpoint_shape(name: str, pins: dict[str, str]) -> SymbolShape:
    """A test point: one pin, and the open circle a probe touches.

    The single pin connects at the origin and the glyph is drawn *above* it,
    so the stub and the net label leave downward into empty sheet exactly as
    they do from any other one-pin part.
    """
    number = next(iter(sorted(pins.values(), key=_pin_sort_key)), "1")
    return SymbolShape(
        lib_id=f"{_ICON_LIB}:{_sanitise(name)}",
        pins=[SymbolPin(str(number), "~", 0, 0, 90)],
        graphics=[_circle(0, _TP_CIRCLE_Y_NM, _TP_CIRCLE_R_NM)],
        hide_pin_numbers=True,
        pin_len_nm=_TP_PIN_LEN_NM,
        extent_w_nm=_TP_CIRCLE_R_NM,
        extent_h_nm=_TP_CIRCLE_Y_NM + _TP_CIRCLE_R_NM,
    )


def _shape_for_device(device, nets_by_name: dict[str, str]) -> SymbolShape:
    """Pick the glyph for one device from its ``kind``.

    ``getattr`` rather than ``device.kind`` because the field defaults to
    ``"ic"`` in the IR and a caller holding an older ``Device`` -- or a test
    fixture built by hand -- must still draw something rather than raise.

    A battery whose spec does not give it exactly two terminals falls through
    to the connector, not to the IC rectangle: a multi-contact holder is still
    a thing wires leave from one edge of, and drawing it as a surface-mount
    chip is precisely the bug this dispatch exists to remove. A switch with
    other than two pins and a test point with other than one fall through the
    same way and for the same reason -- a two-terminal glyph over a part that
    is not two-terminal would misdescribe the part, and the connector shape
    at least numbers every pin it draws.
    """
    kind = getattr(device, "kind", "ic")
    if kind == "battery" and len(device.pins) == 2:
        return _battery_shape(device.name, device.pins)
    if kind == "switch" and len(device.pins) == 2:
        return _switch_shape(device.name, device.pins)
    if kind == "testpoint" and len(device.pins) == 1:
        return _testpoint_shape(device.name, device.pins)
    if kind in ("connector", "battery", "switch", "testpoint"):
        return _connector_shape(device.name, device.pins, nets_by_name)
    return _device_shape(device.name, device.pins, nets_by_name)


def _pin_sort_key(number: str) -> tuple[int, float | str]:
    """Sort pin numbers numerically when they are numbers, else by text."""
    try:
        return (0, float(number))
    except (TypeError, ValueError):
        return (1, str(number))


def _sanitise(name: str) -> str:
    """A library symbol name KiCad will accept."""
    return "".join(c if c.isalnum() or c in "._-+" else "_" for c in name) or "PART"


def _power_lib_id(net: str) -> str:
    """Library name of the power symbol for ``net``.

    Prefixed so a rail called ``VIN`` cannot share a definition with a device
    called ``VIN``; KiCad names the net after the symbol's value and pin,
    never after the library entry, so the prefix is invisible on the sheet.
    """
    return f"{_ICON_LIB}:PWR_{_sanitise(net)}"


def _power_graphics(ground: bool) -> list[str]:
    """Body of a power symbol, symbol-local Y-up, pin at the origin.

    Ground: KiCad's own three-bar earth beneath a short lead. Rail: a short
    lead with a small filled arrow, the shape of KiCad's ``+5V``. Both are
    drawn rather than referenced so the sheet opens without a power library.
    """
    lead = mm(1.27)
    if ground:
        return [
            _polyline([(0, 0), (0, -lead)]),
            _polyline([(-mm(1.27), -lead), (mm(1.27), -lead)]),
            _polyline([(-mm(0.762), -mm(1.905)), (mm(0.762), -mm(1.905))]),
            _polyline([(-mm(0.254), -mm(2.54)), (mm(0.254), -mm(2.54))]),
        ]
    return [
        _polyline([(0, 0), (0, lead)]),
        _polyline(
            [(-mm(0.762), lead), (0, mm(2.54)), (mm(0.762), lead), (-mm(0.762), lead)],
            fill="outline",
        ),
    ]


# --------------------------------------------------------------------------
# Sheet layout
# --------------------------------------------------------------------------


def build_schematic(
    spec: CircuitSpec, *, footprints: dict[str, str] | None = None
) -> SchematicResult:
    """Lay a validated circuit out on the smallest sheet it fits.

    ``footprints`` maps a reference designator to the footprint name the board
    used, so the schematic's Footprint field points at the same land pattern
    that was placed. Omitting it leaves the field empty rather than guessing --
    a schematic that names a footprint the board does not use is a trap.
    """
    spec.validate()
    refs = spec.assign_refs()
    footprints = footprints or {}

    shapes: list[PlacedSymbol] = []
    for device in spec.devices:
        ref = refs[device.name]
        pin_nets_by_name = spec.nets_of(device.name)
        # The spec keys nets by pin *name*; a symbol pin is addressed by
        # number. Translating here keeps the emitter free of spec knowledge.
        by_number = {
            str(device.pins[pin_name]): net
            for pin_name, net in pin_nets_by_name.items()
            if pin_name in device.pins
        }
        shapes.append(
            PlacedSymbol(
                ref=ref,
                value=device.name,
                shape=_shape_for_device(device, pin_nets_by_name),
                footprint=footprints.get(ref, ""),
                pin_nets=by_number,
            )
        )

    for passive in spec.passives:
        ref = refs[passive.name]
        pin_nets = spec.nets_of(passive.name)
        shapes.append(
            PlacedSymbol(
                ref=ref,
                value=passive.value or passive.type.value,
                shape=_passive_shape(passive.type),
                footprint=footprints.get(ref, ""),
                rotation=_passive_rotation(pin_nets),
                pin_nets=pin_nets,
            )
        )

    paper, warnings = _lay_out(shapes)
    power = _power_symbols(shapes)
    return SchematicResult(
        symbols=shapes,
        nets=[c.net for c in spec.connections],
        warnings=warnings,
        power=power,
        flags=_power_flags(shapes, power),
        no_connects=[
            (sym.ref, pin.number, *_pin_on_sheet(sym, pin))
            for sym in shapes
            for pin in sym.shape.pins
            if pin.number not in sym.pin_nets
        ],
        paper=paper,
    )


def _passive_rotation(pin_nets: dict[str, str]) -> int:
    """Turn a passive over when that puts its ground down and its rail up.

    Pin 1 is drawn at the top. A decoupling capacitor whose ground is on pin 1
    would otherwise wear its ground symbol upside down above its head.
    """
    top = net_class(pin_nets.get("1", ""))
    bottom = net_class(pin_nets.get("2", ""))
    if top == "ground" and bottom != "ground":
        return 180
    if bottom == "rail" and top is None:
        return 180
    return 0


def _cell(sym: PlacedSymbol) -> tuple[int, int, int, int]:
    """Room a symbol needs around its anchor: ``(left, right, up, down)``.

    Sized from the text the symbol carries, not from its body: the reference
    and value beside or above it, the net name on every stub, and the power
    symbol on any rail or ground pin. Every number here is an upper bound, and
    the tests measure the emitted file with their own arithmetic rather than
    trusting these.
    """
    shape = sym.shape
    sideways = any(pin.angle in (0, 180) for pin in shape.pins)
    fields_w = max(len(sym.ref), len(sym.value)) * _CHAR_W_NM

    if sideways:
        reach_l = reach_r = shape.extent_w_nm + _STUB_NM
        for pin in shape.pins:
            net = sym.pin_nets.get(pin.number, "")
            if not net:
                continue
            text = len(net) * _CHAR_W_NM
            if net_class(net) is not None:
                # Any power pin may end up carrying the net's flag.
                text = max(text, _FLAG_LEN_NM + len(_FLAG_TEXT) * _CHAR_W_NM)
            reach = abs(pin.x_nm) + pin.stub_nm + text + _CHAR_W_NM
            if pin.x_nm < 0:
                reach_l = max(reach_l, reach)
            else:
                reach_r = max(reach_r, reach)
        half_w = max(reach_l, reach_r, fields_w // 2)
        # Reference above and value below the body, one text height out, plus
        # the power symbol rising from the top row's stub.
        half_h = shape.extent_h_nm + max(2 * TEXT_NM, _POWER_REACH_NM - _PIN_PITCH_NM)
        return (
            _snap_up(half_w + _GAP_NM),
            _snap_up(half_w + _GAP_NM),
            _snap_up(half_h + _GAP_NM),
            _snap_up(half_h + _GAP_NM),
        )

    # A passive: fields to the right of the body, labels to the right of the
    # stub ends, a power symbol's name centred on the stub.
    longest_net = max((len(n) for n in sym.pin_nets.values()), default=0)
    flagged = any(net_class(n) is not None for n in sym.pin_nets.values())
    left = max(shape.extent_w_nm, longest_net * _CHAR_W_NM // 2)
    right = max(
        shape.extent_w_nm + TEXT_NM + fields_w,
        longest_net * _CHAR_W_NM,
        shape.extent_w_nm + TEXT_NM,
        _FLAG_LEN_NM + len(_FLAG_TEXT) * _CHAR_W_NM if flagged else 0,
    )
    half_h = shape.extent_h_nm + _STUB_NM + _POWER_REACH_NM
    return (
        _snap_up(left + _GAP_NM),
        _snap_up(right + _GAP_NM),
        _snap_up(half_h + _GAP_NM // 2),
        _snap_up(half_h + _GAP_NM // 2),
    )


def _hits_title_block(
    box: tuple[int, int, int, int], page_w: int, page_h: int
) -> bool:
    x0, y0, x1, y1 = box
    return x1 > page_w - TITLE_BLOCK_W_NM and y1 > page_h - TITLE_BLOCK_H_NM


def _try_lay_out(
    symbols: list[PlacedSymbol], page_w: int, page_h: int
) -> list[PlacedSymbol]:
    """Place every symbol on a page of this size; returns what did not fit.

    ICs go in rows across the top of the drawable area, each given the width
    its pin labels need; passives fill a uniform grid beneath them whose pitch
    is the largest cell any passive asks for, so the grid reads as a grid.
    Cells that would run into the title block corner are skipped. Everything
    that fits is placed even when something does not, so the caller can draw
    the overflow below the page rather than on top of the rest.
    """
    x_min, y_min = MARGIN_NM, MARGIN_NM
    x_max, y_max = page_w - MARGIN_NM, page_h - MARGIN_NM
    for sym in symbols:
        sym.x_nm = sym.y_nm = 0

    devices = [s for s in symbols if any(p.angle in (0, 180) for p in s.shape.pins)]
    passives = [s for s in symbols if s not in devices]
    unplaced: list[PlacedSymbol] = []

    # ---- IC rows, each as tall as its tallest cell
    y = y_min
    rows: list[list[tuple[PlacedSymbol, tuple[int, int, int, int]]]] = [[]]
    x = x_min
    for sym in devices:
        cell = _cell(sym)
        width = cell[0] + cell[1]
        if x + width > x_max and rows[-1]:
            rows.append([])
            x = x_min
        rows[-1].append((sym, cell))
        x += width
    for row in rows:
        if not row:
            continue
        up = max(c[2] for _, c in row)
        down = max(c[3] for _, c in row)
        cx = x_min
        for sym, (left, right, _, _) in row:
            box = (cx, y, cx + left + right, y + up + down)
            if (
                box[2] > x_max
                or box[3] > y_max
                or _hits_title_block(box, page_w, page_h)
            ):
                unplaced.append(sym)
            else:
                sym.x_nm = _snap(cx + left)
                sym.y_nm = _snap(y + up)
            cx += left + right
        y += up + down

    # ---- passive grid
    if not passives:
        return unplaced
    if devices:
        y += _ROW_GAP_NM
    cells = [_cell(s) for s in passives]
    left = max(c[0] for c in cells)
    right = max(c[1] for c in cells)
    up = max(c[2] for c in cells)
    down = max(c[3] for c in cells)
    pitch_x = left + right
    pitch_y = up + down
    columns = (x_max - x_min) // pitch_x
    if columns < 1:
        return unplaced + passives

    col = 0
    for sym in passives:
        while True:
            if y + pitch_y > y_max:
                unplaced.append(sym)
                break
            cx = x_min + col * pitch_x
            box = (cx, y, cx + pitch_x, y + pitch_y)
            col += 1
            if col >= columns:
                col = 0
                y_next = y + pitch_y
            else:
                y_next = y
            free = not _hits_title_block(box, page_w, page_h)
            if free:
                sym.x_nm = _snap(cx + left)
                sym.y_nm = _snap(y + up)
            y = y_next
            if free:
                break
    if not unplaced:
        _centre(symbols, page_w, page_h)
    return unplaced


def _centre(symbols: list[PlacedSymbol], page_w: int, page_h: int) -> None:
    """Shift a finished layout to the middle of the drawable area.

    Packed from the top-left corner the sheet reads as half empty; centred it
    reads as composed. The shift is a whole number of grid steps and is
    dropped, axis by axis, if it would push any cell into the title block.
    """
    boxes = []
    for sym in symbols:
        left, right, up, down = _cell(sym)
        boxes.append(
            (sym.x_nm - left, sym.y_nm - up, sym.x_nm + right, sym.y_nm + down)
        )
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    dx = _snap(((page_w - MARGIN_NM - x1) - (x0 - MARGIN_NM)) // 2)
    dy = _snap(((page_h - MARGIN_NM - y1) - (y0 - MARGIN_NM)) // 2)
    for shift_x, shift_y in ((dx, dy), (dx, 0), (0, dy)):
        moved = [
            (bx0 + shift_x, by0 + shift_y, bx1 + shift_x, by1 + shift_y)
            for bx0, by0, bx1, by1 in boxes
        ]
        if not any(_hits_title_block(b, page_w, page_h) for b in moved):
            for sym in symbols:
                sym.x_nm += shift_x
                sym.y_nm += shift_y
            return


def _lay_out(symbols: list[PlacedSymbol]) -> tuple[str, list[str]]:
    """Fit the sheet to the circuit: the smallest paper that holds it.

    Returns the paper name and any warning. A circuit too big even for the
    largest paper is laid out on it anyway, with the overflow stacked below
    the page where it is at least not drawn on top of anything, and says so:
    silently drawing off the page opens to an apparently empty sheet.
    """
    for name, w, h in PAPERS:
        if not _try_lay_out(symbols, w, h):
            return name, []
    name, w, h = PAPERS[-1]
    unplaced = _try_lay_out(symbols, w, h)
    y = h + MARGIN_NM
    for sym in unplaced:
        _, _, up, down = _cell(sym)
        sym.x_nm = MARGIN_NM
        sym.y_nm = _snap(y + up)
        y += up + down
    return name, [
        f"{len(unplaced)} of {len(symbols)} symbols do not fit one {name} sheet, "
        f"the largest paper size the schematic emitter draws; they are drawn "
        f"below the page border"
    ]


def _pin_on_sheet(sym: PlacedSymbol, pin: SymbolPin) -> tuple[int, int]:
    """A pin's connection point in sheet coordinates.

    **The one Y flip in this module.** Symbol libraries are drawn Y-up and the
    sheet is Y-down, so a pin at symbol-local +y sits *above* the anchor on the
    sheet, at a smaller sheet y. A symbol placed at 180 degrees has its local
    axes negated first, still in the Y-up frame, and then flipped once.
    """
    x, y = pin.x_nm, pin.y_nm
    if sym.rotation == 180:
        x, y = -x, -y
    return (sym.x_nm + x, sym.y_nm - y)


def _stub_on_sheet(sym: PlacedSymbol, pin: SymbolPin) -> tuple[int, int, int, int]:
    """Where a pin's wire stub ends, and which way the label reads there.

    Shares the Y flip with :func:`_pin_on_sheet` rather than repeating it.
    ``SymbolPin.stub`` is symbol-local and therefore Y-up; adding its ``dy``
    straight to a sheet coordinate pointed every vertical stub *into* the body,
    so the wire retraced its own pin and the net label landed on the symbol.

    Returns ``(x, y, angle, justify_left)``. Every label is horizontal: on a
    sideways stub it runs away from the body; on a vertical stub it hangs off
    the wire end to the right, as KiCad draws a label dropped on a wire.
    """
    px, py = _pin_on_sheet(sym, pin)
    dx, dy_up = pin.stub
    if sym.rotation == 180:
        dx, dy_up = -dx, -dy_up
    dy = -dy_up  # the one place the stub crosses into the sheet's Y-down frame
    x, y = px + dx * pin.stub_nm, py + dy * pin.stub_nm
    if dx:
        # Horizontal: text reads left-to-right and must run away from the body.
        return x, y, 0, dx > 0
    return x, y, 0, True


def _power_symbols(symbols: list[PlacedSymbol]) -> list[PowerSymbol]:
    """One power symbol on the end of every stub that carries a power net."""
    out: list[PowerSymbol] = []
    for sym in symbols:
        for pin in sym.shape.pins:
            net = sym.pin_nets.get(pin.number, "")
            kind = net_class(net) if net else None
            if kind is None:
                continue
            x, y, _, _ = _stub_on_sheet(sym, pin)
            out.append(
                PowerSymbol(
                    ref=f"#PWR{len(out) + 1:02d}",
                    net=net,
                    ground=kind == "ground",
                    x_nm=x,
                    y_nm=y,
                    part_ref=sym.ref,
                    pin_number=pin.number,
                )
            )
    return out


def _power_flags(
    symbols: list[PlacedSymbol], power: list[PowerSymbol]
) -> list[PowerFlag]:
    """One ``PWR_FLAG`` per power net, on the stub end that has room for it.

    A passive's stub end is preferred: its flag points sideways into space
    the cell already reserves. On an IC the pin whose stub reaches furthest
    from the body is chosen, because the stagger guarantees nothing else is
    drawn beyond it, and the flag points away from the body.
    """
    by_ref = {sym.ref: sym for sym in symbols}
    chosen: dict[str, tuple[tuple[int, int], PowerSymbol, int]] = {}
    for pwr in power:
        sym = by_ref[pwr.part_ref]
        pin = next(p for p in sym.shape.pins if p.number == pwr.pin_number)
        sideways = pin.angle in (0, 180)
        # Lower sorts first: passives before ICs, then the longest stub.
        rank = (1 if sideways else 0, -pin.stub_nm)
        rotation = 180 if sideways and pin.x_nm < 0 else 0
        if pwr.net not in chosen or rank < chosen[pwr.net][0]:
            chosen[pwr.net] = (rank, pwr, rotation)
    out: list[PowerFlag] = []
    for net in dict.fromkeys(p.net for p in power):
        _, pwr, rotation = chosen[net]
        out.append(
            PowerFlag(
                ref=f"#FLG{len(out) + 1:02d}",
                net=net,
                x_nm=pwr.x_nm,
                y_nm=pwr.y_nm,
                rotation=rotation,
                part_ref=pwr.part_ref,
                pin_number=pwr.pin_number,
            )
        )
    return out


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------

_FONT = f"(effects (font (size {_f(TEXT_NM)} {_f(TEXT_NM)})))"
_FONT_LEFT = f"(effects (font (size {_f(TEXT_NM)} {_f(TEXT_NM)})) (justify left))"
_FONT_HIDDEN = f"(effects (font (size {_f(TEXT_NM)} {_f(TEXT_NM)})) hide)"


def _field_anchors(sym: PlacedSymbol) -> tuple[tuple[int, int], tuple[int, int], str]:
    """Where a symbol's Reference and Value text sit, in sheet coordinates.

    Above and below the body is the natural place, and it is the right one for
    an IC: its pins leave sideways, so its net labels never compete for those
    two spots. A passive's pins leave vertically and its labels land exactly
    there -- same x, one text height away -- so the fields go beside the body
    instead, reference above value, which is where KiCad's own passives keep
    them. Never rotated in either case.

    Found by rendering an emitted sheet rather than by parsing it: the file is
    valid and ERC is clean either way, which is why only looking at it catches
    this.
    """
    shape = sym.shape
    if any(pin.angle in (0, 180) for pin in shape.pins):
        return (
            (sym.x_nm, sym.y_nm - shape.extent_h_nm - TEXT_NM),
            (sym.x_nm, sym.y_nm + shape.extent_h_nm + TEXT_NM),
            _FONT,
        )
    # Clear of the stub column, and left-justified so the text grows away from
    # the body rather than back across it.
    x = sym.x_nm + shape.extent_w_nm + TEXT_NM
    return ((x, sym.y_nm - TEXT_NM), (x, sym.y_nm + TEXT_NM), _FONT_LEFT)


def _lib_symbol(shape: SymbolShape, *, ref_prefix: str) -> list[str]:
    """The ``lib_symbols`` entry for one generated symbol."""
    # KiCad keys a lib_symbols entry by the **full** ``Lib:Name`` and names the
    # unit sub-symbols after the bare name. Writing the bare name on the outer
    # entry leaves every instance's lib_id unresolved, which is exactly the
    # "opens without our library" promise this module makes.
    name = shape.lib_id.split(":", 1)[1]
    out = [
        f'    (symbol "{_esc(shape.lib_id)}"'
        + (" (pin_numbers hide)" if shape.hide_pin_numbers else ""),
        "      (pin_names (offset 0.508))",
        "      (exclude_from_sim no) (in_bom yes) (on_board yes)",
        f'      (property "Reference" "{_esc(ref_prefix)}" '
        f"(at 0 {_f(shape.extent_h_nm + TEXT_NM)} 0) {_FONT})",
        f'      (property "Value" "{_esc(name)}" '
        f"(at 0 {_f(-shape.extent_h_nm - TEXT_NM)} 0) {_FONT})",
        f'      (property "Footprint" "" (at 0 0 0) {_FONT_HIDDEN})',
        f'      (property "Datasheet" "" (at 0 0 0) {_FONT_HIDDEN})',
        f'      (symbol "{_esc(name)}_0_1"',
    ]
    out += [f"        {g}" for g in shape.graphics]
    out.append("      )")
    out.append(f'      (symbol "{_esc(name)}_1_1"')
    for pin in shape.pins:
        out.append(
            f"        (pin passive line "
            f"(at {_f(pin.x_nm)} {_f(pin.y_nm)} {pin.angle}) "
            f"(length {_f(shape.pin_len_nm)})"
        )
        out.append(f'          (name "{_esc(pin.name)}" {_FONT})')
        out.append(f'          (number "{_esc(pin.number)}" {_FONT})')
        out.append("        )")
    out.append("      )")
    out.append("    )")
    return out


def _power_lib_symbol(net: str, *, ground: bool) -> list[str]:
    """The ``lib_symbols`` entry for one power net.

    ``(power)`` plus a single hidden ``power_in`` pin named after the net is
    what makes KiCad read this as a power port: the pin's net becomes global
    and the symbol is excluded from the BOM and the board.
    """
    lib_id = _power_lib_id(net)
    name = lib_id.split(":", 1)[1]
    value_y = -mm(3.81) if ground else mm(3.81)
    out = [
        f'    (symbol "{_esc(lib_id)}" (power) (pin_numbers hide)',
        "      (pin_names (offset 0))",
        "      (exclude_from_sim no) (in_bom no) (on_board no)",
        f'      (property "Reference" "#PWR" (at 0 {_f(value_y)} 0) {_FONT_HIDDEN})',
        f'      (property "Value" "{_esc(net)}" (at 0 {_f(value_y)} 0) {_FONT})',
        f'      (property "Footprint" "" (at 0 0 0) {_FONT_HIDDEN})',
        f'      (property "Datasheet" "" (at 0 0 0) {_FONT_HIDDEN})',
        f'      (symbol "{_esc(name)}_0_1"',
    ]
    out += [f"        {g}" for g in _power_graphics(ground)]
    out.append("      )")
    out.append(f'      (symbol "{_esc(name)}_1_1"')
    out.append(
        f"        (pin power_in line (at 0 0 {270 if ground else 90}) "
        f"(length 0) hide"
    )
    out.append(f'          (name "{_esc(net)}" {_FONT})')
    out.append(f'          (number "1" {_FONT})')
    out.append("        )")
    out.append("      )")
    out.append("    )")
    return out


_FLAG_LIB_ID = f"{_ICON_LIB}:PWR_FLAG"


def _flag_lib_symbol() -> list[str]:
    """The ``lib_symbols`` entry for the power flag.

    KiCad's ``PWR_FLAG`` drawn along +X with its ``power_out`` pin at the
    origin, so an instance at 0 points right and one at 180 points left.
    """
    d = mm(1.016)
    return [
        f'    (symbol "{_FLAG_LIB_ID}" (power) (pin_numbers hide)',
        "      (pin_names (offset 0))",
        "      (exclude_from_sim no) (in_bom no) (on_board no)",
        f'      (property "Reference" "#FLG" (at 0 0 0) {_FONT_HIDDEN})',
        f'      (property "Value" "{_FLAG_TEXT}" (at {_f(_FLAG_LEN_NM)} 0 0) '
        f"{_FONT_LEFT})",
        f'      (property "Footprint" "" (at 0 0 0) {_FONT_HIDDEN})',
        f'      (property "Datasheet" "" (at 0 0 0) {_FONT_HIDDEN})',
        '      (symbol "PWR_FLAG_0_1"',
        "        "
        + _polyline(
            [
                (0, 0),
                (mm(1.27), 0),
                (mm(1.905), d),
                (_FLAG_LEN_NM, 0),
                (mm(1.905), -d),
                (mm(1.27), 0),
            ]
        ),
        "      )",
        '      (symbol "PWR_FLAG_1_1"',
        "        (pin power_out line (at 0 0 180) (length 0) hide",
        f'          (name "pwr" {_FONT})',
        f'          (number "1" {_FONT})',
        "        )",
        "      )",
        "    )",
    ]


def _property(key: str, value: str, x: int, y: int, effects: str) -> str:
    return (
        f'    (property "{_esc(key)}" "{_esc(value)}" '
        f"(at {_f(x)} {_f(y)} 0) {effects})"
    )


def emit_kicad_sch(
    result: SchematicResult,
    *,
    project_name: str = "silkscreen",
    title: str | None = None,
    today: _dt.date | None = None,
) -> str:
    """Render a :class:`SchematicResult` as KiCad 8 ``.kicad_sch`` source.

    ``title`` fills the title block; it defaults to the project name because
    the circuit IR carries no name of its own. ``today`` is the title block's
    date and defaults to :data:`DEFAULT_DATE` rather than the wall clock, so
    the same spec emits the same bytes on any day.
    """
    sheet_uuid = stable_uuid(f"sheet:{project_name}")
    date = today or DEFAULT_DATE
    out: list[str] = [
        "(kicad_sch",
        "  (version 20231120)",
        '  (generator "silkscreen")',
        '  (generator_version "8.0")',
        f'  (uuid "{sheet_uuid}")',
        f'  (paper "{result.paper}")',
        "  (title_block",
        f'    (title "{_esc(title or project_name)}")',
        f'    (date "{date.isoformat()}")',
        '    (rev "A")',
        '    (company "silkscreen")',
        f'    (comment 1 "{_esc(project_name)}.kicad_sch")',
        "  )",
    ]

    # One lib_symbols entry per distinct symbol, not per instance: ten
    # decoupling capacitors share one definition, as they do in KiCad.
    out.append("  (lib_symbols")
    seen: dict[str, SymbolShape] = {}
    prefix_of: dict[str, str] = {}
    for sym in result.symbols:
        if sym.shape.lib_id not in seen:
            seen[sym.shape.lib_id] = sym.shape
            prefix_of[sym.shape.lib_id] = sym.ref.rstrip("0123456789") or "U"
    for lib_id, shape in seen.items():
        out += _lib_symbol(shape, ref_prefix=prefix_of[lib_id])
    power_nets: dict[str, bool] = {}
    for pwr in result.power:
        power_nets.setdefault(pwr.net, pwr.ground)
    for net, ground in power_nets.items():
        out += _power_lib_symbol(net, ground=ground)
    if result.flags:
        out += _flag_lib_symbol()
    out.append("  )")

    wires: list[str] = []
    labels: list[str] = []

    for sym in result.symbols:
        shape = sym.shape
        out.append(f'  (symbol (lib_id "{_esc(shape.lib_id)}")')
        out.append(f"    (at {_f(sym.x_nm)} {_f(sym.y_nm)} {sym.rotation}) (unit 1)")
        out.append("    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)")
        out.append(f'    (uuid "{stable_uuid("sym:" + sym.ref)}")')
        (ref_x, ref_y), (val_x, val_y), field_font = _field_anchors(sym)
        out.append(_property("Reference", sym.ref, ref_x, ref_y, field_font))
        out.append(_property("Value", sym.value, val_x, val_y, field_font))
        out.append(
            _property("Footprint", sym.footprint, sym.x_nm, sym.y_nm, _FONT_HIDDEN)
        )
        for pin in shape.pins:
            out.append(
                f'    (pin "{_esc(pin.number)}" '
                f'(uuid "{stable_uuid(f"pin:{sym.ref}:{pin.number}")}"))'
            )
        out.append("    (instances")
        out.append(f'      (project "{_esc(project_name)}"')
        out.append(
            f'        (path "/{sheet_uuid}" '
            f'(reference "{_esc(sym.ref)}") (unit 1))'
        )
        out.append("      )")
        out.append("    )")
        out.append("  )")

        for pin in shape.pins:
            net = sym.pin_nets.get(pin.number, "")
            if not net:
                continue
            px, py = _pin_on_sheet(sym, pin)
            ex, ey, angle, justify_left = _stub_on_sheet(sym, pin)
            wires.append(
                f"  (wire (pts (xy {_f(px)} {_f(py)}) (xy {_f(ex)} {_f(ey)})) "
                f"(stroke (width 0) (type default)) "
                f'(uuid "{stable_uuid(f"w:{sym.ref}:{pin.number}")}"))'
            )
            if net_class(net) is not None:
                continue  # a power symbol stands here instead of a label
            # A *global* label, so the net KiCad extracts is the bare name
            # the board's pads carry. A local label on the root sheet names
            # its net "/NET", and "Update PCB from Schematic" then offered
            # to rename every signal net on every generated board. The
            # passive shape keeps ERC's input/output shape rules quiet.
            justify = "left" if justify_left else "right"
            font = (
                f"(effects (font (size {_f(TEXT_NM)} {_f(TEXT_NM)})) "
                f"(justify {justify})"
            )
            # ``hide`` inside the effects, the KiCad 8 form _FONT_HIDDEN uses:
            # 8's parser rejects a property-level ``(hide yes)`` outright.
            labels.append(
                f'  (global_label "{_esc(net)}" (shape passive) '
                f"(at {_f(ex)} {_f(ey)} {angle}) (fields_autoplaced yes) "
                f"{font}) "
                f'(uuid "{stable_uuid(f"l:{sym.ref}:{pin.number}")}")\n'
                f'    (property "Intersheetrefs" "${{INTERSHEET_REFS}}" '
                f"(at {_f(ex)} {_f(ey)} {angle}) {font} hide)))"
            )

    for pwr in result.power:
        lib_id = _power_lib_id(pwr.net)
        value_y = pwr.y_nm + (mm(3.81) if pwr.ground else -mm(3.81))
        uid = f"pwr:{pwr.part_ref}:{pwr.pin_number}"
        out.append(f'  (symbol (lib_id "{_esc(lib_id)}")')
        out.append(f"    (at {_f(pwr.x_nm)} {_f(pwr.y_nm)} 0) (unit 1)")
        out.append("    (exclude_from_sim no) (in_bom no) (on_board no) (dnp no)")
        out.append(f'    (uuid "{stable_uuid(uid)}")')
        out.append(_property("Reference", pwr.ref, pwr.x_nm, value_y, _FONT_HIDDEN))
        out.append(_property("Value", pwr.net, pwr.x_nm, value_y, _FONT))
        out.append(_property("Footprint", "", pwr.x_nm, pwr.y_nm, _FONT_HIDDEN))
        out.append(f'    (pin "1" (uuid "{stable_uuid(uid + ":1")}"))')
        out.append("    (instances")
        out.append(f'      (project "{_esc(project_name)}"')
        out.append(
            f'        (path "/{sheet_uuid}" (reference "{_esc(pwr.ref)}") (unit 1))'
        )
        out.append("      )")
        out.append("    )")
        out.append("  )")

    for flag in result.flags:
        uid = f"flag:{flag.part_ref}:{flag.pin_number}"
        away = -1 if flag.rotation == 180 else 1
        text_x = flag.x_nm + away * (_FLAG_LEN_NM + TEXT_NM // 2)
        justify = "right" if flag.rotation == 180 else "left"
        font = (
            f"(effects (font (size {_f(TEXT_NM)} {_f(TEXT_NM)})) "
            f"(justify {justify}))"
        )
        out.append(f'  (symbol (lib_id "{_FLAG_LIB_ID}")')
        out.append(
            f"    (at {_f(flag.x_nm)} {_f(flag.y_nm)} {flag.rotation}) (unit 1)"
        )
        out.append("    (exclude_from_sim no) (in_bom no) (on_board no) (dnp no)")
        out.append(f'    (uuid "{stable_uuid(uid)}")')
        out.append(
            _property("Reference", flag.ref, flag.x_nm, flag.y_nm, _FONT_HIDDEN)
        )
        out.append(_property("Value", _FLAG_TEXT, text_x, flag.y_nm, font))
        out.append(_property("Footprint", "", flag.x_nm, flag.y_nm, _FONT_HIDDEN))
        out.append(f'    (pin "1" (uuid "{stable_uuid(uid + ":1")}"))')
        out.append("    (instances")
        out.append(f'      (project "{_esc(project_name)}"')
        out.append(
            f'        (path "/{sheet_uuid}" (reference "{_esc(flag.ref)}") (unit 1))'
        )
        out.append("      )")
        out.append("    )")
        out.append("  )")

    out += wires
    out += labels
    for ref, number, x, y in result.no_connects:
        out.append(
            f"  (no_connect (at {_f(x)} {_f(y)}) "
            f'(uuid "{stable_uuid(f"nc:{ref}:{number}")}"))'
        )
    out.append('  (sheet_instances (path "/" (page "1")))')
    out.append(")")
    return "\n".join(out) + "\n"


def emit_kicad_pro(project_name: str = "silkscreen") -> str:
    """A minimal ``.kicad_pro`` so KiCad opens the pair as one project.

    Without it the schematic and the board are two loose files that KiCad will
    not associate, and "generate a schematic, then open the board from it" --
    the workflow this whole change exists to support -- does not work.
    """
    sheet_uuid = stable_uuid(f"sheet:{project_name}")
    return (
        "{\n"
        '  "board": {"design_settings": {"defaults": {}}},\n'
        '  "boards": [],\n'
        '  "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},\n'
        f'  "meta": {{"filename": "{project_name}.kicad_pro", "version": 1}},\n'
        '  "net_settings": {"classes": [{"name": "Default"}]},\n'
        '  "pcbnew": {"page_layout_descr_file": ""},\n'
        '  "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},\n'
        f'  "sheets": [["{sheet_uuid}", "Root"]],\n'
        '  "text_variables": {}\n'
        "}\n"
    )


def write_schematic(
    result: SchematicResult,
    path: str | Path,
    *,
    project_name: str | None = None,
    title: str | None = None,
    today: _dt.date | None = None,
) -> Path:
    """Write the schematic to ``path`` and return it."""
    path = Path(path)
    name = project_name or path.stem
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        emit_kicad_sch(result, project_name=name, title=title, today=today),
        encoding="utf-8",
    )
    return path


def write_project(path: str | Path, *, project_name: str | None = None) -> Path:
    """Write the ``.kicad_pro`` beside the schematic and board."""
    path = Path(path)
    name = project_name or path.stem
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_kicad_pro(name), encoding="utf-8")
    return path

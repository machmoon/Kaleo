"""Emit a complete ``.kicad_pcb`` from a validated circuit.

This is the step the previous project never had. Its "layout engine" pasted a
board a human had already drawn, through the clipboard, into a running KiCad
window -- so it could only ever produce the one design it had been handed.

Here a :class:`~silkscreen.netlist.CircuitSpec` becomes real footprints with
real pads on real nets, placed by the CP-SAT solver, written as KiCad 8
s-expressions. No KiCad installation, no footprint library on disk, no GUI.

The output is verified by round-tripping through ``kiutils`` in the test suite:
if KiCad's own parser cannot read what we wrote, the tests fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .footprints import (
    BATTERY_PACKAGES,
    CONNECTOR_PACKAGES,
    SWITCH_PACKAGES,
    TESTPOINT_PACKAGES,
    Footprint,
    UnsupportedPackage,
    battery_holder,
    cathode_mark,
    connector,
    dual_row_header,
    for_passive,
    lqfp,
    pin1_mark,
    probe_point,
    silk_segments,
    soic,
    sot223,
    switch,
)
from .ids import stable_uuid
from .models3d import model_for
from .netlist import CircuitSpec
from .packing import Layer, Part, Placement, pack
from .packing import Net as PackNet
from .routing import RoutePad, RouteResult, Track, Via, route
from .units import DEFAULT_CLEARANCE_NM, NM_PER_MM, mm

__all__ = [
    "BoardResult",
    "PlacedPart",
    "DEFAULT_BOARD_MARGIN_NM",
    "PASSIVE_SPACING_NM",
    "IC_SPACING_NM",
    "REF_TEXT_SIZE_NM",
    "REF_TEXT_STROKE_NM",
    "REF_TEXT_CLEARANCE_NM",
    "REF_TEXT_BAND_NM",
    "ref_text_offset_nm",
    "build_board",
    "board_pads",
    "package_errors",
    "supported_packages_text",
    "part_anchor",
    "placed_half_extents",
    "rotate_offset",
    "route_board",
    "emit_kicad_pcb",
    "write_board",
]

#: Gap between the outermost courtyard and the board edge. Shared by the
#: emitter and the router: the router must know where the copper may go, and
#: the edge is drawn from the same number, so they cannot drift apart.
DEFAULT_BOARD_MARGIN_NM = mm(2.0)

#: Room a generated board reserves on every side of a part's courtyard, on top
#: of the solver clearance. Two neighbours end up ``clearance + a + b`` apart,
#: so with the 0.25 mm default clearance two passives sit 0.75 mm apart and
#: anything next to an IC sits at least 1.0 mm from it. The solver's own
#: clearance is a manufacturing minimum, and a board packed at the minimum
#: reads as compressed rather than placed: nowhere to probe, nowhere to rework,
#: nowhere for a designator to go. These are the generated-board defaults
#: only -- ``pack()`` still places a real board exactly as asked.
PASSIVE_SPACING_NM = mm(0.25)
IC_SPACING_NM = mm(0.5)

#: The reference designator: KiCad text size, pen width, and the gap kept
#: between its glyph box and the courtyard it labels (the same 0.2 mm the
#: silkscreen keeps from copper, see :data:`footprints.SILK_PAD_CLEARANCE_NM`).
REF_TEXT_SIZE_NM = mm(0.8)
REF_TEXT_STROKE_NM = mm(0.12)
REF_TEXT_CLEARANCE_NM = mm(0.2)

#: Height of the band above every courtyard that the designator occupies:
#: clearance plus the glyph box (size plus one pen width). The placer reserves
#: this band with the part, so the text can never land on a neighbour --
#: which is where it went when parts were packed at the bare clearance.
REF_TEXT_BAND_NM = REF_TEXT_CLEARANCE_NM + REF_TEXT_SIZE_NM + REF_TEXT_STROKE_NM

#: Horizontal room per glyph. KiCad's stroke font advances about 0.7 of the
#: size per character; reserving one full size per character over-reserves
#: rather than under, so a long designator on a narrow part widens the part's
#: reservation instead of hanging over the neighbour.
_REF_GLYPH_ADVANCE_NM = REF_TEXT_SIZE_NM


def ref_text_offset_nm(fp: Footprint) -> int:
    """Distance from the anchor to the designator's centre, straight up.

    Footprint-local, along the courtyard's short side: the text sits centred
    above the courtyard with :data:`REF_TEXT_CLEARANCE_NM` between its glyph
    box and the courtyard line. The emitter writes it (negated, since KiCad's
    footprint frame is Y-down) and :func:`build_board` reserves the band it
    needs, so the two cannot disagree about where the label is.
    """
    return (
        fp.courtyard_h_nm
        + REF_TEXT_CLEARANCE_NM
        + (REF_TEXT_SIZE_NM + REF_TEXT_STROKE_NM) // 2
    )


def _ref_text_width_nm(ref: str) -> int:
    """Conservative width of a designator's glyph box."""
    return len(ref) * _REF_GLYPH_ADVANCE_NM + REF_TEXT_STROKE_NM


@dataclass(frozen=True)
class _Reserve:
    """Room reserved around a courtyard in the solver's box, per side.

    ``top`` is the side the designator lives on (Y-up, above the courtyard);
    it is the larger of the spacing and the text band, since the band already
    exceeds any spacing this module defines. ``side`` grows when the designator
    is wider than the courtyard, so a long ref on an 0402 still stays inside
    the part's own reservation.
    """

    side: int
    bottom: int
    top: int


def _reserve_for(fp: Footprint, ref: str, spacing_nm: int) -> _Reserve:
    overhang = max(0, (_ref_text_width_nm(ref) - 2 * fp.courtyard_w_nm + 1) // 2)
    return _Reserve(
        side=max(spacing_nm, overhang),
        bottom=spacing_nm,
        top=max(spacing_nm, REF_TEXT_BAND_NM),
    )


_LAYERS = """    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)"""


@dataclass
class PlacedPart:
    """A footprint with its reference designator, position and value."""

    ref: str
    footprint: Footprint
    value: str = ""
    x_nm: int = 0
    y_nm: int = 0
    rotated: bool = False
    layer: Layer = Layer.TOP


@dataclass
class BoardResult:
    parts: list[PlacedPart]
    nets: list[str]
    width_nm: int
    height_nm: int
    solver_status: str
    wirelength_nm: int | None = None
    warnings: list[str] = field(default_factory=list)
    #: Copper laid by :func:`route_board`. Empty means the board is placed but
    #: unrouted -- which is what KiCad draws as a ratsnest.
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    #: Nets the router could not finish, each mapped to the reason. Read this
    #: before telling anyone the board is routed.
    unrouted_nets: dict[str, str] = field(default_factory=dict)
    routed_nets: list[str] = field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        return (self.width_nm / NM_PER_MM, self.height_nm / NM_PER_MM)

    @property
    def is_routed(self) -> bool:
        """True only when every routable net came out fully connected."""
        return bool(self.tracks) and not self.unrouted_nets

    @property
    def route_completion(self) -> float:
        """Fraction of routable nets finished.

        1.0 with both lists empty means there was nothing to route, not that a
        refusal succeeded: :func:`route_board` names every net a refusal covers
        in ``unrouted_nets``, so a skipped route reads 0.0 rather than 100%.
        """
        total = len(self.routed_nets) + len(self.unrouted_nets)
        return 1.0 if total == 0 else len(self.routed_nets) / total


@dataclass(frozen=True)
class _ModulePackage:
    """A plug-in module's land pattern, keyed by name rather than pin count.

    A module is not a chip. Its pin count says nothing about its package -- a
    30-pin Arduino Nano and a 30-pin SSOP share a number and nothing else --
    so the count can never select the geometry the way it does for a SOIC. The
    name selects it and the count is then *checked*, which is what turns a
    mismatch into a refusal instead of a board with pads in the wrong place.
    """

    label: str
    pin_count: int
    row_spacing_mm: float
    body_w_mm: float
    body_h_mm: float


#: Modules this pipeline has a characterised land pattern for. Adding an entry
#: means someone has checked the dimensions against the module's own drawing;
#: it is deliberately not a guess from the pin count.
_MODULE_PACKAGES: dict[str, _ModulePackage] = {
    # Arduino Nano and its pin-compatible clones: two 15-pin 0.1" rows on a
    # 0.6" span, 17.78 x 43.18 mm board outline (Arduino Nano rev 3.2 drawing).
    "arduino_nano": _ModulePackage(
        label="Arduino Nano",
        pin_count=30,
        row_spacing_mm=15.24,
        body_w_mm=17.78,
        body_h_mm=43.18,
    ),
}


def _module_key(name: str) -> str | None:
    """The module registry key this device name names, if any.

    Names arrive decorated ("U1_ARDUINO_NANO"), so the match is a token
    search over a normalised name rather than an equality test.
    """
    normalised = "".join(c if c.isalnum() else "_" for c in name.lower())
    for key in _MODULE_PACKAGES:
        if key in normalised:
            return key
    return None


#: The kinds whose land pattern is chosen by *name*. One tuple rather than a
#: literal repeated at each branch, so a kind added to the dispatch table below
#: and forgotten at the ``_footprint_for_device`` branch cannot happen -- which
#: would refuse every board carrying the new part while the table advertised it.
_NAMED_KINDS = ("connector", "battery", "switch", "testpoint")


def _named_package(
    kind: str, name: str, package: str | None, pin_count: int, nets: dict[str, str]
) -> Footprint:
    """Draw a connector or battery holder from its named land pattern.

    Split out because these two kinds are selected by *name*, not pin count:
    a 2-pin JST and a 2-pin screw terminal share a number and nothing else,
    exactly as with the plug-in modules above.
    """
    known, build = {
        "connector": (CONNECTOR_PACKAGES, connector),
        "battery": (BATTERY_PACKAGES, battery_holder),
        "switch": (SWITCH_PACKAGES, switch),
        "testpoint": (TESTPOINT_PACKAGES, probe_point),
    }[kind]
    if package is None:
        raise UnsupportedPackage(
            f"{kind} {name!r} names no package; its land pattern cannot be "
            f"guessed from the pin count. Supported: {sorted(known)}."
        )
    if package not in known:
        raise UnsupportedPackage(
            f"No {kind} land pattern named {package!r} (for {name!r}). "
            f"Supported: {sorted(known)}."
        )
    # A count *above* the package's is the module rule again: the name says
    # which part this is, and a circuit declaring more pins than it has means
    # one of the two is wrong. Fewer is legitimate -- a USB-C power receptacle
    # wired for VBUS and GND alone leaves its other pins unconnected.
    if pin_count > known[package]:
        raise UnsupportedPackage(
            f"{name!r} is a {package} with {known[package]} pins, but the "
            f"circuit declares {pin_count}. Refusing to place a connector on "
            f"a pattern that does not match its pinout."
        )
    try:
        return build(package, nets)
    except ValueError as exc:
        # ``footprints`` raises plain ``ValueError`` for an unknown package so
        # it need not import this module; converted here so every caller sees
        # the one refusal type. The membership test above should already have
        # caught it -- this is the belt for a table that drifts.
        raise UnsupportedPackage(str(exc)) from exc


def _footprint_for_device(
    name: str,
    pin_count: int,
    nets: dict[str, str],
    *,
    kind: str = "ic",
    package: str | None = None,
) -> Footprint:
    """Pick a package from the device's kind, then its name, then the pin count.

    Deliberately conservative: it covers the shapes this pipeline generates and
    raises otherwise. Guessing a land pattern is how boards come back dead.

    ``kind`` is consulted first and that ordering is the whole point. While the
    pin count came first, a 2-pin power connector fell through to the SOIC rule,
    was padded to 4 pins, and was drawn as a surface-mount chip: the board had
    no power input, the schematic claimed one, and nothing raised.
    """
    if kind in _NAMED_KINDS:
        return _named_package(kind, name, package, pin_count, nets)
    if kind != "ic":
        raise UnsupportedPackage(
            f"device {name!r} has unknown kind {kind!r}; known kinds are "
            f"'ic' and {sorted(_NAMED_KINDS)}."
        )
    if package is not None:
        raise UnsupportedPackage(
            f"device {name!r} is an 'ic' but names package {package!r}; an "
            f"IC's land pattern follows from its pin count."
        )
    key = _module_key(name)
    if key is not None:
        module = _MODULE_PACKAGES[key]
        if pin_count != module.pin_count:
            # The name says which land pattern this is; a count that disagrees
            # means one of the two is wrong, and building either would put
            # copper where the part has no pins.
            raise UnsupportedPackage(
                f"{name!r} matches the {module.label} land pattern, which has "
                f"{module.pin_count} pins, but the circuit declares "
                f"{pin_count}. Refusing to place a module on a pattern that "
                f"does not match its pinout."
            )
        return dual_row_header(
            module.pin_count,
            row_spacing_mm=module.row_spacing_mm,
            body_w_mm=module.body_w_mm,
            body_h_mm=module.body_h_mm,
            nets=nets,
        )
    if pin_count == 3:
        return sot223(nets)
    if pin_count in SOIC_PINS:
        return soic(pin_count, nets=nets)
    if pin_count in LQFP_BODY_MM:
        return lqfp(pin_count, body_mm=LQFP_BODY_MM[pin_count], nets=nets)
    raise UnsupportedPackage(
        f"No package rule for {name!r} with {pin_count} pins. Supported: "
        f"{supported_packages_text()}."
    )


#: The pin counts each land-pattern generator actually covers. The prompt
#: text and the refusal message are both rendered from these, so a package
#: the builder does not have cannot be advertised to the model.
SOIC_PINS = (4, 6, 8, 10, 14, 16, 20, 24, 28)
LQFP_BODY_MM = {32: 5.0, 44: 10.0, 48: 7.0, 64: 10.0, 100: 14.0, 144: 20.0}


def supported_packages_text() -> str:
    """The package rule, in words, for prompts and error messages.

    One string in one place: the propose prompt tells the model what it may
    pick from, and the refusal above says the same thing, so the two cannot
    drift apart and leave the model designing against a list the builder no
    longer honours.
    """
    # Rendered from the same tuples the builder branches on. The old string
    # said "4-28 pins even (SOIC)", which advertises 12, 18, 22 and 26 --
    # none of which `_footprint_for_device` accepts. The whole point of this
    # function is that the prompt and the refusal cannot drift, and they had.
    soic = "/".join(str(n) for n in SOIC_PINS)
    lqfp_pins = "/".join(str(n) for n in sorted(LQFP_BODY_MM))
    # Connectors and batteries are listed by *name*, rendered straight from the
    # tables the builder branches on for the same anti-drift reason -- and
    # deliberately without their pin counts, which are implied by the name and
    # would otherwise read as pin counts an IC could be built at.
    return (
        f"kind 'ic': 3 pins (SOT-223), {soic} pins (SOIC), {lqfp_pins} pins "
        f"(LQFP), and the named modules {sorted(_MODULE_PACKAGES)}. "
        f"kind 'connector', by package name: {sorted(CONNECTOR_PACKAGES)}. "
        f"kind 'battery', by package name: {sorted(BATTERY_PACKAGES)}. "
        f"kind 'switch', by package name: {sorted(SWITCH_PACKAGES)}. "
        f"kind 'testpoint', by package name: {sorted(TESTPOINT_PACKAGES)}"
    )


def package_errors(spec: CircuitSpec) -> list[str]:
    """Every device in ``spec`` that no land-pattern rule covers, as messages.

    This is the footprint rule run early, at proposal time, so an unsupported
    package goes back to the model as one more repair item instead of surfacing
    after the proposal was accepted, where nothing can fix it. Empty means
    :func:`build_board` will not raise :class:`UnsupportedPackage`.
    """
    errors: list[str] = []
    for device in spec.devices:
        try:
            _footprint_for_device(
                device.name,
                _package_pin_count(device.pins),
                {},
                kind=device.kind,
                package=device.package,
            )
        except UnsupportedPackage as exc:
            errors.append(str(exc))
    return errors


def _package_pin_count(pins: dict[str, str]) -> int:
    """Highest physical pin number, which is the package's pin count."""
    numbers = []
    for value in pins.values():
        try:
            numbers.append(int(str(value)))
        except (TypeError, ValueError):
            continue
    return max(numbers) if numbers else len(pins)


_POWER_TOKENS = ("gnd", "vss", "vcc", "vdd", "vbus", "vin", "vout", "agnd", "dgnd")


def _is_power(net: str) -> bool:
    lowered = net.lower().lstrip("+-/")
    return any(lowered.startswith(t) for t in _POWER_TOKENS)


def solver_pad_offset(fp, pad, reserve) -> tuple[int, int]:
    """A pad as an offset from the bottom-left of the box the solver places.

    The one place `build_board` crosses frames, factored out so it can be
    checked against independent arithmetic rather than only through a whole
    solve.

    X runs the same way in both frames, so the pad's own offset is added. Y
    does not: ``pad.y_nm`` is the footprint frame's **Y-down** offset from the
    package centre (see footprints.py's "Frame" note), while the solver
    measures Y **up** from the reserved box's bottom-left corner. So the
    half-extent minus the pad, never plus it.

    Adding it mirrored every package vertically before CP-SAT ever saw it --
    pin 1 of a SOIC-8 was handed over as though it sat at the bottom of the
    part. Nothing overlapped and no test failed, because the geometry that is
    *written* is converted correctly elsewhere (``kicad.py``'s ``max_y - pad_y``
    and ``board_pads``' single flip). What was wrong was the netlist the
    placer optimised against -- and so the placement, and so the reported
    ``wirelength_nm``.
    """
    ox = (pad.x_nm if pad else 0) + fp.courtyard_w_nm + reserve.side
    oy = (fp.courtyard_h_nm - (pad.y_nm if pad else 0)) + reserve.bottom
    return ox, oy


def build_board(
    spec: CircuitSpec,
    *,
    clearance_nm: int = DEFAULT_CLEARANCE_NM,
    time_limit_s: float | None = 20.0,
    edge_refs: set[str] | None = None,
    rotatable_refs: set[str] | None = None,
    two_sided: bool = False,
) -> BoardResult:
    """Turn a validated circuit into footprints, nets and a placement.

    ``rotatable_refs`` names parts the solver may turn 90 degrees. Rotation is
    off by default because it only pays when a board is tight, and it costs
    determinism nothing either way.

    With ``two_sided``, passives may go on either side while ICs stay on top --
    an IC on the underside complicates assembly and rework for little area
    saved, whereas moving decoupling capacitors under their chip is standard
    practice and shortens the loop.
    """
    spec.validate()

    # The schematic emitter numbers parts from the same call, so R2 on the
    # drawing is R2 on the board. Numbering them separately would give two
    # self-consistent files that describe different circuits.
    ref_of = spec.assign_refs()

    # A ref that names nothing is a caller error, not a no-op -- the same
    # convention pack() and kicad.py already follow. Silently ignoring it means
    # a part the caller asked to rotate quietly does not, and the board looks
    # like the solver simply preferred it that way.
    known = set(ref_of.values())
    unknown = sorted(set(rotatable_refs or ()) - known)
    if unknown:
        raise ValueError(
            f"rotatable_refs names {unknown}, which are not on this board; "
            f"known refs: {sorted(known)}"
        )

    placed: list[PlacedPart] = []
    # How much room each part reserves beyond its courtyard. Devices are the
    # ICs; everything from the passives list is a chip passive.
    spacing_of: dict[str, int] = {}

    for device in spec.devices:
        pin_nets: dict[str, str] = {}
        for conn in spec.connections:
            for endpoint in conn.endpoints:
                part, _, pin_name = endpoint.rpartition(".")
                if part == device.name:
                    number = device.pins.get(pin_name)
                    if number:
                        pin_nets[str(number)] = conn.net
        fp = _footprint_for_device(
            device.name,
            _package_pin_count(device.pins),
            pin_nets,
            kind=device.kind,
            package=device.package,
        )
        ref = ref_of[device.name]
        placed.append(PlacedPart(ref=ref, footprint=fp, value=device.name))
        spacing_of[ref] = IC_SPACING_NM

    for passive in spec.passives:
        legs = {"1": "", "2": ""}
        for conn in spec.connections:
            for endpoint in conn.endpoints:
                part, _, leg = endpoint.rpartition(".")
                if part == passive.name and leg in legs:
                    legs[leg] = conn.net
        fp = for_passive(
            passive.type.value, passive.value, net1=legs["1"], net2=legs["2"]
        )
        ref = ref_of[passive.name]
        placed.append(PlacedPart(ref=ref, footprint=fp, value=passive.value))
        spacing_of[ref] = PASSIVE_SPACING_NM

    # The solver places a box larger than the courtyard: the courtyard plus
    # the spacing on every side plus the designator's band on top. The solver
    # clearance then separates *boxes*, so courtyards end up spacing-plus-
    # clearance apart and the label above one part never reaches the next.
    # The courtyard's own position is recovered from the box after solving.
    reserve_of = {
        p.ref: _reserve_for(p.footprint, p.ref, spacing_of[p.ref]) for p in placed
    }
    parts = [
        Part(
            width_nm=p.footprint.courtyard_w_nm * 2 + 2 * reserve_of[p.ref].side,
            height_nm=(
                p.footprint.courtyard_h_nm * 2
                + reserve_of[p.ref].bottom
                + reserve_of[p.ref].top
            ),
            ref=p.ref,
            must_be_on_edge=p.ref in (edge_refs or set()),
            allow_rotation=p.ref in (rotatable_refs or set()),
            layer=Layer.EITHER if two_sided and p.ref.startswith(("C", "R"))
            else Layer.TOP,
        )
        for p in placed
    ]
    index_of = {p.ref: i for i, p in enumerate(placed)}

    pack_nets: list[PackNet] = []
    for conn in spec.connections:
        terminals: list[tuple[int, tuple[int, int]]] = []
        seen: set[int] = set()
        for endpoint in conn.endpoints:
            part_name, _, pin = endpoint.rpartition(".")
            ref = ref_of.get(part_name)
            if ref is None:
                continue
            idx = index_of[ref]
            if idx in seen:
                continue
            seen.add(idx)
            fp = placed[idx].footprint
            number = pin
            device = next((d for d in spec.devices if d.name == part_name), None)
            if device is not None:
                number = str(device.pins.get(pin, pin))
            pad = fp.pad_by_number(number)
            # Offsets are measured from the bottom-left corner of the box the
            # solver places, which is the courtyard's corner pushed out by
            # the reservation on that side.
            reserve = reserve_of[ref]
            terminals.append((idx, solver_pad_offset(fp, pad, reserve)))
        if len(terminals) >= 2:
            pack_nets.append(
                PackNet(
                    terminals=tuple(terminals),
                    name=conn.net,
                    weight=0.25 if _is_power(conn.net) else 1.0,
                )
            )

    result = pack(
        parts, nets=pack_nets, clearance_nm=clearance_nm, time_limit_s=time_limit_s
    )
    by_ref: dict[str, Placement] = {p.ref: p for p in result.placements}
    for part in placed:
        placement = by_ref.get(part.ref)
        if placement:
            # The placement is the reserved box's bottom-left corner; the
            # part's own corner is inside it by the reservation. A rotated
            # box has turned with the part: pack maps a local (ox, oy) to
            # (H - oy, ox), which puts the top band on the left and the side
            # room underneath. That is the same turn rotate_offset applies
            # to the pads and the emitter applies to the label, so all three
            # agree on which side of the part the designator is on.
            reserve = reserve_of[part.ref]
            if placement.rotated:
                part.x_nm = placement.x_nm + reserve.top
                part.y_nm = placement.y_nm + reserve.side
            else:
                part.x_nm = placement.x_nm + reserve.side
                part.y_nm = placement.y_nm + reserve.bottom
            part.rotated = placement.rotated
            part.layer = placement.layer

    return BoardResult(
        parts=placed,
        nets=[c.net for c in spec.connections],
        width_nm=result.board_width_nm,
        height_nm=result.board_height_nm,
        solver_status=result.status.value,
        wirelength_nm=result.wirelength_nm,
        warnings=list(result.warnings),
    )


def placed_half_extents(part: PlacedPart) -> tuple[int, int]:
    """The part's courtyard half-extents **as placed**, not as drawn.

    A 90-degree rotation swaps them. The placer reserves the swapped box and
    reports its bottom-left corner, so anything deriving a position from that
    corner has to swap too. Not swapping is the recorded bug this function
    exists to end: the anchor came out short by exactly ``(ch-cw, cw-ch)``, the
    file still parsed, the courtyard still drew, and only the pads were wrong.
    """
    fp = part.footprint
    if part.rotated:
        return fp.courtyard_h_nm, fp.courtyard_w_nm
    return fp.courtyard_w_nm, fp.courtyard_h_nm


def part_anchor(part: PlacedPart) -> tuple[int, int]:
    """The footprint anchor in the solver's Y-up frame.

    One definition, used by the emitter and by the router's pad geometry. Two
    copies of this arithmetic is how the copper and the footprints ended up in
    different places while both looked right on their own.
    """
    half_w, half_h = placed_half_extents(part)
    return part.x_nm + half_w, part.y_nm + half_h


def rotate_offset(x_nm: int, y_nm: int, *, rotated: bool) -> tuple[int, int]:
    """A footprint-local offset mapped into the board frame.

    Only 0 and 90 degrees occur, and both are exact in integer nanometres, so
    this stays integer arithmetic rather than going through sin/cos. KiCad
    turns a footprint counter-clockwise on screen and screen Y points down, so
    the board-frame image of a local point is a rotation by ``-angle``: at 90
    degrees, ``(x, y) -> (y, -x)``. That is the same mapping
    :func:`silkscreen.kicad._rotate_mm` applies when reading a board back, and
    the two must agree or a board does not survive a round trip.
    """
    if not rotated:
        return x_nm, y_nm
    return y_nm, -x_nm


def board_pads(board: BoardResult) -> list[RoutePad]:
    """Every pad on the board, absolute, in the solver's Y-up frame.

    The placer hands back a courtyard's bottom-left corner with Y up; a pad
    offset inside a footprint is measured from the anchor with Y **down**,
    because that is the frame it is written to the file in. Composing the two
    is the single place those frames meet outside the emitter, and getting it
    wrong is the silent-geometry bug class: the router would lay copper to
    coordinates no pad occupies, the run would report success, and the board
    would come back with tracks ending in bare laminate.

    A rotated part turns its pads with it: the offset is mapped through
    :func:`rotate_offset` and the pad's own width and height swap, because a
    1.95 x 0.6 mm pad laid on its side is 0.6 x 1.95 mm of copper for the
    router to keep clear of.
    """
    pads: list[RoutePad] = []
    for part in board.parts:
        anchor_x, anchor_y = part_anchor(part)
        for pad in part.footprint.pads:
            ox, oy = rotate_offset(pad.x_nm, pad.y_nm, rotated=part.rotated)
            w_nm, h_nm = (
                (pad.h_nm, pad.w_nm) if part.rotated else (pad.w_nm, pad.h_nm)
            )
            pads.append(
                RoutePad(
                    net=pad.net,
                    x_nm=anchor_x + ox,
                    # The one Y flip: pad offsets are written Y-down, the
                    # router works Y-up.
                    y_nm=anchor_y - oy,
                    w_nm=w_nm,
                    h_nm=h_nm,
                    layer=part.layer,
                    # A plated hole is copper on both faces whichever side
                    # the part sits on; the emitter writes it on "*.Cu" and
                    # the router has to keep both layers clear of it.
                    through_hole=pad.is_tht,
                    ref=part.ref,
                    number=pad.number,
                )
            )
    return pads


def route_board(
    board: BoardResult,
    *,
    margin_nm: int = DEFAULT_BOARD_MARGIN_NM,
    two_layer: bool = True,
    **kwargs,
) -> RouteResult:
    """Route ``board`` in place, filling its tracks, vias and unrouted nets.

    Placement and routing are separate calls, not one, so the placed board can
    be written out and inspected before any copper exists. That is the step the
    pipeline used to skip past.

    Rotated parts route like any other. They used to abort here, because the
    emitter misplaced a rotated footprint's anchor and routing to those
    coordinates would have turned a latent placement bug into copper landing on
    bare laminate. That bug is fixed: :func:`part_anchor` is now the one
    definition of where a part sits, and the emitter and this function both
    read it.
    """
    result = route(
        board_pads(board),
        min_x_nm=-margin_nm,
        min_y_nm=-margin_nm,
        max_x_nm=board.width_nm + margin_nm,
        max_y_nm=board.height_nm + margin_nm,
        two_layer=two_layer,
        **kwargs,
    )
    board.tracks = list(result.tracks)
    board.vias = list(result.vias)
    board.unrouted_nets = dict(result.unrouted)
    board.routed_nets = list(result.routed)
    board.warnings.extend(result.warnings)
    return result


def _uuid(seed: str) -> str:
    """Stable UUID from a seed, so output is byte-identical across runs."""
    return stable_uuid(seed)


def emit_kicad_pcb(
    board: BoardResult, *, margin_nm: int = DEFAULT_BOARD_MARGIN_NM
) -> str:
    """Render a :class:`BoardResult` as KiCad 8 ``.kicad_pcb`` source."""

    def f(nm: int) -> str:
        return f"{nm / NM_PER_MM:.4f}".rstrip("0").rstrip(".") or "0"

    # Net 0 is KiCad's unconnected net and must exist.
    net_names: list[str] = [""]
    for name in dict.fromkeys(board.nets):
        if name and name not in net_names:
            net_names.append(name)
    net_index = {name: i for i, name in enumerate(net_names)}

    out: list[str] = []
    out.append('(kicad_pcb (version 20240108) (generator "silkscreen")')
    out.append("  (general (thickness 1.6))")
    out.append('  (paper "A4")')
    out.append("  (layers")
    out.append(_LAYERS)
    out.append("  )")
    for i, name in enumerate(net_names):
        out.append(f'  (net {i} "{name}")')

    m = margin_nm
    corners = [
        (-m, -m),
        (board.width_nm + m, -m),
        (board.width_nm + m, board.height_nm + m),
        (-m, board.height_nm + m),
    ]
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        out.append(
            f"  (gr_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
            f' (stroke (width 0.1) (type solid)) (layer "Edge.Cuts")'
            f' (uuid "{_uuid(f"edge{i}")}"))'
        )

    for part in board.parts:
        fp = part.footprint
        # The placer gives a bottom-left corner with Y up; KiCad wants the
        # anchor with Y down, and the courtyard is centred on the anchor.
        # part_anchor owns the rotation swap, so this cannot drift from the
        # pad geometry the router was handed.
        anchor_up_x, anchor_up_y = part_anchor(part)
        anchor_x = anchor_up_x
        anchor_y = board.height_nm - anchor_up_y
        angle = 90 if part.rotated else 0
        at = f"{f(anchor_x)} {f(anchor_y)}" + (f" {angle}" if angle else "")

        # A bottom-side footprint lives on B.Cu with its pads and graphics on
        # the B layers; KiCad renders it mirrored from the same coordinates.
        bottom = part.layer is Layer.BOTTOM
        side = "B" if bottom else "F"
        out.append(f'  (footprint "silkscreen:{fp.name}"')
        out.append(f'    (layer "{side}.Cu")')
        out.append(f'    (uuid "{_uuid(part.ref)}")')
        out.append(f"    (at {at})")
        out.append(f'    (descr "{fp.description}")')
        # The library 3D model, when the land pattern has an honest one: the
        # 3D viewer and ``kicad-cli pcb export glb`` draw a part only from a
        # model line, and a board without any exports as a bare substrate.
        # The offset is the model's own claim about where it sits relative to
        # this land pattern's origin, and it is zero only because KiCad and
        # this pipeline agree on centring for ICs and chip packages. A
        # through-hole connector is anchored on pin 1 in KiCad's library and
        # on the body here, so its model carries the difference; writing zero
        # for those put the plastic beside the part. The rotation is the
        # model's claim about whose pin-1 convention it was drawn for
        # (models3d.Model3D).
        model = model_for(fp.name, part.ref)
        if model is not None:
            ox, oy, oz = model.offset_mm
            out.append(
                f'    (model "{model.path}" (offset (xyz {ox} {oy} {oz}))'
                f" (scale (xyz 1 1 1)) (rotate (xyz 0 0 {model.rotate_deg})))"
            )
        # The designator sits centred just above the courtyard, in the band
        # build_board reserved for it. Its angle is written as the part's own:
        # KiCad stores footprint text angles as absolute on-screen angles, so
        # a rotated part's label written at 0 would stay upright and lie
        # across the part (checked against kicad-cli 10), while one written
        # at the part's angle turns with it and stays in the band.
        text_size = f(REF_TEXT_SIZE_NM)
        text_stroke = f(REF_TEXT_STROKE_NM)
        out.append(
            f'    (property "Reference" "{part.ref}"'
            f" (at 0 {f(-ref_text_offset_nm(fp))} {angle})"
            f' (layer "{side}.SilkS") (uuid "{_uuid(part.ref + "ref")}")'
            f" (effects (font (size {text_size} {text_size})"
            f" (thickness {text_stroke}))))"
        )
        out.append(
            f'    (property "Value" "{part.value}"'
            f" (at 0 {f(ref_text_offset_nm(fp))} {angle})"
            f' (layer "{side}.Fab") (uuid "{_uuid(part.ref + "val")}")'
            f" (effects (font (size {text_size} {text_size})"
            f" (thickness {text_stroke}))))"
        )

        cw, ch = fp.courtyard_w_nm, fp.courtyard_h_nm
        cpts = [(-cw, -ch), (cw, -ch), (cw, ch), (-cw, ch)]
        for i in range(4):
            sx, sy = cpts[i]
            ex, ey = cpts[(i + 1) % 4]
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.05) (type solid)) (layer "{side}.CrtYd")'
                f' (uuid "{_uuid(part.ref + f"crt{i}")}"))'
            )

        # The body outline, clipped clear of the pads: stroking the raw body
        # rectangle put ink on every pad the body edge touches (see
        # footprints.silk_segments), and ink on a pad is a solderability
        # defect a fab will clip or flag.
        for i, (sx, sy, ex, ey) in enumerate(silk_segments(fp)):
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.12) (type solid)) (layer "{side}.SilkS")'
                f' (uuid "{_uuid(part.ref + f"silk{i}")}"))'
            )

        # Orientation on the legend itself: the outline is symmetric, so a
        # pin-1 dot (ICs) or cathode bar (diodes) is the only mark the fab
        # and the assembler read. Footprint-local like the pads, so a rotated
        # or bottom-side part carries it round.
        dot = pin1_mark(fp)
        if dot is not None:
            dx, dy, r = dot
            out.append(
                f"    (fp_circle (center {f(dx)} {f(dy)}) (end {f(dx + r)} {f(dy)})"
                f' (stroke (width 0.12) (type solid)) (fill solid)'
                f' (layer "{side}.SilkS") (uuid "{_uuid(part.ref + "pin1")}"))'
            )
        bar = cathode_mark(fp)
        if bar is not None:
            sx, sy, ex, ey = bar
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.12) (type solid)) (layer "{side}.SilkS")'
                f' (uuid "{_uuid(part.ref + "cathode")}"))'
            )

        for pad_index, pad in enumerate(fp.pads):
            idx = net_index.get(pad.net, 0)
            net_decl = f' (net {idx} "{pad.net}")' if idx else ""
            # Seeded by position, not number: a SOT-223 tab shares pin 2's
            # number, and two pads with one uuid is a file KiCad repairs
            # silently on load.
            uuid = _uuid(f"{part.ref}p{pad_index}:{pad.number}")
            if pad.is_tht:
                # A plated hole is on every copper layer, so it is emitted
                # with "*.Cu" rather than the placed side -- a through-hole
                # pin lands on both faces whichever side the module sits on.
                # It gets no F.Paste: a stencil aperture over an open hole
                # drops paste through it instead of forming a joint.
                # Pin 1 is square so the polarity mark survives assembly;
                # every other pin is round, the usual header convention.
                shape = "rect" if pad.number == "1" else "circle"
                out.append(
                    f'    (pad "{pad.number}" thru_hole {shape}'
                    f" (at {f(pad.x_nm)} {f(pad.y_nm)})"
                    f" (size {f(pad.w_nm)} {f(pad.h_nm)})"
                    f" (drill {f(pad.drill_nm)})"
                    f' (layers "*.Cu" "*.Mask"){net_decl}'
                    f' (uuid "{uuid}"))'
                )
            else:
                out.append(
                    f'    (pad "{pad.number}" smd roundrect'
                    f" (at {f(pad.x_nm)} {f(pad.y_nm)})"
                    f" (size {f(pad.w_nm)} {f(pad.h_nm)})"
                    f' (layers "{side}.Cu" "{side}.Paste" "{side}.Mask")'
                    f" (roundrect_rratio 0.25){net_decl}"
                    f' (uuid "{uuid}"))'
                )
        out.append("  )")

    # Copper last, after every footprint, matching KiCad's own ordering.
    # The Y flip here is the same one applied to footprint anchors above: the
    # router works in the placer's Y-up frame and this is the only place its
    # output crosses into KiCad's Y-down frame.
    def flip_y(y_nm: int) -> int:
        return board.height_nm - y_nm

    for index, track in enumerate(board.tracks):
        layer = "B.Cu" if track.layer is Layer.BOTTOM else "F.Cu"
        idx = net_index.get(track.net, 0)
        out.append(
            f"  (segment (start {f(track.start_x_nm)} {f(flip_y(track.start_y_nm))})"
            f" (end {f(track.end_x_nm)} {f(flip_y(track.end_y_nm))})"
            f" (width {f(track.width_nm)}) (layer \"{layer}\") (net {idx})"
            f' (uuid "{_uuid(f"seg{index}:{track.net}")}"))'
        )

    for index, via in enumerate(board.vias):
        idx = net_index.get(via.net, 0)
        out.append(
            f"  (via (at {f(via.x_nm)} {f(flip_y(via.y_nm))})"
            f" (size {f(via.diameter_nm)}) (drill {f(via.drill_nm)})"
            f' (layers "F.Cu" "B.Cu") (net {idx})'
            f' (uuid "{_uuid(f"via{index}:{via.net}")}"))'
        )

    out.append(")")
    return "\n".join(out) + "\n"


def write_board(board: BoardResult, path: str | Path) -> Path:
    """Write the board to ``path`` and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_kicad_pcb(board), encoding="utf-8")
    return path

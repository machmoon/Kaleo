"""KiCad-library 3D models for the land patterns :mod:`footprints` generates.

A ``.kicad_pcb`` carries no Z. The 3D viewer -- and ``kicad-cli pcb export
glb``, which the order step runs -- draws a component only when its footprint
names a model, so a board emitted without one exports as a bare substrate: the
picture a hardware engineer expects at the order step, with nothing on it.

The models are **looked up, not shipped**: every path here is
``${KISYS3DMOD}/<library>.3dshapes/<name>.step``, resolved by KiCad to the
model library it installed itself. ``KISYS3DMOD`` is the legacy alias that
every KiCad version since 5 maps onto its own versioned variable
(``KICAD8_3DMODEL_DIR``, ``KICAD10_3DMODEL_DIR``, ...), so one spelling opens
in whichever KiCad the engineer has -- checked against ``kicad-cli`` 10.0.6:
a board naming ``${KISYS3DMOD}/Resistor_SMD.3dshapes/R_0603_1608Metric.step``
exports 21 KB larger than the same board without it, the part's geometry.

Each entry is a claim that KiCad's library model has the same body and pin
pitch as the land pattern the emitter drew, so the table is deliberately
conservative: a package with no exact counterpart (a 2-pin 1210 crystal, a
44-pin LQFP at 0.5 mm pitch when the library's 10x10 is 0.8, a dual-row
header whose name records no row spacing) maps to ``None`` with the reason,
rather than to a picture of a different part. A
model that looks right and is not is the wrong kind of help. The mapping is
verified against the installed library by a gated test (the ngspice /
ngspice convention) so a renamed library file cannot leave a dangling path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Model3D", "MODELS_VAR", "model_for", "why_unmatched"]

#: The path variable every model path starts with. See the module docstring.
MODELS_VAR = "KISYS3DMOD"


@dataclass(frozen=True)
class Model3D:
    """One library model: where KiCad finds it, what it is, how it is turned.

    ``rotate_deg`` is the Z rotation the footprint's ``(model ...)`` line
    carries. Every library model is drawn for its own library footprint, and
    the generated land patterns share that footprint's frame (pin 1 top-left,
    anticlockwise on screen), so the fixed packages need none. A chip diode
    does: KiCad's ``D_*`` footprints put the cathode on pad 1, this
    pipeline's diode has the anode on pin 1 (the schematic symbol draws it
    so), and the 180 turns the model's band onto pad 2 where the legend's
    cathode bar is. Without it the 3D view shows the stripe on the anode.
    """

    library: str
    name: str
    rotate_deg: int = 0
    #: Where the model sits relative to the land pattern's own origin, in mm,
    #: in KiCad's ``(offset (xyz ...))`` frame (X right, Y **down**, matching
    #: the footprint file).
    #:
    #: Zero for every IC and chip package, because KiCad centres those
    #: footprints on the package and so does this pipeline. It is NOT zero for
    #: a through-hole connector: KiCad anchors those on **pin 1**, while these
    #: land patterns are recentred on the body, so the model is authored about
    #: a different origin. On a 1x10 header that difference is 11.43 mm -- the
    #: plastic renders beside the part instead of on it, and nothing raises,
    #: because DRC does not read models and the GLB draws it happily in the
    #: wrong place.
    offset_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def path(self) -> str:
        return f"${{{MODELS_VAR}}}/{self.library}.3dshapes/{self.name}.step"


#: Imperial chip code -> the metric code KiCad's library names carry.
_CHIP_METRIC: dict[str, str] = {
    "0402": "1005",
    "0603": "1608",
    "0805": "2012",
    "1206": "3216",
    "1210": "3225",
}

#: Reference-designator prefix -> (library, model-name prefix, chip sizes the
#: library has). The land pattern is the same rectangle for all of them
#: (``chip_passive`` names it ``C_<size>``; ``for_passive`` renames it to the
#: type's initial, ``R_0603``); the model differs, so the ref decides, and
#: the name's own letter is accepted but never trusted over the ref. The
#: diode library stops at 0603 and 1206: it has no 0402 or 1210 chip diode,
#: found by the gated library test.
_CHIP_LIBRARIES: dict[str, tuple[str, str, frozenset[str]]] = {
    "R": ("Resistor_SMD", "R", frozenset(_CHIP_METRIC)),
    "C": ("Capacitor_SMD", "C", frozenset(_CHIP_METRIC)),
    "L": ("Inductor_SMD", "L", frozenset(_CHIP_METRIC)),
    "D": ("Diode_SMD", "D", frozenset({"0603", "0805", "1206"})),
}

#: Z rotation per chip library, where the library's pin-1 convention is not
#: this pipeline's. See :class:`Model3D`. Passives are symmetric: nothing to
#: turn.
_CHIP_ROTATE_DEG: dict[str, int] = {"D": 180}

#: Package name (``Footprint.name``) -> model, for the fixed-geometry packages.
#: SOIC entries exist only for the 3.9 mm narrow body ``soic()`` draws; the
#: library's 20-28 pin SOICs are the 7.5 mm wide body, a different part.
#: LQFP entries pair pin count with the body ``board._footprint_for_device``
#: picks (5, 7, 10, 14, 20 mm) at the 0.5 mm pitch ``lqfp()`` draws.
_FIXED: dict[str, Model3D] = {
    "SOT-23": Model3D("Package_TO_SOT_SMD", "SOT-23"),
    "SOT-223-3_TabPin2": Model3D("Package_TO_SOT_SMD", "SOT-223"),
    "SOIC-8": Model3D("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm"),
    "SOIC-14": Model3D("Package_SO", "SOIC-14_3.9x8.7mm_P1.27mm"),
    "SOIC-16": Model3D("Package_SO", "SOIC-16_3.9x9.9mm_P1.27mm"),
    "LQFP-32": Model3D("Package_QFP", "LQFP-32_5x5mm_P0.5mm"),
    "LQFP-48": Model3D("Package_QFP", "LQFP-48_7x7mm_P0.5mm"),
    "LQFP-64": Model3D("Package_QFP", "LQFP-64_10x10mm_P0.5mm"),
    "LQFP-100": Model3D("Package_QFP", "LQFP-100_14x14mm_P0.5mm"),
    "LQFP-144": Model3D("Package_QFP", "LQFP-144_20x20mm_P0.5mm"),
    # The one connector that clears both bars. ``footprints._usb_c_power``
    # draws the GCT USB4125 6-way pad for pad, and -- unlike every
    # through-hole connector in this section -- the library footprint is
    # already anchored on the body, so our origin and its coincide exactly
    # and the model needs no offset the emitter cannot write (see
    # _FRAME_MISMATCH). The land pattern omits the library's two ``SH``
    # shield tabs, which is a soldering difference and not a body one: the
    # real tabs need 0.6 x 1.2 mm oval slots and ``Pad`` carries only a round
    # drill, so a round hole would be a hole the shell does not fit. The
    # shell, body and 0.5 mm pin pitch the model draws are this part's.
    "USB_C_Receptacle_Power": Model3D(
        "Connector_USB", "USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal"
    ),
    # The second connector-family entry that clears both bars, and for the
    # same two reasons. ``footprints._switch_tl3305a`` draws
    # ``Button_Switch_SMD.pretty/SW_SPST_TL3305A.kicad_mod`` pad for pad, and
    # that library footprint is body-anchored (its four pads are symmetric
    # about the origin), so our origin and its coincide and no offset is
    # needed. The A of TL3305A is the model as well as the land pattern: A, B
    # and C share the pattern exactly and differ only in actuator height
    # (3.8 / 5.0 / 7.0 mm, measured off these very models), so naming the
    # wrong one here would draw a button of the wrong height on a board whose
    # case was sized for another.
    "SW_SPST_TL3305A": Model3D("Button_Switch_SMD", "SW_SPST_TL3305A"),
}

#: The reason the remaining through-hole entries in this table answer None,
#: said once. ``footprints.py`` recentres each land pattern onto the middle of
#: the library part's ``F.Fab`` body -- this pipeline's footprint frame is
#: package-centred -- while KiCad anchors its through-hole connectors on pin
#: 1. The model is authored about the *library* origin, so the body lands that
#: translation away from the pads unless something carries it: measured,
#: 11.43 mm for a 1x10 header, which puts the plastic beside the part rather
#: than on it.
#:
#: ``Model3D.offset_mm`` carries that translation now and
#: ``board.emit_kicad_pcb`` writes it, which is how the single-row headers and
#: the JST PH became mappings. What still separates those two from these is
#: **the sign, and the fact that it was checked by eye**: ``(offset (xyz ...))``
#: is Y-up while the footprint file is Y-down, the two candidates differ by
#: twice the offset, and neither DRC nor any test in this repo can tell them
#: apart -- the header's sign was settled by rendering both. Nobody has
#: rendered these, so the offset would be a number nobody has seen land on the
#: pads, and an unverified offset is a picture in the wrong place, which is
#: the same lie as a picture of the wrong part. Recorded in ``TODO.txt``.
_FRAME_MISMATCH = (
    "the pattern is recentred on the body while KiCad anchors this part on "
    "pin 1, and the {} mm offset that would correct it has not been verified "
    "by render, so the body could land twice that far from the pads instead"
)

#: Packages the emitter can draw that the library has no honest match for.
_UNMATCHED: dict[str, str] = {
    "LQFP-44": "KiCad's LQFP-44 model is the 10x10 mm 0.8 mm-pitch body; "
    "this land pattern is 0.5 mm pitch",
    "SOIC-4": "KiCad's library has no SOIC-4 model",
    "SOIC-6": "KiCad's library has no SOIC-6 model",
    "SOIC-10": "KiCad's library has no SOIC-10 model",
    "SOIC-20": "KiCad's SOIC-20 model is the 7.5 mm wide body; this land "
    "pattern is the 3.9 mm narrow body",
    "SOIC-24": "KiCad's SOIC-24 model is the 7.5 mm wide body; this land "
    "pattern is the 3.9 mm narrow body",
    "SOIC-28": "KiCad's SOIC-28 model is the 7.5 mm wide body; this land "
    "pattern is the 3.9 mm narrow body",
    # Connectors, power entry and batteries. Two distinct reasons: the library
    # has no model of the part the pattern was drawn from, or it has one and
    # the two frames do not share an origin (_FRAME_MISMATCH).
    "Barrel_Jack_5.5x2.1mm": "this pattern is the CUI PJ-102AH and KiCad "
    "ships no PJ-102AH model; its generic BarrelJack_Horizontal is a "
    "different jack whose three pads do not correspond to these under any "
    "translation",
    "TerminalBlock_2P_5.08mm": "KiCad's Phoenix MKDS-1,5-2-5.08 model is this "
    "part, but " + _FRAME_MISMATCH.format("(2.54, -0.3)"),
    "BatteryHolder_CR2032": "this pattern is the Keystone 3002 and KiCad "
    "ships no 3002 model; the coin-cell holders it does model (Keystone "
    "1058/1060, Multicomp BC-2001, Renata SMTU2032) are different bodies "
    "with different contacts",
    "BatteryHolder_AAA_1x": "KiCad's Keystone 2466 model is this part, but "
    + _FRAME_MISMATCH.format("(22.35, 0)"),
    # KiCad's TestPoint.pretty/TestPoint_Pad_1.5x1.5mm.kicad_mod carries no
    # (model ...) line at all, and TestPoint.3dshapes has no Pad_* model to
    # name: the library's test *pads* are bare copper with nothing above the
    # board, unlike its Keystone posts and loops. So there is nothing to claim
    # here, and nothing missing from the render either -- a flat pad looks
    # like a flat pad.
    "TestPoint_Pad_1.5x1.5mm": "KiCad's TestPoint_Pad_1.5x1.5mm footprint "
    "names no 3D model and its library ships none: a bare test pad has no "
    "body above the board to draw",
}

#: The single-row headers and JST sizes `footprints.py` draws, and therefore
#: the only ones whose library model this can claim. A size outside these has
#: no land pattern here, so a model for it would be a claim about nothing.
_SINGLE_HEADER_PINS = frozenset({2, 3, 4, 5, 6, 8, 10})
_JST_PH_PINS = frozenset({2, 3, 4})

_CHIP_NAME = re.compile(r"^[RCLDIY]_(\d{4})$")
_HEADER_NAME = re.compile(r"^PinHeader_2x(\d+)_P2\.54mm$")
_SINGLE_HEADER_NAME = re.compile(r"^PinHeader_1x(\d+)_P2\.54mm$")
_JST_PH_NAME = re.compile(r"^JST_PH_(\d+)P$")


def _ref_prefix(ref: str) -> str:
    return re.match(r"^[A-Za-z]*", ref).group(0).upper()


def model_for(footprint_name: str, ref: str) -> Model3D | None:
    """The library model for a generated land pattern, or None.

    ``footprint_name`` is :attr:`~silkscreen.footprints.Footprint.name`;
    ``ref`` is the reference designator, whose prefix tells a resistor from a
    capacitor on the chip rectangle they share. None means the library has
    no model this pattern can honestly claim -- :func:`why_unmatched` says
    why -- never that the lookup was skipped.
    """
    chip = _CHIP_NAME.match(footprint_name)
    if chip:
        size = chip.group(1)
        metric = _CHIP_METRIC.get(size)
        entry = _CHIP_LIBRARIES.get(_ref_prefix(ref))
        if metric is None or entry is None or size not in entry[2]:
            return None
        library, prefix, _ = entry
        return Model3D(
            library,
            f"{prefix}_{size}_{metric}Metric",
            _CHIP_ROTATE_DEG.get(_ref_prefix(ref), 0),
        )
    # Through-hole connectors: the library has the part, and the two frames
    # differ by a pure translation that `Model3D.offset_mm` now carries. The
    # translation is derived from the geometry rather than tabulated, because
    # it is a property of where each frame's origin sits: KiCad anchors these
    # on pin 1, this pipeline centres them on the body.
    #
    # The Y sign is the trap. `(offset (xyz ...))` is Y-**up** while the
    # footprint file is Y-**down**, so the offset is the negation of the
    # footprint-frame translation. Settled by rendering both signs on a
    # 1x10 header: +11.43 lands the plastic exactly on the ten pads,
    # -11.43 slides it 22.86 mm off the part and past the board edge.
    single = _SINGLE_HEADER_NAME.match(footprint_name)
    if single:
        pins = int(single.group(1))
        if pins in _SINGLE_HEADER_PINS:
            return Model3D(
                "Connector_PinHeader_2.54mm",
                f"PinHeader_1x{pins:02d}_P2.54mm_Vertical",
                offset_mm=(0.0, (pins - 1) * 1.27, 0.0),
            )
        return None
    jst = _JST_PH_NAME.match(footprint_name)
    if jst:
        pins = int(jst.group(1))
        if pins in _JST_PH_PINS:
            return Model3D(
                "Connector_JST",
                f"JST_PH_S{pins}B-PH-K_1x{pins:02d}_P2.00mm_Horizontal",
                # X grows with the pin count because the row centre moves and
                # pin 1 does not; Y is the row offset, negated as above.
                offset_mm=(-(pins - 1) * 1.0, 2.45, 0.0),
            )
        return None
    return _FIXED.get(footprint_name)


def why_unmatched(footprint_name: str, ref: str) -> str:
    """The reason :func:`model_for` answered None, for a BOM row to carry."""
    chip = _CHIP_NAME.match(footprint_name)
    if chip:
        prefix = _ref_prefix(ref)
        if prefix == "Y":
            return (
                "KiCad's library has no 2-pin chip crystal model; the "
                f"{chip.group(1)} rectangle stands in for one"
            )
        if prefix not in _CHIP_LIBRARIES:
            return f"no chip model library for a {prefix!r} reference"
        library = _CHIP_LIBRARIES[prefix][0]
        return f"KiCad's {library} library has no {chip.group(1)} chip model"
    header = _HEADER_NAME.match(footprint_name)
    if header:
        # This entry used to claim PinHeader_2xNN_P2.54mm_Vertical and was
        # wrong on both axes of the geometry it is supposed to certify:
        # KiCad's part is a 2.54 mm-wide double row numbered alternately
        # across the two rows (1 left, 2 right, 3 left ...), while
        # ``dual_row_header`` straddles a module at 15.24 mm by default and
        # numbers 1..n/2 down one column then back up the other. The name
        # records the pitch and never the row spacing, so no name in this
        # family can pin a library body -- the honest answer is None.
        return (
            f"KiCad's PinHeader_2x{int(header.group(1)):02d}_P2.54mm_Vertical is "
            "a 2.54 mm row spacing numbered alternately across the rows; this "
            "pattern straddles a module (15.24 mm by default) and numbers down "
            "one column and back up the other, and its name records no row "
            "spacing to match on"
        )
    # The single-row headers and the JST PH are *mapped* -- `Model3D.offset_mm`
    # carries the pin-1-versus-body frame difference and the emitter writes it
    # -- for every size `footprints.py` draws. So reaching here with one of
    # these names means the size itself is not drawn, and the reason is that
    # and not a frame at all. Saying "the library has the model but the frames
    # disagree" would have been the old answer and is now simply untrue.
    single = _SINGLE_HEADER_NAME.match(footprint_name)
    if single:
        pins = int(single.group(1))
        return (
            f"no 1x{pins:02d} 2.54 mm header land pattern is drawn here "
            f"(drawn: {', '.join(f'1x{n:02d}' for n in sorted(_SINGLE_HEADER_PINS))}), "
            "so a model for it would be a claim about a pattern that does "
            "not exist"
        )
    jst = _JST_PH_NAME.match(footprint_name)
    if jst:
        pins = int(jst.group(1))
        return (
            f"no {pins}-way JST PH land pattern is drawn here "
            f"(drawn: {', '.join(f'{n}P' for n in sorted(_JST_PH_PINS))}), "
            "so a model for it would be a claim about a pattern that does "
            "not exist"
        )
    reason = _UNMATCHED.get(footprint_name)
    if reason:
        return reason
    return f"no KiCad library model is mapped for {footprint_name}"

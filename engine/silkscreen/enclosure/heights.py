"""Component heights for enclosure cavity sizing.

A ``.kicad_pcb`` carries no Z at all, so the third dimension has to come from
somewhere else. This table maps *footprint classes* (substrings of the
footprint's library id, e.g. ``LQFP`` or ``C_0805``) to nominal maximum body
heights in **integer nanometres**. The values are deliberately conservative --
a cavity slightly taller than the part is a working enclosure, one slightly
shorter is scrap plastic.

A footprint whose name matches no class gets :data:`DEFAULT_HEIGHT_NM` and the
lookup says so (``was_default=True``), which :mod:`.board_shape` records on the
part and :mod:`.verify` surfaces as a warning in the fit report. A silent
guess would be a quiet zero in disguise (plan decision 9).
"""

from __future__ import annotations

from ..units import mm

__all__ = ["DEFAULT_HEIGHT_NM", "HEIGHTS_NM", "height_for"]

#: Height used when no class matches. Tall enough for most SMD parts, and it
#: always arrives paired with ``was_default=True`` so the caller can warn.
DEFAULT_HEIGHT_NM: int = mm(3.0)

#: Footprint class -> nominal maximum height. Keys are matched
#: case-insensitively as substrings of the footprint's library id; when
#: several keys match, the longest one wins (``SOT-223`` beats ``SOT-2``),
#: with the alphabetically first breaking any remaining tie so the lookup is
#: deterministic.
HEIGHTS_NM: dict[str, int] = {
    # Nothing sticks up out of a hole; the case puts a standoff under it.
    "MountingHole": 0,
    "Fiducial": 0,
    "TestPoint": 0,
    # IC packages.
    "LQFP": mm(1.6),
    "TQFP": mm(1.2),
    "QFN": mm(1.0),
    "DFN": mm(0.8),
    "WSON": mm(0.8),
    "SOIC": mm(1.75),
    "SSOP": mm(2.0),
    "TSSOP": mm(1.2),
    "SOT-23": mm(1.45),
    "SOT-223": mm(1.8),
    "SOT-89": mm(1.6),
    "TO-252": mm(2.4),
    "TO-263": mm(4.6),
    # Chip passives (body height per EIA size).
    "C_0402": mm(0.6),
    "C_0603": mm(0.9),
    "C_0805": mm(1.4),
    "C_1206": mm(1.8),
    "R_0402": mm(0.4),
    "R_0603": mm(0.55),
    "R_0805": mm(0.7),
    "R_1206": mm(0.7),
    "L_0805": mm(1.1),
    "L_1206": mm(1.4),
    # Discretes and misc.
    "SOD-123": mm(1.35),
    "SOD-323": mm(1.1),
    "LED_0603": mm(0.8),
    "LED_0805": mm(1.1),
    "Crystal_SMD": mm(1.3),
    # Things that poke up. Connectors are the usual cutout candidates, so
    # their heights matter most.
    "PinHeader": mm(8.5),
    "PinSocket": mm(8.5),
    "USB_C": mm(3.2),
    "USB_Micro": mm(2.9),
    "Barrel_Jack": mm(11.0),
    # SMD tactile switch. ``footprints._switch_tl3305a`` draws the E-Switch
    # TL3305**A** and nothing else, and KiCad's own models measure the
    # actuator at z -0.05..3.8 for the A, ..5.0 for the B and ..7.0 for the C
    # -- the same measurement that produced the JST and terminal-block rows
    # below. This key held 3.5 mm, which is 0.3 mm *under* the only switch
    # this pipeline can put on a board: a lid sized to it rests on the button
    # and holds it down, the one direction this table refuses to err in. The
    # generic key carries the A's number because the A is the only part it
    # can describe; a B or a C would need its own key longer than this one,
    # since the lookup takes the longest matching key.
    "SW_SPST": mm(3.8),
    # Wire-to-board connectors and power entry. Each number is the part body's
    # height above the board, cross-checked against the Z extent of KiCad's own
    # 3D model for the part named (the model sits with the board plane at
    # z = 0, so its maximum Z *is* this dimension) -- the only measurement of
    # these parts available offline, and the one the case is built around.
    #
    # JST eS-PH B<n>B-PH-K top-entry header: 6.0 mm on JST's drawing, and
    # JST_PH_B2B-PH-K_1x02_P2.00mm_Vertical.step spans z -3.434 .. 6.000.
    "JST_PH": mm(6.0),
    # JST XH B<n>B-XH-A: JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical.step spans
    # z -3.482 .. 7.000. PLUG_ENVELOPES has known this class since v2 and this
    # table did not, so an XH on a board took DEFAULT_HEIGHT_NM (3.0 mm) and
    # the lid closed 4 mm into it.
    "JST_XH": mm(7.0),
    # 5.08 mm screw terminal. The package name pins no vendor, so this takes
    # the tallest 5.08 mm two-position body KiCad ships:
    # TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal.step spans
    # z -3.500 .. 13.800. A generic KF301-style block is nearer 10 mm, and
    # erring tall is the direction this table errs on purpose: too tall is a
    # working case, too short is scrap plastic and a crushed part.
    "TerminalBlock": mm(13.8),
    # CR2032 coin-cell holders: BatteryHolder_Keystone_1058_1x2032.step and
    # _1060_ both span z 0 .. 5.180 (Keystone's own 5.2 mm). Two spellings
    # because two vocabularies name the same cell: this pipeline's
    # BatteryHolder_CR2032 and KiCad's library ids, which say 1x2032. A bare
    # "2032" key would also swallow any vendor part number containing it.
    "CR2032": mm(5.2),
    "1x2032": mm(5.2),
    # 1xAAA holder: BatteryHolder_Keystone_2466_1xAAA.step reaches z 11.100,
    # Keystone's 0.437 in. The 2xAAA and 3xAAA holders are the same cell lying
    # the same way up, so the one key is right for all of them.
    "AAA": mm(11.1),
}


def height_for(
    footprint_name: str, table: dict[str, int] | None = None
) -> tuple[int, bool]:
    """Height for a footprint name, and whether it fell back to the default.

    ``footprint_name`` is the footprint's library id (``Package_QFP:LQFP-48``)
    or any string containing the class token. ``table`` substitutes the whole
    class table (used by :func:`.board_shape.board_envelope` when a caller
    supplies overrides); ``None`` means :data:`HEIGHTS_NM`.
    """
    entries = HEIGHTS_NM if table is None else table
    haystack = footprint_name.lower()
    # Longest key first so the most specific class wins; alphabetical second
    # so ties cannot depend on dict insertion order.
    for key in sorted(entries, key=lambda k: (-len(k), k)):
        if key.lower() in haystack:
            return entries[key], False
    return DEFAULT_HEIGHT_NM, True

"""Mechanical design rules for a 3D-printed PCB enclosure.

This is the knowledge a mechanical engineer brings to the case that the
``.kicad_pcb`` file cannot: how thick a wall prints, how big a hole an M3
heat-set insert wants, how much larger than the receptacle a USB-C *plug*
is. Every number here is sourced from the generators the community has
converged on (YAPP_Box v3, kicad2freecad-enclosures, turbocase, Ultimate Box
Maker, NopSCADlib ``inserts.scad``), the USB-IF connector specification, and
the Covestro/Bayer snap-fit design guide -- see ``docs/ai-cad-plan.md`` v2
for the citations. The model never chooses any of these; it chooses *style*
(which lid, which insert size, which parts get openings) and the emitter
looks the numbers up here.

Integer nanometres throughout, the ``units.py`` rule. A value in mm appears
only inside a ``mm(...)`` call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..units import DEFAULT_GRID_NM, mm

__all__ = [
    "FIT_PRESS_NM",
    "FIT_SLIDE_NM",
    "FIT_LOOSE_NM",
    "HOLE_COMPENSATION_NM",
    "ELEPHANT_FOOT_NM",
    "THIN_MARGIN_NM",
    "MATERIALS",
    "Material",
    "INSERTS",
    "INSERTS_SHORT",
    "Insert",
    "SELF_TAP_UNDERSIZE_NM",
    "STANDOFF_HEIGHT_NM",
    "STANDOFF_OD_NM",
    "STANDOFF_MIN_FLOOR_NM",
    "BOSS_MIN_WALL_NM",
    "LOCATING_PIN_NM",
    "LOCATING_PIN_SLACK_NM",
    "LIP_WIDTH_NM",
    "LIP_DEPTH_NM",
    "LIP_SLACK_NM",
    "LIP_VERTICAL_GAP_NM",
    "SCREW_HEAD_POCKET_NM",
    "SCREW_HEAD_DEPTH_NM",
    "SNAP_BEAD_NM",
    "SNAP_GAP_NM",
    "SNAP_CATCH_NM",
    "SNAP_CATCH_HEIGHT_NM",
    "SNAP_HOOK_WIDTH_NM",
    "SNAP_HOOK_SLOT_NM",
    "snap_arm_length_nm",
    "snap_strain_ppm",
    "VENT_SLOT_NM",
    "VENT_PITCH_NM",
    "VENT_BOSS_KEEPOUT_NM",
    "VENT_MAX_BRIDGE_NM",
    "DEFAULT_CORNER_RADIUS_NM",
    "MIN_CORNER_RADIUS_NM",
    "HEADROOM_NM",
    "CUTOUT_MARGIN_NM",
    "CUTOUT_CHAMFER_NM",
    "PlugEnvelope",
    "PLUG_ENVELOPES",
    "connector_class",
    "plug_envelope",
    "MOUNTING_HOLE_MIN_DRILL_NM",
    "OVERHANG_LIMIT_DEG",
]

# --- FDM fits (per side) -----------------------------------------------------
#: Press fit: parts that should not move once assembled.
FIT_PRESS_NM: int = mm(0.1)
#: Sliding / registration fit: the lid lip in the cavity. Every surveyed
#: generator uses this number (YAPP ridgeSlack, turbocase, k2f fit_clearance).
FIT_SLIDE_NM: int = mm(0.2)
#: Loose fit: something that must never bind (a connector opening's margin).
FIT_LOOSE_NM: int = mm(0.4)
#: FDM holes print undersize; add this to every bore diameter.
HOLE_COMPENSATION_NM: int = mm(0.3)
#: 45-degree chamfer on every bed-facing mating edge so first-layer flare does
#: not eat the sliding fit on both halves.
ELEPHANT_FOOT_NM: int = mm(0.5)
#: Band around zero in which a *passing* kernel clause is only nominally
#: passing. A clause that clears by less than one slide-fit allowance has
#: cleared by less than the printer's own process tolerance -- the same
#: 0.2 mm every surveyed generator budgets for a part actually coming off the
#: bed -- so the next revision, or the next spool, turns it into a failure.
#: It is a warning, not a pass, and the receipt says so.
THIN_MARGIN_NM: int = FIT_SLIDE_NM


@dataclass(frozen=True)
class Material:
    name: str
    shrinkage_ppm: int  # linear shrinkage, parts per million
    #: Extra per-side clearance on warm-running fits (PETG/ABS creep).
    fit_extra_nm: int
    #: Permissible short-term strain for a snap arm, in ppm (1 % = 10_000).
    snap_strain_ppm: int


#: Keyed by the name the model may put in ``"material"``.
MATERIALS: dict[str, Material] = {
    "PLA": Material("PLA", 3_000, fit_extra_nm=0, snap_strain_ppm=8_000),
    "PETG": Material("PETG", 5_000, fit_extra_nm=mm(0.05), snap_strain_ppm=15_000),
    "ABS": Material("ABS", 9_000, fit_extra_nm=mm(0.1), snap_strain_ppm=20_000),
}


# --- Threaded hardware ---------------------------------------------------------
@dataclass(frozen=True)
class Insert:
    """One heat-set insert size (NopSCADlib ``inserts.scad`` numbers).

    ``bore_nm`` is the hole NopSCADlib prints for the insert -- its ``h``
    column -- and is already the as-printed size: the knurl needs the
    0.3 mm of plastic that :data:`HOLE_COMPENSATION_NM` would remove, so an
    insert bore never gets the compensation a clearance hole gets.
    """

    name: str
    thread_nm: int        # nominal screw diameter
    od_nm: int            # insert outer diameter
    bore_nm: int          # pilot bore the insert is pressed into (as printed)
    length_nm: int        # insert length; bore depth is length + 1 mm
    clearance_nm: int     # through-hole for the screw in the mating part
    head_nm: int          # socket/pan head diameter for a head pocket


#: The standard-length rows (NopSCADlib ``F1BM*``): what a lid boss running
#: the full case height takes.
INSERTS: dict[str, Insert] = {
    "M2": Insert("M2", mm(2.0), mm(3.6), mm(3.2), mm(4.0), mm(2.4), mm(3.8)),
    "M2.5": Insert("M2.5", mm(2.5), mm(4.6), mm(4.0), mm(5.8), mm(2.9), mm(4.5)),
    "M3": Insert("M3", mm(3.0), mm(4.6), mm(4.0), mm(5.8), mm(3.4), mm(5.5)),
    "M4": Insert("M4", mm(4.0), mm(6.3), mm(5.6), mm(8.2), mm(4.5), mm(7.0)),
}

#: The short rows a board standoff takes (NopSCADlib ``CNCKM2p5`` 4.0,
#: ``CNCKM3`` 3.0, ``CNCKM4`` 4.0 mm long, same OD and hole as the ``F1BM``
#: row of that thread; there is no short M2, ``F1BM2`` is already 4.0 mm).
#: A 5.8 mm insert in a 4 mm standoff stands proud and the board sits on
#: the brass, not the boss; the short row is what the standoff is sized for.
INSERTS_SHORT: dict[str, Insert] = {
    "M2": INSERTS["M2"],
    "M2.5": Insert("M2.5", mm(2.5), mm(4.6), mm(4.0), mm(4.0), mm(2.9), mm(4.5)),
    "M3": Insert("M3", mm(3.0), mm(4.6), mm(4.0), mm(3.0), mm(3.4), mm(5.5)),
    "M4": Insert("M4", mm(4.0), mm(6.3), mm(5.6), mm(4.0), mm(4.5), mm(7.0)),
}

#: A self-tapping screw wants a hole this much *smaller* than its thread.
SELF_TAP_UNDERSIZE_NM: int = mm(0.3)


# --- Standoffs and bosses -------------------------------------------------------
#: Board-to-floor gap: clears through-hole leads and solder fillets (k2f 4.0,
#: turbocase 5.0, YAPP 5.0). Was 2.0 in v1, which put THT leads on the floor.
STANDOFF_HEIGHT_NM: int = mm(4.0)
#: Boss outer diameter for an M3 insert: two times the insert OD is the rule
#: of thumb; YAPP/k2f both ship 7. A minimum, not the number: a larger
#: insert grows the boss to keep :data:`BOSS_MIN_WALL_NM` around its bore.
STANDOFF_OD_NM: int = mm(7.0)
#: Floor that must remain under a blind insert bore. A standoff shorter than
#: the bore plus this floor is raised, never the bore shortened: a shortened
#: bore leaves the insert proud and the board on the brass.
STANDOFF_MIN_FLOOR_NM: int = mm(1.2)
#: Plastic around an insert bore: the printable wall plus the loose fit the
#: knurl displaces as it is pressed (1.2 + 0.4). An M4 insert (6.3 mm OD) in
#: a 7 mm boss leaves 0.35 mm and splits it.
BOSS_MIN_WALL_NM: int = mm(1.2) + FIT_LOOSE_NM
#: Locating pin for boards held by the lid instead of screws (YAPP 2.4).
LOCATING_PIN_NM: int = mm(2.4)
LOCATING_PIN_SLACK_NM: int = mm(0.4)


# --- Lid registration -----------------------------------------------------------
#: The lip is a *ring*, not a plug: this wide, running just inside the wall.
LIP_WIDTH_NM: int = mm(1.2)
#: How far the ring reaches into the cavity (yawor 2.4 .. YAPP 5.0).
LIP_DEPTH_NM: int = mm(3.0)
#: Sliding slack per side between lip and cavity wall.
LIP_SLACK_NM: int = FIT_SLIDE_NM
#: Vertical gap under the ring so the seam closes on the walls, not the ridge
#: (YAPP ``ridgeGap``).
LIP_VERTICAL_GAP_NM: int = mm(0.5)
#: Screw-lid head pocket (k2f: 6.0 dia x 2.0 deep for M3).
SCREW_HEAD_POCKET_NM: int = mm(6.0)
SCREW_HEAD_DEPTH_NM: int = mm(2.0)
#: Snap lid. Not a press-fit ring: a closed 1.2 mm ring cannot flex 0.2 mm
#: without ~4 % strain in PLA (allowable 0.8 %), so the snap is discrete
#: **cantilever hooks** -- segments of the lip ring freed by a slot on each
#: side, hanging from the plate, with a bead at the tip that latches under a
#: catch ledge on the cavity wall. The bead is :data:`SNAP_BEAD_NM` tall
#: (k2f), the ledge protrudes :data:`SNAP_CATCH_NM` into the cavity and is
#: :data:`SNAP_CATCH_HEIGHT_NM` tall; the hook's engagement, and therefore
#: the tip deflection at assembly, is the catch minus the lip slack.
#: :data:`SNAP_GAP_NM` is the vertical play under the ledge.
SNAP_BEAD_NM: int = mm(0.8)
SNAP_GAP_NM: int = mm(0.2)
SNAP_CATCH_NM: int = mm(0.4)
SNAP_CATCH_HEIGHT_NM: int = mm(1.5)
#: Hook width along the wall, and the slot on each side that frees it.
SNAP_HOOK_WIDTH_NM: int = mm(8.0)
SNAP_HOOK_SLOT_NM: int = mm(1.0)


def snap_strain_ppm(thickness_nm: int, deflection_nm: int, length_nm: int) -> int:
    """Tip strain of a constant-section cantilever, in ppm.

    Bayer/Covestro *Snap-Fit Joints for Plastics*, rectangular beam of
    constant cross-section: ``epsilon = 1.5 * t * y / L**2`` with ``t`` the
    beam thickness in the bending direction, ``y`` the tip deflection and
    ``L`` the beam length. Integer arithmetic; ``length_nm`` must be > 0.
    """
    if length_nm <= 0:
        raise ValueError("snap arm length must be positive")
    return (3 * thickness_nm * deflection_nm * 1_000_000) // (2 * length_nm * length_nm)


def snap_arm_length_nm(
    thickness_nm: int, deflection_nm: int, material: Material
) -> int:
    """The shortest arm that keeps :func:`snap_strain_ppm` within
    ``material.snap_strain_ppm``: ``L = sqrt(1.5 * t * y / epsilon)``,
    rounded up to the placement grid (:data:`~silkscreen.units.DEFAULT_GRID_NM`)."""
    if thickness_nm <= 0 or deflection_nm <= 0:
        raise ValueError("snap arm thickness and deflection must be positive")
    allowed = material.snap_strain_ppm
    need = (3 * thickness_nm * deflection_nm * 1_000_000) / (2 * allowed)
    length = int(math.ceil(math.sqrt(need)))
    length = -(-length // DEFAULT_GRID_NM) * DEFAULT_GRID_NM
    while snap_strain_ppm(thickness_nm, deflection_nm, length) > allowed:
        length += DEFAULT_GRID_NM
    return length


# --- Vents --------------------------------------------------------------------------
#: Slot width the nozzle can actually leave open (UBM ``Vent_width``).
VENT_SLOT_NM: int = mm(1.5)
#: Centre-to-centre slot pitch (slot + one wall of plastic).
VENT_PITCH_NM: int = mm(3.5)
#: No vent within this distance of a boss or standoff (k2f).
VENT_BOSS_KEEPOUT_NM: int = mm(2.0)
#: Longest unsupported bridge a wall slot may create (k2f ``MAX_BRIDGE``).
VENT_MAX_BRIDGE_NM: int = mm(8.0)


# --- Shell ----------------------------------------------------------------------------
#: Outer vertical-edge radius when the model does not choose one (k2f 3.0).
DEFAULT_CORNER_RADIUS_NM: int = mm(3.0)
#: Below this a fillet is silkscreen, not a feature; the emitter rounds up.
MIN_CORNER_RADIUS_NM: int = mm(0.5)
#: Air above the tallest top-side part (k2f ``top_clearance`` is 3.0; the
#: v1 emitter reused the 1.0 mm board clearance and lids grazed parts).
HEADROOM_NM: int = mm(3.0)
#: Opening margin per side around a *plug* envelope when the model gives none.
CUTOUT_MARGIN_NM: int = mm(0.5)
#: Inside chamfer that guides a plug into its opening.
CUTOUT_CHAMFER_NM: int = mm(0.6)
#: A pad drilled at least this wide, with no net, is a mounting hole.
MOUNTING_HOLE_MIN_DRILL_NM: int = mm(2.0)
#: Faces steeper than this from horizontal print without support.
OVERHANG_LIMIT_DEG: int = 45


# --- Connector plug envelopes ---------------------------------------------------
@dataclass(frozen=True)
class PlugEnvelope:
    """The *mating* plug's cross-section, which is what a wall opening must pass.

    A receptacle's courtyard is the wrong thing to cut for: a USB-C receptacle
    is 8.94 x 3.2 mm, its plug overmold is up to 12.35 x 6.5 mm (USB-IF
    Type-C R2.0). ``width_nm`` runs along the wall, ``height_nm`` is vertical,
    both already include the loose-fit margin so an opening equal to the
    envelope admits the plug.

    The plug is **centred on the receptacle**: a mating plug's axis is the
    receptacle's axis, so the opening is centred on the part's mid-height
    (board top plus half the part's height) and ``axis_offset_nm`` shifts
    that axis for the few receptacles whose bore is not at mid-body. A
    6.5 mm USB-C overmold on a 3.2 mm top-mount receptacle therefore spans
    about -1.65 .. +4.85 mm from the board top -- an opening that started
    *at* the board top put the wall in the way of the overmold's lower half
    and made the case 2 mm taller than the plug needs.
    """

    name: str
    width_nm: int
    height_nm: int
    axis_offset_nm: int = 0
    #: True when the opening should be a circle of ``width_nm`` diameter.
    round: bool = False


#: Footprint-class substring -> plug envelope. Matched like ``heights.py``:
#: case-insensitive substring of the library id, longest key wins.
PLUG_ENVELOPES: dict[str, PlugEnvelope] = {
    "USB_C": PlugEnvelope("USB-C plug", mm(13.0), mm(7.0)),
    "USB_Micro": PlugEnvelope("USB micro-B plug", mm(11.5), mm(7.5)),
    "USB_Mini": PlugEnvelope("USB mini-B plug", mm(12.0), mm(8.0)),
    "USB_A": PlugEnvelope("USB-A plug", mm(16.0), mm(9.0)),
    "Barrel_Jack": PlugEnvelope("barrel plug", mm(11.0), mm(11.0), round=True),
    # KiCad names the library Connector_BarrelJack:BarrelJack_*, no underscore.
    "BarrelJack": PlugEnvelope("barrel plug", mm(11.0), mm(11.0), round=True),
    "Jack_3.5mm": PlugEnvelope("3.5 mm plug", mm(8.0), mm(8.0), round=True),
    "RJ45": PlugEnvelope("RJ45 plug", mm(15.0), mm(14.0)),
    "JST_XH": PlugEnvelope("JST-XH housing", mm(0), mm(11.0)),   # width from courtyard
    "JST_PH": PlugEnvelope("JST-PH housing", mm(0), mm(8.0)),
    # A screw terminal has no mating plug: what enters it is bare wire, and
    # what has to reach it is a screwdriver. So there is no plug cross-section
    # to look up, and both axes are the zero that means "the part's own extent
    # plus the loose-fit margin" -- an opening the size of the block's own
    # wire-entry face, which admits the wires and the driver together. Cutting
    # for an invented plug body would be the same lie as cutting a receptacle
    # courtyard for a USB-C overmold, in the other direction.
    "TerminalBlock": PlugEnvelope("screw terminal wire entry", mm(0), mm(0)),
    "PinHeader": PlugEnvelope("header shroud", mm(0), mm(10.0)),
    "PinSocket": PlugEnvelope("socket", mm(0), mm(10.0)),
    "SW_": PlugEnvelope("button", mm(0), mm(0)),                  # sized from courtyard
    "LED": PlugEnvelope("light pipe", mm(0), mm(0)),
    "microSD": PlugEnvelope("microSD card", mm(13.0), mm(3.0)),
    "SD_Card": PlugEnvelope("SD card", mm(26.0), mm(4.0)),
}


def connector_class(lib_id: str) -> str | None:
    """The ``PLUG_ENVELOPES`` key matching a footprint library id, or None.

    Longest match wins, alphabetical tiebreak -- the ``heights.py`` lookup
    rule, so ``USB_Micro`` beats ``USB_``.
    """
    name = lib_id.lower()
    hits = [k for k in PLUG_ENVELOPES if k.lower() in name]
    if not hits:
        return None
    hits.sort(key=lambda k: (-len(k), k))
    return hits[0]


def plug_envelope(
    cls: str | None, courtyard_width_nm: int, part_height_nm: int
) -> PlugEnvelope:
    """The opening a part of class ``cls`` needs, filled in from the courtyard.

    A zero ``width_nm``/``height_nm`` in the table means "the part's own
    extent plus the loose-fit margin"; a class of ``None`` (not a connector)
    gets the courtyard plus margin on both axes, which is the v1 behaviour and
    still right for a display window or a button.
    """
    margin = 2 * CUTOUT_MARGIN_NM
    base = PLUG_ENVELOPES.get(cls or "", PlugEnvelope("opening", 0, 0))
    width = base.width_nm or courtyard_width_nm + margin
    height = base.height_nm or part_height_nm + margin
    return PlugEnvelope(base.name, width, height, base.axis_offset_nm, base.round)

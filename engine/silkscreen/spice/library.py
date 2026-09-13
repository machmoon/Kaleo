"""The subcircuit models this repo is allowed to ship, and what each one is.

Every model here was **written for this repository from published circuit
topology**, and every one of them is a *generic behavioural stand-in*: an
op-amp that behaves roughly like an op-amp, a timer that behaves roughly like
a 555. Not one of them is a manufacturer model, and that distinction is
carried on the model itself (:attr:`ModelProvenance.generic`) rather than
mentioned in a comment, because "the LM358 model" and "a generic op-amp that
behaves roughly like one" support completely different claims.

**Why nothing is vendored.** The obvious move -- copy the manufacturer's own
``.lib`` -- is not available. Texas Instruments' own licence file
(``https://dev.ti.com/gallery/assets/TI_Text_File_License.txt``, "TI TEXT FILE
LICENSE") permits source redistribution only if derivative works are "licensed
by TI for use only with TI Devices"; the older SPICE macro-model agreements in
TI application notes sloa070/sloa071 go further and grant no right to "sell,
load, rent, lease or license the SPICE macro-model ... to anyone other than
the user". Analog Devices' model files carry an equivalent "nonexclusive,
nontransferable" statement limiting copies to use "within their company only".
Those are freely *downloadable* and not freely *redistributable*, which are
different things, so this package ships none of them and reads
operator-supplied ones from disk instead (see
:func:`~silkscreen.spice.registry.load_model_files`).

**Prior art, and where this differs from it.**

* KiCad ships no device model libraries either -- ``eeschema/sim`` resolves a
  symbol to a model through the ``Sim.Library`` / ``Sim.Name`` fields pointing
  at a file the *user* supplies, and ``SIM_LIBRARY::FindModel`` returns
  ``nullptr`` on a miss rather than substituting anything. When no model can
  be determined at all, ``SIM_MODEL::ReadTypeFromFields`` reports "No
  simulation model definition found for symbol '%s'." through its ``REPORTER``
  and yields a marked ``SIM_MODEL_SPICE_FALLBACK`` placeholder. Refuse and say
  so; that is the behaviour this registry copies.
* ngspice itself is the counter-example. Given an undefined ``.model`` it does
  not refuse -- it substitutes default parameters for the primitive type and
  prints ``"Unable to find definition of model <name> - default assumed"``, and
  the run then produces a well-formed, entirely misleading answer. That is the
  exact failure this package exists to prevent, and it is why a generic
  stand-in here is a *warning* that :class:`~silkscreen.spice.deck.Testbench`
  ``strict`` promotes to an error unless the caller named the part it is
  accepting a stand-in for.
* ngspice's own documentation page for models
  (``ngspice.sourceforge.io/modelparams.html``) points outward at four
  third-party collections and tags the page GNU FDL -- a documentation licence
  over netlist text, which is too ambiguous to vendor from.

**What a generic stand-in is for.** It answers topology and first-order
behaviour questions: does the rail come up, does the astable oscillate at the
frequency the RC network implies, does the amplifier have the closed-loop gain
the feedback ratio implies. It cannot answer part-specific questions --
stability margin, PSRR, noise, temperature drift, output current limit,
process spread -- and :attr:`ModelProvenance.note` on each entry says so in
words that reach the caller's result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "ModelProvenance",
    "SILKSCREEN_LICENCE",
    "GENERIC_KIND",
    "OPERATOR_KIND",
    "opamp_subckt",
    "timer555_subckt",
    "linear_regulator_subckt",
    "port_subckt",
    "OPAMP_AOL",
    "OPAMP_GBW_HZ",
    "OPAMP_RO_OHM",
    "OPAMP_SWING_HEADROOM_V",
    "LDO_DROPOUT_V",
    "LDO_ROUT_OHM",
    "TIMER555_DISCHARGE_RON_OHM",
    "TIMER555_OUTPUT_DROP_V",
]

#: The licence every model in this module is available under: the repository's
#: own, because the text was written here from published topology rather than
#: copied from a vendor.
SILKSCREEN_LICENCE = "MIT (written for this repository; no vendor model text)"

#: ``ModelProvenance.kind`` for a stand-in for a *class* of part.
GENERIC_KIND = "generic"

#: ``ModelProvenance.kind`` for a model file the operator put on this machine.
OPERATOR_KIND = "operator"


@dataclass(frozen=True)
class ModelProvenance:
    """Where a subcircuit came from, and what claims it can carry.

    ``generic`` is the field that matters. It is ``True`` when the subcircuit
    stands in for a *class* of part rather than modelling the specific part
    number in the design, and it travels with the model into the deck, into
    the deck's warnings and into every result, so a verdict can never be read
    as being about a part it is not about.
    """

    #: Where the text came from, in words a person can check.
    source: str
    #: The licence the text is available under.
    licence: str
    #: True when this is a stand-in for a class of part, not the part itself.
    generic: bool
    #: :data:`GENERIC_KIND` or :data:`OPERATOR_KIND`.
    kind: str
    #: What the model does *not* answer. Reaches the caller's result verbatim.
    note: str = ""

    def line(self) -> str:
        """One sentence naming the model's standing, for a warning or report."""
        label = "generic stand-in" if self.generic else "model"
        detail = f" {self.note}" if self.note else ""
        return f"{label} from {self.source} [{self.licence}].{detail}"


# ==========================================================================
# Generic operational amplifier
# ==========================================================================

#: Open-loop DC gain of the generic op-amp. 100 dB, the order every
#: general-purpose part is in.
OPAMP_AOL = 1.0e5

#: Gain-bandwidth product, Hz. 1 MHz is the LM358/TL07x/MCP600x order.
OPAMP_GBW_HZ = 1.0e6

#: Open-loop output resistance, ohms.
OPAMP_RO_OHM = 50.0

#: How close to each rail the output can swing, volts.
OPAMP_SWING_HEADROOM_V = 0.05


def opamp_subckt(name: str, sections: int) -> str:
    """A single-pole op-amp macromodel with ``sections`` amplifiers in it.

    The topology is the textbook one-pole macromodel and nothing more: a
    voltage-controlled source of gain :data:`OPAMP_AOL`, clamped to the
    supplies, through a single RC pole placed so the gain-bandwidth product
    is :data:`OPAMP_GBW_HZ`, out through :data:`OPAMP_RO_OHM`. Two consequences
    are worth stating because a reader will otherwise assume them away:

    * The closed-loop response of anything built with it is exactly
      first-order, so there is **no phase margin to measure**. A stability
      question asked of this model has an answer that is a property of the
      model, not of the design.
    * The input stage is an ideal high impedance with no bias current, no
      offset and no common-mode limit, so an input near a rail behaves better
      than any real part would.

    Terminals, in order: ``IN+ IN- OUT`` per section, then ``V+ V-``.
    """
    if sections < 1:
        raise ValueError(f"an op-amp needs at least one section, got {sections}")
    pole_r = 1.0e3
    pole_hz = OPAMP_GBW_HZ / OPAMP_AOL
    pole_c = 1.0 / (2.0 * math.pi * pole_r * pole_hz)

    terminals: list[str] = []
    for index in range(1, sections + 1):
        terminals.extend([f"INP{index}", f"INN{index}", f"OUT{index}"])
    terminals.extend(["VP", "VN"])

    lines = [
        f"* {name}: generic single-pole op-amp, {sections} section(s).",
        f"* Aol={OPAMP_AOL:g} GBW={OPAMP_GBW_HZ:g}Hz Ro={OPAMP_RO_OHM:g}ohm."
        " Not a model of any specific part number.",
        f".subckt {name} {' '.join(terminals)}",
    ]
    for index in range(1, sections + 1):
        p, n, o = f"INP{index}", f"INN{index}", f"OUT{index}"
        lines.extend(
            [
                f"RIN{index} {p} {n} 1T",
                f"RCMP{index} {p} VN 1T",
                f"RCMN{index} {n} VN 1T",
                f"BE{index} E{index} 0 V = {OPAMP_AOL:g} * V({p},{n})",
                f"BC{index} EC{index} 0 V = min( max( V(E{index}), "
                f"V(VN)+{OPAMP_SWING_HEADROOM_V:g} ), "
                f"V(VP)-{OPAMP_SWING_HEADROOM_V:g} )",
                f"RP{index} EC{index} P{index} {pole_r:g}",
                f"CP{index} P{index} 0 {pole_c:.6e}",
                f"BO{index} O{index} 0 V = V(P{index})",
                f"RO{index} O{index} {o} {OPAMP_RO_OHM:g}",
            ]
        )
    lines.append(".ends")
    return "\n".join(lines)


# ==========================================================================
# Generic 555 timer
# ==========================================================================

#: On-resistance of the discharge transistor, ohms. Real parts are tens of
#: ohms; it appears in series with the timing resistor and so shifts the
#: astable period by roughly its ratio to that resistor.
TIMER555_DISCHARGE_RON_OHM = 25.0

#: Output high is this far below V+, volts (the bipolar 555's totem-pole drop).
TIMER555_OUTPUT_DROP_V = 1.7


def timer555_subckt(name: str) -> str:
    """A behavioural 555, built from its own block diagram.

    Three 5 k resistors set the two comparator references at 1/3 and 2/3 of
    the supply -- ``CTRL`` is the 2/3 node itself, exactly as the datasheet
    block diagram has it, so an external capacitor or divider on ``CTRL``
    moves both thresholds the way it does on a real part. The set/reset latch
    is a behavioural expression of its own next state fed back through a 1 ns
    RC, which is the standard way to give a SPICE B-source memory without an
    algebraic loop.

    What it does not model: supply current, rise/fall times, comparator offset
    and delay, the reset pin's threshold beyond a 0.7 V trip, and any CMOS
    variant's rail-to-rail output. The output drop
    (:data:`TIMER555_OUTPUT_DROP_V`) and discharge resistance
    (:data:`TIMER555_DISCHARGE_RON_OHM`) are bipolar-NE555 order.

    Terminals, in order: ``GND TRIG OUT RESET CTRL THRES DISCH VCC`` -- the
    datasheet's own pin 1..8 order, which is the one thing about a 555 that
    everybody already knows.
    """
    return "\n".join(
        [
            f"* {name}: behavioural 555 timer from the datasheet block diagram.",
            "* Not a model of any specific part number, and not a model of any"
            " CMOS variant.",
            f".subckt {name} GND TRIG OUT RESET CTRL THRES DISCH VCC",
            "RD1 VCC CTRL 5k",
            "RD2 CTRL REF13 5k",
            "RD3 REF13 GND 5k",
            "BQD QD 0 V = (V(RESET,GND) < 0.7) ? 0 :"
            " ( (V(TRIG,GND) < V(REF13,GND)) ? 1 :"
            " ( (V(THRES,GND) > V(CTRL,GND)) ? 0 : V(Q) ) )",
            "RQ QD Q 1k",
            "CQ Q 0 1p",
            f"BOUT OUTI 0 V = (V(Q) > 0.5) ?"
            f" (V(VCC,GND) - {TIMER555_OUTPUT_DROP_V:g}) : (V(GND) + 0.1)",
            "ROUT OUTI OUT 10",
            "BDIS DISCH GND I = (V(Q) > 0.5) ? (1e-12*V(DISCH,GND)) :"
            f" (V(DISCH,GND)/{TIMER555_DISCHARGE_RON_OHM:g})",
            "RLEAK DISCH GND 1G",
            ".ends",
        ]
    )


# ==========================================================================
# Generic fixed-output linear regulator
# ==========================================================================

#: Dropout voltage of the generic regulator, volts. An AMS1117 is about this
#: at full load; a modern low-dropout part is an order better, so a headroom
#: verdict from this model is pessimistic rather than optimistic.
LDO_DROPOUT_V = 1.1

#: Output impedance, ohms. Stands for load regulation and nothing else.
LDO_ROUT_OHM = 0.05


def linear_regulator_subckt(
    name: str, vout: float, *, enable: bool = False, extra_pins: tuple[str, ...] = ()
) -> str:
    """A fixed-output three-terminal regulator, behaviourally.

    The output is ``min(vout, vin - dropout)`` behind :data:`LDO_ROUT_OHM`,
    floored at zero, which is the whole model. It answers exactly two
    questions honestly -- is there enough input headroom, and does the load
    pull the rail down -- and it is silent on every other reason one would
    simulate a regulator. In particular **it has no control loop**, so it can
    say nothing at all about stability, about the output capacitor's ESR, or
    about transient response, which are the usual reasons to want an LDO in a
    simulation. Nor does it model quiescent current, current limit, thermal
    shutdown or PSRR.

    Terminals, in order: ``GND VOUT VIN``, then ``EN`` when ``enable``, then
    ``extra_pins`` (tied off inside at 1 T-ohm to ground -- a pin the model
    ignores is given a DC path rather than left floating).
    """
    if not math.isfinite(vout) or vout <= 0:
        raise ValueError(f"regulator output voltage must be positive, got {vout!r}")
    terminals = ["GND", "VOUT", "VIN"]
    if enable:
        terminals.append("EN")
    terminals.extend(extra_pins)
    gate = " * ((V(EN,GND) > 1.2) ? 1 : 0)" if enable else ""
    lines = [
        f"* {name}: generic fixed {vout:g} V linear regulator, behavioural.",
        "* No control loop: says nothing about stability, ESR or transient"
        " response.",
        f".subckt {name} {' '.join(terminals)}",
        f"BREG REG GND V = max( 0, min( {vout:g},"
        f" V(VIN,GND) - {LDO_DROPOUT_V:g} ) ){gate}",
        f"RREG REG VOUT {LDO_ROUT_OHM:g}",
    ]
    for index, pin in enumerate(extra_pins, start=1):
        lines.append(f"RNC{index} {pin} GND 1T")
    lines.append(".ends")
    return "\n".join(lines)


# ==========================================================================
# Ideal port (connector, header, battery holder)
# ==========================================================================


def port_subckt(name: str, terminals: tuple[str, ...]) -> str:
    """A connector or battery holder: terminals and nothing else.

    A connector genuinely has no behaviour -- it is where the circuit stops
    and the outside world starts -- so the honest model is an open terminal
    that the testbench may drive. The 1 T-ohm to ground per terminal exists
    only so an undriven pin has a DC path instead of producing a singular
    matrix that reads like a broken circuit.

    A battery holder gets the same treatment and for the same reason: its
    voltage is not in the circuit IR, so the model that invents one would be
    the model that lies. The testbench supplies the source.
    """
    if not terminals:
        raise ValueError("a port needs at least one terminal")
    lines = [
        f"* {name}: ideal port. Terminals only -- no source, no contact"
        " resistance.",
        f".subckt {name} {' '.join(terminals)}",
    ]
    for index, pin in enumerate(terminals, start=1):
        lines.append(f"RT{index} {pin} 0 1T")
    lines.append(".ends")
    return "\n".join(lines)

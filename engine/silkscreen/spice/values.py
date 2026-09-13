"""Component values: SPICE's number syntax, read exactly as SPICE reads it.

The Silkscreen IR carries component values as free text ("10k", "100nF",
"4.7uF") because that is how a datasheet writes them and how a model proposes
them. SPICE has its own rules for those strings, and they contain one genuine
trap: ``M`` is *milli*, ``MEG`` is mega, and a trailing unit letter is ignored.
"1M" is a milliohm, not a megohm.

Two decisions follow from that:

* **Parse the way SPICE parses.** A scale prefix is matched at the start of the
  suffix and everything after it is discarded, longest first so ``meg`` wins
  over ``m``. Diverging from the simulator here would mean the number this
  package reports and the number that was simulated are different numbers.
* **Emit canonical scientific notation into the deck.** Once parsed, the deck
  carries ``1.0000000000e+04`` rather than the original text, so there is no
  second interpretation step and no way for the two to drift apart.

The one place this is likely to surprise a caller is a bare ``F`` suffix:
``10F`` is ten femtofarads to SPICE and ten farads to most people writing it.
:func:`parse_value` returns a warning for that rather than silently picking a
side.
"""

from __future__ import annotations

import re

from .errors import ValueSyntaxError

__all__ = ["parse_value", "format_value", "SCALE_SUFFIXES"]

#: SPICE scale prefixes, longest first so ``meg`` is matched before ``m``.
SCALE_SUFFIXES: tuple[tuple[str, float], ...] = (
    ("meg", 1e6),
    ("mil", 25.4e-6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("µ", 1e-6),  # micro sign
    ("μ", 1e-6),  # greek small letter mu, what most editors insert
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
)

_NUMBER_RE = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$"
)

#: Suffix text that is a unit rather than a scale, once a scale has been
#: stripped. SPICE ignores it; we only use it to tell a bare unit apart from a
#: typo, so that garbage still raises.
_UNIT_WORDS = {
    "",
    "a",
    "amp",
    "amps",
    "f",
    "farad",
    "farads",
    "h",
    "henry",
    "henries",
    "hz",
    "hertz",
    "ohm",
    "ohms",
    "r",
    "s",
    "sec",
    "second",
    "seconds",
    "v",
    "volt",
    "volts",
    "w",
    "watt",
    "watts",
    "Ω",  # ohm sign
    "Ω",  # ohm sign (legacy codepoint)
}


def parse_value(text: str, *, part: str = "value") -> tuple[float, str | None]:
    """Read a SPICE component value.

    Returns ``(magnitude, warning)``. The warning is a human-readable string
    when the text is legal but likely means something other than what it says,
    and ``None`` otherwise -- it is surfaced in the simulation result rather
    than raised, because the value *did* parse and refusing to run would be
    worse than running and saying so.

    Raises :class:`~silkscreen.spice.errors.ValueSyntaxError` when the text is
    not a number SPICE could read at all.
    """
    if not isinstance(text, str):
        raise ValueSyntaxError(part, str(text), "expected a string")

    match = _NUMBER_RE.match(text)
    if not match:
        raise ValueSyntaxError(part, text, "no leading number")

    mantissa_text, suffix = match.group(1), match.group(2)
    mantissa = float(mantissa_text)
    lowered = suffix.lower().replace(" ", "")

    # An explicit exponent means the scale is already in the mantissa; a scale
    # prefix on top of it ("1e3k") is a typo, not a value.
    has_exponent = "e" in mantissa_text.lower()

    scale = 1.0
    warning: str | None = None
    matched = ""
    if not has_exponent:
        for prefix, factor in SCALE_SUFFIXES:
            if lowered.startswith(prefix):
                scale, matched = factor, prefix
                break

    remainder = lowered[len(matched) :]
    if remainder not in _UNIT_WORDS:
        raise ValueSyntaxError(
            part, text, f"unrecognised suffix {suffix!r}"
        )

    # Case is the signal, not what follows. Both guards used to require
    # `remainder == ""`, so the exact spelling that springs the trap silenced
    # it: "1MOhm" is 1 milliohm to SPICE and warned about nothing, putting a
    # short where a 1 M pull-up belonged -- wrong by 10^9, and every clause
    # then measured on a different circuit and reported a confident PASS.
    #
    # SPICE is case-insensitive, so "M" and "m" are both milli. A writer who
    # typed an *upper-case* M meant mega; one who typed "1mF" meant a
    # millifarad and should not be nagged. Same for femto: "1F" is a mistake,
    # "1fF" is a femtofarad.
    typed = suffix[: len(matched)]
    unit = f" (the {remainder!r} after it is a unit, not a scale)" if remainder else ""
    if matched == "f" and typed == "F":
        warning = (
            f"{part}: value {text!r} read as {mantissa}e-15 (SPICE reads a bare "
            f"'F' as femto, not farads){unit}. Write '{mantissa_text}' for farads."
        )
    if matched == "m" and typed == "M":
        warning = (
            f"{part}: value {text!r} read as {mantissa}e-3 (SPICE reads 'M' as "
            f"milli; write 'MEG' for mega){unit}."
        )

    return mantissa * scale, warning


def format_value(magnitude: float) -> str:
    """Render a magnitude as a SPICE literal with no scale prefix.

    Scientific notation with a full mantissa, so the deck says exactly what
    :func:`parse_value` computed and no suffix has to be reinterpreted.
    """
    return f"{magnitude:.10e}"

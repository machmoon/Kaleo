"""JSON in, testbench out.

The Python API is the real one, but an agent reaching this package through a
tool call has JSON and nothing else. This module is the bridge: plain dicts to
:class:`~silkscreen.spice.deck.Testbench` and
:class:`~silkscreen.spice.assertions.Assertion`, with the same
collect-every-error convention :func:`~silkscreen.netlist.parse_circuit_spec`
uses, so a malformed request comes back as one list of problems a model can
repair in a single pass rather than one problem per round trip.

Nothing here guesses. A missing ``stop`` on a transient is an error, not a
default, because a default would silently answer a different question than the
one asked.
"""

from __future__ import annotations

import math
from typing import Any

from .assertions import Assertion
from .deck import (
    ACSweep,
    Analysis,
    DCSweep,
    OperatingPoint,
    Source,
    Testbench,
    Transient,
)
from .errors import DeckError
from .measure import MEASUREMENT_KINDS, Measurement

__all__ = [
    "analysis_from_dict",
    "source_from_dict",
    "testbench_from_dict",
    "measurement_from_dict",
    "assertion_from_dict",
    "assertions_from_dict",
    "REQUEST_SCHEMA",
]


_TESTBENCH_FIELDS = frozenset(
    {"analysis", "sources", "ground", "probes", "temperature_c", "strict"}
)
_ANALYSIS_FIELDS = {
    "op": frozenset({"kind"}),
    "tran": frozenset({"kind", "step", "stop", "start", "max_step"}),
    "ac": frozenset({"kind", "f_start", "f_stop", "points", "sweep"}),
    "dc": frozenset({"kind", "source", "start", "stop", "step"}),
}
_SOURCE_FIELDS = {
    "": frozenset(
        {"name", "positive", "negative", "kind", "dc", "ac", "ac_magnitude"}
    ),
    "dc": frozenset({"name", "positive", "negative", "kind", "dc"}),
    "ac": frozenset(
        {"name", "positive", "negative", "kind", "dc", "ac", "ac_magnitude"}
    ),
    "pulse": frozenset(
        {
            "name",
            "positive",
            "negative",
            "kind",
            "initial",
            "pulsed",
            "delay",
            "rise",
            "fall",
            "width",
            "period",
        }
    ),
    "sine": frozenset(
        {
            "name",
            "positive",
            "negative",
            "kind",
            "offset",
            "amplitude",
            "frequency",
            "delay",
        }
    ),
}
_MAX_ANALYSIS_POINTS = 1_000_000
_MAX_AC_POINTS = 1_000


def _reject_unknown(
    data: dict[str, Any], allowed: frozenset[str], errors: list[str], *, where: str
) -> None:
    for key in sorted(set(data) - allowed):
        errors.append(f"{where}: unknown field {key!r}")


def _number(
    data: dict[str, Any],
    key: str,
    errors: list[str],
    *,
    where: str,
    required: bool = True,
    default: float | None = None,
) -> float | None:
    if key not in data:
        if required:
            errors.append(f"{where}: missing required field {key!r}")
        return default
    if isinstance(data[key], bool):
        errors.append(f"{where}: {key!r} must be a number, got {data[key]!r}")
        return None
    try:
        value = float(data[key])
    except (OverflowError, TypeError, ValueError):
        errors.append(f"{where}: {key!r} must be a number, got {data[key]!r}")
        return None
    if not math.isfinite(value):
        errors.append(f"{where}: {key!r} must be finite, got {data[key]!r}")
        return None
    return value


def _integer(
    data: dict[str, Any],
    key: str,
    errors: list[str],
    *,
    where: str,
    default: int,
) -> int | None:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{where}: {key!r} must be an integer, got {value!r}")
        return None
    return value


def _string(
    data: dict[str, Any], key: str, errors: list[str], *, where: str
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        errors.append(f"{where}: missing or invalid string field {key!r}")
        return ""
    return value


def analysis_from_dict(data: dict[str, Any], errors: list[str]) -> Analysis | None:
    """``{"kind": "tran", "step": ..., "stop": ...}`` to an :class:`Analysis`."""
    raw_kind = data.get("kind", "")
    kind = raw_kind.lower() if isinstance(raw_kind, str) else ""
    where = f"analysis {kind!r}" if kind else "analysis"

    if kind not in _ANALYSIS_FIELDS:
        errors.append(
            f"unknown analysis kind {raw_kind!r}; known: 'op', 'tran', 'ac', 'dc'"
        )
        return None
    _reject_unknown(data, _ANALYSIS_FIELDS[kind], errors, where=where)

    if kind == "op":
        return OperatingPoint()

    if kind == "tran":
        step = _number(data, "step", errors, where=where)
        stop = _number(data, "stop", errors, where=where)
        start = _number(
            data, "start", errors, where=where, required=False, default=0.0
        )
        max_step = _number(
            data, "max_step", errors, where=where, required=False
        )
        if step is None or stop is None or start is None:
            return None
        if step <= 0:
            errors.append(f"{where}: 'step' must be greater than zero")
        if start < 0:
            errors.append(f"{where}: 'start' must be zero or greater")
        if stop <= start:
            errors.append(f"{where}: 'stop' must be greater than 'start'")
        if max_step is not None and max_step <= 0:
            errors.append(f"{where}: 'max_step' must be greater than zero")
        if step > 0 and stop > start:
            points = math.ceil((stop - start) / step) + 1
            if points > _MAX_ANALYSIS_POINTS:
                errors.append(
                    f"{where}: requests about {points} output points; maximum is "
                    f"{_MAX_ANALYSIS_POINTS}"
                )
        if errors:
            return None
        return Transient(
            step=step,
            stop=stop,
            start=start,
            max_step=max_step,
        )

    if kind == "ac":
        f_start = _number(data, "f_start", errors, where=where)
        f_stop = _number(data, "f_stop", errors, where=where)
        points = _integer(data, "points", errors, where=where, default=20)
        sweep = data.get("sweep", "dec")
        if not isinstance(sweep, str) or sweep not in ("dec", "oct", "lin"):
            errors.append(f"{where}: 'sweep' must be 'dec', 'oct' or 'lin'")
        if f_start is None or f_stop is None or points is None:
            return None
        if f_start <= 0:
            errors.append(f"{where}: 'f_start' must be greater than zero")
        if f_stop <= f_start:
            errors.append(f"{where}: 'f_stop' must be greater than 'f_start'")
        if not 1 <= points <= _MAX_AC_POINTS:
            errors.append(
                f"{where}: 'points' must be between 1 and {_MAX_AC_POINTS}"
            )
        if errors:
            return None
        return ACSweep(
            f_start=f_start,
            f_stop=f_stop,
            points=points,
            sweep=sweep,
        )

    if kind == "dc":
        source = _string(data, "source", errors, where=where)
        start = _number(data, "start", errors, where=where)
        stop = _number(data, "stop", errors, where=where)
        step = _number(data, "step", errors, where=where)
        if not source or start is None or stop is None or step is None:
            return None
        if step == 0:
            errors.append(f"{where}: 'step' must not be zero")
        elif (stop - start) * step <= 0:
            errors.append(f"{where}: 'step' must move from 'start' toward 'stop'")
        else:
            points = math.ceil(abs((stop - start) / step)) + 1
            if points > _MAX_ANALYSIS_POINTS:
                errors.append(
                    f"{where}: requests about {points} output points; maximum is "
                    f"{_MAX_ANALYSIS_POINTS}"
                )
        if errors:
            return None
        return DCSweep(source=source, start=start, stop=stop, step=step)

    return None  # all known kinds returned above


def source_from_dict(data: dict[str, Any], errors: list[str]) -> Source | None:
    """One source. ``kind`` selects the stimulus shape; omit it for DC/AC."""
    name = _string(data, "name", errors, where="source")
    positive = _string(data, "positive", errors, where="source")
    negative = _string(data, "negative", errors, where="source")
    where = f"source {name!r}" if name else "source"
    if not (name and positive and negative):
        return None

    raw_kind = data.get("kind", "")
    kind = raw_kind.lower() if isinstance(raw_kind, str) else ""
    if kind not in _SOURCE_FIELDS:
        errors.append(
            f"{where}: unknown source kind {raw_kind!r}; "
            f"known: 'dc', 'ac', 'pulse', 'sine'"
        )
        return None
    _reject_unknown(data, _SOURCE_FIELDS[kind], errors, where=where)

    if kind == "pulse":
        needed = ("initial", "pulsed", "width", "period")
        values = {k: _number(data, k, errors, where=where) for k in needed}
        delay = _number(
            data, "delay", errors, where=where, required=False, default=0.0
        )
        rise = _number(
            data, "rise", errors, where=where, required=False, default=1e-9
        )
        fall = _number(
            data, "fall", errors, where=where, required=False, default=1e-9
        )
        if any(v is None for v in values.values()) or None in (delay, rise, fall):
            return None
        if values["width"] <= 0 or values["period"] <= 0:
            errors.append(f"{where}: 'width' and 'period' must be greater than zero")
        if delay < 0:
            errors.append(f"{where}: 'delay' must be zero or greater")
        if rise <= 0 or fall <= 0:
            errors.append(f"{where}: 'rise' and 'fall' must be greater than zero")
        if errors:
            return None
        return Source.pulse(
            name,
            positive,
            negative,
            initial=values["initial"],
            pulsed=values["pulsed"],
            width=values["width"],
            period=values["period"],
            delay=delay,
            rise=rise,
            fall=fall,
        )

    if kind == "sine":
        needed = ("offset", "amplitude", "frequency")
        values = {k: _number(data, k, errors, where=where) for k in needed}
        delay = _number(
            data, "delay", errors, where=where, required=False, default=0.0
        )
        if any(v is None for v in values.values()) or delay is None:
            return None
        if values["frequency"] <= 0:
            errors.append(f"{where}: 'frequency' must be greater than zero")
        if delay < 0:
            errors.append(f"{where}: 'delay' must be zero or greater")
        if errors:
            return None
        return Source.sine(
            name,
            positive,
            negative,
            offset=values["offset"],
            amplitude=values["amplitude"],
            frequency=values["frequency"],
            delay=delay,
        )

    dc = data.get("dc")
    ac = data.get("ac_magnitude", data.get("ac"))
    if dc is None and ac is None:
        errors.append(
            f"{where}: needs 'dc', 'ac_magnitude', or a 'kind' of 'pulse'/'sine'"
        )
        return None
    dc_value = (
        _number(data, "dc", errors, where=where, required=False)
        if dc is not None
        else None
    )
    ac_key = "ac_magnitude" if "ac_magnitude" in data else "ac"
    ac_value = (
        _number(data, ac_key, errors, where=where, required=False)
        if ac is not None
        else None
    )
    if errors:
        return None
    return Source(
        name=name,
        positive=positive,
        negative=negative,
        dc=dc_value,
        ac_magnitude=ac_value,
    )


def testbench_from_dict(data: dict[str, Any]) -> Testbench:
    """Build a safe, typed :class:`Testbench` from untrusted JSON.

    Raw SPICE model programs and free-form directives deliberately are not part
    of this bridge. Trusted Python code can construct :class:`Testbench`
    directly when it needs a vendor model.
    """
    if not isinstance(data, dict):
        raise DeckError([f"testbench must be an object, got {type(data).__name__}"])

    errors: list[str] = []
    _reject_unknown(data, _TESTBENCH_FIELDS, errors, where="testbench")
    analysis_data = data.get("analysis")
    if not isinstance(analysis_data, dict):
        errors.append("testbench needs an 'analysis' object, e.g. {'kind': 'op'}")
        analysis = None
    else:
        analysis = analysis_from_dict(analysis_data, errors)

    sources: list[Source] = []
    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list):
        errors.append("'sources' must be a list")
        raw_sources = []
    for entry in raw_sources:
        if not isinstance(entry, dict):
            errors.append(f"each source must be an object, got {entry!r}")
            continue
        source = source_from_dict(entry, errors)
        if source is not None:
            sources.append(source)

    temperature = data.get("temperature_c")
    temperature_value = (
        _number(data, "temperature_c", errors, where="testbench", required=False)
        if temperature is not None
        else None
    )
    ground = data.get("ground")
    if ground is not None and (not isinstance(ground, str) or not ground):
        errors.append("testbench: 'ground' must be a non-empty string")
        ground = None
    raw_probes = data.get("probes") or []
    probes: tuple[str, ...] = ()
    if not isinstance(raw_probes, list) or not all(
        isinstance(probe, str) and probe for probe in raw_probes
    ):
        errors.append("testbench: 'probes' must be a list of non-empty strings")
    else:
        probes = tuple(raw_probes)
    strict = data.get("strict", False)
    if not isinstance(strict, bool):
        errors.append("testbench: 'strict' must be a boolean")

    if errors or analysis is None:
        raise DeckError(errors or ["could not build the analysis"])

    return Testbench(
        analysis=analysis,
        sources=sources,
        ground=ground,
        probes=probes,
        temperature_c=temperature_value,
        strict=strict,
    )


def measurement_from_dict(
    data: dict[str, Any], errors: list[str]
) -> Measurement | None:
    if not isinstance(data, dict):
        errors.append(f"measurement must be an object, got {data!r}")
        return None
    kind = str(data.get("kind", ""))
    signal = str(data.get("signal", ""))
    if kind not in MEASUREMENT_KINDS:
        errors.append(
            f"unknown measurement kind {kind!r}; known: {sorted(MEASUREMENT_KINDS)}"
        )
        return None
    if not signal:
        errors.append(f"measurement {kind!r}: missing 'signal'")
        return None

    window = data.get("window")
    if window is not None:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            errors.append(
                f"measurement {kind!r}: 'window' must be [start, end]"
            )
            return None
        window = (float(window[0]), float(window[1]))

    at = data.get("at")
    return Measurement(
        kind=kind,
        signal=signal,
        reference=str(data["reference"]) if data.get("reference") else None,
        window=window,
        at=float(at) if at is not None else None,
        low_pct=float(data.get("low_pct", 0.1)),
        high_pct=float(data.get("high_pct", 0.9)),
        tolerance=float(data.get("settle_tolerance", 0.02)),
    )


def assertion_from_dict(data: dict[str, Any], errors: list[str]) -> Assertion | None:
    if not isinstance(data, dict):
        errors.append(f"assertion must be an object, got {data!r}")
        return None
    name = str(data.get("name", "")) or "unnamed assertion"
    measurement = measurement_from_dict(data.get("measurement") or {}, errors)
    op = str(data.get("op", ""))
    if not op:
        errors.append(f"assertion {name!r}: missing 'op'")
    if "value" not in data:
        errors.append(f"assertion {name!r}: missing 'value'")
        return None
    try:
        value = float(data["value"])
    except (TypeError, ValueError):
        errors.append(f"assertion {name!r}: 'value' must be a number")
        return None
    if measurement is None or not op:
        return None
    return Assertion(
        name=name,
        measurement=measurement,
        op=op,
        value=value,
        tolerance=float(data.get("tolerance", 0.0)),
        absolute_tolerance=bool(data.get("absolute_tolerance", False)),
        unit=str(data.get("unit", "")),
    )


def assertions_from_dict(items: Any) -> list[Assertion]:
    """Build a whole specification. Raises :class:`DeckError` with every problem."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise DeckError(["'assertions' must be a list"])
    errors: list[str] = []
    built: list[Assertion] = []
    for entry in items:
        assertion = assertion_from_dict(entry, errors)
        if assertion is not None:
            built.append(assertion)
    if errors:
        raise DeckError(errors)
    return built


_ANALYSIS_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"kind": {"const": "op"}},
            "required": ["kind"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"const": "tran"},
                "step": {"type": "number", "exclusiveMinimum": 0},
                "stop": {"type": "number", "exclusiveMinimum": 0},
                "start": {"type": "number", "minimum": 0},
                "max_step": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["kind", "step", "stop"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"const": "ac"},
                "f_start": {"type": "number", "exclusiveMinimum": 0},
                "f_stop": {"type": "number", "exclusiveMinimum": 0},
                "points": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_AC_POINTS,
                },
                "sweep": {"type": "string", "enum": ["dec", "oct", "lin"]},
            },
            "required": ["kind", "f_start", "f_stop"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"const": "dc"},
                "source": {"type": "string", "minLength": 1},
                "start": {"type": "number"},
                "stop": {"type": "number"},
                "step": {"type": "number"},
            },
            "required": ["kind", "source", "start", "stop", "step"],
        },
    ]
}

_SOURCE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "positive": {"type": "string", "minLength": 1},
                "negative": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["dc", "ac"]},
                "dc": {"type": "number"},
                "ac": {"type": "number"},
                "ac_magnitude": {"type": "number"},
            },
            "required": ["name", "positive", "negative"],
            "anyOf": [
                {"required": ["dc"]},
                {"required": ["ac"]},
                {"required": ["ac_magnitude"]},
            ],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "positive": {"type": "string", "minLength": 1},
                "negative": {"type": "string", "minLength": 1},
                "kind": {"const": "pulse"},
                "initial": {"type": "number"},
                "pulsed": {"type": "number"},
                "delay": {"type": "number", "minimum": 0},
                "rise": {"type": "number", "exclusiveMinimum": 0},
                "fall": {"type": "number", "exclusiveMinimum": 0},
                "width": {"type": "number", "exclusiveMinimum": 0},
                "period": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": [
                "name",
                "positive",
                "negative",
                "kind",
                "initial",
                "pulsed",
                "width",
                "period",
            ],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "positive": {"type": "string", "minLength": 1},
                "negative": {"type": "string", "minLength": 1},
                "kind": {"const": "sine"},
                "offset": {"type": "number"},
                "amplitude": {"type": "number"},
                "frequency": {"type": "number", "exclusiveMinimum": 0},
                "delay": {"type": "number", "minimum": 0},
            },
            "required": [
                "name",
                "positive",
                "negative",
                "kind",
                "offset",
                "amplitude",
                "frequency",
            ],
        },
    ]
}

#: JSON Schema for a simulation request, shared by the MCP tool and anything
#: else that wants to describe this interface to a model.
REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "circuit": {
            "type": "object",
            "description": "A Silkscreen circuit: devices, passives, nets.",
        },
        "testbench": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "What to drive the circuit with and what to analyse. "
                "The circuit alone does not say. Only typed stimuli are "
                "accepted here; raw SPICE programs are never accepted over MCP."
            ),
            "properties": {
                "analysis": {
                    **_ANALYSIS_SCHEMA,
                    "description": (
                        "{'kind':'op'} | {'kind':'tran','step':s,'stop':s} | "
                        "{'kind':'ac','f_start':Hz,'f_stop':Hz,'points':n} | "
                        "{'kind':'dc','source':'V1','start':v,'stop':v,'step':v}"
                    ),
                },
                "sources": {
                    "type": "array",
                    "description": (
                        "Each: name (must start with V or I), positive, "
                        "negative, and either dc / ac_magnitude or "
                        "kind='pulse' (initial, pulsed, width, period) or "
                        "kind='sine' (offset, amplitude, frequency)."
                    ),
                    "items": _SOURCE_SCHEMA,
                },
                "ground": {"type": "string"},
                "probes": {"type": "array", "items": {"type": "string"}},
                "temperature_c": {"type": "number"},
                "strict": {
                    "type": "boolean",
                    "description": "Treat warnings (e.g. a substituted generic "
                    "diode model) as errors.",
                },
            },
            "required": ["analysis"],
        },
        "assertions": {
            "type": "array",
            "description": (
                "Specification clauses. Each: name, measurement "
                "{kind, signal, window?, at?}, op (<, <=, >, >=, ==, within), "
                "value, tolerance?, unit?."
            ),
            "items": {"type": "object"},
        },
        "simulator": {
            "type": "string",
            "enum": ["ngspice", "ltspice"],
            "description": "'ngspice' or 'ltspice'; omit to auto-select.",
        },
        "timeout_s": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 120,
            "default": 60,
        },
        "max_points": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2000,
            "description": "Downsampled waveform points to return per signal; "
            "0 (the default) returns summary statistics only.",
        },
    },
    "required": ["circuit", "testbench"],
}

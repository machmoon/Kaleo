"""Tests for :mod:`silkscreen.spice`.

Two layers, deliberately separated:

* Everything that does not need a simulator runs always -- value parsing, deck
  construction, rawfile reading, measurement maths, assertion logic. These use
  synthetic results built directly, so the measurement code is exercised without
  a SPICE binary anywhere near it.
* The end-to-end tests are gated on ngspice being installed, the same convention
  ``test_live_model.py`` uses for a live API key, so the default suite stays
  offline and dependency-free. They are the ones that prove the package actually
  simulates; :func:`test_rc_lowpass_matches_closed_form` is the narrow slice
  verified end to end.

The gated tests check simulated numbers against **closed-form circuit theory
computed inline** -- ``2.2 * R * C`` for a 10-90% rise time, ``1 / (2 * pi * R *
C)`` for a corner frequency. That is the same discipline ``test_kicad.py`` uses
for overlap: an expectation written in terms of the code under test would share
its blind spot, and the whole point of this package is to be a trustworthy
oracle.
"""

from __future__ import annotations

import math
import os
import struct

import pytest
from silkscreen.netlist import parse_circuit_spec
from silkscreen.spice import (
    ACSweep,
    Assertion,
    DCSweep,
    Measurement,
    NgspiceSimulator,
    OperatingPoint,
    PrimitiveModel,
    Source,
    SubcircuitModel,
    Testbench,
    Transient,
    build_deck,
    check_all,
    measure,
    parse_rawfile,
    parse_value,
    simulate,
    verify,
)
from silkscreen.spice.errors import (
    ConvergenceError,
    DeckError,
    MeasurementError,
    RawParseError,
    SimulationFailed,
    SimulatorNotFound,
    UnsimulatableError,
    ValueSyntaxError,
)
from silkscreen.spice.registry import (
    MODEL_PATH_ENV,
    ModelRegistry,
    load_model_files,
    model_search_paths,
)
from silkscreen.spice.result import build_result
from silkscreen.spice.simulators import LTspiceSimulator, find_simulator

HAS_NGSPICE = NgspiceSimulator().is_available()
needs_ngspice = pytest.mark.skipif(
    not HAS_NGSPICE, reason="ngspice is not installed on this machine"
)

# --------------------------------------------------------------------------
# Fixtures: circuits expressible in the IR
# --------------------------------------------------------------------------

#: RC low-pass with input decoupling and a load. Every net needs two endpoints
#: to satisfy the IR, which is why the input carries a bypass cap rather than
#: dangling at the source -- a real board's input net looks like this anyway.
RC_LOWPASS = {
    "passives": {
        "Cin": {"type": "capacitor", "value": "1nF"},
        "Rfilt": {"type": "resistor", "value": "1k"},
        "Cfilt": {"type": "capacitor", "value": "100nF"},
        "Rload": {"type": "resistor", "value": "1MEG"},
    },
    "nets": {
        "VIN": ["Cin.1", "Rfilt.1"],
        "VOUT": ["Rfilt.2", "Cfilt.1", "Rload.1"],
        "GND": ["Cin.2", "Cfilt.2", "Rload.2"],
    },
}

#: Resistive divider: the simplest circuit with a checkable DC answer.
DIVIDER = {
    "passives": {
        "Rtop": {"type": "resistor", "value": "10k"},
        "Rbot": {"type": "resistor", "value": "10k"},
        "Cbyp": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["Rtop.1", "Cbyp.1"],
        "VMID": ["Rtop.2", "Rbot.1"],
        "GND": ["Rbot.2", "Cbyp.2"],
    },
}


def rc_spec():
    return parse_circuit_spec(RC_LOWPASS)


def divider_spec():
    return parse_circuit_spec(DIVIDER)


def synthetic(
    variables, data, *, analysis="tran", net_of_node=None, warnings=()
):
    """A :class:`SimulationResult` with no simulator involved."""
    return build_result(
        analysis=analysis,
        sweep_name=variables[0],
        variables=tuple(variables),
        data=data,
        net_of_node=net_of_node or {},
        warnings=tuple(warnings),
        deck="",
        log="",
        simulator="synthetic",
    )


def ramp_result(points=101, t_stop=1e-3, final=5.0):
    """A clean exponential charge, so measurements have an analytic answer."""
    tau = t_stop / 5.0
    times = tuple(t_stop * i / (points - 1) for i in range(points))
    values = tuple(final * (1.0 - math.exp(-t / tau)) for t in times)
    return synthetic(("time", "v(vout)"), {"time": times, "v(vout)": values})


# ==========================================================================
# values
# ==========================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1k", 1e3),
        ("10K", 1e4),
        ("100nF", 100e-9),
        ("4.7uF", 4.7e-6),
        ("4.7µF", 4.7e-6),
        ("1MEG", 1e6),
        ("2.2", 2.2),
        ("1e-9", 1e-9),
        ("10 ohm", 10.0),
        ("22p", 22e-12),
        ("3.3V", 3.3),
    ],
)
def test_parse_value_reads_spice_syntax(text, expected):
    magnitude, _ = parse_value(text)
    assert magnitude == pytest.approx(expected, rel=1e-12)


def test_m_is_milli_not_mega():
    """The trap that makes a 1 megohm resistor a 1 milliohm short."""
    milli, warning = parse_value("1M")
    mega, _ = parse_value("1MEG")
    assert milli == pytest.approx(1e-3)
    assert mega == pytest.approx(1e6)
    assert warning is not None and "milli" in warning


def test_bare_f_suffix_warns_rather_than_guessing():
    magnitude, warning = parse_value("10F", part="C1")
    assert magnitude == pytest.approx(10e-15)
    assert warning is not None and "femto" in warning


@pytest.mark.parametrize("text", ["ten", "", "1 kilohm", "1e3k", "4.7 bananas"])
def test_unreadable_values_raise(text):
    with pytest.raises(ValueSyntaxError):
        parse_value(text, part="R1")


# ==========================================================================
# deck construction
# ==========================================================================


def test_deck_renders_every_passive():
    deck = build_deck(rc_spec(), Testbench(analysis=OperatingPoint()))
    lines = [ln for ln in deck.text.splitlines() if ln and not ln.startswith("*")]
    assert any(ln.startswith("R1 ") for ln in lines)
    assert any(ln.startswith("C1 ") for ln in lines)
    assert len(lines) == 4


def test_reference_designator_is_not_doubled():
    """``R1`` stays ``R1`` rather than becoming ``RR1``."""
    deck = build_deck(divider_spec(), Testbench(analysis=OperatingPoint()))
    assert "RR" not in deck.text
    assert "R1 " in deck.text and "R2 " in deck.text


def test_ground_becomes_node_zero_and_other_nets_do_not():
    deck = build_deck(rc_spec(), Testbench(analysis=OperatingPoint()))
    assert deck.ground == "GND"
    assert deck.node_of_net["GND"] == "0"
    assert deck.node_of_net["VOUT"] != "0"


def test_values_reach_the_deck_as_scientific_notation():
    """No suffix survives into the deck, so SPICE cannot reinterpret one."""
    deck = build_deck(rc_spec(), Testbench(analysis=OperatingPoint()))
    resistor = next(ln for ln in deck.text.splitlines() if ln.startswith("R1 "))
    assert float(resistor.split()[-1]) == pytest.approx(1e3)


def test_net_names_are_sanitised_without_merging_distinct_nets():
    spec = parse_circuit_spec(
        {
            "passives": {
                "Ra": {"type": "resistor", "value": "1k"},
                "Rb": {"type": "resistor", "value": "1k"},
                "Rc": {"type": "resistor", "value": "1k"},
            },
            # These differ only in characters SPICE cannot use in a node name.
            "nets": {
                "V+": ["Ra.1", "Rb.1"],
                "V-": ["Ra.2", "Rc.1"],
                "GND": ["Rb.2", "Rc.2"],
            },
        }
    )
    deck = build_deck(spec, Testbench(analysis=OperatingPoint()))
    nodes = {deck.node_of_net["V+"], deck.node_of_net["V-"]}
    assert len(nodes) == 2, "sanitising must not collapse two nets into one node"
    assert deck.net_of_node[deck.node_of_net["V+"]] == "V+"


def test_no_ground_is_an_error_not_a_singular_matrix():
    spec = parse_circuit_spec(
        {
            "passives": {
                "Ra": {"type": "resistor", "value": "1k"},
                "Rb": {"type": "resistor", "value": "1k"},
            },
            "nets": {"A": ["Ra.1", "Rb.1"], "B": ["Ra.2", "Rb.2"]},
        }
    )
    with pytest.raises(DeckError) as exc:
        build_deck(spec, Testbench(analysis=OperatingPoint()))
    assert "ground" in str(exc.value).lower()


def test_explicit_ground_must_exist():
    with pytest.raises(DeckError) as exc:
        build_deck(rc_spec(), Testbench(analysis=OperatingPoint(), ground="NOPE"))
    assert "NOPE" in str(exc.value)


def test_source_on_a_net_that_does_not_exist_is_an_error():
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[Source.dc_supply("V1", "TYPO", "GND", 5.0)],
    )
    with pytest.raises(DeckError) as exc:
        build_deck(rc_spec(), bench)
    assert "TYPO" in str(exc.value)


def test_source_name_must_declare_its_element_type():
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[Source.dc_supply("SUPPLY", "VIN", "GND", 5.0)],
    )
    with pytest.raises(DeckError) as exc:
        build_deck(rc_spec(), bench)
    assert "'V' or 'I'" in str(exc.value)


def test_deck_errors_are_collected_not_raised_one_at_a_time():
    """One repair prompt should be able to fix everything at once."""
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[
            Source.dc_supply("SUPPLY", "TYPO", "GND", 5.0),
            Source.dc_supply("ALSOBAD", "VIN", "OTHER", 5.0),
        ],
        probes=("NOSUCHNET",),
    )
    with pytest.raises(DeckError) as exc:
        build_deck(rc_spec(), bench)
    assert len(exc.value.errors) >= 4


def test_ac_analysis_without_an_ac_source_is_an_error():
    """Otherwise every node is exactly zero and the sweep looks like a dead
    circuit rather than a missing stimulus."""
    bench = Testbench(
        analysis=ACSweep(f_start=1, f_stop=1e6),
        sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
    )
    with pytest.raises(DeckError) as exc:
        build_deck(rc_spec(), bench)
    assert "AC magnitude" in str(exc.value)


def test_dc_sweep_must_name_a_source_that_exists():
    bench = Testbench(
        analysis=DCSweep(source="V9", start=0, stop=5, step=0.1),
        sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
    )
    with pytest.raises(DeckError):
        build_deck(rc_spec(), bench)


def test_negative_component_value_is_rejected():
    spec = parse_circuit_spec(
        {
            "passives": {
                "Rtop": {"type": "resistor", "value": "-10k"},
                "Rbot": {"type": "resistor", "value": "10k"},
            },
            "nets": {"VIN": ["Rtop.1", "Rbot.2"], "GND": ["Rtop.2", "Rbot.1"]},
        }
    )
    with pytest.raises(DeckError) as exc:
        build_deck(spec, Testbench(analysis=OperatingPoint()))
    assert "positive" in str(exc.value)


@pytest.mark.parametrize(
    "bench",
    [
        Testbench(analysis=OperatingPoint(), title="demo\n.control"),
        Testbench(
            analysis=OperatingPoint(),
            options=("reltol=1e-3\n.control",),
        ),
        Testbench(
            analysis=OperatingPoint(),
            sources=[
                Source(
                    name="V1",
                    positive="VIN",
                    negative="GND",
                    transient="PULSE(0 5 0 1n 1n 1u 2u)\n.control",
                )
            ],
        ),
        Testbench(
            analysis=OperatingPoint(),
            models={
                "unused": PrimitiveModel(
                    name="BAD",
                    text=".model BAD D\n.control\nshell echo injected\n.endc",
                )
            },
        ),
    ],
    ids=["title", "option", "transient", "model-control-block"],
)
def test_deck_rejects_directive_injection_at_the_final_boundary(bench):
    with pytest.raises(DeckError):
        build_deck(rc_spec(), bench)


# --- the honest edge: parts with no behaviour ------------------------------


def test_device_without_a_model_raises_rather_than_being_left_out():
    """The single most important failure in this package.

    Silently dropping the IC would produce a deck that simulates cleanly and
    describes a completely different circuit.
    """
    spec = parse_circuit_spec(
        {
            "devices": {"U_reg": {"pins": {"IN": "1", "GND": "2", "OUT": "3"}}},
            "passives": {
                "Cin": {"type": "capacitor", "value": "10uF"},
                "Cout": {"type": "capacitor", "value": "10uF"},
            },
            "nets": {
                "VIN": ["U_reg.IN", "Cin.1"],
                "VOUT": ["U_reg.OUT", "Cout.1"],
                "GND": ["U_reg.GND", "Cin.2", "Cout.2"],
            },
        }
    )
    with pytest.raises(UnsimulatableError) as exc:
        build_deck(spec, Testbench(analysis=OperatingPoint()))
    assert "U_reg" in str(exc.value)
    assert exc.value.parts == ["U_reg"]


def test_device_with_a_subcircuit_is_wired_in_pin_order():
    spec = parse_circuit_spec(
        {
            "devices": {"U_reg": {"pins": {"IN": "1", "GND": "2", "OUT": "3"}}},
            "passives": {
                "Cin": {"type": "capacitor", "value": "10uF"},
                "Cout": {"type": "capacitor", "value": "10uF"},
            },
            "nets": {
                "VIN": ["U_reg.IN", "Cin.1"],
                "VOUT": ["U_reg.OUT", "Cout.1"],
                "GND": ["U_reg.GND", "Cin.2", "Cout.2"],
            },
        }
    )
    model = SubcircuitModel(
        name="LDO33",
        pins=("IN", "OUT", "GND"),
        text=".subckt LDO33 in out gnd\nVo out gnd 3.3\n.ends",
    )
    deck = build_deck(
        spec,
        Testbench(analysis=OperatingPoint(), models={"U_reg": model}),
    )
    line = next(ln for ln in deck.text.splitlines() if ln.startswith("XU1"))
    _, n_in, n_out, n_gnd, subckt = line.split()
    assert subckt == "LDO33"
    assert n_in == deck.node_of_net["VIN"]
    assert n_out == deck.node_of_net["VOUT"]
    assert n_gnd == "0"


def test_subcircuit_terminal_not_on_the_device_is_an_error():
    spec = parse_circuit_spec(
        {
            "devices": {"U1": {"pins": {"A": "1", "B": "2"}}},
            "passives": {"R1": {"type": "resistor", "value": "1k"}},
            "nets": {"NA": ["U1.A", "R1.1"], "GND": ["U1.B", "R1.2"]},
        }
    )
    model = SubcircuitModel(
        name="X", pins=("A", "NOSUCHPIN"), text=".subckt X a b\n.ends"
    )
    with pytest.raises(DeckError) as exc:
        build_deck(spec, Testbench(analysis=OperatingPoint(), models={"U1": model}))
    assert "NOSUCHPIN" in str(exc.value)


def test_crystal_is_unsimulatable_without_a_model():
    spec = parse_circuit_spec(
        {
            "passives": {
                "Y1": {"type": "crystal", "value": "8MHz"},
                "R1": {"type": "resistor", "value": "1k"},
            },
            "nets": {"XA": ["Y1.1", "R1.1"], "GND": ["Y1.2", "R1.2"]},
        }
    )
    with pytest.raises(UnsimulatableError) as exc:
        build_deck(spec, Testbench(analysis=OperatingPoint()))
    assert "Y1" in str(exc.value)
    assert "motional" in str(exc.value)


def test_diode_without_a_model_warns_about_the_substitution():
    spec = parse_circuit_spec(
        {
            "passives": {
                "D1": {"type": "diode", "value": "1N4148"},
                "R1": {"type": "resistor", "value": "1k"},
            },
            "nets": {"VIN": ["D1.1", "R1.1"], "GND": ["D1.2", "R1.2"]},
        }
    )
    deck = build_deck(spec, Testbench(analysis=OperatingPoint()))
    assert any("1N4148" in w for w in deck.warnings)
    assert "SS_GENERIC_D" in deck.text


def test_strict_testbench_turns_that_warning_into_an_error():
    """An agent verifying against a specification must not accept a result
    about a substituted part."""
    spec = parse_circuit_spec(
        {
            "passives": {
                "D1": {"type": "diode", "value": "1N4148"},
                "R1": {"type": "resistor", "value": "1k"},
            },
            "nets": {"VIN": ["D1.1", "R1.1"], "GND": ["D1.2", "R1.2"]},
        }
    )
    with pytest.raises(DeckError):
        build_deck(spec, Testbench(analysis=OperatingPoint(), strict=True))


def test_supplied_diode_model_is_used_and_emitted_once():
    spec = parse_circuit_spec(
        {
            "passives": {
                "D1": {"type": "diode", "value": "1N4148"},
                "D2": {"type": "diode", "value": "1N4148"},
                "R1": {"type": "resistor", "value": "1k"},
            },
            "nets": {
                "VIN": ["D1.1", "R1.1"],
                "MID": ["D1.2", "D2.1"],
                "GND": ["D2.2", "R1.2"],
            },
        }
    )
    model = PrimitiveModel(name="D1N4148", text=".model D1N4148 D(IS=2.52n N=1.752)")
    deck = build_deck(
        spec,
        Testbench(
            analysis=OperatingPoint(), models={"D1": model, "D2": model}
        ),
    )
    assert deck.text.count(".model D1N4148") == 1
    assert not deck.warnings


def test_unconnected_device_pin_is_warned_not_silently_grounded():
    spec = parse_circuit_spec(
        {
            "devices": {"U1": {"pins": {"A": "1", "B": "2", "NC": "3"}}},
            "passives": {"R1": {"type": "resistor", "value": "1k"}},
            "nets": {"NA": ["U1.A", "R1.1"], "GND": ["U1.B", "R1.2"]},
        }
    )
    model = SubcircuitModel(
        name="X", pins=("A", "B", "NC"), text=".subckt X a b nc\n.ends"
    )
    deck = build_deck(
        spec, Testbench(analysis=OperatingPoint(), models={"U1": model})
    )
    assert any("NC" in w and "unconnected" in w for w in deck.warnings)
    line = next(ln for ln in deck.text.splitlines() if ln.startswith("XU1"))
    assert line.split()[3] != "0", "a floating pin must not be tied to ground"


# ==========================================================================
# rawfile reading
# ==========================================================================


ASCII_RAW = """Title: * test
Date: Sun Aug 30 00:00:00 2026
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tv(out)\tvoltage
Values:
 0\t0.000000000000000e+00
\t0.000000000000000e+00

 1\t1.000000000000000e-06
\t1.500000000000000e+00

 2\t2.000000000000000e-06
\t3.000000000000000e+00
"""


def test_ascii_rawfile_round_trips():
    plot = parse_rawfile(ASCII_RAW.encode("latin-1"))
    assert plot.variables == ("time", "v(out)")
    assert plot.n_points == 3
    assert plot.data["v(out)"] == (0.0, 1.5, 3.0)
    assert not plot.complex_data


COMPLEX_RAW = """Title: * test
Plotname: AC Analysis
Flags: complex
No. Variables: 2
No. Points: 3
Variables:
\t0\tfrequency\tfrequency
\t1\tv(out)\tvoltage
Values:
 0\t1.000000000000000e+00,0.000000000000000e+00
\t1.000000000000000e+00,0.000000000000000e+00

 1\t1.000000000000000e+01,0.000000000000000e+00
\t1.500000000000000e+00,5.000000000000000e-01

 2\t1.000000000000000e+02,0.000000000000000e+00
\t3.000000000000000e+00,-2.000000000000000e+00
"""


def test_ascii_complex_rawfile():
    plot = parse_rawfile(COMPLEX_RAW.encode("latin-1"))
    assert plot.complex_data
    assert plot.data["frequency"][2] == complex(100.0, 0.0)
    assert plot.data["v(out)"][1] == complex(1.5, 0.5)
    assert plot.data["v(out)"][2] == complex(3.0, -2.0)


def test_truncated_rawfile_raises_rather_than_returning_short_data():
    truncated = ASCII_RAW.rsplit("\n 2\t", 1)[0] + "\n"
    with pytest.raises(RawParseError) as exc:
        parse_rawfile(truncated.encode("latin-1"))
    assert "3 points" in str(exc.value)


def test_empty_rawfile_raises():
    with pytest.raises(RawParseError):
        parse_rawfile(b"   \n")


def test_rawfile_with_no_values_section_raises():
    with pytest.raises(RawParseError) as exc:
        parse_rawfile(b"Title: x\nNo. Points: 3\n")
    assert "Values:" in str(exc.value)


def _ltspice_binary(points, *, utf16=True, narrow=True):
    """A rawfile in LTspice's layout: UTF-16LE header, float64 sweep,
    float32 dependents."""
    header = (
        "Title: * ltspice\n"
        "Date: Sun Aug 30 00:00:00 2026\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        "No. Variables: 2\n"
        f"No. Points: {len(points)}\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tV(out)\tvoltage\n"
        "Binary:\n"
    )
    encoding = "utf-16-le" if utf16 else "latin-1"
    body = b""
    for t, v in points:
        body += struct.pack("<d", t)
        body += struct.pack("<f" if narrow else "<d", v)
    return header.encode(encoding) + body


def _ltspice_complex_binary(points):
    """LTspice AC binary: float64 real/imag pair for every variable."""
    header = (
        "Title: * ltspice AC\n"
        "Date: Sun Aug 30 00:00:00 2026\n"
        "Plotname: AC Analysis\n"
        "Flags: complex\n"
        "No. Variables: 2\n"
        f"No. Points: {len(points)}\n"
        "Variables:\n"
        "\t0\tfrequency\tfrequency\n"
        "\t1\tV(out)\tvoltage\n"
        "Binary:\n"
    )
    body = b"".join(
        struct.pack("<dddd", frequency.real, frequency.imag, value.real, value.imag)
        for frequency, value in points
    )
    return header.encode("utf-16-le") + body


def test_ltspice_binary_rawfile_with_utf16_header():
    points = [(0.0, 0.0), (1e-6, 1.5), (2e-6, 3.0)]
    plot = parse_rawfile(_ltspice_binary(points))
    assert plot.variables == ("time", "V(out)")
    assert plot.data["time"] == pytest.approx([t for t, _ in points])
    assert plot.data["V(out)"] == pytest.approx([v for _, v in points], rel=1e-6)


def test_ltspice_binary_complex_values_are_float64_pairs():
    points = [
        (complex(10.0, 0.0), complex(0.5, -0.25)),
        (complex(100.0, 0.0), complex(-1.25, 2.5)),
        (complex(1000.0, 0.0), complex(3.0, -4.0)),
    ]
    plot = parse_rawfile(_ltspice_complex_binary(points))
    assert plot.complex_data
    assert plot.data["frequency"] == pytest.approx([p[0] for p in points])
    assert plot.data["V(out)"] == pytest.approx([p[1] for p in points])


def test_truncated_ltspice_binary_complex_rawfile_raises():
    blob = _ltspice_complex_binary(
        [(complex(10.0), complex(1.0)), (complex(100.0), complex(0.5))]
    )
    with pytest.raises(RawParseError) as exc:
        parse_rawfile(blob[:-8])
    assert "complex points" in str(exc.value)


def test_ngspice_binary_rawfile_uses_float64_throughout():
    """Reading ngspice's all-float64 body with LTspice's narrowed layout would
    not fail -- it would return plausible garbage -- so the widths are driven by
    the header, and this pins that."""
    points = [(0.0, 0.0), (1e-6, 1.5), (2e-6, 3.0)]
    header = (
        "Title: * ngspice\n"
        "Command: ngspice-47\n"
        "Plotname: Transient Analysis\n"
        "Flags: real\n"
        "No. Variables: 2\n"
        "No. Points: 3\n"
        "Variables:\n"
        "\t0\ttime\ttime\n"
        "\t1\tv(out)\tvoltage\n"
        "Binary:\n"
    ).encode("latin-1")
    body = b"".join(struct.pack("<dd", t, v) for t, v in points)
    plot = parse_rawfile(header + body)
    assert plot.data["v(out)"] == pytest.approx([0.0, 1.5, 3.0])


def test_binary_rawfile_shorter_than_declared_raises():
    blob = _ltspice_binary([(0.0, 0.0), (1e-6, 1.0), (2e-6, 2.0)])
    with pytest.raises(RawParseError) as exc:
        parse_rawfile(blob[:-6])
    assert "bytes" in str(exc.value)


# ==========================================================================
# results
# ==========================================================================


def test_signals_come_back_in_circuit_net_names():
    result = synthetic(
        ("time", "v(vout_1)"),
        {"time": (0.0, 1.0), "v(vout_1)": (0.0, 5.0)},
        net_of_node={"vout_1": "VOUT+"},
    )
    assert "v(VOUT+)" in result.names()
    assert result.signal("VOUT+") == (0.0, 5.0)


@pytest.mark.parametrize("name", ["vout", "VOUT", "v(vout)", "V(VOUT)"])
def test_signal_lookup_is_forgiving_about_form(name):
    result = ramp_result()
    assert len(result.signal(name)) == 101


def test_unknown_signal_raises_and_says_what_exists():
    result = ramp_result()
    with pytest.raises(MeasurementError) as exc:
        result.signal("VCC")
    assert "v(vout)" in str(exc.value)


def test_to_dict_is_json_safe_and_omits_waveforms_by_default():
    payload = ramp_result().to_dict()
    assert payload["signals"]["v(vout)"]["points"] == 101
    assert "values" not in payload["signals"]["v(vout)"]
    assert payload["signals"]["v(vout)"]["max"] == pytest.approx(5.0, rel=1e-2)


def test_to_dict_can_downsample_waveforms():
    payload = ramp_result().to_dict(max_points=10)
    assert len(payload["signals"]["v(vout)"]["values"]) <= 10


# ==========================================================================
# measurements
# ==========================================================================


def test_basic_statistics_against_hand_computed_values():
    result = synthetic(
        ("time", "v(a)"),
        {"time": (0.0, 1.0, 2.0, 3.0), "v(a)": (1.0, 3.0, -1.0, 5.0)},
    )
    m = lambda kind, **kw: measure(result, Measurement(kind=kind, signal="a", **kw))  # noqa: E731
    assert m("max") == 5.0
    assert m("min") == -1.0
    assert m("peak_to_peak") == 6.0
    assert m("initial") == 1.0
    assert m("final") == 5.0
    assert m("mean") == pytest.approx(2.0)
    assert m("rms") == pytest.approx(math.sqrt((1 + 9 + 1 + 25) / 4))
    assert m("abs_max") == 5.0


def test_negative_voltages_stay_negative():
    """Regression: real data must not be passed through ``abs``.

    A node sitting at -5 V measured as +5 V would report the wrong ``min`` and
    would sail through an absolute-maximum check it actually violates.
    """
    result = synthetic(
        ("time", "v(neg)"),
        {"time": (0.0, 1.0, 2.0), "v(neg)": (0.0, -5.0, -3.0)},
    )
    assert measure(result, Measurement(kind="min", signal="neg")) == -5.0
    assert measure(result, Measurement(kind="final", signal="neg")) == -3.0
    assert measure(result, Measurement(kind="mean", signal="neg")) < 0
    # abs_max is the one kind that is explicitly about magnitude.
    assert measure(result, Measurement(kind="abs_max", signal="neg")) == 5.0


def test_window_restricts_the_measurement():
    result = synthetic(
        ("time", "v(a)"),
        {"time": (0.0, 1.0, 2.0, 3.0), "v(a)": (1.0, 3.0, -1.0, 5.0)},
    )
    assert measure(
        result, Measurement(kind="max", signal="a", window=(0.0, 1.5))
    ) == 3.0


def test_value_at_interpolates_between_samples():
    result = synthetic(
        ("time", "v(a)"), {"time": (0.0, 1.0), "v(a)": (0.0, 10.0)}
    )
    assert measure(
        result, Measurement(kind="value_at", signal="a", at=0.25)
    ) == pytest.approx(2.5)


def test_value_at_outside_the_simulated_range_raises():
    result = ramp_result()
    with pytest.raises(MeasurementError) as exc:
        measure(result, Measurement(kind="value_at", signal="vout", at=1.0))
    assert "outside the simulated range" in str(exc.value)


def test_rise_time_of_an_exponential_matches_tau_ln_9():
    """10-90% rise of a first-order step is ``tau * ln(9)``.

    The rails come from the window's min and max, and a truncated exponential
    stops just short of its asymptote, so the exact expectation is derived by
    inverting ``v(t) = A(1 - e^(-t/tau))`` at the two thresholds the code
    actually uses. Run long enough, that expression tends to ``tau * ln(9)``,
    and the second assertion pins that it does.
    """
    amplitude, t_stop, points = 5.0, 1e-3, 20001
    tau = t_stop / 10.0
    times = tuple(t_stop * i / (points - 1) for i in range(points))
    values = tuple(amplitude * (1.0 - math.exp(-t / tau)) for t in times)
    result = synthetic(("time", "v(out)"), {"time": times, "v(out)": values})
    measured = measure(result, Measurement(kind="rise_time", signal="out"))

    top = values[-1]  # the high rail the code will pick
    # t at which v = k is -tau * ln(1 - k/amplitude).
    expected = tau * math.log(
        (1.0 - 0.1 * top / amplitude) / (1.0 - 0.9 * top / amplitude)
    )
    assert measured == pytest.approx(expected, rel=1e-3)
    assert measured == pytest.approx(tau * math.log(9.0), rel=1e-3)


def test_fall_time_is_symmetric_with_rise_time():
    points = 2001
    times = tuple(1e-3 * i / (points - 1) for i in range(points))
    values = tuple(5.0 * math.exp(-t / 2e-4) for t in times)
    result = synthetic(("time", "v(out)"), {"time": times, "v(out)": values})
    measured = measure(result, Measurement(kind="fall_time", signal="out"))
    top, bottom = values[0], values[-1]
    span = top - bottom
    expected = 2e-4 * math.log(
        (bottom + 0.9 * span) / (bottom + 0.1 * span)
    )
    assert measured == pytest.approx(expected, rel=1e-3)


def test_rise_time_of_a_flat_signal_raises_rather_than_returning_zero():
    """A zero here reads as an infinitely fast edge -- the exact silent-success
    failure this package is built to avoid."""
    result = synthetic(
        ("time", "v(a)"), {"time": (0.0, 1.0, 2.0), "v(a)": (2.5, 2.5, 2.5)}
    )
    with pytest.raises(MeasurementError) as exc:
        measure(result, Measurement(kind="rise_time", signal="a"))
    assert "flat" in str(exc.value)


def test_settling_time_finds_the_last_excursion():
    times = tuple(float(i) for i in range(11))
    # Settles at 1.0 from t=5 onward; before that it is well outside the band.
    values = (0.0, 2.0, 0.5, 1.5, 0.6, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    result = synthetic(("time", "v(a)"), {"time": times, "v(a)": values})
    settled = measure(
        result, Measurement(kind="settling_time", signal="a", tolerance=0.01)
    )
    assert settled == pytest.approx(5.0)


def test_settling_time_raises_when_it_never_settles():
    times = tuple(float(i) for i in range(6))
    values = (0.0, 5.0, 0.0, 5.0, 0.0, 5.0)
    result = synthetic(("time", "v(a)"), {"time": times, "v(a)": values})
    with pytest.raises(MeasurementError) as exc:
        measure(result, Measurement(kind="settling_time", signal="a"))
    assert "not settled" in str(exc.value)


def test_gain_db_against_a_reference():
    result = synthetic(
        ("frequency", "v(out)", "v(in)"),
        {
            "frequency": (1.0, 10.0),
            "v(out)": (complex(0.5, 0.0), complex(0.1, 0.0)),
            "v(in)": (complex(1.0, 0.0), complex(1.0, 0.0)),
        },
        analysis="ac",
    )
    gain = measure(
        result,
        Measurement(kind="gain_db", signal="out", reference="in", at=1.0),
    )
    assert gain == pytest.approx(20 * math.log10(0.5))


def test_bandwidth_3db_of_a_synthetic_first_order_response():
    fc = 1591.5494
    freqs = tuple(10 ** (i / 100.0) for i in range(100, 601))
    mags = tuple(
        complex(1.0 / math.sqrt(1 + (f / fc) ** 2), 0.0) for f in freqs
    )
    result = synthetic(
        ("frequency", "v(out)"),
        {"frequency": freqs, "v(out)": mags},
        analysis="ac",
    )
    corner = measure(result, Measurement(kind="bandwidth_3db", signal="out"))
    assert corner == pytest.approx(fc, rel=0.02)


def test_bandwidth_3db_aligns_a_reference_to_the_same_window():
    freqs = (1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0)
    # Deliberately non-geometric: pairing the windowed numerator with the
    # unwindowed reference prefix produces a false corner near 13 Hz.
    reference = (1.0, 10.0, 2.0, 8.0, 3.0, 20.0, 4.0, 5.0)
    ratios = (1.0, 1.0, 1.0, 1.0, 0.9, 1 / math.sqrt(2.0), 0.4, 0.2)
    output = tuple(
        complex(ref * ratio)
        for ref, ratio in zip(reference, ratios, strict=True)
    )
    result = synthetic(
        ("frequency", "v(out)", "v(in)"),
        {
            "frequency": freqs,
            "v(out)": output,
            "v(in)": tuple(complex(value) for value in reference),
        },
        analysis="ac",
    )
    corner = measure(
        result,
        Measurement(
            kind="bandwidth_3db",
            signal="out",
            reference="in",
            window=(10.0, 160.0),
        ),
    )
    assert corner == pytest.approx(40.0)


def test_bandwidth_3db_rejects_a_zero_reference():
    result = synthetic(
        ("frequency", "v(out)", "v(in)"),
        {
            "frequency": (10.0, 20.0, 40.0),
            "v(out)": (complex(1.0), complex(0.8), complex(0.5)),
            "v(in)": (complex(1.0), complex(0.0), complex(1.0)),
        },
        analysis="ac",
    )
    with pytest.raises(MeasurementError) as exc:
        measure(
            result,
            Measurement(kind="bandwidth_3db", signal="out", reference="in"),
        )
    assert "zero at 20" in str(exc.value)


def test_bandwidth_3db_requires_an_ac_result():
    with pytest.raises(MeasurementError) as exc:
        measure(ramp_result(), Measurement(kind="bandwidth_3db", signal="vout"))
    assert "AC sweep" in str(exc.value)


def test_bandwidth_3db_raises_when_the_sweep_is_too_narrow():
    freqs = (1.0, 2.0, 3.0)
    mags = tuple(complex(1.0, 0.0) for _ in freqs)
    result = synthetic(
        ("frequency", "v(out)"), {"frequency": freqs, "v(out)": mags}, analysis="ac"
    )
    with pytest.raises(MeasurementError) as exc:
        measure(result, Measurement(kind="bandwidth_3db", signal="out"))
    assert "widen the sweep" in str(exc.value)


def test_unknown_measurement_kind_raises():
    with pytest.raises(MeasurementError) as exc:
        measure(ramp_result(), Measurement(kind="vibes", signal="vout"))
    assert "vibes" in str(exc.value)


# ==========================================================================
# assertions
# ==========================================================================


def test_passing_and_failing_clauses_carry_the_measured_number():
    result = ramp_result()
    report = check_all(
        result,
        [
            Assertion(
                name="reaches 5 V",
                measurement=Measurement(kind="max", signal="vout"),
                op="within",
                value=5.0,
                tolerance=0.02,
                unit="V",
            ),
            Assertion(
                name="stays under 1 V",
                measurement=Measurement(kind="max", signal="vout"),
                op="<",
                value=1.0,
                unit="V",
            ),
        ],
    )
    assert not report.passed
    assert len(report.failures) == 1
    passed, failed = report.outcomes
    assert passed.passed and passed.measured == pytest.approx(5.0, rel=1e-2)
    assert not failed.passed
    assert failed.measured == pytest.approx(5.0, rel=1e-2)
    assert "stays under 1 V" in report.summary()


def test_all_passing_gives_a_passing_report():
    report = check_all(
        ramp_result(),
        [
            Assertion(
                name="never negative",
                measurement=Measurement(kind="min", signal="vout"),
                op=">=",
                value=0.0,
            )
        ],
    )
    assert report.passed
    assert report.summary() == "1/1 assertions passed"


def test_margin_is_signed_and_positive_when_the_clause_holds():
    report = check_all(
        ramp_result(),
        [
            Assertion(
                name="under 6 V",
                measurement=Measurement(kind="max", signal="vout"),
                op="<",
                value=6.0,
            ),
            Assertion(
                name="under 1 V",
                measurement=Measurement(kind="max", signal="vout"),
                op="<",
                value=1.0,
            ),
        ],
    )
    ok, bad = report.outcomes
    assert ok.margin > 0
    assert bad.margin < 0


def test_an_unmeasurable_clause_fails_rather_than_passing_vacuously():
    report = check_all(
        ramp_result(),
        [
            Assertion(
                name="rise time of a node that was never probed",
                measurement=Measurement(kind="rise_time", signal="VCC"),
                op="<",
                value=1e-6,
            )
        ],
    )
    assert not report.passed
    outcome = report.outcomes[0]
    assert outcome.measured is None
    assert outcome.error is not None and "VCC" in outcome.error


def test_unknown_operator_fails_loudly():
    report = check_all(
        ramp_result(),
        [
            Assertion(
                name="nonsense",
                measurement=Measurement(kind="max", signal="vout"),
                op="≈",
                value=5.0,
            )
        ],
    )
    assert not report.passed
    assert "unknown operator" in report.outcomes[0].error


def test_absolute_versus_fractional_tolerance():
    result = synthetic(("time", "v(a)"), {"time": (0.0, 1.0), "v(a)": (0.0, 1.05)})
    fractional = check_all(
        result,
        [
            Assertion(
                name="f",
                measurement=Measurement(kind="final", signal="a"),
                op="within",
                value=1.0,
                tolerance=0.01,
            )
        ],
    )
    absolute = check_all(
        result,
        [
            Assertion(
                name="a",
                measurement=Measurement(kind="final", signal="a"),
                op="within",
                value=1.0,
                tolerance=0.1,
                absolute_tolerance=True,
            )
        ],
    )
    assert not fractional.passed  # 5% off, 1% allowed
    assert absolute.passed  # 0.05 off, 0.1 allowed


def test_report_to_dict_is_json_safe():
    import json

    report = check_all(
        ramp_result(),
        [
            Assertion(
                name="x",
                measurement=Measurement(kind="max", signal="vout"),
                op="<",
                value=6.0,
            )
        ],
    )
    payload = report.to_dict()
    assert json.loads(json.dumps(payload))["passed"] is True


# ==========================================================================
# simulator selection
# ==========================================================================


def test_ngspice_render_puts_the_analysis_in_a_control_block():
    deck = build_deck(
        rc_spec(), Testbench(analysis=Transient(step=1e-6, stop=1e-3))
    )
    text = NgspiceSimulator().render(deck)
    assert ".control" in text
    assert "tran 1e-06 0.001 0" in text
    assert "set filetype=ascii" in text
    assert text.rstrip().endswith(".end")


def test_ltspice_render_uses_a_bare_analysis_card():
    """LTspice has no control block, so the analysis must be a card."""
    deck = build_deck(
        rc_spec(), Testbench(analysis=Transient(step=1e-6, stop=1e-3))
    )
    text = LTspiceSimulator(executable="/nonexistent").render(deck)
    assert ".control" not in text
    assert ".tran 1e-06 0.001 0" in text


def test_find_simulator_rejects_an_unknown_name():
    with pytest.raises(SimulatorNotFound) as exc:
        find_simulator("hspice")
    assert "hspice" in str(exc.value)


def test_missing_simulator_raises_with_what_was_tried():
    deck = build_deck(rc_spec(), Testbench(analysis=OperatingPoint()))
    with pytest.raises(SimulatorNotFound) as exc:
        NgspiceSimulator(executable="/nonexistent/ngspice").run(deck)
    assert "/nonexistent/ngspice" in str(exc.value)
    assert "install" in str(exc.value).lower()


class _StubSimulator:
    """A simulator that returns a well-formed but empty plot.

    Stands in for the class of bug where something upstream succeeds and hands
    back nothing; :func:`simulate_deck` must refuse it rather than pass an empty
    result to a caller.
    """

    name = "stub"

    def is_available(self):
        return True

    def run(self, deck, *, timeout_s=60.0):
        from silkscreen.spice.raw import RawPlot
        from silkscreen.spice.simulators import RunOutcome

        return RunOutcome(
            plot=RawPlot(
                title="", plotname="", flags=(), variables=(), data={}
            ),
            log="",
            warnings=(),
            simulator="stub",
            deck_text="",
        )


def test_a_result_with_no_variables_is_refused():
    from silkscreen.spice import simulate_deck

    deck = build_deck(rc_spec(), Testbench(analysis=OperatingPoint()))
    with pytest.raises(SimulationFailed) as exc:
        simulate_deck(deck, simulator=_StubSimulator())
    assert "no variables" in str(exc.value)


# ==========================================================================
# end to end -- these need a real simulator
# ==========================================================================


@needs_ngspice
def test_operating_point_of_a_divider_matches_ohms_law():
    spec = divider_spec()
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
    )
    result = simulate(spec, bench)
    # Independent arithmetic: 10k over 10k from 5 V.
    expected = 5.0 * 10e3 / (10e3 + 10e3)
    assert result.signal("VMID")[0] == pytest.approx(expected, rel=1e-6)
    assert result.signal("VIN")[0] == pytest.approx(5.0, rel=1e-9)


@needs_ngspice
def test_rc_lowpass_matches_closed_form():
    """The narrow slice, verified end to end.

    A first-order RC low-pass has answers that need no simulator: the 10-90%
    rise time is ``tau * ln(9)``, the corner is ``1 / (2 pi tau)``, and the
    response rolls off 20 dB per decade above it. Every expectation below is
    computed from those formulas, not from a previous run of this code.
    """
    spec = rc_spec()
    # 1 k in series, loaded by 1 M, into 100 nF.
    r_eff = 1e3 * 1e6 / (1e3 + 1e6)
    tau = r_eff * 100e-9
    expected_rise = tau * math.log(9.0)
    expected_final = 5.0 * 1e6 / (1e6 + 1e3)
    expected_fc = 1.0 / (2 * math.pi * tau)

    transient = Testbench(
        analysis=Transient(step=1e-6, stop=2e-3),
        sources=[
            Source.pulse(
                "V1", "VIN", "GND",
                initial=0.0, pulsed=5.0, width=1e-3, period=2e-3,
            )
        ],
    )
    report = verify(
        spec,
        transient,
        [
            Assertion(
                name="settles to the divided input",
                measurement=Measurement(
                    kind="max", signal="VOUT", window=(0.0, 1e-3)
                ),
                op="within",
                value=expected_final,
                tolerance=0.01,
                unit="V",
            ),
            Assertion(
                name="10-90% rise time is tau*ln(9)",
                measurement=Measurement(
                    kind="rise_time", signal="VOUT", window=(0.0, 1e-3)
                ),
                op="within",
                value=expected_rise,
                tolerance=0.02,
                unit="s",
            ),
            Assertion(
                name="never exceeds the 5.5 V absolute maximum",
                measurement=Measurement(kind="abs_max", signal="VOUT"),
                op="<",
                value=5.5,
                unit="V",
            ),
        ],
    )
    assert report.passed, report.summary()

    ac = Testbench(
        analysis=ACSweep(f_start=1.0, f_stop=1e6, points=50),
        sources=[Source.ac_probe("V1", "VIN", "GND", magnitude=1.0)],
    )
    ac_report = verify(
        spec,
        ac,
        [
            Assertion(
                name="passband gain is unity",
                measurement=Measurement(kind="gain_db", signal="VOUT", at=10.0),
                op="within",
                value=0.0,
                tolerance=0.05,
                absolute_tolerance=True,
                unit="dB",
            ),
            Assertion(
                name="-3 dB corner is 1/(2*pi*R*C)",
                measurement=Measurement(kind="bandwidth_3db", signal="VOUT"),
                op="within",
                value=expected_fc,
                tolerance=0.03,
                unit="Hz",
            ),
            Assertion(
                name="rolls off 20 dB in the decade above the corner",
                measurement=Measurement(
                    kind="gain_db", signal="VOUT", at=expected_fc * 10
                ),
                op="<",
                value=-19.0,
                unit="dB",
            ),
        ],
    )
    assert ac_report.passed, ac_report.summary()


@needs_ngspice
def test_a_failing_specification_reports_which_clause_and_by_how_much():
    """The feedback an agent iterates on."""
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
    )
    report = verify(
        divider_spec(),
        bench,
        [
            Assertion(
                name="midpoint is 3.3 V",
                measurement=Measurement(kind="final", signal="VMID"),
                op="within",
                value=3.3,
                tolerance=0.05,
                unit="V",
            )
        ],
    )
    assert not report.passed
    outcome = report.outcomes[0]
    assert outcome.measured == pytest.approx(2.5, rel=1e-6)
    # The divider is 0.8 V below the spec, and the report says so.
    assert outcome.margin == pytest.approx(-0.8, abs=1e-6)


@needs_ngspice
def test_ac_analysis_returns_complex_data_in_net_names():
    bench = Testbench(
        analysis=ACSweep(f_start=10.0, f_stop=1e5, points=10),
        sources=[Source.ac_probe("V1", "VIN", "GND")],
    )
    result = simulate(rc_spec(), bench)
    assert result.analysis == "ac"
    assert result.complex_data
    assert "v(VOUT)" in result.names()
    assert result.sweep[0] == pytest.approx(10.0, rel=1e-6)


@needs_ngspice
def test_a_missing_model_surfaces_the_simulator_error_not_an_empty_result():
    """ngspice writes no rawfile at all here; the failure must not look like a
    circuit that simply did nothing."""
    spec = parse_circuit_spec(
        {
            "passives": {
                "D1": {"type": "diode", "value": "1N4148"},
                "R1": {"type": "resistor", "value": "1k"},
            },
            "nets": {"VIN": ["D1.1", "R1.1"], "GND": ["D1.2", "R1.2"]},
        }
    )
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
        # A model card that never defines the model it names.
        models={"D1": PrimitiveModel(name="NOSUCH", text="* no model here")},
    )
    with pytest.raises(SimulationFailed) as exc:
        simulate(spec, bench)
    assert "nosuch" in str(exc.value).lower()
    assert exc.value.log, "the simulator's own output must be preserved"
    assert not isinstance(exc.value, ConvergenceError)


@needs_ngspice
def test_deck_warnings_travel_all_the_way_to_the_result():
    spec = parse_circuit_spec(
        {
            "passives": {
                "D1": {"type": "diode", "value": "1N4148"},
                "R1": {"type": "resistor", "value": "1k"},
            },
            "nets": {"VIN": ["D1.1", "R1.1"], "GND": ["D1.2", "R1.2"]},
        }
    )
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
    )
    result = simulate(spec, bench)
    assert any("1N4148" in w for w in result.warnings)


@needs_ngspice
def test_oversized_output_is_refused_rather_than_loaded():
    """A too-fine transient is a hundred megabytes and a wedged caller, not a
    hang -- so it is bounded and reported with the actual size."""
    deck = build_deck(
        rc_spec(),
        Testbench(
            analysis=Transient(step=1e-9, stop=1e-3),
            sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
        ),
    )
    simulator = NgspiceSimulator(max_raw_bytes=64 * 1024)
    with pytest.raises(SimulationFailed) as exc:
        simulator.run(deck, timeout_s=120)
    assert "cap" in str(exc.value)


@needs_ngspice
def test_the_deck_that_ran_is_attached_to_the_result():
    """Reproducing a result by hand is a normal thing to want."""
    bench = Testbench(
        analysis=OperatingPoint(),
        sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
    )
    result = simulate(divider_spec(), bench)
    assert "R1" in result.deck and ".control" in result.deck
    assert result.simulator.startswith("ngspice")


def test_spice_refs_match_the_schematic_and_board_refs():
    """The deck, the schematic and the board must agree on what ``C1`` is.

    All three emitters number parts through ``CircuitSpec.assign_refs`` rather
    than counting themselves. If they drifted, a simulation result would be
    about a part the person reading the schematic cannot find -- three files
    describing different circuits while all looking plausible.
    """
    from silkscreen.schematic import build_schematic

    spec = rc_spec()
    deck = build_deck(spec, Testbench(analysis=OperatingPoint()))
    schematic = build_schematic(spec)

    schematic_refs = {symbol.ref for symbol in schematic.symbols}
    assert schematic_refs == set(deck.refs.values())
    # And the deck actually uses them as element names.
    for ref in schematic_refs:
        assert any(
            line.startswith(ref + " ") for line in deck.text.splitlines()
        ), f"{ref} is in the schematic but not in the SPICE deck"


def test_an_uppercase_m_warns_even_when_a_unit_word_follows():
    """"1MOhm" is 1 milliohm to SPICE, and that must never pass silently.

    Both scale guards used to require that nothing followed the suffix, so
    the exact spelling most likely to be written by mistake -- the one that
    carries its unit -- was the one that produced no warning. A 1 M pull-up
    became a 1 mOhm short and every clause was then measured on a different
    circuit, passing confidently.
    """
    from silkscreen.spice.values import parse_value

    value, warning = parse_value("1MOhm", part="R1")
    assert value == 0.001
    assert warning is not None and "milli" in warning

    value, warning = parse_value("2.2MR", part="R2")
    assert value == 0.0022
    assert warning is not None


def test_a_lower_case_milli_with_a_unit_is_not_nagged():
    """"1mF" is a millifarad and is exactly what it says.

    Case is the signal: SPICE reads M and m alike, so a writer who typed the
    upper-case one meant mega. Warning on the lower-case one would be noise,
    and noise is how a real warning gets ignored.
    """
    from silkscreen.spice.values import parse_value

    assert parse_value("1mF", part="C1") == (0.001, None)
    assert parse_value("1fF", part="C2") == (1e-15, None)
    assert parse_value("1MEG", part="R1") == (1_000_000.0, None)


# ==========================================================================
# periodic measurements
# ==========================================================================


def square_result(period=1e-3, duty=0.5, cycles=8, points_per_cycle=200,
                  low=0.0, high=5.0):
    """A clean square wave with an exactly known period and duty cycle."""
    times = []
    values = []
    total = cycles * points_per_cycle
    for index in range(total + 1):
        t = period * index / points_per_cycle
        phase = (t % period) / period
        times.append(t)
        values.append(high if phase < duty else low)
    return synthetic(("time", "v(out)"), {"time": tuple(times),
                                          "v(out)": tuple(values)})


def test_period_of_a_square_wave_is_its_period():
    result = square_result(period=1e-3)
    assert measure(result, Measurement(kind="period", signal="out")) == (
        pytest.approx(1e-3, rel=1e-3)
    )
    assert measure(result, Measurement(kind="frequency", signal="out")) == (
        pytest.approx(1e3, rel=1e-3)
    )


def test_period_of_a_sine_matches_one_over_its_frequency():
    """Computed from the sine's own argument, not from a previous run."""
    frequency = 250.0
    points = 4001
    stop = 8.0 / frequency
    times = tuple(stop * i / (points - 1) for i in range(points))
    values = tuple(math.sin(2 * math.pi * frequency * t) for t in times)
    result = synthetic(("time", "v(a)"), {"time": times, "v(a)": values})
    measured = measure(result, Measurement(kind="period", signal="a"))
    assert measured == pytest.approx(1.0 / frequency, rel=1e-3)


@pytest.mark.parametrize("duty", [0.25, 0.5, 0.75])
def test_duty_cycle_is_a_fraction_not_a_percentage(duty):
    result = square_result(duty=duty, points_per_cycle=400)
    measured = measure(result, Measurement(kind="duty_cycle", signal="out"))
    assert measured == pytest.approx(duty, abs=0.01)


def test_period_of_a_flat_signal_raises_rather_than_returning_zero():
    result = synthetic(("time", "v(a)"), {"time": (0.0, 1.0, 2.0),
                                          "v(a)": (3.0, 3.0, 3.0)})
    with pytest.raises(MeasurementError) as exc:
        measure(result, Measurement(kind="period", signal="a"))
    assert "does not oscillate" in str(exc.value)


def test_period_of_less_than_one_cycle_raises_and_says_so():
    result = square_result(period=1e-3, cycles=1)
    with pytest.raises(MeasurementError) as exc:
        measure(result, Measurement(kind="period", signal="a" if False else "out",
                                    window=(0.0, 6e-4)))
    assert "at least two" in str(exc.value)


# ==========================================================================
# the model registry
# ==========================================================================


LDO_BOARD = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "c_in": {"type": "capacitor", "value": "22uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
        "r_load": {"type": "resistor", "value": "330"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
        "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2", "r_load.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "c_out.1", "r_load.1"],
    },
}

BLINKER_555 = {
    "devices": {
        "NE555": {"pins": {"GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                           "CTRL": "5", "THRES": "6", "DISCH": "7", "VCC": "8"}}
    },
    "passives": {
        "R1": {"type": "resistor", "value": "10k"},
        "R2": {"type": "resistor", "value": "10k"},
        "C1": {"type": "capacitor", "value": "100nF"},
        "C2": {"type": "capacitor", "value": "10nF"},
        "Rled": {"type": "resistor", "value": "330"},
    },
    "nets": {
        "VCC": ["NE555.VCC", "NE555.RESET", "R1.1"],
        "DIS": ["NE555.DISCH", "R1.2", "R2.1"],
        "THR": ["NE555.THRES", "NE555.TRIG", "R2.2", "C1.1"],
        "CTRL": ["NE555.CTRL", "C2.1"],
        "OUT": ["NE555.OUT", "Rled.1"],
        "GND": ["NE555.GND", "C1.2", "C2.2", "Rled.2"],
    },
}

#: A non-inverting amplifier around one half of a dual op-amp. The unused half
#: is a unity buffer on the same input, which is what a real board does with it.
AMP_LM358 = {
    "devices": {
        "LM358": {"pins": {"OUT1": "1", "IN1-": "2", "IN1+": "3", "GND": "4",
                           "IN2+": "5", "IN2-": "6", "OUT2": "7", "VCC": "8"}}
    },
    "passives": {
        "Rf": {"type": "resistor", "value": "10k"},
        "Rg": {"type": "resistor", "value": "10k"},
        "Rl": {"type": "resistor", "value": "100k"},
        "Rtop": {"type": "resistor", "value": "100k"},
        "Rbot": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VCC": ["LM358.VCC", "Rtop.1"],
        "GND": ["LM358.GND", "Rg.2", "Rl.2", "Rbot.2"],
        "VIN": ["LM358.IN1+", "Rtop.2", "Rbot.1", "LM358.IN2+"],
        "FB": ["LM358.IN1-", "Rf.2", "Rg.1"],
        "VOUT": ["LM358.OUT1", "Rf.1", "Rl.1"],
        "BUF": ["LM358.OUT2", "LM358.IN2-"],
    },
}

#: The board the registry must still refuse: a motor driver nothing models.
DRIVER_BOARD = {
    "devices": {"DRV8837": {"pins": {"IN1": "1", "IN2": "2", "VM": "3",
                                     "GND": "4", "OUT1": "5", "OUT2": "6",
                                     "VCC": "7", "nSLEEP": "8"}}},
    "passives": {
        "c_dec": {"type": "capacitor", "value": "100nF"},
        "r_sleep": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VM": ["DRV8837.VM", "DRV8837.VCC", "c_dec.1", "r_sleep.1"],
        "GND": ["DRV8837.GND", "c_dec.2"],
        "A": ["DRV8837.IN1", "DRV8837.OUT1"],
        "B": ["DRV8837.IN2", "DRV8837.OUT2"],
        "S": ["DRV8837.nSLEEP", "r_sleep.2"],
    },
}

#: Every registry test builds its own registry with no operator files, so a
#: model directory on the machine running the suite cannot change an answer.
def registry():
    return ModelRegistry(files={})


def bench_for(spec, analysis, sources, registry_=None, **kwargs):
    """A strict testbench carrying whatever the registry covered."""
    resolution = (registry_ or registry()).resolve(spec)
    return Testbench(
        analysis=analysis,
        sources=sources,
        models=resolution.models,
        accept_generic_models=resolution.generic_parts,
        strict=True,
        **kwargs,
    ), resolution


def test_a_regulator_board_is_covered_and_labelled_generic():
    spec = parse_circuit_spec(LDO_BOARD)
    resolution = registry().resolve(spec)
    assert resolution.uncovered == ()
    assert resolution.generic_parts == ("AMS1117-3.3",)
    entry = resolution.resolved[0]
    assert entry.provenance.kind == "generic"
    assert entry.provenance.licence.startswith("MIT")
    # The note names what the model cannot answer, in words.
    assert "no control loop" in entry.provenance.note
    assert "stability" in entry.provenance.note


def test_the_registry_binds_the_regulator_in_terminal_order():
    """GND, VOUT, VIN -- the subcircuit's order, not the pin numbering."""
    spec = parse_circuit_spec(LDO_BOARD)
    model = registry().resolve(spec).models["AMS1117-3.3"]
    assert model.pins == ("GND", "VOUT", "VIN")
    assert model.generic is True
    assert model.text.splitlines()[2].startswith(".subckt")


def test_a_part_with_no_model_stays_uncovered_and_is_named():
    spec = parse_circuit_spec(DRIVER_BOARD)
    resolution = registry().resolve(spec)
    assert resolution.uncovered == ("DRV8837",)
    assert "DRV8837" in resolution.reasons[0]
    assert "no model" in resolution.reasons[0]


def test_a_microcontroller_is_refused_with_the_reason_a_person_needs():
    spec = parse_circuit_spec({
        "devices": {"ATtiny85": {"pins": {"VCC": "8", "GND": "4",
                                          "PB0": "5", "PB1": "6"}}},
        "passives": {"c_dec": {"type": "capacitor", "value": "100nF"}},
        "nets": {
            "VCC": ["ATtiny85.VCC", "c_dec.1"],
            "GND": ["ATtiny85.GND", "c_dec.2"],
            "L": ["ATtiny85.PB0", "ATtiny85.PB1"],
        },
    })
    resolution = registry().resolve(spec)
    assert resolution.uncovered == ("ATtiny85",)
    assert "firmware" in resolution.reasons[0]


def test_an_adjustable_regulator_is_refused_rather_than_given_a_fixed_rail():
    """LM317's output is set by external resistors. A fixed stand-in would
    simulate a different rail than the board produces, and pass."""
    spec = parse_circuit_spec({
        "devices": {"LM317": {"pins": {"ADJ": "1", "VOUT": "2", "VIN": "3"}}},
        "passives": {
            "r1": {"type": "resistor", "value": "240"},
            "r2": {"type": "resistor", "value": "720"},
        },
        "nets": {
            "VIN": ["LM317.VIN", "r1.1"],
            "OUT": ["LM317.VOUT", "r1.2", "r2.1"],
            "GND": ["LM317.ADJ", "r2.2"],
        },
    })
    resolution = registry().resolve(spec)
    assert resolution.uncovered == ("LM317",)
    assert "external resistors" in resolution.reasons[0]


def test_an_undecodable_output_voltage_refuses_and_names_the_spelling():
    """A manufacturer order code is not decoded: the schemes differ between
    manufacturers and a wrong guess simulates a rail the board does not have."""
    spec = parse_circuit_spec({
        "devices": {"MCP1700-3302": {"pins": {"GND": "1", "VIN": "2",
                                              "VOUT": "3"}}},
        "passives": {
            "c": {"type": "capacitor", "value": "1uF"},
            "r": {"type": "resistor", "value": "1k"},
        },
        "nets": {
            "VIN": ["MCP1700-3302.VIN", "c.1"],
            "GND": ["MCP1700-3302.GND", "c.2", "r.2"],
            "OUT": ["MCP1700-3302.VOUT", "r.1"],
        },
    })
    reason = registry().resolve(spec).reasons[0]
    assert "cannot be read from the part name" in reason
    assert "MCP1700-3.3" in reason


def test_a_crystal_stays_uncovered_with_its_own_reason():
    spec = parse_circuit_spec({
        "passives": {
            "Y1": {"type": "crystal", "value": "8MHz"},
            "C1": {"type": "capacitor", "value": "22pF"},
        },
        "nets": {"XA": ["Y1.1", "C1.1"], "GND": ["Y1.2", "C1.2"]},
    })
    resolution = registry().resolve(spec)
    assert resolution.uncovered == ("Y1",)
    assert "motional" in resolution.reasons[0]


def test_a_pin_map_the_stand_in_cannot_read_refuses_instead_of_guessing():
    """A wrong name refuses. The alternative -- binding by position -- would
    have simulated a different circuit and said nothing."""
    spec = parse_circuit_spec({
        "devices": {"NE555": {"pins": {"GND": "1", "TRIG": "2", "OUT": "3",
                                       "RESET": "4", "CTRL": "5",
                                       "THRESHOLD_PIN": "6", "DISCH": "7",
                                       "VCC": "8"}}},
        "passives": {"C1": {"type": "capacitor", "value": "100nF"}},
        "nets": {
            "VCC": ["NE555.VCC", "NE555.RESET"],
            "GND": ["NE555.GND", "C1.2"],
            "T": ["NE555.TRIG", "NE555.THRESHOLD_PIN", "C1.1"],
            "O": ["NE555.OUT", "NE555.CTRL", "NE555.DISCH"],
        },
    })
    resolution = registry().resolve(spec)
    assert resolution.uncovered == ("NE555",)
    assert "THRES" in resolution.reasons[0]


def test_a_generic_substitution_warns_and_names_its_provenance():
    spec = parse_circuit_spec(LDO_BOARD)
    resolution = registry().resolve(spec)
    deck = build_deck(
        spec,
        Testbench(
            analysis=OperatingPoint(),
            sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
            models=resolution.models,
        ),
    )
    assert len(deck.warnings) == 1
    warning = deck.warnings[0]
    assert "generic stand-in" in warning
    assert "AMS1117-3.3" in warning
    assert "MIT" in warning  # the licence travels with the model


def test_an_undeclared_generic_substitution_is_refused_under_strict():
    """The whole rule: substituting a part *silently* is what strict catches."""
    spec = parse_circuit_spec(LDO_BOARD)
    resolution = registry().resolve(spec)
    with pytest.raises(DeckError) as exc:
        build_deck(
            spec,
            Testbench(
                analysis=OperatingPoint(),
                sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
                models=resolution.models,
                strict=True,
            ),
        )
    assert "promoted to error" in str(exc.value)
    assert "AMS1117-3.3" in str(exc.value)


def test_declaring_the_substitution_permits_it_and_still_discloses_it():
    spec = parse_circuit_spec(LDO_BOARD)
    bench, _ = bench_for(
        spec, OperatingPoint(), [Source.dc_supply("V1", "VIN", "GND", 5.0)]
    )
    deck = build_deck(spec, bench)
    # Accepted, and still warned about: acknowledgement silences the refusal,
    # never the disclosure.
    assert any("generic stand-in" in w for w in deck.warnings)


def test_accepting_a_part_the_circuit_does_not_have_is_an_error():
    """The edge_refs convention: an acknowledgement that names nothing is a
    mistake, not a no-op, because the acknowledgement's whole value is that
    it was checked."""
    spec = parse_circuit_spec(LDO_BOARD)
    with pytest.raises(DeckError) as exc:
        build_deck(
            spec,
            Testbench(
                analysis=OperatingPoint(),
                sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
                strict=True,
                accept_generic_models=("U404",),
            ),
        )
    assert "U404" in str(exc.value)


def test_a_diode_substitution_is_still_refused_under_strict():
    """The pre-existing rule is unchanged by the new one: nothing named the
    diode, so nothing declared it."""
    spec = parse_circuit_spec({
        "passives": {
            "D1": {"type": "diode", "value": "1N4148"},
            "R1": {"type": "resistor", "value": "1k"},
        },
        "nets": {"VIN": ["D1.1", "R1.1"], "GND": ["D1.2", "R1.2"]},
    })
    with pytest.raises(DeckError):
        build_deck(spec, Testbench(analysis=OperatingPoint(), strict=True))


def test_an_operator_model_file_is_found_and_beats_the_stand_in(tmp_path):
    (tmp_path / "vendor.lib").write_text(
        ".SUBCKT AMS1117_3_3 GND VOUT VIN\n"
        "VREF VOUT GND 3.3\n"
        ".ENDS\n"
    )
    found = load_model_files([tmp_path])
    assert set(found) == {"AMS1117_3_3"}
    spec = parse_circuit_spec(LDO_BOARD)
    resolution = ModelRegistry(files=found).resolve(spec)
    entry = resolution.resolved[0]
    assert entry.provenance.kind == "operator"
    assert entry.provenance.generic is False
    assert str(tmp_path) in entry.provenance.source
    assert "has not read, verified or vetted" in entry.provenance.note
    # A real model is not a stand-in, so nothing warns and nothing needs
    # declaring.
    deck = build_deck(
        spec,
        Testbench(
            analysis=OperatingPoint(),
            sources=[Source.dc_supply("V1", "VIN", "GND", 5.0)],
            models=resolution.models,
            strict=True,
        ),
    )
    assert deck.warnings == ()


def test_an_operator_terminal_that_matches_no_pin_refuses_and_names_it(tmp_path):
    (tmp_path / "v.sub").write_text(
        ".subckt AMS1117-3.3 GND VOUT VADJ\n.ends\n"
    )
    spec = parse_circuit_spec(LDO_BOARD)
    device = spec.devices[0]
    reg = ModelRegistry(files=load_model_files([tmp_path]))
    assert reg.model_for(device) is None
    assert "VADJ" in reg.why_unmatched(device)


def test_a_nested_subcircuit_stays_inside_its_parent(tmp_path):
    (tmp_path / "n.lib").write_text(
        ".subckt OUTER A B\n"
        ".subckt INNER C D\nR1 C D 1k\n.ends\n"
        "XI A B INNER\n"
        ".ends\n"
    )
    found = load_model_files([tmp_path])
    assert set(found) == {"OUTER"}
    assert "INNER" in found["OUTER"].text


def test_model_search_paths_reads_the_environment_it_is_given():
    paths = model_search_paths({MODEL_PATH_ENV: f"/a{os.pathsep}/b"})
    assert [str(p) for p in paths] == ["/a", "/b"]
    assert model_search_paths({}) == ()


# ==========================================================================
# end to end with a registry model -- these need a real simulator
# ==========================================================================


@needs_ngspice
def test_the_ldo_board_holds_its_rail_and_obeys_ohms_law():
    """The 3.3 V LDO demo board, verified.

    Both expectations are independent of this code: 3.3 V is the nameplate the
    part number states, and the load current is Ohm's law over the resistor in
    the spec. What the simulation adds is that the topology delivers it -- and
    the verdict is about a *generic* fixed regulator, which is why the deck
    warns and the warning reaches the result.
    """
    spec = parse_circuit_spec(LDO_BOARD)
    bench, resolution = bench_for(
        spec, OperatingPoint(), [Source.dc_supply("V1", "VIN", "GND", 5.0)]
    )
    result = simulate(spec, bench)
    assert any("generic stand-in" in w for w in result.warnings)
    rail = result.signal("+3V3")[0]
    # Nameplate, minus the drop the 330 ohm load makes across the model's own
    # output resistance: 3.3 / (330 + Rout) * Rout. Both numbers are stated
    # constants, not measurements.
    from silkscreen.spice.library import LDO_ROUT_OHM

    expected = 3.3 * 330.0 / (330.0 + LDO_ROUT_OHM)
    assert rail == pytest.approx(expected, rel=1e-4)
    # Ohm's law over the load, which the simulator was never told.
    assert rail / 330.0 == pytest.approx(0.01, rel=2e-3)


@needs_ngspice
def test_the_ldo_board_reports_a_headroom_failure_with_a_signed_margin():
    """Below dropout the rail collapses, and the clause says by how much."""
    spec = parse_circuit_spec(LDO_BOARD)
    bench, _ = bench_for(
        spec, OperatingPoint(), [Source.dc_supply("V1", "VIN", "GND", 3.6)]
    )
    report = verify(spec, bench, [
        Assertion(
            name="3.3 V rail is up",
            measurement=Measurement(kind="final", signal="+3V3"),
            op=">=", value=3.2, unit="V",
        )
    ])
    assert not report.passed
    outcome = report.outcomes[0]
    from silkscreen.spice.library import LDO_DROPOUT_V

    assert outcome.measured == pytest.approx(3.6 - LDO_DROPOUT_V, rel=1e-3)
    assert outcome.margin < 0


@needs_ngspice
def test_the_555_blinker_oscillates_at_the_frequency_its_rc_network_implies():
    """The NE555 blinker demo board, verified against textbook 555 theory.

    ``f = 1.44 / ((R1 + 2*R2) * C)`` and ``D = (R1 + R2) / (R1 + 2*R2)`` are
    the astable's published closed-form results, computed inline here from the
    component values in the spec. Nothing in the expectation comes from a
    previous run of this code. The capacitor swing is a third expectation from
    a different direction entirely: the internal divider puts the thresholds at
    one and two thirds of the supply, so the timing node must swing exactly
    V+/3 whatever the resistors are.
    """
    spec = parse_circuit_spec(BLINKER_555)
    r1, r2, c = 10e3, 10e3, 100e-9
    supply = 5.0
    expected_period = math.log(2.0) * (r1 + 2 * r2) * c
    expected_duty = (r1 + r2) / (r1 + 2 * r2)

    bench, resolution = bench_for(
        spec, Transient(step=1e-6, stop=60e-3),
        [Source.dc_supply("V1", "VCC", "GND", supply)],
    )
    assert resolution.generic_parts == ("NE555",)
    # Skip the first cycles: an RC oscillator's first charge starts from zero.
    window = (10e-3, 60e-3)
    report = verify(spec, bench, [
        Assertion(
            name="period is ln(2)*(R1+2*R2)*C",
            measurement=Measurement(kind="period", signal="OUT", window=window),
            op="within", value=expected_period, tolerance=0.02, unit="s",
        ),
        Assertion(
            name="duty cycle is (R1+R2)/(R1+2*R2)",
            measurement=Measurement(kind="duty_cycle", signal="OUT",
                                    window=window),
            op="within", value=expected_duty, tolerance=0.02,
        ),
        Assertion(
            name="timing node swings a third of the supply",
            measurement=Measurement(kind="peak_to_peak", signal="THR",
                                    window=window),
            op="within", value=supply / 3.0, tolerance=0.02, unit="V",
        ),
    ])
    assert report.passed, report.summary()


@needs_ngspice
def test_the_555_output_never_leaves_its_rails():
    """An absolute-maximum clause, on real data that is never passed through
    abs before the comparison."""
    spec = parse_circuit_spec(BLINKER_555)
    bench, _ = bench_for(
        spec, Transient(step=1e-6, stop=20e-3),
        [Source.dc_supply("V1", "VCC", "GND", 5.0)],
    )
    result = simulate(spec, bench)
    out = result.signal("OUT")
    assert min(out) >= -1e-9 and max(out) <= 5.0


@needs_ngspice
def test_the_lm358_amplifier_has_the_gain_its_feedback_ratio_implies():
    """The op-amp reference board, against closed-loop theory.

    ``1 + Rf/Rg`` for the gain and ``GBW / (1 + Rf/Rg)`` for the closed-loop
    corner are the textbook results for a non-inverting amplifier; the input
    is a resistive divider off the supply, which is Ohm's law. The stand-in's
    own gain-bandwidth is a stated constant of the model, not a measurement,
    which is exactly why this test can be written at all -- and equally why
    the corner it checks is a property of a *generic* amplifier and not of an
    LM358.
    """
    from silkscreen.spice.library import OPAMP_GBW_HZ

    spec = parse_circuit_spec(AMP_LM358)
    supply = 12.0
    v_in = supply * 10e3 / (100e3 + 10e3)
    gain = 1.0 + 10e3 / 10e3

    dc_bench, resolution = bench_for(
        spec, OperatingPoint(), [Source.dc_supply("V1", "VCC", "GND", supply)]
    )
    assert resolution.generic_parts == ("LM358",)
    report = verify(spec, dc_bench, [
        Assertion(
            name="divider sets the input",
            measurement=Measurement(kind="final", signal="VIN"),
            op="within", value=v_in, tolerance=1e-3, unit="V",
        ),
        Assertion(
            name="closed-loop gain is 1 + Rf/Rg",
            measurement=Measurement(kind="final", signal="VOUT"),
            op="within", value=v_in * gain, tolerance=1e-3, unit="V",
        ),
    ])
    assert report.passed, report.summary()

    ac_bench, _ = bench_for(
        spec, ACSweep(f_start=10.0, f_stop=1e7, points=40),
        [Source.dc_supply("V1", "VCC", "GND", supply),
         # A single-supply amplifier biased at 0 V sits on its negative rail and
        # has no small-signal gain at all -- true of the model and true of the
        # part -- so the AC probe carries the bias the divider would.
        Source.ac_probe("V2", "VIN", "GND", magnitude=1.0, dc=v_in)],
    )
    ac_report = verify(spec, ac_bench, [
        Assertion(
            name="closed-loop corner is GBW/(1+Rf/Rg)",
            measurement=Measurement(kind="bandwidth_3db", signal="VOUT"),
            op="within", value=OPAMP_GBW_HZ / gain, tolerance=0.05, unit="Hz",
        )
    ])
    assert ac_report.passed, ac_report.summary()


@needs_ngspice
def test_an_operator_model_actually_reaches_the_simulator(tmp_path):
    """The path that matters for a real manufacturer model: a file on disk,
    no generic anywhere, and a number only that file could produce."""
    (tmp_path / "vendor.sub").write_text(
        ".SUBCKT AMS1117_3_3 GND VOUT VIN\n"
        "BREG VOUT GND V = 2.7\n"
        ".ENDS\n"
    )
    spec = parse_circuit_spec(LDO_BOARD)
    bench, resolution = bench_for(
        spec, OperatingPoint(), [Source.dc_supply("V1", "VIN", "GND", 5.0)],
        registry_=ModelRegistry(files=load_model_files([tmp_path])),
    )
    assert resolution.generic_parts == ()
    result = simulate(spec, bench)
    assert result.warnings == ()
    assert result.signal("+3V3")[0] == pytest.approx(2.7, rel=1e-6)

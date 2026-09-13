"""End-to-end demo: verify a circuit's behaviour with SPICE.

    python scripts/simulate_demo.py

Needs ngspice on PATH (``brew install ngspice`` / ``apt-get install ngspice``).
Nothing else -- no network, no API key, no KiCad, no GUI.

What it shows is the point of the package: the same RC low-pass, checked against
a specification written in the units a datasheet uses, with every expectation
computed from closed-form circuit theory rather than from a previous run. The
last section deliberately fails, because a verifier that can only say yes is not
a verifier.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from silkscreen.netlist import parse_circuit_spec  # noqa: E402
from silkscreen.spice import (  # noqa: E402
    ACSweep,
    Assertion,
    Measurement,
    Source,
    Testbench,
    Transient,
    build_deck,
    find_simulator,
    verify,
)
from silkscreen.spice.errors import SimulatorNotFound  # noqa: E402

CIRCUIT = {
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


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * len(title))


def show(report) -> None:
    for outcome in report.outcomes:
        mark = "PASS" if outcome.passed else "FAIL"
        if outcome.measured is None:
            print(f"  {mark}  {outcome.name}: {outcome.error}")
            continue
        print(
            f"  {mark}  {outcome.name}\n"
            f"        measured {outcome.measured:.6g}{outcome.unit}, "
            f"expected {outcome.op} {outcome.expected:.6g}{outcome.unit} "
            f"(margin {outcome.margin:+.3g})"
        )


def main() -> int:
    try:
        simulator = find_simulator()
    except SimulatorNotFound as exc:
        print(exc)
        return 1

    spec = parse_circuit_spec(CIRCUIT)

    rule("circuit")
    print(f"  {spec.part_count()} parts, {spec.net_count()} nets")
    print(f"  simulator: {simulator.name}")

    rule("generated SPICE netlist")
    deck = build_deck(spec, Testbench(analysis=Transient(step=1e-6, stop=2e-3)))
    for line in deck.text.splitlines():
        print(f"  {line}")

    # Closed-form answers for a first-order RC, computed here and nowhere else.
    r_eff = 1e3 * 1e6 / (1e3 + 1e6)  # 1k series, loaded by 1M
    tau = r_eff * 100e-9
    expected_rise = tau * math.log(9.0)
    expected_final = 5.0 * 1e6 / (1e6 + 1e3)
    expected_fc = 1.0 / (2 * math.pi * tau)

    rule("closed-form expectations")
    print(f"  tau                 {tau * 1e6:.3f} us")
    print(f"  10-90% rise time    {expected_rise * 1e6:.3f} us")
    print(f"  settled output      {expected_final:.4f} V")
    print(f"  -3 dB corner        {expected_fc:.2f} Hz")

    rule("transient: does it settle, how fast, and does it stay in range")
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
                op="within", value=expected_final, tolerance=0.01, unit="V",
            ),
            Assertion(
                name="10-90% rise time is tau*ln(9)",
                measurement=Measurement(
                    kind="rise_time", signal="VOUT", window=(0.0, 1e-3)
                ),
                op="within", value=expected_rise, tolerance=0.02, unit="s",
            ),
            Assertion(
                name="never exceeds the 5.5 V absolute maximum",
                measurement=Measurement(kind="abs_max", signal="VOUT"),
                op="<", value=5.5, unit="V",
            ),
        ],
    )
    show(report)
    print(f"\n  {report.summary().splitlines()[0]}")

    rule("ac: where is the corner, and does it roll off")
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
                op="within", value=0.0, tolerance=0.05,
                absolute_tolerance=True, unit="dB",
            ),
            Assertion(
                name="-3 dB corner is 1/(2*pi*R*C)",
                measurement=Measurement(kind="bandwidth_3db", signal="VOUT"),
                op="within", value=expected_fc, tolerance=0.03, unit="Hz",
            ),
            Assertion(
                name="rolls off 20 dB in the decade above the corner",
                measurement=Measurement(
                    kind="gain_db", signal="VOUT", at=expected_fc * 10
                ),
                op="<", value=-19.0, unit="dB",
            ),
        ],
    )
    show(ac_report)
    print(f"\n  {ac_report.summary().splitlines()[0]}")

    rule("a specification this circuit does not meet")
    print("  A verifier that can only say yes is not a verifier. Asking for a")
    print("  10 kHz bandwidth from a 1.6 kHz filter must fail, and say by how much.")
    strict = verify(
        spec,
        ac,
        [
            Assertion(
                name="bandwidth is at least 10 kHz",
                measurement=Measurement(kind="bandwidth_3db", signal="VOUT"),
                op=">", value=10e3, unit="Hz",
            ),
            Assertion(
                name="rise time of a net nobody probed",
                measurement=Measurement(kind="rise_time", signal="VCC"),
                op="<", value=1e-6, unit="s",
            ),
        ],
    )
    show(strict)
    print(f"\n  passed: {strict.passed}")

    rule("summary")
    both_pass = report.passed and ac_report.passed
    print(f"  transient specification met: {report.passed}")
    print(f"  frequency specification met: {ac_report.passed}")
    print(f"  impossible specification met: {strict.passed} (expected False)")
    return 0 if both_pass and not strict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

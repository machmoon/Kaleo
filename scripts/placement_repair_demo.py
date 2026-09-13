"""Measure what ``silkscreen.placement`` actually does to a generated board.

    python scripts/placement_repair_demo.py                # every case
    python scripts/placement_repair_demo.py --case ldo     # one case
    python scripts/placement_repair_demo.py --json out.json

No model, no key, no network: every circuit below is a literal ``CircuitSpec``
dict, so the only nondeterminism is CP-SAT's, and that is pinned by
``workers=1`` inside :func:`silkscreen.packing.pack`.  Lives in ``scripts/``
and is never collected by pytest -- the ``scripts/simulate_demo.py``
convention.

Why this exists
---------------

``engine/silkscreen/placement/`` sits between CP-SAT and the emitters and had
never been measured on a board this repository actually produces.  The prose
described it as a verifier, a repair oracle and an outline-growing adapter;
nobody had written down how often each of those three fires.  This script
answers that per board, and every number it prints about the *result* is
computed by a second, independent reader of the emitted ``.kicad_pcb``
(:mod:`silkscreen.audit.geometry`), never by the placement package's own
geometry -- the same discipline ``engine/tests/test_kicad.py`` uses for
overlap.

The columns
-----------

``crtyd_gap``    smallest gap between any two courtyard rectangles, in mm,
                 read back out of the emitted board file.  This is the
                 quantity KiCad's own ``DRC_TEST_PROVIDER_COURTYARD_CLEARANCE``
                 measures (``pcbnew/drc/drc_test_provider_courtyard_clearance.cpp``,
                 ``testCourtyardClearances()``), so a negative number here is
                 what KiCad would call ``DRCE_OVERLAPPING_FOOTPRINTS``.
``hpwl``         half-perimeter wirelength summed over nets, recomputed from
                 pad positions in the written file.  This is the objective
                 CP-SAT minimised; the placement package has no net model at
                 all, so this column says what its preference pass costs.
``grew``         how much :func:`_profile_frame` had to enlarge the outline
                 before the profile's edge margin was satisfiable.
``moved``        parts whose position changed, and the largest displacement.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "engine"))

from silkscreen.audit.geometry import AuditBoard, load_audit_board  # noqa: E402
from silkscreen.board import BoardResult, build_board, write_board  # noqa: E402
from silkscreen.netlist import parse_circuit_spec  # noqa: E402
from silkscreen.placement.adapter import (  # noqa: E402
    _profile_frame,
    repair_generated_board,
    verifier_board,
)
from silkscreen.placement.pcb_repair import evaluate, get_profile  # noqa: E402
from silkscreen.units import to_mm  # noqa: E402

TIME_LIMIT_S = 20.0


# --------------------------------------------------------------- the circuits


def _ldo() -> dict[str, Any]:
    """TODO.txt demo prompt 1: the AMS1117-3.3 LDO, passives only."""
    return {
        "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
        "passives": {
            "c_in": {"type": "capacitor", "value": "10uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
            "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2"],
            "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
        },
    }


def _blinker() -> dict[str, Any]:
    """TODO.txt demo prompt 2: the ~1 Hz NE555D blinker."""
    return {
        "devices": {
            "NE555D": {
                "pins": {
                    "GND": "1", "TRIG": "2", "OUT": "3", "RESET": "4",
                    "CTRL": "5", "THRES": "6", "DISCH": "7", "VCC": "8",
                },
            }
        },
        "passives": {
            "r1": {"type": "resistor", "value": "68k"},
            "r2": {"type": "resistor", "value": "68k"},
            "r_led": {"type": "resistor", "value": "1k"},
            "c_t": {"type": "capacitor", "value": "10uF"},
            "c_ctrl": {"type": "capacitor", "value": "10nF"},
            "c_dec": {"type": "capacitor", "value": "100nF"},
            "d1": {"type": "diode", "value": "LED"},
        },
        "nets": {
            "+9V": ["NE555D.VCC", "NE555D.RESET", "r1.1", "c_dec.1"],
            "GND": ["NE555D.GND", "c_t.2", "c_ctrl.2", "c_dec.2", "d1.2"],
            "DISCH": ["NE555D.DISCH", "r1.2", "r2.1"],
            "THRES": ["NE555D.THRES", "NE555D.TRIG", "r2.2", "c_t.1"],
            "CTRL": ["NE555D.CTRL", "c_ctrl.1"],
            "OUT": ["NE555D.OUT", "r_led.1"],
            "LED_A": ["r_led.2", "d1.1"],
        },
    }


def _attiny() -> dict[str, Any]:
    """TODO.txt demo prompt 3: the ATtiny85-20SU board."""
    return {
        "devices": {
            "ATtiny85-20SU": {
                "pins": {
                    "PB5": "1", "PB3": "2", "PB4": "3", "GND": "4",
                    "PB0": "5", "PB1": "6", "PB2": "7", "VCC": "8",
                },
            }
        },
        "passives": {
            "c_dec": {"type": "capacitor", "value": "100nF"},
            "r_reset": {"type": "resistor", "value": "10k"},
            "r_led": {"type": "resistor", "value": "1k"},
            "d_led": {"type": "diode", "value": "LED"},
        },
        "nets": {
            "+5V": ["ATtiny85-20SU.VCC", "c_dec.1", "r_reset.1"],
            "GND": ["ATtiny85-20SU.GND", "c_dec.2", "d_led.2"],
            "NRST": ["ATtiny85-20SU.PB5", "r_reset.2"],
            "LED_DRV": ["ATtiny85-20SU.PB1", "r_led.1"],
            "LED_A": ["r_led.2", "d_led.1"],
        },
    }


def _connector_demo() -> dict[str, Any]:
    """TODO.txt demo prompt 6: barrel jack in, terminal block and header out."""
    return {
        "devices": {
            "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
            "J_PWR": {
                "kind": "connector",
                "package": "Barrel_Jack_5.5x2.1mm",
                "pins": {"VBUS": "1", "GND": "2", "SHIELD": "3"},
            },
            "J_OUT": {
                "kind": "connector",
                "package": "TerminalBlock_2P_5.08mm",
                "pins": {"A": "1", "B": "2"},
            },
            "J_HDR": {
                "kind": "connector",
                "package": "PinHeader_1x04_P2.54mm",
                "pins": {"P1": "1", "P2": "2", "P3": "3", "P4": "4"},
            },
            "TP1": {
                "kind": "testpoint",
                "package": "TestPoint_Pad_1.5x1.5mm",
                "pins": {"P": "1"},
            },
        },
        "passives": {
            "c_in": {"type": "capacitor", "value": "10uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": ["J_PWR.VBUS", "AMS1117-3.3.VIN", "c_in.1"],
            "GND": [
                "J_PWR.GND", "J_PWR.SHIELD", "AMS1117-3.3.GND", "c_in.2",
                "c_out.2", "J_OUT.B", "J_HDR.P2",
            ],
            "+3V3": [
                "AMS1117-3.3.VOUT", "c_out.1", "J_OUT.A", "J_HDR.P1", "TP1.P",
            ],
            # The two spare header pins are brought to a test pad rather
            # than left dangling: netlist.py rejects a net with one endpoint.
            "SPARE": ["J_HDR.P3", "J_HDR.P4"],
        },
    }


def _lqfp(pin_count: int) -> dict[str, Any]:
    """A fine-pitch stress board: one LQFP plus a decoupling cap per rail.

    The point is not that this is a good design -- it is that an LQFP-64
    courtyard next to 0402 capacitors is the geometry most likely to expose a
    verifier that measures the wrong rectangle.
    """
    pins = {f"P{n}": str(n) for n in range(1, pin_count + 1)}
    caps = {f"c{n}": {"type": "capacitor", "value": "100nF"} for n in range(1, 5)}
    nets: dict[str, list[str]] = {
        "+3V3": [f"U.P{1}"] + [f"c{n}.1" for n in range(1, 5)],
        "GND": [f"U.P{2}"] + [f"c{n}.2" for n in range(1, 5)],
    }
    # Pins are tied in adjacent pairs: netlist.py refuses a net with a single
    # endpoint, and a pairwise net also gives the HPWL column something to
    # measure on the fine-pitch cases.
    for n in range(3, pin_count, 2):
        nets[f"IO{n}"] = [f"U.P{n}", f"U.P{n + 1}"]
    return {"devices": {"U": {"pins": pins}}, "passives": caps, "nets": nets}


CASES: dict[str, dict[str, Any]] = {
    "ldo": _ldo(),
    "blinker": _blinker(),
    "attiny": _attiny(),
    "connectors": _connector_demo(),
    "lqfp32": _lqfp(32),
    "lqfp48": _lqfp(48),
    "lqfp64": _lqfp(64),
}


# ------------------------------------------------- independent geometry oracle
#
# Everything below reads the *written* board file through
# silkscreen.audit.geometry, which parses .kicad_pcb absolute coordinates on
# its own and never calls board.py or the placement package.  A gap or a
# wirelength computed with the placer's own helpers would agree with the
# placer by construction.


def _min_courtyard_gap_mm(audit: AuditBoard) -> float | None:
    parts = [p for p in audit.parts if p.courtyard is not None]
    if len(parts) < 2:
        return None
    worst: int | None = None
    for index, first in enumerate(parts):
        for second in parts[index + 1 :]:
            gap = first.courtyard.gap_to(second.courtyard)
            worst = gap if worst is None else min(worst, gap)
    return None if worst is None else to_mm(worst)


def _overlapping_pairs(audit: AuditBoard) -> list[tuple[str, str]]:
    """Pairs KiCad would report as DRCE_OVERLAPPING_FOOTPRINTS.

    ``drc_test_provider_courtyard_clearance.cpp::testCourtyardClearances()``
    reports an overlap when the two front courtyards collide at zero
    clearance; ``gap_to`` returns 0 for touching rectangles, so the test is
    strict inequality.
    """
    parts = [p for p in audit.parts if p.courtyard is not None]
    out: list[tuple[str, str]] = []
    for index, first in enumerate(parts):
        for second in parts[index + 1 :]:
            if first.courtyard.gap_to(second.courtyard) < 0:
                out.append(tuple(sorted((first.ref, second.ref))))
    return out


def _hpwl_mm(audit: AuditBoard) -> float:
    total = 0
    for pads in audit.pads_by_net().values():
        if len(pads) < 2:
            continue
        xs = [pad.centre[0] for pad in pads]
        ys = [pad.centre[1] for pad in pads]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return to_mm(total)


def _audit(board: BoardResult, directory: Path, name: str) -> AuditBoard:
    path = directory / f"{name}.kicad_pcb"
    write_board(board, path)
    return load_audit_board(path)


# ------------------------------------------------------------- the measurement


@dataclass
class CaseReport:
    name: str
    parts: int
    profile: str
    solver_status: str
    board_before_mm: tuple[float, float]
    board_after_mm: tuple[float, float]
    violations_before: dict[str, int]
    violations_after_framing: dict[str, int]
    grew_mm: tuple[float, float]
    completed: bool
    applied: bool
    hard_before: float
    hard_after: float
    moves: int
    parts_moved: int
    max_displacement_mm: float
    gap_before_mm: float | None
    gap_after_mm: float | None
    overlaps_before: list[tuple[str, str]]
    overlaps_after: list[tuple[str, str]]
    hpwl_before_mm: float
    hpwl_after_mm: float
    wirelength_reported: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in self.__dict__.items()
        }


def _kinds(violations) -> dict[str, int]:
    counts: dict[str, int] = {}
    for violation in violations:
        counts[violation.kind] = counts.get(violation.kind, 0) + 1
    return counts


def measure(name: str, spec_value: dict[str, Any], profile_name: str) -> CaseReport:
    spec = parse_circuit_spec(spec_value)
    board = build_board(spec, time_limit_s=TIME_LIMIT_S)
    profile = get_profile(profile_name)

    raw = verifier_board(board)
    before = evaluate(raw, profile)
    framed = _profile_frame(raw, profile)
    after_framing = evaluate(framed, profile)

    result = repair_generated_board(board, profile=profile_name)
    updated = result.board

    moved = 0
    worst = 0
    for original, part in zip(board.parts, updated.parts, strict=True):
        delta = max(
            abs(part.x_nm - original.x_nm), abs(part.y_nm - original.y_nm)
        )
        if delta:
            moved += 1
            worst = max(worst, delta)

    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        audit_before = _audit(board, directory, "before")
        audit_after = _audit(updated, directory, "after")
        report = CaseReport(
            name=name,
            parts=len(board.parts),
            profile=profile_name,
            solver_status=board.solver_status,
            board_before_mm=board.size_mm,
            board_after_mm=updated.size_mm,
            violations_before=_kinds(before.violations),
            violations_after_framing=_kinds(after_framing.violations),
            grew_mm=(
                round(framed.width - raw.width, 3),
                round(framed.height - raw.height, 3),
            ),
            completed=result.run.completed,
            applied=result.applied,
            hard_before=evaluate(result.run.start, profile).hard,
            hard_after=evaluate(result.run.board, profile).hard,
            moves=sum(len(step.accepted) for step in result.run.steps),
            parts_moved=moved,
            max_displacement_mm=round(to_mm(worst), 3),
            gap_before_mm=_min_courtyard_gap_mm(audit_before),
            gap_after_mm=_min_courtyard_gap_mm(audit_after),
            overlaps_before=_overlapping_pairs(audit_before),
            overlaps_after=_overlapping_pairs(audit_after),
            hpwl_before_mm=round(_hpwl_mm(audit_before), 3),
            hpwl_after_mm=round(_hpwl_mm(audit_after), 3),
            wirelength_reported=updated.wirelength_nm is not None,
        )
    return report


def _row(report: CaseReport) -> str:
    def gap(value: float | None) -> str:
        return "  n/a " if value is None else f"{value:6.3f}"

    return (
        f"{report.name:<12} {report.parts:>3} "
        f"{report.board_before_mm[0]:6.2f}x{report.board_before_mm[1]:<6.2f}"
        f"-> {report.board_after_mm[0]:6.2f}x{report.board_after_mm[1]:<6.2f} "
        f"grew {report.grew_mm[0]:5.2f}/{report.grew_mm[1]:<5.2f} "
        f"viol {sum(report.violations_before.values()):>3}"
        f"/{sum(report.violations_after_framing.values()):<3} "
        f"moves {report.moves:>2} parts {report.parts_moved:>2} "
        f"max {report.max_displacement_mm:5.2f} "
        f"gap {gap(report.gap_before_mm)}->{gap(report.gap_after_mm)} "
        f"hpwl {report.hpwl_before_mm:7.1f}->{report.hpwl_after_mm:<7.1f} "
        f"{'applied' if report.applied else 'NOT-APPLIED'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    parser.add_argument("--profile", default="compact-control")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    names = args.case or sorted(CASES)
    reports: list[CaseReport] = []
    for name in names:
        report = measure(name, CASES[name], args.profile)
        reports.append(report)
        print(_row(report), flush=True)

    print()
    print(f"profile: {args.profile}")
    illegal_in = sum(1 for r in reports if r.violations_before)
    illegal_framed = sum(1 for r in reports if r.violations_after_framing)
    print(
        f"boards CP-SAT produced that the profile calls illegal: "
        f"{illegal_in}/{len(reports)}"
    )
    print(
        f"still illegal after the adapter grew the outline: "
        f"{illegal_framed}/{len(reports)}"
    )
    print(
        f"repairs applied: {sum(1 for r in reports if r.applied)}/{len(reports)}; "
        f"incomplete and therefore withheld: "
        f"{sum(1 for r in reports if not r.applied)}/{len(reports)}"
    )
    print(
        f"boards whose courtyards overlap after repair: "
        f"{sum(1 for r in reports if r.overlaps_after)}/{len(reports)}"
    )
    worse = [r for r in reports if r.hpwl_after_mm > r.hpwl_before_mm]
    print(
        f"boards whose wirelength the preference pass made worse: "
        f"{len(worse)}/{len(reports)}"
        + (
            "  (" + ", ".join(
                f"{r.name} +{r.hpwl_after_mm - r.hpwl_before_mm:.1f} mm"
                for r in worse
            ) + ")"
            if worse
            else ""
        )
    )

    if args.json:
        args.json.write_text(
            json.dumps([r.as_dict() for r in reports], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

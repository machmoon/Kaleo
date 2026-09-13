"""Order demo: design a board, route it, and see whether it may be ordered.

    python scripts/order_demo.py

Runs with no network, no API key and no KiCad install. The point of the demo
is the gate in the middle: an unrouted board is refused, because copper that
does not exist cannot carry a signal, and a board fabricated in that state
arrives dead. Route it and the same gate passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from silkscreen.board import build_board, route_board  # noqa: E402
from silkscreen.fab import fab_files  # noqa: E402
from silkscreen.netlist import parse_circuit_spec  # noqa: E402
from silkscreen.order import OrderOptions, order_manifest, preflight  # noqa: E402

#: A 3.3 V LDO with input and output bulk capacitors -- the same circuit the
#: live pipeline produces from a plain-language prompt. Note that Device.pins
#: maps a pin NAME to its NUMBER, not the other way round.
CIRCUIT = {
    "devices": {"U1": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "C1": {"type": "capacitor", "value": "22uF"},
        "C2": {"type": "capacitor", "value": "22uF"},
    },
    "nets": {
        "VIN": ["U1.VIN", "C1.1"],
        "+3V3": ["U1.VOUT", "C2.1"],
        "GND": ["U1.GND", "C1.2", "C2.2"],
    },
}


def rule(title: str) -> None:
    print(f"\n{title}\n" + "-" * 62)


def show(pre) -> None:
    for issue in pre.issues:
        print(f"  [{issue.severity}] {issue.code}: {issue.title}")


def main() -> int:
    spec = parse_circuit_spec(json.dumps(CIRCUIT))

    rule("1. Place the board")
    board = build_board(spec, time_limit_s=8.0)
    print(f"  {len(board.parts)} parts, {len(board.nets)} nets")
    print(
        f"  {board.width_nm / 1e6:.2f} x {board.height_nm / 1e6:.2f} mm"
        f"  [{board.solver_status}]"
    )

    options = OrderOptions(quantity=10, assembly=True, panel_columns=2)

    rule("2. Try to order it, unrouted")
    before = preflight(board, spec=spec, options=options)
    print(f"  orderable: {before.orderable}")
    show(before)

    rule("3. Route it")
    route_board(board)
    print(f"  routed: {', '.join(board.routed_nets) or 'nothing'}")
    print(f"  {len(board.tracks)} tracks, {len(board.vias)} vias")
    for net, why in sorted(board.unrouted_nets.items()):
        print(f"  unrouted: {net} -- {why}")

    rule("4. Try again, routed")
    after = preflight(board, spec=spec, options=options)
    print(f"  ORDERABLE: {after.orderable}")
    show(after)

    rule("5. The fab package")
    files = fab_files(board)
    for layer in files:
        print(f"  {layer.filename:32s} {len(layer.content):6d} bytes")
    manifest = order_manifest(board, options, after)
    print(
        f"\n  {options.quantity} boards, assembly={options.assembly},"
        f" {manifest['board']['boards_per_panel']} per panel"
    )
    print(f"  orderable: {manifest['orderable']}")
    print(f"  human approval required: {manifest['requires_human_approval']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

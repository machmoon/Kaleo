"""End-to-end demo: load a real KiCad board, re-place it, write it back.

    python scripts/demo.py [board.kicad_pcb] [-o OUT]

Runs the whole engine with no network, no API key, and no KiCad install.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from silkscreen.kicad import (  # noqa: E402
    apply_placements,
    extract_nets,
    extract_parts,
    load_board,
    save_board,
    set_board_outline,
    to_parts,
)
from silkscreen.packing import pack  # noqa: E402
from silkscreen.units import to_mm  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{title}\n" + "-" * 62)


def main() -> int:
    default_board = ROOT / "engine" / "tests" / "fixtures" / "ref.kicad_pcb"
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("board", nargs="?", default=str(default_board))
    ap.add_argument("-o", "--out", default="placed.kicad_pcb")
    ap.add_argument("--clearance-mm", type=float, default=0.25)
    ap.add_argument("--time-limit", type=float, default=20.0)
    args = ap.parse_args()

    src = Path(args.board)
    if not src.exists():
        print(f"error: no such board: {src}", file=sys.stderr)
        return 2

    rule("1. Read the board")
    board = load_board(src)
    infos = extract_parts(board)
    print(f"  {len(infos)} footprints from {src}")
    for i in infos:
        print(
            f"    {i.ref:<5} {to_mm(i.width_nm):6.2f} x {to_mm(i.height_nm):5.2f} mm"
            f"  {len(i.pad_offsets):>3} pads  {i.library_id}"
        )

    rule("2. Build the placer model")
    parts = to_parts(infos)
    nets = extract_nets(infos)
    print(f"  {len(parts)} parts, {len(nets)} nets")

    rule("3. Solve (OR-Tools CP-SAT)")
    result = pack(
        parts,
        nets=nets,
        clearance_nm=int(args.clearance_mm * 1e6),
        time_limit_s=args.time_limit,
    )
    w, h = to_mm(result.board_width_nm), to_mm(result.board_height_nm)
    print(f"  status     : {result.status.value}")
    print(f"  board size : {w:.2f} x {h:.2f} mm  ({w * h:.1f} mm^2)")
    if result.wirelength_nm is not None:
        print(f"  HPWL       : {to_mm(result.wirelength_nm):.1f} mm")
    print(f"  solve time : {result.solve_time_s:.2f} s")
    for warning in result.warnings:
        print(f"  warning    : {warning}")

    rule("4. Write a real .kicad_pcb")
    moved = apply_placements(board, infos, result.placements, result.board_height_nm)
    set_board_outline(
        board, result.board_width_nm, result.board_height_nm, margin_nm=500_000
    )
    out = save_board(board, args.out)
    print(f"  placed {moved}/{len(infos)} -> {out}  ({out.stat().st_size:,} bytes)")

    rule("5. Prove the round-trip")
    reloaded = load_board(out)
    assert len(reloaded.footprints) == len(board.footprints)
    print(f"  reparsed OK, {len(reloaded.footprints)} footprints preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Export deterministic PCB repair trajectories as behavior-cloning JSONL."""

from __future__ import annotations

import argparse

from silkscreen.placement.synthetic import write_jsonl


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", help="JSONL destination")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count must be positive")
    write_jsonl(args.output, range(args.seed, args.seed + args.count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic placement trajectories for cloning and frozen evaluation."""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from .agent import placement_prompt
from .grader import board_to_dict, board_to_text
from .pcb_repair import Board, demo_board, get_profile, repair

__all__ = ["corrupt", "trajectory", "write_jsonl"]


def corrupt(board: Board, seed: int) -> Board:
    rng = random.Random(seed)
    movable = [component for component in board.components if not component.fixed]
    if len(movable) < 2:
        return board
    source, target = rng.sample(movable, 2)
    broken = replace(source, x=target.x + 0.25, y=target.y + 0.25)
    return board.replace_component(broken)


def trajectory(seed: int, profile_name: str = "compact-control") -> dict:
    profile = get_profile(profile_name)
    start = corrupt(demo_board(), seed)
    final, actions = repair(start, profile)
    completion = "\n".join(action.as_text() for action in actions)
    return {
        "seed": seed,
        "profile": profile_name,
        "prompt": board_to_text(start),
        "completion": completion,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a PCB placement repair policy. Return only ordered "
                    "PLACE or MOVE action lines."
                ),
            },
            {"role": "user", "content": placement_prompt(start, profile, 1)},
            {"role": "assistant", "content": completion},
        ],
        "start": board_to_dict(start),
        "final": board_to_dict(final),
    }


def write_jsonl(path: str | Path, seeds: Iterable[int]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for seed in seeds:
            handle.write(json.dumps(trajectory(int(seed)), sort_keys=True) + "\n")
    return output

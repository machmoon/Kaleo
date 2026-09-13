"""Backend-agnostic placement reward and serialization functions.

This module deliberately imports no trainer. The same functions can grade a
standalone demo, Verifiers rollout, SFT evaluation, or another RL backend.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .pcb_repair import (
    Board,
    CompanyProfile,
    Component,
    Keepout,
    apply_actions,
    evaluate,
    parse_actions,
)

__all__ = [
    "apply_model_output",
    "board_from_dict",
    "board_score",
    "board_to_dict",
    "board_to_json",
    "board_to_text",
    "is_legal",
    "outcome_reward",
    "progress_reward",
    "quality_reward",
]


def board_score(board: Board, profile: CompanyProfile) -> float:
    return evaluate(board, profile).total


def is_legal(board: Board, profile: CompanyProfile) -> bool:
    return evaluate(board, profile).hard == 0


def outcome_reward(board: Board, profile: CompanyProfile) -> float:
    return 1.0 if is_legal(board, profile) else 0.0


def progress_reward(start: Board, final: Board, profile: CompanyProfile) -> float:
    before = evaluate(start, profile).hard
    after = evaluate(final, profile).hard
    if before == 0:
        return 1.0 if after == 0 else 0.0
    return max(-1.0, min(1.0, (before - after) / before))


def quality_reward(start: Board, final: Board, profile: CompanyProfile) -> float:
    """Small preference reward, intended to sit below the legality reward."""
    if not is_legal(final, profile):
        return 0.0
    # An intentionally corrupted start is often artificially compact because
    # parts overlap. Comparing its soft cost with a legal result would reward
    # the overlap. Score only the final legal design, on a deliberately small
    # scale so this term can never outweigh the binary outcome reward.
    return 0.1 / (1.0 + evaluate(final, profile).soft)


def apply_model_output(board: Board, text: str, profile: CompanyProfile) -> Board:
    return apply_actions(board, parse_actions(text), profile)


def board_to_dict(board: Board) -> dict[str, Any]:
    return {
        "width": board.width,
        "height": board.height,
        "components": [asdict(component) for component in board.components],
        "keepouts": [asdict(keepout) for keepout in board.keepouts],
    }


def board_from_dict(value: dict[str, Any]) -> Board:
    if not isinstance(value, dict):
        raise ValueError("board must be an object")
    try:
        components = tuple(Component(**item) for item in value.get("components", []))
        keepouts = tuple(Keepout(**item) for item in value.get("keepouts", []))
        return Board(
            width=float(value["width"]),
            height=float(value["height"]),
            components=components,
            keepouts=keepouts,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid board: {exc}") from exc


def board_to_json(board: Board) -> str:
    return json.dumps(
        board_to_dict(board),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def board_to_text(board: Board) -> str:
    lines = [f"BOARD {board.width:.3f} {board.height:.3f}"]
    lines.extend(
        f"COMP {component.ref} {component.x:.3f} {component.y:.3f} "
        f"{component.width:.3f} {component.height:.3f} {component.angle} "
        f"{'FIXED' if component.fixed else component.kind.upper()}"
        for component in board.components
    )
    lines.extend(
        f"KEEPOUT {keepout.name} {keepout.x:.3f} {keepout.y:.3f} "
        f"{keepout.width:.3f} {keepout.height:.3f}"
        for keepout in board.keepouts
    )
    return "\n".join(lines)

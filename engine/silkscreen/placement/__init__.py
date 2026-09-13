"""Silkscreen's portable, verifier-grounded PCB placement repair."""

from .adapter import GeneratedPlacement, repair_generated_board, verifier_board
from .agent import PlacementAgent, PlacementRun, PlacementStep
from .grader import board_score, is_legal, outcome_reward, progress_reward
from .pcb_repair import (
    Board,
    CompanyProfile,
    Component,
    Keepout,
    PlacementAction,
    Violation,
    apply_actions,
    demo_board,
    get_profile,
    parse_actions,
    repair,
)
from .traces import build_failure_traces

__all__ = [
    "Board",
    "CompanyProfile",
    "Component",
    "Keepout",
    "PlacementAction",
    "PlacementAgent",
    "GeneratedPlacement",
    "PlacementRun",
    "PlacementStep",
    "repair_generated_board",
    "verifier_board",
    "Violation",
    "apply_actions",
    "board_score",
    "build_failure_traces",
    "demo_board",
    "get_profile",
    "is_legal",
    "outcome_reward",
    "parse_actions",
    "progress_reward",
    "repair",
]

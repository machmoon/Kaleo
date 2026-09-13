"""Multi-turn Silkscreen placement agent with deterministic verifier feedback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .grader import board_to_text
from .pcb_repair import (
    Board,
    CompanyProfile,
    PlacementAction,
    apply_actions,
    evaluate,
    parse_actions,
    repair,
)

__all__ = [
    "PlacementAgent",
    "PlacementPolicyError",
    "PlacementReceipt",
    "PlacementRun",
    "PlacementStep",
    "TextModel",
    "placement_prompt",
]


class PlacementPolicyError(RuntimeError):
    """A proposal backend failed or returned an unusable response."""


class TextModel(Protocol):
    def generate(
        self,
        prompt: str,
        *,
        documents=None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str: ...


@dataclass(frozen=True)
class PlacementReceipt:
    action: PlacementAction
    accepted: bool
    hard_before: float
    hard_after: float
    soft_before: float
    soft_after: float
    reason: str


@dataclass(frozen=True)
class PlacementStep:
    turn: int
    proposer: str
    prompt: str
    response: str
    board_before: Board
    proposed: tuple[PlacementAction, ...]
    accepted: tuple[PlacementAction, ...]
    receipts: tuple[PlacementReceipt, ...]
    hard_before: float
    hard_after: float
    soft_before: float
    soft_after: float
    reason: str


@dataclass(frozen=True)
class PlacementRun:
    start: Board
    board: Board
    profile: CompanyProfile
    steps: tuple[PlacementStep, ...]
    policy: str
    completed: bool


def _profile_text(profile: CompanyProfile) -> str:
    return "\n".join(
        (
            f"PROFILE {profile.name}",
            (
                f"HARD clearance={profile.clearance:.3f} "
                f"edge_margin={profile.edge_margin:.3f}"
            ),
            f"FIXED {','.join(profile.fixed_refs) or 'none'}",
            f"EDGE {','.join(profile.edge_refs) or 'none'}",
            "GROUPS " + ";".join(",".join(group) for group in profile.groups),
            "THERMAL "
            + ";".join(
                f"{a},{b},{distance}" for a, b, distance in profile.thermal_pairs
            ),
        )
    )


def placement_prompt(board: Board, profile: CompanyProfile, turn: int) -> str:
    result = evaluate(board, profile)
    violations = "\n".join(f"- {item.message}" for item in result.violations)
    _, suggested = repair(board, profile)
    candidates = "\n".join(action.as_text() for action in suggested[:8])
    return f"""PCB PLACEMENT REPAIR TURN {turn}

You control component placement. Choose one to four lines from CANDIDATE
ACTIONS and copy their numeric values exactly. Return action lines only:
PLACE REF X_MM Y_MM [ANGLE]
MOVE REF DX_MM DY_MM [ANGLE]

Do not output X_MM, Y_MM, DX_MM, DY_MM, brackets, commentary, or new values.
Never move a fixed component. Hard legality has priority over preferences.
The deterministic verifier accepts the longest prefix whose individual moves
improve the lexicographic score (hard violations first, then preferences).

{_profile_text(profile)}

CURRENT SCORE hard={result.hard:.6f} soft={result.soft:.6f}
VIOLATIONS
{violations or '- none; improve the profile score'}

CANDIDATE ACTIONS
{candidates or '- none; return no action'}

{board_to_text(board)}
"""


class PlacementAgent:
    def __init__(
        self,
        model: TextModel | None = None,
        *,
        fallback_model: TextModel | None = None,
        max_turns: int = 8,
    ):
        if isinstance(max_turns, bool) or not isinstance(max_turns, int):
            raise ValueError("max_turns must be an integer")
        if max_turns <= 0 or max_turns > 16:
            raise ValueError("max_turns must be between 1 and 16")
        self.model = model
        self.fallback_model = fallback_model
        self.max_turns = max_turns

    def run(
        self,
        board: Board,
        profile: CompanyProfile,
        *,
        policy: str = "deterministic",
    ) -> PlacementRun:
        if policy == "deterministic":
            return _deterministic_run(board, profile)
        if policy not in {"gemini", "tinker", "ollama", "hybrid"}:
            raise ValueError(
                "policy must be 'deterministic', 'gemini', 'tinker', "
                "'ollama', or 'hybrid'"
            )
        if self.model is None:
            raise ValueError(f"{policy} policy requires a proposal model")
        default_proposer = "tinker" if policy == "hybrid" else policy
        proposer = (
            "gemini"
            if policy == "gemini"
            else getattr(self.model, "proposer_name", default_proposer)
        )
        fallback = self.fallback_model if policy == "hybrid" else None
        return _model_run(
            board,
            profile,
            self.model,
            self.max_turns,
            policy=policy,
            proposer=proposer,
            fallback_model=fallback,
        )


def _deterministic_step(
    board: Board,
    profile: CompanyProfile,
    action: PlacementAction,
    turn: int,
) -> tuple[Board, PlacementStep]:
    before = evaluate(board, profile)
    updated = apply_actions(board, (action,), profile)
    after = evaluate(updated, profile)
    step = PlacementStep(
        turn=turn,
        proposer="deterministic",
        prompt="",
        response=action.as_text(),
        board_before=board,
        proposed=(action,),
        accepted=(action,),
        receipts=(
            PlacementReceipt(
                action=action,
                accepted=True,
                hard_before=before.hard,
                hard_after=after.hard,
                soft_before=before.soft,
                soft_after=after.soft,
                reason="accepted: deterministic score improvement",
            ),
        ),
        hard_before=before.hard,
        hard_after=after.hard,
        soft_before=before.soft,
        soft_after=after.soft,
        reason="accepted: deterministic score improvement",
    )
    return updated, step


def _deterministic_run(
    board: Board, profile: CompanyProfile
) -> PlacementRun:
    final, actions = repair(board, profile)
    current = board
    steps: list[PlacementStep] = []
    for turn, action in enumerate(actions, start=1):
        current, step = _deterministic_step(current, profile, action, turn)
        steps.append(step)
    if current != final:
        raise RuntimeError("deterministic repair trace did not reproduce")
    return PlacementRun(
        start=board,
        board=final,
        profile=profile,
        steps=tuple(steps),
        policy="deterministic",
        completed=evaluate(final, profile).hard == 0,
    )


def _model_step(
    board: Board,
    profile: CompanyProfile,
    model: TextModel,
    turn: int,
    proposer: str,
) -> tuple[Board, PlacementStep]:
    before = evaluate(board, profile)
    prompt = placement_prompt(board, profile, turn)
    try:
        response = model.generate(
            prompt,
            system=(
                "You are a PCB placement repair policy. Emit placement action "
                "lines only. The geometry verifier is authoritative."
            ),
            temperature=0.0,
            max_output_tokens=256,
        )
    except Exception as exc:
        raise PlacementPolicyError(f"{proposer} proposal failed") from exc
    if not isinstance(response, str):
        raise PlacementPolicyError(f"{proposer} returned non-text output")
    try:
        proposed = tuple(parse_actions(response))
    except ValueError as exc:
        raise PlacementPolicyError(
            f"{proposer} returned invalid placement actions"
        ) from exc
    updated, accepted, receipts = _verified_prefix(board, proposed, profile)
    after = evaluate(updated, profile)
    if not proposed:
        reason = "rejected: no valid placement actions"
    elif len(accepted) == len(proposed):
        reason = f"accepted speculative prefix {len(accepted)}/{len(proposed)}"
    else:
        ratio = f"{len(accepted)}/{len(proposed)}"
        reason = f"accepted speculative prefix {ratio}; stopped at rejection"
    step = PlacementStep(
        turn=turn,
        proposer=proposer,
        prompt=prompt,
        response=response,
        board_before=board,
        proposed=proposed,
        accepted=accepted,
        receipts=receipts,
        hard_before=before.hard,
        hard_after=after.hard,
        soft_before=before.soft,
        soft_after=after.soft,
        reason=reason,
    )
    return updated, step


def _verified_prefix(
    board: Board,
    proposed: tuple[PlacementAction, ...],
    profile: CompanyProfile,
) -> tuple[Board, tuple[PlacementAction, ...], tuple[PlacementReceipt, ...]]:
    """Accept the longest prefix whose individual actions improve (H, P)."""
    current = board
    accepted: list[PlacementAction] = []
    receipts: list[PlacementReceipt] = []
    for action in proposed:
        before = evaluate(current, profile)
        candidate = apply_actions(current, (action,), profile)
        after = evaluate(candidate, profile)
        improved = (after.hard, after.soft) < (before.hard, before.soft)
        receipts.append(
            PlacementReceipt(
                action=action,
                accepted=improved,
                hard_before=before.hard,
                hard_after=after.hard if improved else before.hard,
                soft_before=before.soft,
                soft_after=after.soft if improved else before.soft,
                reason=(
                    "accepted: lexicographic score improved"
                    if improved
                    else "rejected: no lexicographic improvement"
                ),
            )
        )
        if not improved:
            break
        current = candidate
        accepted.append(action)
    return current, tuple(accepted), tuple(receipts)


def _model_run(
    board: Board,
    profile: CompanyProfile,
    model: TextModel,
    max_turns: int,
    *,
    policy: str,
    proposer: str,
    fallback_model: TextModel | None,
) -> PlacementRun:
    current = board
    steps: list[PlacementStep] = []
    for turn in range(1, max_turns + 1):
        current, step = _model_step(current, profile, model, turn, proposer)
        steps.append(step)
        if not step.accepted and fallback_model is not None:
            current, fallback_step = _model_step(
                current, profile, fallback_model, turn, "gemini-recovery"
            )
            steps.append(fallback_step)
        if evaluate(current, profile).hard == 0:
            break
    return PlacementRun(
        start=board,
        board=current,
        profile=profile,
        steps=tuple(steps),
        policy=policy,
        completed=evaluate(current, profile).hard == 0,
    )

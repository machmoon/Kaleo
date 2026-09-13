"""Durable, consent-aware placement traces for later policy post-training."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .grader import board_from_dict
from .pcb_repair import CompanyProfile, repair

__all__ = [
    "FactFailureTraceStore",
    "FailureTraceStore",
    "JsonlFailureTraceStore",
    "MemoryFailureTraceStore",
    "build_failure_traces",
]

TRACE_SCHEMA_VERSION = 1
_MODEL_PROPOSERS = {"tinker", "qwen-tinker", "gemma-local"}


class FailureTraceStore(Protocol):
    def append(self, trace: dict[str, Any]) -> str: ...


@dataclass
class MemoryFailureTraceStore:
    traces: list[dict[str, Any]] = field(default_factory=list)

    def append(self, trace: dict[str, Any]) -> str:
        self.traces.append(dict(trace))
        return str(trace["trace_id"])


class JsonlFailureTraceStore:
    """Append-only local trace store with one complete JSON object per line."""

    _lock = threading.Lock()

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, trace: dict[str, Any]) -> str:
        line = json.dumps(
            trace,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return str(trace["trace_id"])


class FactFailureTraceStore:
    """Store traces in a key-value collection such as FirestoreFactStore."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def append(self, trace: dict[str, Any]) -> str:
        trace_id = str(trace["trace_id"])
        self.store.put(trace_id, trace)
        return trace_id


def _failure_kind(step: dict[str, Any]) -> str | None:
    proposed = step.get("proposed") or []
    accepted = step.get("accepted") or []
    if not proposed:
        return "no-valid-actions"
    if not accepted:
        return "all-actions-rejected"
    if len(accepted) < len(proposed):
        return "partial-prefix-rejected"
    return None


def _is_policy_step(step: dict[str, Any]) -> bool:
    proposer = str(step.get("proposer", ""))
    return proposer in _MODEL_PROPOSERS or proposer.startswith("qwen")


def _oracle_response(step: dict[str, Any], profile_value: dict[str, Any]) -> str:
    board = board_from_dict(step["board_before"])
    profile = CompanyProfile.from_dict(profile_value)
    _, actions = repair(board, profile)
    return "\n".join(action.as_text() for action in actions)


def _recovery(steps: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    if index + 1 >= len(steps):
        return None
    candidate = steps[index + 1]
    if candidate.get("proposer") != "gemini-recovery":
        return None
    if not candidate.get("accepted"):
        return None
    return candidate


def _chosen_response(
    step: dict[str, Any],
    recovery: dict[str, Any] | None,
    profile: dict[str, Any],
) -> tuple[str, str]:
    if recovery is not None:
        return str(recovery.get("response", "")), "gemini-recovery"
    return _oracle_response(step, profile), "deterministic-oracle"


def _step_failure_kind(
    step: dict[str, Any],
    *,
    index: int,
    final_policy_index: int | None,
    run_completed: bool,
) -> str | None:
    failure_kind = _failure_kind(step)
    terminal_failure = not run_completed and index == final_policy_index
    if failure_kind is None and terminal_failure:
        return "turn-limit-illegal"
    return failure_kind


def _trace_record(
    result: dict[str, Any],
    step: dict[str, Any],
    *,
    failure_kind: str,
    recovery: dict[str, Any] | None,
    model_id: str,
    input_origin: str,
    recorded_at: float,
    trace_id: str,
) -> dict[str, Any]:
    chosen, chosen_source = _chosen_response(
        step, recovery, result["profile"]
    )
    prompt = step.get("prompt", "")
    rejected = step.get("response", "")
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_id": trace_id,
        "recorded_at": recorded_at,
        "model_id": model_id,
        "proposer": step.get("proposer"),
        "run_policy": result.get("policy"),
        "input_origin": input_origin,
        "failure_kind": failure_kind,
        "turn": step.get("turn"),
        "profile": result.get("profile"),
        "board_before": step.get("board_before"),
        "prompt": prompt,
        "rejected_response": rejected,
        "chosen_response": chosen,
        "chosen_source": chosen_source,
        "proposed": step.get("proposed", []),
        "accepted": step.get("accepted", []),
        "receipts": step.get("receipts", []),
        "run_completed": bool(result.get("completed")),
        "reward": result.get("reward", {}),
        "post_training": {
            "prompt": prompt,
            "chosen": chosen,
            "rejected": rejected,
        },
    }


def build_failure_traces(
    result: dict[str, Any],
    *,
    model_id: str,
    input_origin: str,
    now: Callable[[], float] = time.time,
    id_factory: Callable[[], Any] = uuid.uuid4,
) -> list[dict[str, Any]]:
    """Build post-training pairs for every failed small-policy proposal."""
    steps = list(result.get("steps") or [])
    policy_indexes = [
        index for index, step in enumerate(steps) if _is_policy_step(step)
    ]
    final_policy_index = policy_indexes[-1] if policy_indexes else None
    traces: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not _is_policy_step(step):
            continue
        failure_kind = _step_failure_kind(
            step,
            index=index,
            final_policy_index=final_policy_index,
            run_completed=bool(result.get("completed")),
        )
        if failure_kind is None:
            continue
        recovery = _recovery(steps, index)
        traces.append(
            _trace_record(
                result,
                step,
                failure_kind=failure_kind,
                recovery=recovery,
                model_id=model_id,
                input_origin=input_origin,
                recorded_at=now(),
                trace_id=str(id_factory()),
            )
        )
    return traces

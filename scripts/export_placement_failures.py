"""Export stored verifier failures as portable post-training JSONL."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def read_traces(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, parse_constant=_reject_nonfinite)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"invalid JSON on line {line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"trace on line {line_number} must be an object")
            if value.get("schema_version") != 1:
                raise ValueError(
                    f"unsupported trace schema on line {line_number}"
                )
            yield value


def post_training_row(trace: dict[str, Any]) -> dict[str, Any]:
    try:
        pair = trace["post_training"]
        trace_id = trace["trace_id"]
        model_id = trace["model_id"]
        failure_kind = trace["failure_kind"]
        chosen_source = trace["chosen_source"]
        profile_name = trace["profile"]["name"]
        input_origin = trace["input_origin"]
        run_completed = trace["run_completed"]
        prompt = pair["prompt"]
        chosen = pair["chosen"]
        rejected = pair["rejected"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed failure trace: missing {exc}") from exc
    if not all(isinstance(value, str) for value in (prompt, chosen, rejected)):
        raise ValueError("post_training prompt/chosen/rejected must be strings")
    return {
        "trace_id": trace_id,
        "prompt": prompt,
        "chosen": chosen,
        "rejected": rejected,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a PCB placement repair policy. Emit placement "
                    "action lines only. The geometry verifier is authoritative."
                ),
            },
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": chosen},
        ],
        "metadata": {
            "model_id": model_id,
            "failure_kind": failure_kind,
            "chosen_source": chosen_source,
            "profile": profile_name,
            "input_origin": input_origin,
            "run_completed": run_completed,
        },
    }


def export_traces(source: Path, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            for trace in read_traces(source):
                row = post_training_row(trace)
                handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export verifier failures for SFT or preference training"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    count = export_traces(args.source, args.destination)
    print(f"exported {count} failure traces to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

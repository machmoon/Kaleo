"""LoRA SFT for the speculative placement policy on Thinking Machines Tinker."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "Qwen/Qwen3.5-4B"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("data", help="Conversation JSONL from export_placement_sft.py")
    parser.add_argument("--log-dir", default="artifacts/tinker-placement-sft")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--test-size", type=int, default=64)
    return parser


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _validate_messages(value: Any, line_number: int) -> None:
    if not isinstance(value, list) or not value:
        raise SystemExit(f"training line {line_number} needs a non-empty messages list")
    for message in value:
        if not isinstance(message, dict):
            raise SystemExit(f"training line {line_number} has a non-object message")
        if message.get("role") not in {"system", "user", "assistant"}:
            raise SystemExit(f"training line {line_number} has an invalid message role")
        if not isinstance(message.get("content"), str):
            raise SystemExit(f"training line {line_number} has non-text content")
    if not any(message.get("role") == "assistant" for message in value):
        raise SystemExit(f"training line {line_number} needs an assistant target")


def _validate_dataset(path: Path, test_size: int) -> int:
    count = 0
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, parse_constant=_reject_nonfinite)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SystemExit(
                    f"invalid training JSON on line {line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise SystemExit(f"training line {line_number} must be an object")
            _validate_messages(value.get("messages"), line_number)
            count += 1
    if count == 0:
        raise SystemExit("training data contains no examples")
    if test_size >= count:
        raise SystemExit(
            f"test size ({test_size}) must leave at least one of "
            f"{count} examples for training"
        )
    return count


def _validate(args: argparse.Namespace) -> None:
    if not Path(args.data).is_file():
        raise SystemExit(f"training data not found: {args.data}")
    if args.batch_size <= 0 or args.epochs <= 0:
        raise SystemExit("batch size and epochs must be positive")
    if args.max_steps is not None and args.max_steps <= 0:
        raise SystemExit("max steps must be positive when supplied")
    if args.test_size < 0:
        raise SystemExit("test size cannot be negative")
    if not args.model.strip():
        raise SystemExit("model cannot be empty")
    _validate_dataset(Path(args.data), args.test_size)
    log_dir = Path(args.log_dir)
    if log_dir.exists() and (
        not log_dir.is_dir() or any(log_dir.iterdir())
    ):
        raise SystemExit(
            f"log directory is not empty: {log_dir}; choose a new --log-dir"
        )
    if not os.environ.get("TINKER_API_KEY"):
        raise SystemExit("TINKER_API_KEY is required; create it in the Tinker console")


def _non_thinking_renderer(model_info: Any, model: str) -> str:
    """Match runtime inference, which explicitly disables model thinking."""
    recommended = model_info.get_recommended_renderer_name(model)
    non_thinking = {
        "qwen3": "qwen3_disable_thinking",
        "qwen3_5": "qwen3_5_disable_thinking",
        "qwen3_8_xhigh_reasoning": "qwen3_8_disable_thinking",
        "qwen3_8_medium_reasoning": "qwen3_8_disable_thinking",
        "qwen3_8_low_reasoning": "qwen3_8_disable_thinking",
        "deepseekv3_thinking": "deepseekv3",
        "kimi_k25": "kimi_k25_disable_thinking",
        "kimi_k26": "kimi_k26_disable_thinking",
        "nemotron3": "nemotron3_disable_thinking",
        "nemotron3_ultra": "nemotron3_ultra_disable_thinking",
    }
    return non_thinking.get(recommended, recommended)


def main() -> int:
    args = _parser().parse_args()
    _validate(args)

    from tinker_cookbook import model_info
    from tinker_cookbook.renderers import TrainOnWhat
    from tinker_cookbook.supervised import train
    from tinker_cookbook.supervised.data import FromConversationFileBuilder
    from tinker_cookbook.supervised.types import ChatDatasetBuilderCommonConfig

    renderer = _non_thinking_renderer(model_info, args.model)
    common = ChatDatasetBuilderCommonConfig(
        model_name_for_tokenizer=args.model,
        renderer_name=renderer,
        max_length=4096,
        batch_size=args.batch_size,
        train_on_what=TrainOnWhat.ALL_ASSISTANT_MESSAGES,
    )
    dataset = FromConversationFileBuilder(
        common_config=common,
        file_path=str(Path(args.data)),
        test_size=args.test_size,
        shuffle_seed=0,
    )
    config = train.Config(
        log_path=args.log_dir,
        model_name=args.model,
        recipe_name="silkscreen_placement_sft",
        renderer_name=renderer,
        dataset_builder=dataset,
        learning_rate=2e-4,
        lr_schedule="linear",
        num_epochs=args.epochs,
        lora_rank=32,
        eval_every=0,
        save_every=0,
        max_steps=args.max_steps,
        submit_ahead=1,
    )
    asyncio.run(train.main(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``python -m meetings poll`` -- read finished meetings, draft what they asked for.

One subcommand. ``poll`` validates the configuration, asks Meet for every
in-scope conference that finished inside the age window, and runs
:func:`~meetings.runner.poll_once` once -- or every ``--every`` seconds until
interrupted. Everything the poll declined to do is printed alongside what it
built: every request the model found, its verbatim quote, and whether it was
built, skipped below the confidence floor, or failed. A report that listed
only the boards would make a skipped request invisible, which is exactly the
"it ignored what I asked for" failure this package exists to avoid.

Meetings already handled are remembered in ``--seen-file`` (a JSON list of
conference record names) so a restart does not re-run every meeting in the
window, each one a paid pipeline run. Without the flag the memory is
process-local and says so.

Exit codes follow the CLI's convention: 0 success, 1 a Meet API failure,
2 a configuration problem (missing token, missing API key, bad value).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import ConfigError, MeetConfig
from .meet import MeetClient, MeetError
from .runner import MeetingReport, poll_once

__all__ = ["main", "render_report"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meetings",
        description="Google Meet transcripts in, drafted KiCad boards out.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    poll = sub.add_parser(
        "poll",
        help="process every in-scope conference that finished recently",
    )
    poll.add_argument(
        "--every", type=float, default=None, metavar="SECONDS",
        help="keep polling at this interval until interrupted (default: once)",
    )
    poll.add_argument(
        "--seen-file", default=None, metavar="PATH",
        help="JSON file remembering conferences already handled across runs "
             "(default: remember only for this process)",
    )
    poll.add_argument(
        "-o", "--output", default=None, metavar="DIR",
        help="write each drafted board under DIR/<n>-<intent>/board.kicad_pcb "
             "(default: keep results in memory and print the summary)",
    )
    poll.add_argument(
        "--model", default=None,
        help="worker model (default: the engine default)",
    )
    poll.add_argument(
        "--time-limit", type=float, default=20.0,
        help="placement solver budget in seconds",
    )
    poll.add_argument(
        "--no-review", action="store_true",
        help="skip the adversarial review pass",
    )
    poll.add_argument(
        "--no-route", action="store_true",
        help="stop after placement, leaving the copper empty",
    )
    return parser


# -- memory across runs ------------------------------------------------------


def _load_seen(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    try:
        names = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"--seen-file {path} is not a JSON list: {exc}") from exc
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ConfigError(f"--seen-file {path} is not a JSON list of strings")
    return set(names)


def _save_seen(path: Path | None, seen: set[str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(seen), indent=0) + "\n", "utf-8")
    tmp.replace(path)


# -- output ------------------------------------------------------------------


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "board"


def _generate_into(directory: Path, generate: Callable[..., Any]) -> Callable[..., Any]:
    """Give every drafted board its own directory under ``directory``.

    ``run_meeting`` passes the same keyword arguments to every request, so a
    single ``output=`` would make the second board overwrite the first. The
    counter lives here, in the one process that knows the order.
    """
    counter = 0

    def generate_numbered(model, intent, **kwargs):
        nonlocal counter
        counter += 1
        target = directory / f"{counter:03d}-{_slug(intent)}" / "board.kicad_pcb"
        target.parent.mkdir(parents=True, exist_ok=True)
        return generate(model, intent, output=target, **kwargs)

    return generate_numbered


def render_report(report: MeetingReport) -> str:
    """Everything one meeting produced and declined, as printable lines."""
    lines = [report.summary()]
    runs_by_request = {id(run.request): run for run in report.runs}
    for request in report.considered:
        lines.append(f"  request: {request}")
        lines.append(f'    quote: "{request.quote}"')
        run = runs_by_request.get(id(request))
        if run is None:
            lines.append("    not run")
        elif run.built:
            summary = getattr(run.result, "summary", None)
            lines.append(f"    built: {summary() if callable(summary) else run.result}")
            for path in getattr(run.result, "artifacts", None) or []:
                lines.append(f"    wrote {path}")
        elif run.skipped:
            lines.append(f"    skipped: {run.skipped}")
        else:
            lines.append(f"    failed: {run.error}")
    for warning in report.warnings:
        lines.append(f"  warning: {warning}")
    return "\n".join(lines)


# -- the command --------------------------------------------------------------


def _build_model(name: str | None):
    from silkscreen.agents.model import (  # noqa: PLC0415
        DEFAULT_MODEL,
        GeminiModel,
        ModelError,
    )

    try:
        return GeminiModel(name or DEFAULT_MODEL)
    except ModelError as exc:
        # A missing key or a bad model name is configuration, not a run failure.
        raise ConfigError(str(exc)) from exc


def _cmd_poll(
    args: argparse.Namespace,
    config: MeetConfig,
    *,
    transport,
    model,
    generate,
    sleep: Callable[[float], None],
) -> int:
    if args.every is not None and args.every <= 0:
        raise ConfigError("--every must be a positive number of seconds")
    seen_path = Path(args.seen_file).expanduser() if args.seen_file else None
    seen = _load_seen(seen_path)
    if seen_path is None:
        print("memory of handled meetings is process-local; pass --seen-file "
              "to keep it across runs")

    client = MeetClient(config, transport)
    model = model if model is not None else _build_model(args.model)
    kwargs: dict[str, Any] = {
        "review": not args.no_review,
        "route": not args.no_route,
        "time_limit_s": args.time_limit,
    }
    if generate is None and args.output:
        from silkscreen.agents import generate_pcb  # noqa: PLC0415

        generate = generate_pcb
    if generate is not None:
        if args.output:
            generate = _generate_into(Path(args.output), generate)
        kwargs["generate"] = generate

    while True:
        try:
            reports = poll_once(client, model, seen=seen, config=config, **kwargs)
        except MeetError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            _save_seen(seen_path, seen)
        if not reports:
            print(f"no unhandled conference finished in the last "
                  f"{config.max_age_hours:g} h ({len(seen)} already handled)")
        for report in reports:
            print(render_report(report))
        if args.every is None:
            return 0
        try:
            sleep(args.every)
        except KeyboardInterrupt:
            return 0


def main(
    argv: list[str] | None = None,
    *,
    transport=None,
    model=None,
    generate: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    env: dict[str, str] | None = None,
) -> int:
    """Entry point. The keyword seams exist so the tests never open a socket,
    never construct a Gemini client, and never run the solver."""
    args = _parser().parse_args(argv)
    try:
        config = MeetConfig.from_env(env)
        return _cmd_poll(
            args, config,
            transport=transport, model=model, generate=generate, sleep=sleep,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

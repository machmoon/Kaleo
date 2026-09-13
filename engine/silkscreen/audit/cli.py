"""``silkscreen-review board.kicad_pcb --effort deep -o review/``.

A separate entry point from ``python -m silkscreen`` on purpose: this reviews
a board that already exists, whoever laid it out, and never generates one. It
runs with no API key and no network -- the deterministic half is the whole
review at ``--effort quick``, and at the other levels a missing key degrades to
that half with the reason printed, rather than failing.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from . import PROFILES, Effort, review_board, slider, write_reports
from .report import json_report, text_report

__all__ = ["main"]


def _load_dotenv(path: Path) -> None:
    """Same minimal reader the generate CLI uses, for the same reason."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _build_model(name: str | None):
    """A live model, or None with the reason printed. Never raises."""
    try:
        from ..agents.model import DEFAULT_MODEL, GeminiModel, ModelError
    except ImportError as exc:
        print(f"note: model layer unavailable ({exc}); running rules only",
              file=sys.stderr)
        return None
    try:
        return GeminiModel(name or DEFAULT_MODEL)
    except ModelError as exc:
        print(f"note: {exc}; running the deterministic half only", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="silkscreen-review",
        description=(
            "Review a .kicad_pcb: deterministic geometry checks plus an "
            "optional model pass, rendered onto the board."
        ),
    )
    parser.add_argument("board", help="the .kicad_pcb to review")
    parser.add_argument(
        "-e", "--effort", default=Effort.STANDARD.value,
        choices=[level.value for level in Effort],
        help="how hard to think (default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output", metavar="DIR",
        help="write review.html, review.svg and review.json here",
    )
    parser.add_argument("--model", help="override the model name")
    parser.add_argument(
        "--no-model", action="store_true",
        help="deterministic rules only, whatever the effort level asks for",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the JSON report to stdout"
    )
    parser.add_argument(
        "--open", action="store_true",
        help="open the HTML report in a browser (implies --output)",
    )
    parser.add_argument(
        "--fail-on-blocker", action="store_true",
        help="exit 1 when any blocker is found, for CI",
    )
    args = parser.parse_args(argv)

    _load_dotenv(Path.cwd() / ".env")

    profile = PROFILES[Effort(args.effort)]
    model = None
    if profile.uses_model and not args.no_model:
        model = _build_model(args.model)

    print(f"reviewing {args.board}", file=sys.stderr)
    print(f"thinking: {slider(profile.level)}", file=sys.stderr)

    try:
        result = review_board(args.board, effort=profile.level, model=model)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json_report(result), end="")
    else:
        print(text_report(result), end="")

    out_dir = args.output or ("review" if args.open else None)
    if out_dir:
        written = write_reports(result, out_dir)
        print()
        for path in written:
            print(f"wrote {path}")
        if args.open:
            html = next(p for p in written if p.suffix == ".html")
            webbrowser.open(html.resolve().as_uri())

    if args.fail_on_blocker and result.blockers:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

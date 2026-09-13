"""Fail if the docs quote a test count that is no longer true.

    python scripts/check_docs.py

The test count is quoted in prose in two documents and verified by neither, so
it goes stale every time tests are added -- it has already drifted twice
(40 -> 54 -> 143). Prose that nothing checks is prose that rots, and a
submission document claiming the wrong number is worse than one claiming no
number at all.

This asks pytest for the real count rather than counting ``def test_`` lines,
because parametrised tests expand at collection time: ``test_mcp.py`` has 19
test functions and collects 23 cases. A static count would be wrong by four and
would drift again the moment someone adds a ``parametrize``.

Run as its own CI step rather than as a test, so that pytest is never invoked
from inside pytest.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Documents that quote the count, and the patterns that quote it. Each pattern
#: must capture the number in group 1.
DOCS = ("README.md", "DEVPOST.md")
PATTERNS = (
    re.compile(r"(\d+)\s+tests\b"),
    re.compile(r"(\d+)\s+passed\b"),
)

#: Phrases that look like a count but are not one -- a historical figure being
#: quoted precisely because it is out of date. Checked against the whole line.
IGNORE = re.compile(r"\bwas\b|\bpreviously\b|->|\bdrift", re.IGNORECASE)

#: A claim on a line naming one of these maps to those test files rather than to
#: the total. The README's status table quotes a per-module count per row, which
#: is a different true number from the suite total; checking it against the
#: total would be a false positive on every row. A module may map to several
#: files -- service/ is covered by two -- in which case the counts are summed.
#: Order matters: the first key found in the line wins, so a row naming two
#: modules is attributed to the one whose tests actually cover it.
MODULES: dict[str, tuple[str, ...]] = {
    "packing.py": ("test_packing.py",),
    "netlist.py": ("test_netlist.py",),
    "kicad.py": ("test_kicad.py",),
    "board.py": ("test_board.py",),
    "schematic.py": ("test_schematic.py",),
    "routing.py": ("test_routing.py",),
    "footprints.py": ("test_footprints.py",),
    "retrieval.py": ("test_retrieval.py",),
    "resilience.py": ("test_resilience.py",),
    "adk/": ("test_adk.py",),
    "agents/": ("test_agents.py",),
    "mcp/": ("test_mcp.py",),
    "audit/": ("test_audit.py",),
    "service/": ("test_app.py", "test_cache.py"),
    "slackbot/": (
        "test_commands.py",
        "test_slack.py",
        "test_render.py",
        "test_blocks.py",
        "test_slack_order.py",
        "test_config.py",
        "test_slack_runner.py",
        "test_http.py",
        "test_end_to_end.py",
    ),
    "googleapps/": (
        "test_addresses.py",
        "test_auth.py",
        "test_calendar.py",
        "test_chat.py",
        "test_gmail.py",
        "test_transport.py",
        "test_workspace_cli.py",
        "test_workspace_config.py",
        "test_workspace_end_to_end.py",
    ),
}


def collect() -> tuple[int, dict[str, int]]:
    """Ask pytest for the real total and the real per-file counts."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("error: pytest collection failed; cannot verify docs")

    per_file: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if "::" not in line:
            continue
        name = Path(line.split("::", 1)[0]).name
        per_file[name] = per_file.get(name, 0) + 1

    # The summary line is like "160 tests collected in 0.39s".
    match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
    if match is None:
        raise SystemExit(
            "error: could not find a collection summary in pytest output.\n"
            + proc.stdout[-2000:]
        )
    return int(match.group(1)), per_file


def expected_for(
    line: str, total: int, per_file: dict[str, int]
) -> tuple[int, str] | None:
    """What this line's number should be, and what to call it in an error.

    Returns ``None`` when the claim cannot be verified -- a module whose tests do
    not live in a test file of the matching name, such as ``footprints.py``.
    Reporting those as zero would be a false alarm, and a check that cries wolf
    is a check people learn to skip.
    """
    for module, test_files in MODULES.items():
        if module in line:
            known = [f for f in test_files if f in per_file]
            if not known:
                return None
            return sum(per_file[f] for f in known), " + ".join(known)
    return total, "the suite"


def fix_line(line: str, total: int, per_file: dict[str, int]) -> str:
    """Rewrite every stale number on one line, leaving the rest untouched.

    Spans are replaced right-to-left so an earlier replacement cannot shift the
    offsets of a later one.
    """
    resolved = expected_for(line, total, per_file)
    if resolved is None:
        return line
    expected, _ = resolved

    spans = [
        m.span(1)
        for pattern in PATTERNS
        for m in pattern.finditer(line)
        if int(m.group(1)) != expected
    ]
    for start, end in sorted(spans, reverse=True):
        line = line[:start] + str(expected) + line[end:]
    return line


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fix = "--fix" in argv

    total, per_file = collect()

    wrong: list[str] = []
    checked = 0
    for name in DOCS:
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        changed = False

        for index, raw in enumerate(lines):
            line = raw.rstrip("\r\n")
            if IGNORE.search(line):
                continue
            for pattern in PATTERNS:
                for match in pattern.finditer(line):
                    claimed = int(match.group(1))
                    resolved = expected_for(line, total, per_file)
                    if resolved is None:
                        continue
                    expected, what = resolved
                    checked += 1
                    if claimed == expected:
                        continue
                    if fix:
                        changed = True
                    else:
                        wrong.append(
                            f"  {name}:{index + 1}: claims {claimed}, "
                            f"{what} has {expected}\n      {line.strip()}"
                        )

            if fix:
                fixed = fix_line(line, total, per_file)
                if fixed != line:
                    lines[index] = fixed + raw[len(line) :]

        if fix and changed:
            path.write_text("".join(lines), encoding="utf-8")
            print(f"fixed: {name}")

    if fix:
        # Re-check, so --fix can never report success on something it did not
        # actually correct.
        return main([])

    if wrong:
        print(f"error: docs quote a stale test count (pytest collects {total}):")
        print("\n".join(wrong))
        print(
            "\nRun `python scripts/check_docs.py --fix` to update them, "
            "or adjust scripts/check_docs.py"
        )
        return 1

    print(
        f"docs ok: {checked} claim(s) across {len(DOCS)} files "
        f"match a suite of {total}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

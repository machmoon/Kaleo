"""``silkscreen setup``: the one command between a fresh clone and a running app.

Everything the engine does works offline, so this is deliberately not a gate --
it reports what is present, asks once for the key the agents layer needs, and
prints the exact next command. It writes exactly one file, ``.env``, and never
prints the key back: a secret echoed to a terminal ends up in scrollback, in
``script`` transcripts, and in whatever the user pastes into a bug report.

Also home to the ``silkscreen`` console-script dispatcher, so ``silkscreen
setup`` and ``silkscreen serve`` are subcommands while any other argument is
still the plain-language intent that ``python -m silkscreen`` has always taken.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import sys
from importlib.util import find_spec
from pathlib import Path

__all__ = ["dispatch", "load_dotenv", "main", "repo_root"]

MIN_PYTHON = (3, 11)

_KEY = "GOOGLE_API_KEY"

# Import name -> what stops working without it. Probed rather than imported:
# find_spec answers in microseconds, while importing google-genai or ortools
# costs seconds that a status report has no reason to spend.
_EXTRAS = (
    ("ortools", "engine", "placement solver (CP-SAT)"),
    ("kiutils", "engine", "KiCad board read/write"),
    ("google.genai", "agents", "Gemini model calls"),
    ("pypdf", "agents", "datasheet PDF reading"),
    ("google.cloud.firestore", "cloud", "Firestore datasheet-fact cache"),
    ("google.adk", "adk", "default pipeline engine (ADK workflow)"),
    ("pytest", "dev", "test suite"),
)

# The engine name pipeline.generate_pcb resolves, duplicated here rather than
# imported: importing the pipeline drags in the whole agents layer (and adk
# itself), which is exactly what this report exists to check for. Keep in step
# with agents/pipeline.py if the default ever moves again.
_ENGINE_VAR = "SILKSCREEN_ENGINE"
_DEFAULT_ENGINE = "adk"


def _installed(module: str) -> bool:
    """Is ``module`` importable, without importing it?

    find_spec raises rather than returning None for a dotted name whose parent
    is missing or is not a package -- ``google.cloud.firestore`` does exactly
    that when only google-genai is installed, which turned a status report into
    a traceback. Any failure to resolve means "not installed".
    """
    try:
        return find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def repo_root() -> Path:
    """The checkout this package was installed from, editable-install style.

    ``service/`` and ``frontend/`` live beside ``engine/`` and are not part of
    the installed package, so both setup and serve have to find the repository
    on disk rather than by import. From ``engine/silkscreen/onboard.py`` that
    is three parents up.
    """
    return Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> None:
    """Minimal .env reader, the same one both CLIs use, for the same reason.

    ``setdefault``, never assignment: a value already exported into the
    environment is a deliberate override and must win over the file.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_env_key(path: Path) -> str:
    """The value of GOOGLE_API_KEY in .env, or "" if unset or absent."""
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == _KEY:
            return value.strip().strip('"').strip("'")
    return ""


def _write_env_key(path: Path, example: Path, value: str) -> None:
    """Set GOOGLE_API_KEY in .env, rewriting in place and keeping everything else.

    The file is seeded from .env.example when missing so the other documented
    settings stay visible, and an existing file is edited line by line rather
    than rewritten wholesale -- clobbering a teammate's GOOGLE_CLOUD_PROJECT to
    set one key would be a silent, and very confusing, regression.
    """
    if path.exists():
        lines = path.read_text().splitlines()
    elif example.exists():
        lines = example.read_text().splitlines()
    else:
        lines = [f"{_KEY}="]

    replaced = False
    for i, line in enumerate(lines):
        key, sep, _ = line.partition("=")
        if sep and key.strip() == _KEY:
            lines[i] = f"{_KEY}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{_KEY}={value}")

    path.write_text("\n".join(lines) + "\n")
    # Owner-only: .env holds a live credential and lands in a directory the
    # user may well be sharing over Dropbox or a shared machine. Windows has no
    # equivalent bit and raises or no-ops here, which is not worth failing over.
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _prompt_key(existing: str, interactive: bool) -> str | None:
    """The key to store, or None to leave .env alone.

    Returns None on every path where the user has not clearly asked for a
    change: no tty, an empty answer, or an existing key they declined to
    replace. Overwriting a working credential because someone pressed Enter is
    the failure this shape guards against.
    """
    from getpass import getpass

    if not interactive:
        return None

    if existing:
        print(f"  a {_KEY} is already set in .env "
              f"(ends {existing[-4:]!r}, {len(existing)} chars)")
        answer = input("  replace it? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("  keeping the existing key")
            return None

    print("  paste a Gemini API key (https://aistudio.google.com/apikey),")
    print("  or press Enter to skip -- the engine, tests and board review all")
    print("  run without one.")
    try:
        # getpass, not input: the key must never reach the terminal, the
        # scrollback, or a copied-and-pasted session transcript.
        value = getpass("  key: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return value or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="silkscreen setup",
        description="Check the install, store an API key, and print how to start.",
    )
    parser.add_argument(
        "--no-input", action="store_true",
        help="report only; never prompt, never write .env",
    )
    parser.add_argument(
        "--no-kicad-check", action="store_true",
        help="skip looking for a KiCad install (it is never required)",
    )
    args = parser.parse_args(argv)

    # A pipe, a CI runner, or `| tee` all land here. Printing instructions
    # instead of prompting is the difference between a script that finishes and
    # one that hangs forever on a read nobody can answer.
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.no_input

    root = repo_root()
    env_path = root / ".env"
    example = root / ".env.example"

    print("Silkscreen setup")
    print(f"  repository : {root}")

    # -- python ------------------------------------------------------------
    version = sys.version_info[:3]
    ok = sys.version_info[:2] >= MIN_PYTHON
    mark = "ok " if ok else "!! "
    print(f"  python     : {mark}{'.'.join(str(n) for n in version)} "
          f"(need >= {'.'.join(str(n) for n in MIN_PYTHON)})")
    if not ok:
        print()
        print("error: this interpreter is too old. Re-run scripts/install.sh "
              "(or scripts/install.ps1) to build a virtualenv on a newer Python.",
              file=sys.stderr)
        return 2

    # -- extras ------------------------------------------------------------
    print("  packages   :")
    missing_extras: set[str] = set()
    for module, extra, purpose in _EXTRAS:
        present = _installed(module)
        if not present:
            missing_extras.add(extra)
        print(f"      {'ok ' if present else '-- '}{module:<24} "
              f"[{extra}]  {purpose}")
    if missing_extras:
        wanted = ",".join(sorted(missing_extras))
        print(f"    install the rest with: pip install -e '.[{wanted}]'")

    # -- engine ------------------------------------------------------------
    # Not cosmetic: adk is the default engine, so a checkout installed without
    # that extra raises on every generate call. Worth stating outright rather
    # than leaving it as one "--" among six.
    chosen = os.environ.get(_ENGINE_VAR, "") or _DEFAULT_ENGINE
    source = f"{_ENGINE_VAR}={chosen}" if os.environ.get(_ENGINE_VAR) else "default"
    if chosen == _DEFAULT_ENGINE and not _installed("google.adk"):
        print(f"  engine     : !!  {chosen} ({source}) -- but google-adk "
              "is not installed")
        print("               every board generation will fail until you run:")
        print("                   pip install -e '.[dev,agents,cloud,adk]'")
        print(f"               or switch engines with {_ENGINE_VAR}=sdk")
    else:
        print(f"  engine     : ok  {chosen} ({source})")
        if chosen != "sdk":
            print(f"               {_ENGINE_VAR}=sdk selects the plain SDK pipeline")

    # -- web bundle --------------------------------------------------------
    dist = Path(os.getenv("SILKSCREEN_WEB_DIST") or root / "frontend" / "dist")
    if (dist / "index.html").exists():
        print(f"  web UI     : ok  built at {dist}")
    else:
        print(f"  web UI     : --  not built ({dist} has no index.html)")
        print("               the API still serves; build it with "
              "'cd frontend && npm ci && npm run build'")

    # -- kicad (a convenience, never a requirement) ------------------------
    if not args.no_kicad_check:
        _report_kicad(interactive)

    # -- api key -----------------------------------------------------------
    print("  api key    :")
    existing = _read_env_key(env_path)
    if os.getenv(_KEY) and not existing:
        # An exported key is a perfectly good configuration; saying so stops
        # the user from concluding that setup did nothing.
        print(f"      ok  {_KEY} is exported in this environment (no .env needed)")
    elif existing:
        print(f"      ok  {_KEY} is set in {env_path}")
    else:
        print(f"      --  no {_KEY} in {env_path}")

    if not interactive and not existing and not os.getenv(_KEY):
        print()
        print("  non-interactive: not prompting for a key. Either export it")
        print(f"      export {_KEY}=...")
        print(f"  or write it to {env_path} (copy {example.name} first).")
    else:
        value = _prompt_key(existing, interactive)
        if value is not None:
            _write_env_key(env_path, example, value)
            # Length only, never the value.
            print(f"      wrote a {len(value)}-character key to {env_path}")

    print()
    print("Start the app:")
    print(f"  {_launcher_hint()} serve")
    print()
    print("Or generate a board from the command line:")
    print(f"  {_launcher_hint()} \"an stm32 stepper driver\"")
    return 0


def _kicad_version(cli: str) -> str:
    """``kicad-cli --version``, or "" if it will not answer.

    Never raises and never blocks for long: this is a nicety in a status
    report, and a wedged or half-installed binary must not hold up setup.
    """
    import subprocess

    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [cli, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout \
        else ""


def _find_kicad() -> tuple[str, str]:
    """``(where, version)``; ``where`` is "" when KiCad is not installed.

    Silkscreen never launches KiCad -- the .kicad_pcb file is the whole API, and
    the engine, the tests and scripts/demo.py all run with no KiCad anywhere.
    This looks for it purely so setup can say whether the boards it writes will
    open on a double-click, which is why every branch below is informational.
    """
    from shutil import which

    cli = which("kicad-cli")
    if cli:
        return cli, _kicad_version(cli)

    if sys.platform == "darwin":
        # Both layouts ship in the wild: the 7/8 installers drop an outer
        # KiCad folder, older and some homebrew-cask installs do not.
        for app in (Path("/Applications/KiCad/KiCad.app"),
                    Path("/Applications/KiCad.app")):
            if not app.exists():
                continue
            # The bundle ships its own kicad-cli even when nothing was
            # symlinked onto PATH, which is the usual macOS install.
            bundled = app / "Contents" / "MacOS" / "kicad-cli"
            version = _kicad_version(str(bundled)) if bundled.exists() else ""
            return str(app), version
    elif os.name == "nt":
        # os.environ is case-insensitive on Windows; the capitalised spelling
        # is what ruff wants and resolves the same ProgramFiles value.
        base = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "KiCad"
        if base.exists():
            # Versioned subdirectories (KiCad\8.0\bin\kicad-cli.exe).
            for exe in sorted(base.glob("*/bin/kicad-cli.exe"), reverse=True):
                return str(exe), _kicad_version(str(exe))
            return str(base), ""
    else:
        gui = which("kicad")
        if gui:
            return gui, ""
        flatpak = Path("/var/lib/flatpak/app/org.kicad.KiCad")
        if flatpak.exists():
            return "flatpak: org.kicad.KiCad", ""

    return "", ""


def _offer_kicad_install() -> None:
    """Ask -- once, explicitly -- whether to install KiCad, then hand off.

    Only ever called on a tty. The confirmation defaults to no and only a typed
    "y"/"yes" proceeds, because installing a multi-gigabyte application is not
    something anyone should get by pressing Enter through a setup script. The
    work is always delegated to the platform's own package manager: no remote
    script is fetched, nothing is piped into a shell, and when there is no
    package manager to delegate to we print the download page rather than
    inventing a fallback.
    """
    import subprocess
    from shutil import which

    if sys.platform == "darwin":
        manager, argv = "Homebrew", ["brew", "install", "--cask", "kicad"]
    elif os.name == "nt":
        manager, argv = "winget", ["winget", "install", "--id", "KiCad.KiCad"]
    else:
        # apt, dnf, pacman and flatpak all name it differently and package
        # different versions; guessing wrong is worse than saying so.
        print("      install it with your distribution's package manager "
              "(apt/dnf/pacman/flatpak),")
        print("      or download it from https://www.kicad.org/download/")
        return

    if which(argv[0]) is None:
        print(f"      {manager} is not installed; download KiCad from "
              "https://www.kicad.org/download/")
        return

    try:
        answer = input(f"      install it now with {manager}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer not in {"y", "yes"}:
        print("      skipped -- https://www.kicad.org/download/ when you want it")
        return

    print(f"      running: {' '.join(argv)}")
    try:
        code = subprocess.call(argv)  # noqa: S603 - fixed argv, no shell
    except OSError as exc:
        print(f"      could not run {manager}: {exc}")
        return
    if code != 0:
        print(f"      {manager} exited {code}; "
              "see https://www.kicad.org/download/")


def _report_kicad(interactive: bool) -> None:
    """The KiCad line of the status report. Never fails setup.

    KiCad is a convenience here, not a dependency: absent, everything still
    works and the only thing the user loses is a double-click that opens the
    board Silkscreen wrote. The wording matters -- an alarming "missing" line
    would misrepresent a deliberate design property of the engine.
    """
    where, version = _find_kicad()
    if where:
        label = version or "found"
        print(f"  kicad      : ok  {label}")
        print(f"               {where}")
        print("               boards Silkscreen writes will open directly")
        return

    print("  kicad      : --  not found")
    print("               Silkscreen does not need it: the engine writes")
    print("               .kicad_pcb files itself. You will want it to open them.")
    if interactive:
        _offer_kicad_install()
    else:
        print("               https://www.kicad.org/download/")


def _launcher_hint() -> str:
    """How to invoke this install again, matching the platform's venv layout."""
    if os.name == "nt":
        return r".\.venv\Scripts\silkscreen.exe"
    return "./.venv/bin/silkscreen"


def dispatch(argv: list[str] | None = None) -> int:
    """The ``silkscreen`` console script: two subcommands, else an intent.

    ``silkscreen setup`` and ``silkscreen serve`` are matched exactly, and
    anything else -- including a description that merely starts with those
    words -- falls through to the generate CLI unchanged, so adding this
    dispatcher cannot change what an existing invocation does.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "setup":
        return main(args[1:])
    if args and args[0] == "serve":
        from .serve import main as serve_main

        return serve_main(args[1:])

    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())

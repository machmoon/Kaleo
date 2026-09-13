"""Find ``kicad-cli`` and export a board's 3D model with it.

KiCad's 3D viewer is the picture a hardware engineer expects at the moment a
board is declared orderable, and ``kicad-cli pcb export glb|step`` produces it
without a KiCad process or plugin -- the board file stays the API. The binary
is found, never assumed: KiCad is a desktop install, not a dependency of this
package, and on a machine without it the step still succeeds and says so.

Every failure is named in :attr:`ExportReport.warnings` rather than swallowed:
a missing binary, a non-zero exit, a timeout, or an export that reported
success and left an empty file (KiCad has done this) each produce one entry.
A file is recorded in :attr:`ExportReport.files` only when it exists and is
non-empty, so a caller that sees a path can open it. Nothing here is a quiet
success.

Standard library only, the ``service/`` convention.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ExportReport", "find_kicad_cli", "export_models", "MODEL_FORMATS"]

#: The environment variable that names the binary outright.
ENV_VAR = "KICAD_CLI"

#: Fixed install locations tried after the environment and PATH, in order.
_FIXED_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/bin/kicad-cli",
)
_WINDOWS_GLOB = "C:/Program Files/KiCad/*/bin/kicad-cli.exe"

#: ``(files key, kicad-cli export subcommand, suffix)`` for each model written.
MODEL_FORMATS: tuple[tuple[str, str, str], ...] = (
    ("model_glb", "glb", ".glb"),
    ("model_step", "step", ".step"),
)

#: The export includes the board body and every component model (the
#: defaults), substitutes STEP models for VRML where one exists, and adds the
#: copper and mask so the picture is of the board that will be ordered rather
#: than a bare substrate. ``--force`` overwrites a stale export.
_EXPORT_FLAGS = (
    "--force",
    "--subst-models",
    "--include-tracks",
    "--include-pads",
    "--include-silkscreen",
    "--include-soldermask",
)

DEFAULT_TIMEOUT_S = 120.0
_STDERR_TAIL_CHARS = 400


@dataclass
class ExportReport:
    """What the export produced and everything it could not."""

    #: ``files`` key -> absolute path, only for non-empty outputs.
    files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def find_kicad_cli(environ: dict[str, str] | None = None) -> str | None:
    """The ``kicad-cli`` binary to run, or None.

    ``KICAD_CLI`` wins outright when set: an explicit configuration that names
    nothing is a misconfiguration to report, not a reason to run whichever
    binary happens to be on PATH. Otherwise PATH, then the fixed install
    locations, then the newest KiCad under ``C:/Program Files``.
    """
    env = os.environ if environ is None else environ
    configured = env.get(ENV_VAR, "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    for candidate in _FIXED_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    windows = sorted(glob.glob(_WINDOWS_GLOB))
    if windows:
        return windows[-1]
    return None


def _missing_reason(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    configured = env.get(ENV_VAR, "").strip()
    if configured:
        return f"{ENV_VAR}={configured!r} is not a file"
    return "kicad-cli not found (install KiCad or set KICAD_CLI)"


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) <= _STDERR_TAIL_CHARS:
        return text
    return "..." + text[-_STDERR_TAIL_CHARS:]


def export_models(
    board: Path,
    stem: Path,
    *,
    binary: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    environ: dict[str, str] | None = None,
) -> ExportReport:
    """Export ``board`` to ``<stem>.glb`` and ``<stem>.step``.

    ``stem`` is the output path without its suffix (``dir/my-board``). When
    ``binary`` is None the binary is found with :func:`find_kicad_cli`. One
    format failing does not stop the other; each failure is one warning.
    """
    report = ExportReport()
    cli = binary if binary is not None else find_kicad_cli(environ)
    if cli is None:
        report.warnings.append(
            f"no 3D model exported: {_missing_reason(environ)}"
        )
        return report
    for key, fmt, suffix in MODEL_FORMATS:
        out = Path(f"{stem}{suffix}")
        argv = [cli, "pcb", "export", fmt, *_EXPORT_FLAGS, "-o", str(out), str(board)]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            report.warnings.append(
                f"{fmt} export timed out after {timeout_s:g} s: "
                "kicad-cli did not finish"
            )
            continue
        except OSError as exc:
            report.warnings.append(f"{fmt} export could not start kicad-cli: {exc}")
            continue
        if proc.returncode != 0:
            tail = _tail(proc.stderr) or "(no stderr)"
            report.warnings.append(
                f"{fmt} export failed: kicad-cli exited {proc.returncode}: {tail}"
            )
            continue
        if not out.is_file() or out.stat().st_size == 0:
            tail = _tail(proc.stderr)
            detail = f": {tail}" if tail else ""
            report.warnings.append(
                f"{fmt} export wrote no file at {out} despite exit 0{detail}"
            )
            continue
        report.files[key] = str(out)
    return report

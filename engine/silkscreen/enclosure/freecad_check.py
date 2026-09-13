"""An independent reader for a finished STEP: FreeCAD, out of process.

Everything this package builds and measures runs on one OCCT, reached through
build123d. That is a blind spot of exactly the kind ``audit/geometry.py``
exists to close for ``.kicad_pcb`` files, and ``engine/tests/test_kicad.py``
closes with hand-written overlap maths: a checker written in terms of the
library that produced the artifact agrees with it while both are wrong. So
the *exported* assembly gets read back by a different program, with its own
OCCT build, and the two are compared.

**The hard rule, and the reason this module is a subprocess.** FreeCAD and
build123d each link their own copy of OCCT. Loading both into one interpreter
is undefined behaviour -- two OCCT runtimes, two sets of static handles, one
address space. So FreeCAD is reached *only* by running
``freecadcmd <script.py>`` as a child process and reading its stdout; nothing
in ``silkscreen`` may ever ``import FreeCAD``, and
``engine/tests/test_enclosure_assembly.py`` pins that by searching the package
source for the import.

FreeCAD is a **cross-check, never a builder**. It is not needed to make the
assembly (build123d's ``import_step`` reads kicad-cli's STEP directly), and
its absence costs this check and nothing else -- but the absence is said in
words naming the exact binary, never a quiet empty result:
:class:`~.errors.FreeCADUnavailable`.

The script it runs is the documented headless API -- ``FreeCAD.newDocument``
then ``Import.insert(step, doc)``, which is what the KiCad StepUp workbench
(``kicadStepUptools.py``) uses to pull a STEP into a document, minus the GUI
half (``ImportGui``) that a headless run has no display for.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import FreeCADUnavailable

__all__ = [
    "FREECAD_CMD_ENV",
    "FREECAD_MISSING",
    "crosscheck_step",
    "freecad_cmd",
]

#: Environment variable naming the ``freecadcmd`` binary.
FREECAD_CMD_ENV: str = "SILKSCREEN_FREECAD_CMD"

#: Where ``freecadcmd`` lives when it is not on ``PATH``.
FREECAD_CANDIDATES: tuple[str, ...] = (
    "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
    "/usr/bin/freecadcmd",
    "/usr/local/bin/freecadcmd",
)

FREECAD_MISSING: str = (
    "freecadcmd was not found; the independent STEP cross-check needs FreeCAD "
    f"-- install it, or point {FREECAD_CMD_ENV} at the binary"
)

DEFAULT_TIMEOUT_S: float = 120.0

#: The marker the script prints its JSON behind. FreeCAD writes its banner and
#: any OCCT chatter to the same stream, so the answer has to be findable
#: rather than assumed to be the whole of stdout.
_MARKER = "SILKSCREEN-FREECAD-JSON:"

_SCRIPT = '''\
# Written by silkscreen.enclosure.freecad_check; run by freecadcmd only.
import json, sys
import FreeCAD, Import  # noqa: F401 - FreeCAD's own headless API

step = sys.argv[-1]
doc = FreeCAD.newDocument("silkscreen")
Import.insert(step, doc.Name)
out = []
for obj in doc.Objects:
    shape = getattr(obj, "Shape", None)
    if shape is None or not shape.Solids:
        continue                      # datum planes and axes carry no solid
    bb = shape.BoundBox
    out.append({
        "label": obj.Label,
        "solids": len(shape.Solids),
        "volume_mm3": shape.Volume,
        "bbox_mm": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax],
    })
print("%s" + json.dumps(out))
'''


def freecad_cmd() -> str | None:
    """The ``freecadcmd`` to use, or ``None`` when there is none."""
    override = os.environ.get(FREECAD_CMD_ENV, "").strip()
    if override:
        return override if Path(override).exists() else None
    found = shutil.which("freecadcmd")
    if found:
        return found
    for candidate in FREECAD_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def crosscheck_step(
    step_path: str | Path,
    *,
    cmd: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """Re-read ``step_path`` in FreeCAD and report what it found.

    One dict per object carrying a solid: ``label``, ``solids``,
    ``volume_mm3`` and ``bbox_mm``. Note that FreeCAD reports both the
    assembly compound and its children, so a two-part case comes back as three
    rows -- the caller compares the rows it cares about by label.

    Raises :class:`~.errors.FreeCADUnavailable` when the binary is missing,
    times out, exits non-zero, or prints no answer. It never returns an empty
    list to mean "could not check": an empty list means FreeCAD read the file
    and found no solid in it, which is a real and different fact.
    """
    binary = cmd or freecad_cmd()
    if binary is None:
        raise FreeCADUnavailable(FREECAD_MISSING)
    step_path = Path(step_path)
    if not step_path.exists():
        raise FreeCADUnavailable(f"{step_path}: no such file to cross-check")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "silkscreen_freecad_check.py"
        script.write_text(_SCRIPT.replace('"%s"', repr(_MARKER)), encoding="utf-8")
        command = [binary, str(script), str(step_path)]
        shown = " ".join(command)
        try:
            done = subprocess.run(  # noqa: S603 - argv list, no shell
                command, capture_output=True, text=True,
                timeout=timeout_s, check=False,
            )
        except (FileNotFoundError, OSError) as exc:
            raise FreeCADUnavailable(f"{shown}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FreeCADUnavailable(
                f"{shown}: no answer in {timeout_s:g} s"
            ) from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        raise FreeCADUnavailable(
            f"{shown}: exit {done.returncode}: {tail[-1] if tail else 'no output'}"
        )
    for line in (done.stdout or "").splitlines():
        if line.startswith(_MARKER):
            return json.loads(line[len(_MARKER):])
    raise FreeCADUnavailable(f"{shown}: exit 0 but printed no {_MARKER} line")

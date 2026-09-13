"""The simulators, behind one interface.

The caller asked for LTspice; ngspice is what runs headless in CI. Both are here
behind :class:`Simulator` so an agent calling this package never has to know
which is installed, and the test suite can exercise the whole path on a machine
with no GUI.

They are genuinely different programs and the differences are not cosmetic:

* ngspice takes a ``.control`` block and can be told to write an ASCII rawfile.
  LTspice has no control block; it runs the analysis card in the deck and always
  writes a binary rawfile next to the input.
* ngspice reports most fatal problems on stderr and exits non-zero. LTspice's
  batch mode reports into a ``.log`` file beside the deck.
* ngspice exits **zero** on a singular matrix, having recovered via gmin
  stepping, and writes a perfectly well-formed rawfile. That case is the reason
  :attr:`RunOutcome.warnings` exists: an answer was produced, but the matrix was
  singular and a caller must be told, because the number is not trustworthy.

Both are given a wall-clock timeout and an output size cap. A transient with a
too-fine step is not a hang and not an error -- it is a hundred megabytes of
rawfile and a wedged agent -- so it is bounded here rather than discovered later.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .deck import Analysis, SpiceDeck
from .errors import ConvergenceError, SimulationFailed, SimulatorNotFound
from .raw import RawPlot, parse_rawfile

__all__ = [
    "Simulator",
    "RunOutcome",
    "NgspiceSimulator",
    "LTspiceSimulator",
    "find_simulator",
    "available_simulators",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_MAX_RAW_BYTES",
]

#: Wall clock a single run may take before it is killed.
DEFAULT_TIMEOUT_S = 60.0

#: Largest rawfile that will be parsed. A 1e-9 step over 1e-3 is a hundred
#: megabytes of ASCII and nothing useful; refusing it with the actual number is
#: more helpful than loading it.
DEFAULT_MAX_RAW_BYTES = 64 * 1024 * 1024

#: Environment overrides, checked before anything on PATH.
ENV_SIMULATOR = "SILKSCREEN_SPICE"
ENV_LTSPICE = "SILKSCREEN_LTSPICE"

_ERROR_PATTERNS = (
    re.compile(r"^\s*Error\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Fatal\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"could not find a valid modelname", re.IGNORECASE),
    re.compile(r"incomplete or empty netlist", re.IGNORECASE),
    re.compile(r"no simulations run", re.IGNORECASE),
    re.compile(r"unknown subckt", re.IGNORECASE),
    re.compile(r"unable to find definition of model", re.IGNORECASE),
)

_CONVERGENCE_PATTERNS = (
    re.compile(r"timestep too small", re.IGNORECASE),
    re.compile(r"iteration limit reached", re.IGNORECASE),
    re.compile(r"too many iterations without convergence", re.IGNORECASE),
    re.compile(r"no convergence", re.IGNORECASE),
    re.compile(r"transient analysis failed", re.IGNORECASE),
    re.compile(r"analysis failed", re.IGNORECASE),
)

#: Conditions that produce a result but invalidate it to some degree. These do
#: not stop the run; they are attached to it.
_WARNING_PATTERNS = (
    re.compile(r"singular matrix[^\n]*", re.IGNORECASE),
    re.compile(r"gmin stepping failed", re.IGNORECASE),
    re.compile(r"source stepping failed", re.IGNORECASE),
    re.compile(r"^\s*Warning:[^\n]*", re.IGNORECASE | re.MULTILINE),
)


@dataclass(frozen=True)
class RunOutcome:
    """One simulator invocation: the data, the log, and what went odd."""

    plot: RawPlot
    log: str
    warnings: tuple[str, ...]
    simulator: str
    deck_text: str


class Simulator(Protocol):
    """What the rest of the package needs from a SPICE program."""

    name: str

    def is_available(self) -> bool:
        ...

    def run(
        self, deck: SpiceDeck, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> RunOutcome:
        ...


def _classify(log: str) -> tuple[list[str], list[str]]:
    """Split a simulator log into fatal messages and warnings."""
    fatal: list[str] = []
    warnings: list[str] = []
    for pattern in _CONVERGENCE_PATTERNS:
        fatal.extend(m.group(0).strip() for m in pattern.finditer(log))
    for pattern in _ERROR_PATTERNS:
        fatal.extend(m.group(0).strip() for m in pattern.finditer(log))
    for pattern in _WARNING_PATTERNS:
        for match in pattern.finditer(log):
            text = match.group(0).strip()
            if text not in warnings:
                warnings.append(text)
    # A line counted as fatal must not also be reported as a warning.
    warnings = [w for w in warnings if not any(w in f or f in w for f in fatal)]
    return fatal, warnings


def _is_convergence(log: str) -> bool:
    return any(p.search(log) for p in _CONVERGENCE_PATTERNS)


def _read_raw(path: Path, max_bytes: int, log: str, deck_text: str) -> bytes:
    if not path.exists():
        raise SimulationFailed(
            f"the simulator wrote no rawfile ({path.name}); it did not get as "
            f"far as producing results",
            log=log,
            deck=deck_text,
        )
    size = path.stat().st_size
    if size > max_bytes:
        raise SimulationFailed(
            f"rawfile is {size / 1e6:.1f} MB, over the {max_bytes / 1e6:.0f} MB "
            f"cap. The analysis step is almost certainly far finer than needed; "
            f"raise Transient.step or the simulator's max_raw_bytes.",
            log=log,
            deck=deck_text,
        )
    if size == 0:
        raise SimulationFailed(
            "the simulator wrote an empty rawfile", log=log, deck=deck_text
        )
    return path.read_bytes()


class NgspiceSimulator:
    """ngspice in batch mode, writing an ASCII rawfile.

    The analysis is issued inside a ``.control`` block rather than as a bare
    card, because that is the only way to make ngspice write a rawfile in batch
    mode without a ``.print`` line, and ASCII is chosen so a failure to parse is
    debuggable by looking at the file.
    """

    name = "ngspice"

    def __init__(
        self,
        executable: str | None = None,
        *,
        max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
    ):
        self.executable = executable or os.environ.get(ENV_SIMULATOR) or "ngspice"
        self.max_raw_bytes = max_raw_bytes

    def is_available(self) -> bool:
        return (
            shutil.which(self.executable) is not None
            or Path(self.executable).exists()
        )

    def _control_command(self, analysis: Analysis) -> str:
        """``.tran 1e-6 1e-3 0`` becomes ``tran 1e-6 1e-3 0``."""
        return analysis.card().lstrip(".")

    def render(self, deck: SpiceDeck) -> str:
        return (
            deck.text
            + ".control\n"
            + "set filetype=ascii\n"
            + "set noaskquit\n"
            + f"{self._control_command(deck.analysis)}\n"
            + "write out.raw all\n"
            + ".endc\n"
            + ".end\n"
        )

    def run(
        self, deck: SpiceDeck, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> RunOutcome:
        if not self.is_available():
            raise SimulatorNotFound([self.executable])

        deck_text = self.render(deck)
        with tempfile.TemporaryDirectory(prefix="silkscreen-spice-") as tmp:
            work = Path(tmp)
            cir = work / "deck.cir"
            cir.write_text(deck_text, encoding="utf-8")

            try:
                proc = subprocess.run(
                    [self.executable, "-b", cir.name],
                    cwd=work,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SimulationFailed(
                    f"ngspice did not finish within {timeout_s:g} s and was "
                    f"killed. This is usually an analysis whose step is far too "
                    f"fine, or a circuit the solver cannot get through.",
                    deck=deck_text,
                ) from exc

            log = (proc.stdout or "") + (proc.stderr or "")
            fatal, warnings = _classify(log)

            if proc.returncode != 0 or fatal:
                message = (
                    f"ngspice failed (exit {proc.returncode})"
                    if proc.returncode
                    else "ngspice reported an error"
                )
                if _is_convergence(log):
                    raise ConvergenceError(
                        f"{message}: the solver did not converge",
                        log=log,
                        deck=deck_text,
                    )
                raise SimulationFailed(message, log=log, deck=deck_text)

            blob = _read_raw(work / "out.raw", self.max_raw_bytes, log, deck_text)

        plot = parse_rawfile(blob)
        return RunOutcome(
            plot=plot,
            log=log,
            warnings=tuple(warnings),
            simulator=f"ngspice ({self.executable})",
            deck_text=deck_text,
        )


class LTspiceSimulator:
    """LTspice in batch mode.

    **Status: not verified end to end.** LTspice is not installed on the machine
    this was developed on, so the invocation and discovery paths below are built
    from LTspice's documented batch interface and have never been executed. The
    rawfile reader they feed *is* tested, against both of LTspice's binary
    layouts -- so what is unproven is specifically the process launch, not the
    parse. Treat a first LTspice run as something to check, not something to
    trust.

    LTspice writes its rawfile beside the deck and takes the analysis from a card
    in the file, so no control block is emitted here.
    """

    name = "ltspice"

    #: Where LTspice installs itself, per platform.
    CANDIDATE_PATHS = (
        "/Applications/LTspice.app/Contents/MacOS/LTspice",
        "/Applications/LTspice.app/Contents/MacOS/LTspiceXVII",
        r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe",
        r"C:\Program Files\ADI\LTspice\LTspice.exe",
        r"C:\Program Files\LTC\LTspiceIV\scad3.exe",
    )

    def __init__(
        self,
        executable: str | None = None,
        *,
        max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
    ):
        self.executable = executable or os.environ.get(ENV_LTSPICE) or self._discover()
        self.max_raw_bytes = max_raw_bytes

    @classmethod
    def _discover(cls) -> str:
        for candidate in cls.CANDIDATE_PATHS:
            if Path(candidate).exists():
                return candidate
        for name in ("LTspice", "ltspice", "XVIIx64.exe"):
            found = shutil.which(name)
            if found:
                return found
        return "LTspice"

    def is_available(self) -> bool:
        return (
            Path(self.executable).exists()
            or shutil.which(self.executable) is not None
        )

    def render(self, deck: SpiceDeck) -> str:
        return deck.text + f"{deck.analysis.card()}\n.end\n"

    def run(
        self, deck: SpiceDeck, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> RunOutcome:
        if not self.is_available():
            raise SimulatorNotFound([self.executable, *self.CANDIDATE_PATHS])

        deck_text = self.render(deck)
        with tempfile.TemporaryDirectory(prefix="silkscreen-spice-") as tmp:
            work = Path(tmp)
            cir = work / "deck.cir"
            cir.write_text(deck_text, encoding="utf-8")

            try:
                proc = subprocess.run(
                    [self.executable, "-b", "-Run", str(cir)],
                    cwd=work,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SimulationFailed(
                    f"LTspice did not finish within {timeout_s:g} s and was killed.",
                    deck=deck_text,
                ) from exc

            log_file = work / "deck.log"
            log = (proc.stdout or "") + (proc.stderr or "")
            if log_file.exists():
                log += "\n" + log_file.read_text(encoding="latin-1", errors="replace")

            fatal, warnings = _classify(log)
            if proc.returncode != 0 or fatal:
                message = f"LTspice failed (exit {proc.returncode})"
                if _is_convergence(log):
                    raise ConvergenceError(
                        f"{message}: the solver did not converge",
                        log=log,
                        deck=deck_text,
                    )
                raise SimulationFailed(message, log=log, deck=deck_text)

            blob = _read_raw(work / "deck.raw", self.max_raw_bytes, log, deck_text)

        plot = parse_rawfile(blob)
        return RunOutcome(
            plot=plot,
            log=log,
            warnings=tuple(warnings),
            simulator=f"LTspice ({self.executable})",
            deck_text=deck_text,
        )


def available_simulators() -> list[Simulator]:
    """Every simulator this machine can actually run, best first."""
    found: list[Simulator] = []
    for candidate in (NgspiceSimulator(), LTspiceSimulator()):
        if candidate.is_available():
            found.append(candidate)
    return found


def find_simulator(prefer: str | None = None) -> Simulator:
    """Pick a simulator, honouring ``prefer`` (``"ngspice"`` / ``"ltspice"``).

    ngspice is tried first when there is no preference: it is scriptable,
    cross-platform, installable in CI, and the one this package is verified
    against. Raises :class:`~silkscreen.spice.errors.SimulatorNotFound` listing
    what was tried, rather than returning ``None`` for a caller to trip over.
    """
    candidates: list[Simulator] = [NgspiceSimulator(), LTspiceSimulator()]
    if prefer:
        wanted = prefer.strip().lower()
        matched = [c for c in candidates if c.name == wanted]
        if not matched:
            raise SimulatorNotFound(
                [f"{prefer!r} (known: {[c.name for c in candidates]})"]
            )
        chosen = matched[0]
        if not chosen.is_available():
            raise SimulatorNotFound([str(getattr(chosen, "executable", chosen.name))])
        return chosen

    for candidate in candidates:
        if candidate.is_available():
            return candidate
    raise SimulatorNotFound([str(c.executable) for c in candidates])  # type: ignore[attr-defined]

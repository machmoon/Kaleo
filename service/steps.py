"""The pipeline as approval-gated steps, for an engineer reviewing in KiCad.

``/generate`` runs every stage in one request and answers with the finished
board. That is right for a web viewer and wrong for the desktop overlay, where
the senior engineer reviews each stage in KiCad and says "go on" -- or does
not. This module exposes the same stage bodies (``agents/stages.py``) one at a
time, holding the run between steps in process memory:

    POST /steps                 read datasheets, propose a circuit -> schematic
    POST /steps/<id>/place      solve placement                    -> placed board
    POST /steps/<id>/route      lay copper                         -> routed board
    POST /steps/<id>/review     the critic argues against it
    POST /steps/<id>/sourcing   the bill of materials: MPN proposals, probed datasheets
    POST /steps/<id>/order      prepare (never submit) a fab order
    POST /steps/<id>/case       propose and verify a case (build123d/OCCT kernel)
    POST /steps/<id>/view3d     open KiCad's own 3D viewer on the routed board
    POST /steps/<id>/open_case  open the case STEP in FreeCAD
    GET  /steps/<id>            where the run stands

Two steps start before they are pressed, because they need only the placed
board: ``place`` launches the default case design and the parts sourcing on
background threads, and ``case`` and ``sourcing`` (or ``order``, which needs
the BOM) collect them, waiting if they must. That spends two model calls the
engineer has not asked for by name, plus the datasheet probes -- the trade
the desktop mode makes so both are ready by the time they get to them -- and
the envelope says so: ``background`` lists each step while it is being worked
on, ``background_outcome`` says how each finished one ended (``{ok, detail}``,
peeked without collecting it, with ``detail`` the very warning the collecting
step will report), and a failure in the background is reported by the step
that collects it, never turned into a quiet success. Part numbers are proposals -- no
distributor is consulted -- and the status vocabulary says so.

Each step writes the stage's files beside each other in one directory, named
the way ``desktop/kicad_live.py`` expects (``<stem>.kicad_sch``,
``<stem>.placed.kicad_pcb``, ``<stem>.kicad_pcb``, ``<stem>.step``, with the
BOM as ``<stem>-bom.csv`` beside them), and when
the session asked for ``kicad_live`` it spawns that bridge so the stage appears
in the open KiCad. The bridge runs in its own interpreter and is
found, never imported; on a machine without it the step still succeeds and
``shown_in_kicad`` says false.

Order is enforced: ``route`` before ``place`` is a 409, not a guess. Sessions
live only in this process's memory (the ``slackbot`` convention), so a restart
forgets them and a step against a forgotten id is a 404 rather than a run on
the wrong board. Nothing here submits an order or spends money beyond the
model calls each step names.
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from silkscreen.agents.effort import (
    DEFAULT_EFFORT,
    StageModels,
    build_receipt,
    cheap_sibling,
    model_name,
    profile_for,
)
from silkscreen.agents.pipeline import EventingModel
from silkscreen.agents.review import ReviewReport
from silkscreen.agents.sourcing import probe_pdf
from silkscreen.agents.stages import (
    EnclosureJob,
    SourcingJob,
    enclosure_stage,
    place_stage,
    plan_stage,
    propose_stage,
    read_stage,
    review_stage,
    route_stage,
    start_enclosure_stage,
    start_sourcing_stage,
)
from silkscreen.board import emit_kicad_pcb, write_board
from silkscreen.schematic import build_schematic, write_project, write_schematic
from silkscreen.sourcing import SourcingResult, bom_csv, bom_rows, grouped_bom_csv
from silkscreen.units import to_mm

from . import amend as _amend

__all__ = [
    "StepNotFound",
    "StepOrderError",
    "handle_post",
    "handle_get",
    "start_once",
    "reset_sessions",
    "bridge_command",
    "review_block",
]

MAX_SESSIONS = 32
MAX_EVENTS = 200
MAX_ENCLOSURE_STYLE_CHARS = 500

#: How many ``Idempotency-Key``s ``start`` remembers, and how long a key may
#: be. The length is Stripe's (``stripe/_api_requestor.py`` sends a 36-char
#: uuid-shaped value and the API documents a 255-character ceiling); the count
#: is this service's, sized like ``MAX_SESSIONS`` because a key that outlives
#: every session it could name protects nothing.
MAX_IDEMPOTENCY_KEYS = 64
MAX_IDEMPOTENCY_KEY_CHARS = 255

#: The datasheet probe the background sourcing uses. A module-level seam
#: (the ``Handler.model_factory`` convention) so the test suite can pin it
#: offline: the real probe opens a network connection per proposed URL.
PROBE = probe_pdf

#: Also the order `next` lists them in, and the desktop draws the first as the
#: primary button: once copper exists the case is the natural next step, and
#: review is a choice beside it rather than the default.
STEPS = ("place", "route", "case", "review", "sourcing", "order")

#: What each step needs to have happened first. ``review`` and ``order`` read
#: the routed board and do not change it, so they may run in any order once
#: copper exists; ``case`` and ``sourcing`` need only the placed parts (copper
#: is irrelevant to a case, and to a part number), so they open as soon as
#: placement lands. Each runs at most once.
_REQUIRES = {
    "place": "proposed",
    "route": "placed",
    "review": "routed",
    "sourcing": "placed",
    "order": "routed",
    "case": "placed",
}

#: The bridge stage name for each step that has something to show.
#:
#: ``case`` is deliberately absent. The enclosure is a STEP assembly now, and
#: nothing in this repo launches a CAD GUI to show it: the desktop hands the
#: file to whatever owns ``.step``. The OpenSCAD live window that used to sit
#: here went with the v1 emitter on 2026-09-08.
_BRIDGE_STAGE = {
    "propose": "schematic",
    "place": "placement",
    "route": "routing",
}

#: The on-demand action that opens KiCad's own 3D viewer on this run's board.
#: Not a step (see :func:`handle_post`) -- it is the "show me the board in 3D"
#: button, and the honest answer to that is the viewer the engineer already
#: trusts, reading the routed ``.kicad_pcb`` with the component bodies
#: :mod:`silkscreen.models3d` names on every footprint.
VIEW_3D = "view3d"

#: Stages that pull a second one behind them once they land. Routing is the
#: end of the board, so that is where the 3D viewer belongs: opened any
#: earlier it shows a board with no copper on it. The follow-up is advisory --
#: it opens a window, it does not produce the board -- so it can fail without
#: making the stage that earned it read as "not shown".
_FOLLOW_STAGE = {"routing": "3d"}

_REPO_ROOT = Path(__file__).resolve().parents[1]


class StepNotFound(LookupError):
    """No live session has this id (or the service restarted since)."""


class StepOrderError(RuntimeError):
    """The step cannot run yet, or already ran."""


@dataclass
class Session:
    id: str
    intent: str
    stem: str
    directory: Path
    kicad_live: bool
    time_limit_s: float | None
    #: The effort level this session runs at, from the frozen vocabulary in
    #: :mod:`silkscreen.agents.effort`, and the receipt saying what it cost.
    #: The receipt rides every step envelope: the default level is ``fast``,
    #: which places the board inside a quarter of balanced's solver budget,
    #: and a strip that renders a verdict has to be able to say which level
    #: produced what it is showing.
    effort: str = str(DEFAULT_EFFORT)
    effort_receipt: Any = None
    created: float = field(default_factory=time.time)
    stage: str = "new"
    done: set[str] = field(default_factory=set)
    facts: list[Any] = field(default_factory=list)
    #: What the propose step decided the request meant, or None before it
    #: ran. Kept so the overlay can show what the board was designed against
    #: -- a brief the engineer never sees is a brief they cannot correct.
    plan: Any = None
    spec: Any = None
    board: Any = None
    route: Any = None
    findings: list[Any] = field(default_factory=list)
    #: What the critic pass produced, as a
    #: :class:`~silkscreen.agents.review.ReviewReport`. Held beside
    #: ``findings`` because the list alone cannot say whether it is empty
    #: because the board is clean, because the review has not run, or because
    #: the critic's answer could not be read -- and the overlay renders a
    #: verdict from it.
    review: Any = None
    order: dict[str, Any] | None = None
    enclosure: Any = None
    #: The default case being designed in the background since ``place``, and
    #: the events that design emitted; ``case`` collects both. At most one per
    #: session -- ``place`` runs once, and only ``place`` starts it.
    case_job: EnclosureJob | None = None
    case_events: list[dict[str, Any]] = field(default_factory=list)
    #: The parts being sourced in the background since ``place``, the events
    #: that produced, and the BOM once collected -- by ``sourcing`` or by
    #: ``order``, whichever is pressed first; the other reuses it.
    sourcing_job: SourcingJob | None = None
    sourcing_events: list[dict[str, Any]] = field(default_factory=list)
    sourcing: SourcingResult | None = None
    #: The agenda ``review`` proposed, when it was asked for one. Kept as the
    #: whole result rather than the review, so ``deliver`` can tell a model
    #: failure (``review`` None plus a warning) from a board that genuinely
    #: needs no meeting (a review with no blocking item).
    spec_review: Any = None
    #: How each background job ended, keyed ``"case"`` / ``"sourcing"``,
    #: filled the first time a finished job is observed (``_case_outcome`` /
    #: ``_sourcing_outcome``) and reused after -- one observation per job, so
    #: the answer cannot change between the envelope that first reported it
    #: and the step that collects it.
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Path strings by role; ``case_snapshots`` is the one list-valued entry.
    files: dict[str, Any] = field(default_factory=dict)
    #: Why the latest stage did not reach KiCad, in the bridge's words; None
    #: while nothing is known to have gone wrong. See ``_show``.
    shown_detail: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Set when the run is cancelled. An ``Event`` rather than a bool so the
    #: worker threads that read it (the background case and sourcing sinks)
    #: see the write without a lock of their own. Read by ``advance``, which
    #: then refuses every remaining step, and by the event sinks, which raise
    #: ``RunCancelled`` and so abandon whatever stage is running -- see
    #: ``service/amend.py`` for what that does and does not actually stop.
    cancelled: threading.Event = field(default_factory=threading.Event)
    #: What the engineer typed at the strip while the run was going, in the
    #: order they typed it (``amend.Amendment``). Guarded by ``notes_lock``
    #: and never by ``lock``: a running step holds ``lock`` for its whole
    #: body, and a note typed during a long placement must not wait for it.
    amendments: list[Any] = field(default_factory=list)
    notes_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def background(self) -> list[str]:
        """The steps being worked on without having been pressed."""
        running: list[str] = []
        job = self.sourcing_job
        if job is not None and job.running and "sourcing" not in self.done:
            running.append("sourcing")
        case = self.case_job
        if case is not None and case.running and "case" not in self.done:
            running.append("case")
        return running

    @property
    def background_outcome(self) -> dict[str, dict[str, Any]]:
        """How each *finished* background job ended: ``{name: {ok, detail}}``.

        A job still running is absent here (it is in :attr:`background`), and
        one never started is absent too. ``ok`` false carries, as ``detail``,
        the exact warning the collecting step reports -- so a client that
        reads this before pressing the step sees what pressing it will say.
        Peeked, never collected: the jobs memoise their result, so observing
        the outcome changes nothing about what ``case`` or ``sourcing``
        returns later, and no model call is spent.
        """
        outcome: dict[str, dict[str, Any]] = {}
        case = _case_outcome(self)
        if case is not None:
            outcome["case"] = case
        sourcing = _sourcing_outcome(self)
        if sourcing is not None:
            outcome["sourcing"] = sourcing
        return outcome

    def path(self, suffix: str) -> Path:
        return self.directory / f"{self.stem}{suffix}"


_SESSIONS: dict[str, Session] = {}
_REGISTRY_LOCK = threading.Lock()


#: Start requests answered under an ``Idempotency-Key``, oldest first. The
#: value is the envelope the first request answered with, or ``None`` while
#: that request is still running. See :func:`start_once`.
_STARTED: collections.OrderedDict[str, dict[str, Any] | None] = (
    collections.OrderedDict()
)
_STARTED_LOCK = threading.Lock()


def reset_sessions() -> None:
    """Forget every session, and every idempotency key with it (tests)."""
    with _REGISTRY_LOCK:
        _SESSIONS.clear()
    with _STARTED_LOCK:
        _STARTED.clear()


def _register(session: Session) -> None:
    with _REGISTRY_LOCK:
        while len(_SESSIONS) >= MAX_SESSIONS:
            oldest = min(_SESSIONS.values(), key=lambda s: s.created)
            del _SESSIONS[oldest.id]
        _SESSIONS[session.id] = session


def _get(session_id: str) -> Session:
    with _REGISTRY_LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise StepNotFound(
            f"no step session {session_id!r} (it may have expired, or the "
            "service restarted -- start again from POST /steps)"
        )
    return session


# ---------------------------------------------------------------- naming


def slug(intent: str) -> str:
    """A file stem from the intent: ``build me a toy car`` -> ``build-me-a-toy-car``."""
    words = re.sub(r"[^a-z0-9]+", "-", intent.lower()).strip("-")
    return (words[:40].rstrip("-")) or "board"


def steps_root() -> Path:
    """Where step sessions write their files (``SILKSCREEN_STEPS_DIR``)."""
    configured = os.getenv("SILKSCREEN_STEPS_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "silkscreen-steps"


# ---------------------------------------------------------------- bridge


def bridge_command(pcb: Path, stage: str) -> list[str] | None:
    """The argv that shows ``stage`` in KiCad, or None when no bridge exists.

    ``SILKSCREEN_KICAD_LIVE_PYTHON`` names the bridge's interpreter (it needs
    ``kicad-python``, which cannot share the engine's venv); by default the
    checkout's ``.venv-kicad`` is tried. The script is ``desktop/kicad_live.py``
    in this checkout. Either missing means "cannot show", never an error.
    """
    interpreter = os.getenv("SILKSCREEN_KICAD_LIVE_PYTHON", "").strip()
    if not interpreter:
        candidate = _REPO_ROOT / ".venv-kicad" / "bin" / "python"
        interpreter = str(candidate) if candidate.is_file() else ""
    script = _REPO_ROOT / "desktop" / "kicad_live.py"
    if not interpreter or not Path(interpreter).is_file() or not script.is_file():
        return None
    return [interpreter, str(script), str(pcb), stage]


#: Seconds the bridge gets to fail before the step answers. Longer than the
#: bridge's own socket grace (``kicad_live.API_SOCKET_GRACE_S``), so "the API
#: server is off" arrives inside the step's response rather than after it;
#: a bridge still waiting for a board to finish loading past this is left
#: running and reports through ``shown_detail`` on ``GET /steps/<id>``.
BRIDGE_GRACE_S = 12.0

NO_BRIDGE_DETAIL = (
    "no KiCad bridge: create .venv-kicad or set SILKSCREEN_KICAD_LIVE_PYTHON "
    "(see desktop/README.md)"
)


def _bridge_failure(stage: str, stderr: str | None, code: int | None) -> str:
    """One line about why ``stage`` did not reach KiCad, from the bridge's own words."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    reason = lines[-1] if lines else f"the bridge exited {code} without saying why"
    if reason.startswith("error: "):
        reason = reason[len("error: "):]
    return f"{stage} was not shown in KiCad: {reason}"


def _watch_bridge(session: Session, stage: str, proc: subprocess.Popen) -> None:
    """Record how a bridge that outlived the grace period ended."""
    _, err = proc.communicate()
    if proc.returncode != 0:
        detail = _bridge_failure(stage, err, proc.returncode)
        sys.stderr.write(f"kicad_live bridge: {detail}\n")
        # Never over a later stage's own reason; the stage name keeps it legible.
        if session.shown_detail is None:
            session.shown_detail = detail
        return
    # The follow-up belongs here too, and this is the branch that matters:
    # routing a real board takes KiCad longer than BRIDGE_GRACE_S, so the
    # common case is the bridge outliving the grace and finishing here. Doing
    # it only on the fast path meant the 3D viewer opened for a toy board and
    # never for a real one.
    _follow(session, stage)


def _show(session: Session, step: str) -> tuple[bool, str | None]:
    """Spawn the bridge for ``step`` if the session wants it. Never raises.

    Returns whether the stage reached KiCad and, when it did not, why in
    one sentence the overlay can show. "Reached" means the bridge either
    finished cleanly or is still working after ``BRIDGE_GRACE_S``; a bridge
    that failed inside the grace -- the API server off, no board editor,
    no ``kicad-python`` -- makes this false with its last stderr line, so
    the flag on the wire is not "a process was started".
    """
    if not session.kicad_live:
        return False, None
    stage = _BRIDGE_STAGE.get(step)
    if stage is None:
        return False, None
    session.shown_detail = None
    argv = bridge_command(session.path(".kicad_pcb"), stage)
    if argv is None:
        session.shown_detail = NO_BRIDGE_DETAIL
        return False, NO_BRIDGE_DETAIL
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        detail = f"{stage} was not shown in KiCad: the bridge failed to start ({exc})"
        sys.stderr.write(f"kicad_live bridge: {detail}\n")
        session.shown_detail = detail
        return False, detail
    try:
        _, err = proc.communicate(timeout=BRIDGE_GRACE_S)
    except subprocess.TimeoutExpired:
        threading.Thread(
            target=_watch_bridge, args=(session, stage, proc), daemon=True
        ).start()
        return True, None
    if proc.returncode == 0:
        _follow(session, stage)
        return True, None
    detail = _bridge_failure(stage, err, proc.returncode)
    sys.stderr.write(f"kicad_live bridge: {detail}\n")
    session.shown_detail = detail
    return False, detail


#: Seconds the viewer bridge gets to answer before the route returns anyway.
#: Longer than ``BRIDGE_GRACE_S`` because this one may have to *launch* a board
#: editor from cold (measured on KiCad 10.0.6 / M-series: ~4 s to a live menu
#: bar), where the other stages push into an editor that is already up.
VIEW_3D_GRACE_S = 20.0


def show_board_3d(session_id: str) -> dict[str, Any]:
    """Open KiCad's 3D viewer on this run's routed board.

    Answers ``{"opened": bool, "detail": str | None}`` and never raises for a
    KiCad problem: "the viewer did not open, because X" is the product here,
    and X is the whole value -- the API server is off, no Accessibility grant,
    KiCad is not installed. A silent false would send an engineer looking at
    the board when the problem is a preference.

    It opens the routed ``.kicad_pcb``, not the pre-routing placed file: the
    routed board on disk is the product, copper included, and the viewer can
    only ever show what the editor has open.
    """
    session = _get(session_id)
    pcb = session.path(".kicad_pcb")
    if not pcb.is_file():
        return {
            "opened": False,
            "detail": "there is no board yet: run place, then route",
        }
    argv = bridge_command(pcb, "3d")
    if argv is None:
        return {"opened": False, "detail": NO_BRIDGE_DETAIL}
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return {"opened": False, "detail": f"the bridge failed to start ({exc})"}
    try:
        out, err = proc.communicate(timeout=VIEW_3D_GRACE_S)
    except subprocess.TimeoutExpired:
        # Still working. KiCad opening a board from cold is slow and that is
        # not a failure, so say what is actually true rather than guessing.
        threading.Thread(target=proc.communicate, daemon=True).start()
        return {"opened": True, "detail": "KiCad is still opening the board"}
    if proc.returncode == 0:
        return {"opened": True, "detail": (out or "").strip() or None}
    return {"opened": False, "detail": _bridge_failure("3d", err, proc.returncode)}


#: The on-demand action that opens this run's case STEP in FreeCAD. Not a step,
#: for the ``view3d`` reasons: it produces nothing and may be pressed again.
OPEN_CASE = "open_case"

#: Names the FreeCAD application outright (a ``.app`` bundle on macOS, an
#: executable elsewhere), the ``KICAD_CLI`` convention.
FREECAD_APP_ENV = "SILKSCREEN_FREECAD_APP"

#: Where FreeCAD is looked for when the environment names nothing. The macOS
#: bundles are what the FreeCAD release DMG and ``brew install --cask
#: freecad`` install; the executables are the Linux package names.
FREECAD_APP_CANDIDATES: tuple[str, ...] = (
    "/Applications/FreeCAD.app",
    str(Path.home() / "Applications" / "FreeCAD.app"),
)
FREECAD_EXECUTABLES: tuple[str, ...] = ("freecad", "FreeCAD")

FREECAD_NOT_INSTALLED = (
    "FreeCAD is not installed (looked in /Applications and ~/Applications, and "
    f"for 'freecad' on PATH): install it from freecad.org or set {FREECAD_APP_ENV}"
)

#: Seconds ``open`` gets to launch FreeCAD. It returns once the app is
#: launched, not once the model has loaded.
FREECAD_OPEN_TIMEOUT_S = 30.0

#: Seconds a freshly spawned FreeCAD is watched for an immediate failure (a
#: bad binary, a missing library) before the open is reported as done. A
#: FreeCAD that is already running and receives the file over its
#: single-instance socket exits 0 well inside this; a GUI that stays up is
#: left running.
FREECAD_SPAWN_GRACE_S = 2.0


def freecad_app(environ: dict[str, str] | None = None) -> str | None:
    """The FreeCAD application to hand a STEP to, or None when there is none."""
    env = os.environ if environ is None else environ
    configured = env.get(FREECAD_APP_ENV, "").strip()
    if configured:
        return configured if Path(configured).exists() else None
    if sys.platform == "darwin":
        for candidate in FREECAD_APP_CANDIDATES:
            if Path(candidate).is_dir():
                return candidate
    for name in FREECAD_EXECUTABLES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _freecad_executable(app: str) -> str:
    """The binary in a ``.app`` bundle (its ``CFBundleExecutable``), else ``app``."""
    if not app.endswith(".app"):
        return app
    bundle = Path(app)
    try:
        import plistlib

        with (bundle / "Contents" / "Info.plist").open("rb") as fh:
            name = plistlib.load(fh).get("CFBundleExecutable") or "FreeCAD"
    except (OSError, ValueError):
        name = "FreeCAD"
    return str(bundle / "Contents" / "MacOS" / str(name))


def open_in_freecad(path: Path) -> tuple[bool, str | None]:
    """Open ``path`` in FreeCAD. Answers ``(opened, detail)``; never raises.

    The file goes to FreeCAD **as a command-line argument**, never through
    LaunchServices, and both halves of that are measured, not assumed
    (2026-09-13, FreeCAD 1.1.3 at /Applications/FreeCAD.app, macOS 26):

    * ``open case.step`` fails with ``kLSApplicationNotFoundErr``: FreeCAD's
      ``Info.plist`` declares only ``FCStd``/``FCMat``/``FCParam``/
      ``FCMacro``/``FCScript``, so nothing on the machine claims ``.step``.
      That is why the desktop's ``openPath`` on the case showed a path and
      no model.
    * ``open -a FreeCAD case.step`` exits 0 and opens nothing. The Apple
      Event arrives as a ``QFileOpenEvent``, and FreeCAD's handler
      (``src/Gui/GuiApplication.cpp``, ``GUIApplication::event``) opens it
      only when ``fi.suffix().toLower() == "fcstd"`` -- a STEP is dropped.

    A path on the command line goes through ``App::Application::processFiles``
    instead, which imports any type FreeCAD has an importer for (the same
    call ``MainWindow::processMessages`` in ``src/Gui/MainWindow.cpp`` makes).
    On macOS it is launched as ``open -n -a <app> --args <file>``: spawning
    the bundle's binary directly also imported the STEP, but its window came
    up behind everything and never took focus; LaunchServices activates the
    app it launches. ``-n`` because ``--args`` is dropped when ``open`` merely
    activates a FreeCAD that is already running (FreeCAD allows multiple
    instances by default, so each case opens its own window).
    """
    if not path.is_file():
        return False, f"there is no case model at {path}"
    app = freecad_app()
    if app is None:
        configured = os.environ.get(FREECAD_APP_ENV, "").strip()
        if configured:
            return False, f"{FREECAD_APP_ENV}={configured!r} does not exist"
        return False, FREECAD_NOT_INSTALLED
    if sys.platform == "darwin" and app.endswith(".app"):
        argv = ["/usr/bin/open", "-n", "-a", app, "--args", str(path.resolve())]
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=FREECAD_OPEN_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"FreeCAD did not open the case: {exc}"
        if done.returncode != 0:
            stderr = (done.stderr or "").splitlines()
            lines = [ln.strip() for ln in stderr if ln.strip()]
            reason = lines[-1] if lines else f"open exited {done.returncode}"
            return False, f"FreeCAD did not open the case: {reason}"
        return True, f"opened {path.name} in FreeCAD"
    binary = _freecad_executable(app)
    if not Path(binary).is_file() and shutil.which(binary) is None:
        return False, f"FreeCAD did not open the case: no executable at {binary}"
    try:
        proc = subprocess.Popen(
            [binary, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"FreeCAD did not open the case: {exc}"
    try:
        _, err = proc.communicate(timeout=FREECAD_SPAWN_GRACE_S)
    except subprocess.TimeoutExpired:
        # Still up: the GUI is running with the file. Drain stderr so a
        # chatty FreeCAD cannot block on a full pipe.
        threading.Thread(target=proc.communicate, daemon=True).start()
        return True, f"opened {path.name} in FreeCAD"
    if proc.returncode == 0:
        # Exited cleanly inside the grace: a launcher that handed off.
        return True, f"opened {path.name} in FreeCAD"
    lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    reason = lines[-1] if lines else f"FreeCAD exited {proc.returncode}"
    return False, f"FreeCAD did not open the case: {reason}"


def _case_model(session: Session) -> Path | None:
    raw = session.files.get("case_step") or session.files.get("case")
    return Path(raw) if raw else None


def open_case_in_freecad(session_id: str) -> dict[str, Any]:
    """``POST /steps/<id>/open_case``: ``{"opened": bool, "detail": str | None}``."""
    session = _get(session_id)
    model = _case_model(session)
    if model is None:
        return {"opened": False, "detail": "there is no case yet: run case first"}
    opened, detail = open_in_freecad(model)
    return {"opened": opened, "detail": detail}


def _follow(session: Session, stage: str) -> None:
    """Spawn the stage that rides on ``stage``, if any. Never raises.

    Fire and forget on purpose: the caller has already decided the stage
    reached KiCad, and whether a viewer window opened must not change that
    verdict. A failure is recorded in ``shown_detail`` so it is visible
    rather than swallowed, and it never becomes the step's answer.
    """
    nxt = _FOLLOW_STAGE.get(stage)
    if nxt is None:
        return
    argv = bridge_command(session.path(".kicad_pcb"), nxt)
    if argv is None:
        return
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        sys.stderr.write(f"kicad_live bridge: {nxt} was not opened ({exc})\n")


# ---------------------------------------------------------------- steps


def _cheap_for(session: Session, model: Any, stage: str) -> Any:
    """The cheap-tier model for one background lane, or None.

    None means "run this lane on the model the request handed in", which is
    both the answer when the level keeps the lane on the reasoning tier and
    the answer when the model family offers no cheaper tier. Those two are
    told apart on the receipt (``cheap_stages`` versus ``cheap_applied``),
    not here, because the caller only needs to know which object to call.
    """
    if model is None:
        return None
    profile = profile_for(session.effort)
    if stage not in profile.cheap_stages:
        return None
    return cheap_sibling(model)


def _events_sink(
    started: float,
    model: Any = None,
    session: Session | None = None,
    *,
    cheap: Any = None,
) -> tuple[list[dict[str, Any]], Any, Any, Any, Any]:
    """The step's event list, its ``emit``/``enter`` seam, and its model.

    The model comes back wrapped in the pipeline's own :class:`EventingModel`,
    so a step reports the same ``model.call`` frames -- provider, model name,
    elapsed, ok -- that ``generate_pcb`` does. Without it a step run named no
    model and counted no calls, and a client could only say so. The wrapper is
    per sink: ``enter`` sets its stage, and the background case and sourcing
    jobs each get their own, so a call made on a worker thread is never
    attributed to the stage the request thread is in. A sink built with no
    model (the deterministic steps -- place, route, order) wraps nothing:
    CP-SAT, the A* router and the order gate make no model call, and a tap over
    them would only be a place for one to appear.

    ``session``, when given, makes the sink cancellation-aware: every emission
    first checks the session's cancel flag and raises ``amend.RunCancelled``
    if it is set. That is not a new mechanism -- it is the pipeline's existing,
    tested contract that a callback exception abandons the run, the same one
    ``/generate/stream`` uses to stop a run whose reader hung up. It follows
    that cancellation lands at an *event boundary*: a stage already inside its
    model call or its solve finishes that work first. The background case and
    sourcing sinks get the same session, so a cancel reaches those threads too,
    at their next event.

    ``cheap`` is the effort level's cheap-tier sibling of ``model``, when the
    level moved this step's lane there and the model family offers one. It
    gets a tap of its own -- its own call-id prefix, following the same
    ``enter`` -- so a BOM sourced on the cheap tier still reports which model
    actually answered instead of borrowing the reasoning tier's name. The
    fifth return is that tapped model, or None; a caller that has no cheap
    lane ignores it.
    """
    events: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> None:
        if session is not None:
            _amend.check_cancelled(session)
        if len(events) >= MAX_EVENTS:
            return
        event = dict(event)
        event["t_s"] = round(time.monotonic() - started, 3)
        events.append(event)

    tap = None if model is None else EventingModel(model, emit)
    cheap_tap = (
        None if cheap is None else EventingModel(cheap, emit, call_prefix="cheap")
    )

    def enter(stage: str) -> None:
        if tap is not None:
            tap.stage = stage
        if cheap_tap is not None:
            cheap_tap.stage = stage

    return (
        events,
        emit,
        enter,
        tap if tap is not None else model,
        cheap_tap,
    )


def _write_schematic(session: Session) -> None:
    footprints = None
    if session.board is not None:
        footprints = {
            p.ref: f"silkscreen:{p.footprint.name}" for p in session.board.parts
        }
    sheet = build_schematic(session.spec, footprints=footprints)
    if session.board is not None:
        session.board.warnings.extend(sheet.warnings)
    session.files["schematic"] = str(
        write_schematic(
            sheet,
            session.path(".kicad_sch"),
            project_name=session.stem,
            title=session.intent,
            today=datetime.date.today(),
        )
    )
    session.files["project"] = str(
        write_project(session.path(".kicad_pro"), project_name=session.stem)
    )


def start(payload: dict[str, Any], *, model, store) -> dict[str, Any]:
    """Read the datasheets and propose a circuit; the schematic appears."""
    intent = str(payload.get("intent") or "").strip()
    if not intent:
        raise ValueError("'intent' is required")
    datasheets = payload.get("datasheets") or {}
    if not isinstance(datasheets, dict) or any(
        not isinstance(u, str) or not u for u in datasheets.values()
    ):
        raise ValueError("'datasheets' must be an object of {part: url}")
    kicad_live = payload.get("kicad_live", False)
    if not isinstance(kicad_live, bool):
        raise ValueError("'kicad_live' must be a boolean")
    # The thinking level, validated before anything spends time or quota and
    # never defaulted on a bad name -- ``/generate``'s rule, for the same
    # reason: a session that answers a 'thorough' request at 'fast' while
    # reporting 'thorough' is what the level exists to prevent.
    effort = payload.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ValueError("'effort' must be a string")
    profile = profile_for(effort)
    overrides: list[str] = []
    # Absent, not defaulted: the level owns a budget nobody named. Reading
    # ``payload.get("time_limit_s", 20.0)`` -- the old default -- made every request
    # an explicit override and left the level nothing to decide.
    if "time_limit_s" in payload:
        try:
            time_limit_s: float | None = float(payload["time_limit_s"])
        except (TypeError, ValueError):
            raise ValueError("'time_limit_s' must be a number") from None
        overrides.append("time_limit_s")
    else:
        time_limit_s = profile.time_limit_s
    if "max_repairs" in payload:
        max_repairs = payload["max_repairs"]
        if (
            isinstance(max_repairs, bool)
            or not isinstance(max_repairs, int)
            or max_repairs < 0
        ):
            raise ValueError("'max_repairs' must be a non-negative integer")
        overrides.append("max_repairs")
    else:
        max_repairs = profile.max_repairs

    session_id = uuid.uuid4().hex[:12]
    stem = slug(intent)
    directory = steps_root() / session_id
    directory.mkdir(parents=True, exist_ok=True)
    session = Session(
        id=session_id,
        intent=intent,
        stem=stem,
        directory=directory,
        kicad_live=kicad_live,
        time_limit_s=time_limit_s,
        effort=str(profile.level),
    )
    # Built from the model the request handed in, so the receipt names the
    # cheap tier only when this model family actually offers one -- the
    # pipeline's rule, and the reason the receipt has both ``cheap_stages``
    # (asked for) and ``cheap_applied`` (happened).
    session.effort_receipt = build_receipt(
        profile,
        StageModels(
            profile=profile,
            primary=model,
            cheap=cheap_sibling(model) if profile.cheap_stages else None,
            cheap_name=model_name(
                cheap_sibling(model) if profile.cheap_stages else None
            ),
        ),
        time_limit_s=time_limit_s,
        max_repairs=max_repairs,
        overrides=tuple(overrides),
    )

    started = time.monotonic()
    events, emit, enter, tapped, _ = _events_sink(started, model)

    # Cached facts stand in for a read, the ``generate()`` rule: skipping the
    # read without supplying the facts would design blind.
    preloaded = []
    to_read: dict[str, str] = {}
    for part, url in datasheets.items():
        raw = store.get(part)
        if raw is None:
            to_read[part] = url
            continue
        try:
            from silkscreen.agents.datasheet import PartFacts

            preloaded.append(PartFacts.from_dict(raw))
        except (TypeError, ValueError):
            to_read[part] = url
    session.facts = read_stage(
        tapped, sheets=to_read, preloaded_facts=preloaded, emit=emit, enter=enter
    )
    # The reads above are already paid for, so a cache that refuses the write
    # is a cache miss next time, not a failed step -- the same rule the read
    # loop above follows for an unreadable entry. The failure is named in the
    # step's warnings rather than swallowed.
    cache_warnings: list[str] = []
    for fact in session.facts:
        part = getattr(fact, "part_number", None)
        if not part:
            continue
        try:
            store.put(part, fact.to_dict())
        except Exception as exc:
            cache_warnings.append(
                f"datasheet facts for {part} were not cached: "
                f"{type(exc).__name__}: {exc}"
            )

    # Planning runs inside the propose step, not beside it. It is a model
    # call, but it is not an *unpressed* one: the engineer pressed propose,
    # and deciding what the request means -- where power enters, which rails,
    # which connectors -- is part of proposing. Without it a five-word request
    # ("a home security camera system") reached the netlist with nothing
    # having decided how the board is powered, and came back with no power
    # input at all. A plan that cannot be made is a warning, never a dead
    # step: `plan_stage` answers with `plan=None` and propose designs from the
    # bare intent exactly as it did before.
    plan_result = plan_stage(
        tapped,
        intent=intent,
        plan=True,
        max_repairs=max_repairs,
        emit=emit,
        enter=enter,
    )
    session.plan = plan_result
    session.spec, attempts = propose_stage(
        tapped,
        intent=intent,
        facts=session.facts,
        brief=(
            plan_result.plan.brief_text()
            if plan_result is not None and plan_result.plan is not None
            else None
        ),
        max_repairs=max_repairs,
        emit=emit,
        enter=enter,
        propose_on_event=emit,
    )
    _write_schematic(session)
    session.stage = "proposed"
    _register(session)

    from .app import _schematic_dict

    refs = session.spec.assign_refs()
    body: dict[str, Any] = {
        "parts": session.spec.part_count(),
        "nets": session.spec.net_count(),
        "repair_rounds": max(0, len(attempts) - 1),
        "schematic": _schematic_dict(session.spec, refs),
        "datasheets": [
            {"part": f.part_number, "pins": len(f.pins)} for f in session.facts
        ],
    }
    if cache_warnings:
        body["warnings"] = cache_warnings
    return _envelope(
        session,
        step="propose",
        events=events,
        started=started,
        body=body,
    )


def _place(session: Session, payload: dict[str, Any], *, model) -> dict[str, Any]:
    started = time.monotonic()
    # No model here: the placer is CP-SAT. The prefetched case and BOM below
    # get the request's own model, each tapped into its own sink, so their
    # calls are never counted against the placement.
    events, emit, enter, _, _ = _events_sink(started, session=session)
    session.board = place_stage(
        session.spec, time_limit_s=session.time_limit_s, emit=emit, enter=enter
    )
    # The schematic is rewritten now that footprints exist, so its Footprint
    # fields name the land patterns the board actually carries.
    _write_schematic(session)
    session.files["placed_board"] = str(
        write_board(session.board, session.path(".placed.kicad_pcb"))
    )
    session.stage = "placed"
    _prefetch_case(session, model)
    _prefetch_sourcing(session, model)

    from .app import _placements_dict

    board = session.board
    return _envelope(
        session,
        step="place",
        events=events,
        started=started,
        body={
            "status": str(board.solver_status),
            "board_mm": [
                round(to_mm(board.width_nm), 3),
                round(to_mm(board.height_nm), 3),
            ],
            "parts": [
                {"ref": p.ref, "footprint": p.footprint.name} for p in board.parts
            ],
            "placements": _placements_dict(board),
            "wirelength_mm": (
                None
                if board.wirelength_nm is None
                else round(to_mm(board.wirelength_nm), 3)
            ),
            "warnings": list(board.warnings),
            "kicad_pcb": emit_kicad_pcb(board),
        },
    )


def _prefetch_case(session: Session, model) -> None:
    """Start designing the default case now that the board is placed.

    Honesty trade-off, stated: this spends one model call the engineer has
    not pressed a button for. It is what the desktop mode asks for -- the
    case ready when they get to it -- and the ``background`` field on every
    envelope and on ``GET /steps/<id>`` says it is happening. Fast mode and no
    style, because nothing has been asked for yet; ``case`` with a style or
    ``enclosure_rigorous`` designs afresh instead.

    ``model`` is the request's own instance from ``model_factory`` -- the
    place step itself never calls a model, so nothing else shares it. The
    thread works on a snapshot of the placed board, so ``route`` mutating
    ``session.board`` in place cannot reach it, and it is a daemon that
    stores its result on the session object it holds: a session evicted
    while designing finishes quietly and is simply never asked.
    """
    if session.case_job is not None:
        return
    started = time.monotonic()
    events, emit, enter, tapped, cheap = _events_sink(
        started, model, session=session, cheap=_cheap_for(session, model, "enclosure")
    )
    session.case_events = events
    session.case_job = start_enclosure_stage(
        cheap if cheap is not None else tapped,
        session.board,
        enclosure=True,
        enclosure_style="",
        rigorous=False,
        output=None,
        emit_stages=False,
        export_dir=session.directory,
        stem=session.stem,
        emit=emit,
        enter=enter,
        daemon=True,
    )


def _prefetch_sourcing(session: Session, model) -> None:
    """Start looking the parts up now that the board is placed.

    The same honesty trade-off as :func:`_prefetch_case`, and one more thing
    to state: besides a model call nobody pressed for, this probes every
    datasheet URL the model proposes -- a network GET per part, through
    :data:`PROBE` -- so a run in step mode reaches out beyond the model
    provider the moment placement lands. ``background`` lists ``sourcing``
    while it runs. The row shape is what the board alone knows (ref, value,
    kind, land pattern, KiCad 3D model); the model adds a manufacturer,
    a part number and a datasheet, and every part number stays a proposal.

    Same thread rules as the case: a snapshot of the placed board, a daemon
    that stores its result on the session it holds, and the request's own
    model instance -- shared with the case thread, which is fine, since the
    ``Model`` protocol carries no per-call state and a ``FallbackModel``
    fails over per call.
    """
    if session.sourcing_job is not None:
        return
    started = time.monotonic()
    events, emit, enter, tapped, cheap = _events_sink(
        started, model, session=session, cheap=_cheap_for(session, model, "sourcing")
    )
    session.sourcing_events = events
    session.sourcing_job = start_sourcing_stage(
        cheap if cheap is not None else tapped,
        session.board,
        emit=emit,
        enter=enter,
        daemon=True,
        probe=PROBE,
    )


#: The one prefix every sourcing failure carries, whether the stage caught a
#: bad answer itself (``sourcing_stage`` writes ``parts were not sourced:``
#: on its warning) or the job raised and :func:`_sourcing_failure` wrote it.
#: ``_sourcing_outcome`` keys on it, so a finished job whose rows carry that
#: warning is reported ``ok: false`` rather than as a BOM that merely has a
#: note attached.
SOURCING_FAILED_PREFIX = "parts were not sourced"

#: What ``case`` says when the design produced nothing (the stage answered
#: ``None``); the same sentence whether or not the job also raised.
NO_CASE_WARNING = "enclosure generation failed; the board stands without a case"


def _failure_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def _sourcing_failure(exc: BaseException) -> str:
    """The warning ``sourcing``/``order`` report for a background job that raised."""
    return (
        f"{SOURCING_FAILED_PREFIX}: the lookup in the background failed "
        f"({_failure_text(exc)}); the BOM lists the board's parts with no "
        "part numbers"
    )


def _case_failure(exc: BaseException) -> str:
    """The warning ``case`` reports for a background design that raised."""
    return f"the case designed in the background failed: {_failure_text(exc)}"


def review_block(report: Any) -> dict[str, Any]:
    """The ``review`` block every surface puts on the wire.

    ``status`` is ``ok`` / ``failed`` / ``skipped``, ``ran`` is whether the
    critic was asked, ``detail`` is why its answer could not be read (null
    when there is nothing to say), and ``note`` is the report's own
    sentence. ``None`` -- a session whose review step has not been pressed,
    a result with no report -- is the skipped block, which is what a review
    that never happened is; nothing here ever reads as a verdict.
    """
    if report is None or not hasattr(report, "as_dict"):
        report = ReviewReport()
    block = dict(report.as_dict())
    block["detail"] = block.get("detail") or None
    return block


def _peek(job: Any) -> tuple[bool, Any, BaseException | None]:
    """``(finished, result, error)`` for a background job, without waiting.

    A job that is still running (or was never started) answers
    ``(False, None, None)`` and nothing else is touched. A finished one is
    asked for its result: :meth:`EnclosureJob.result` and
    :meth:`SourcingJob.result` memoise -- the value and the exception both
    stay on the job and every later call answers the same -- so this is a
    read, and the step that collects the job afterwards sees exactly what it
    would have seen first. The join inside ``result()`` is a no-op on a
    finished thread; ``running`` is checked first so it can never block.
    """
    if job is None or job.running:
        return False, None, None
    try:
        return True, job.result(), None
    except Exception as exc:  # noqa: BLE001 -- reported, never hidden
        return True, None, exc


def _case_outcome(session: Session) -> dict[str, Any] | None:
    """``{ok, detail}`` for the background case once it has finished, else None."""
    cached = session.outcomes.get("case")
    if cached is not None:
        return cached
    finished, result, error = _peek(session.case_job)
    if not finished:
        return None
    if error is not None:
        outcome = {"ok": False, "detail": _case_failure(error)}
    elif result is None:
        outcome = {"ok": False, "detail": NO_CASE_WARNING}
    else:
        outcome = {"ok": True, "detail": None}
    session.outcomes["case"] = outcome
    return outcome


def _sourcing_outcome(session: Session) -> dict[str, Any] | None:
    """``{ok, detail}`` for the background sourcing once finished, else None.

    A job that returned rows carrying a ``parts were not sourced`` warning
    is a failed lookup that the stage caught for itself; it reads as not ok
    here with that warning as the detail, because that warning is what the
    ``sourcing`` step will report.
    """
    cached = session.outcomes.get("sourcing")
    if cached is not None:
        return cached
    finished, result, error = _peek(session.sourcing_job)
    if not finished:
        return None
    if error is not None:
        outcome = {"ok": False, "detail": _sourcing_failure(error)}
    else:
        failed = [
            w
            for w in (getattr(result, "warnings", None) or ())
            if str(w).startswith(SOURCING_FAILED_PREFIX)
        ]
        if result is None:
            outcome = {
                "ok": False,
                "detail": f"{SOURCING_FAILED_PREFIX}: the lookup in the "
                "background produced no result",
            }
        elif failed:
            outcome = {"ok": False, "detail": str(failed[0])}
        else:
            outcome = {"ok": True, "detail": None}
    session.outcomes["sourcing"] = outcome
    return outcome


def _collect_sourcing(session: Session) -> tuple[SourcingResult, list[str]]:
    """The BOM: collected from the background job, waiting if it still runs.

    Idempotent -- ``sourcing`` and ``order`` both call it, and whichever is
    second gets the same result. A job that raised (a model outage, most
    likely; the stage itself already turns a bad answer into a warning)
    answers with the deterministic :func:`bom_rows` -- every status
    ``"none"`` -- and a warning naming the failure, so the order still has a
    parts list and nobody reads an empty one as "no parts". Writes
    ``<stem>-bom.csv`` the first time through.
    """
    warnings: list[str] = []
    if session.sourcing is None:
        job = session.sourcing_job
        try:
            if job is None:
                raise RuntimeError("sourcing was never started for this session")
            result = job.result()
        except Exception as exc:  # noqa: BLE001 -- reported, never hidden
            result = SourcingResult(
                bom_rows(session.board), warnings=[_sourcing_failure(exc)]
            )
        session.sourcing = result
        bom_path = session.path("-bom.csv")
        bom_path.write_text(bom_csv(result), encoding="utf-8")
        session.files["bom"] = str(bom_path)
        grouped_path = session.path("-bom-grouped.csv")
        grouped_path.write_text(grouped_bom_csv(result), encoding="utf-8")
        session.files["bom_grouped"] = str(grouped_path)
    warnings.extend(session.sourcing.warnings)
    return session.sourcing, warnings


def _route(session: Session, payload: dict[str, Any], *, model) -> dict[str, Any]:
    started = time.monotonic()
    events, emit, enter, _, _ = _events_sink(started, session=session)
    session.route = route_stage(session.board, route=True, emit=emit, enter=enter)
    session.files["board"] = str(write_board(session.board, session.path(".kicad_pcb")))
    session.stage = "routed"
    route = session.route
    return _envelope(
        session,
        step="route",
        events=events,
        started=started,
        body={
            "routing": {
                "tracks": len(route.tracks),
                "vias": len(route.vias),
                "routed": list(route.routed),
                "unrouted": dict(route.unrouted),
                "warnings": list(route.warnings),
                "completion": round(route.completion, 4),
            },
            "kicad_pcb": emit_kicad_pcb(session.board),
        },
    )


def _wants_agenda(payload: dict[str, Any]) -> bool:
    """Did the caller ask for a spec-review agenda alongside the findings?

    Two ways, because there are two callers: ``{"spec_review": true}`` says it
    outright, and ``{"summary": "structured"}`` is the desktop's Structured
    toggle, whose whole meaning is "give me an agenda rather than prose".
    Default off. The agenda is a model call, and this service does not spend
    one nobody asked for -- the background case and BOM are the two exceptions
    and they are stated where they are started.
    """
    flag = payload.get("spec_review", False)
    if not isinstance(flag, bool):
        raise ValueError("'spec_review' must be a boolean")
    summary = payload.get("summary", "prose")
    if summary not in ("prose", "structured"):
        raise ValueError("'summary' must be 'prose' or 'structured'")
    return flag or summary == "structured"


def _known_refs(session: Session) -> list[str] | None:
    """What the board actually contains, for the agenda's ref filter.

    Part refs **and** net names, because both are what an agenda item cites:
    the evidence the model is shown is review findings and unrouted nets, so
    an item about ``ESP_EN`` is about something real. Passing only part refs
    dropped every item that named a net -- the filter fired hardest exactly
    where the meeting was most warranted, and an empty agenda came back from a
    board carrying a blocker and five ratsnest nets. Proven end to end
    2026-09-06.
    """
    board = session.board
    if board is None:
        return None
    refs = [p.ref for p in board.parts]
    refs.extend(board.nets)
    route = session.route
    if route is not None:
        refs.extend(route.unrouted)
        refs.extend(route.routed)
    # Order-preserving dedupe: the filter only asks "is this known?", but a
    # stable list keeps a dropped-item message readable.
    return list(dict.fromkeys(str(ref) for ref in refs if str(ref).strip()))


def _agenda(
    session: Session, *, model, emit, enter
) -> tuple[dict[str, Any] | None, list[str]]:
    """Propose the agenda, and never let it cost the run its findings.

    The findings are the product of this step; an agenda is a convenience on
    top. So a failure here becomes a warning and a null block, the way a failed
    background case does -- but a *null block* is not silence: it travels with
    the warning that says why, and ``needs_meeting`` stays false so nothing
    downstream books a meeting off an error.

    The block on the envelope is the :class:`~silkscreen.specreview.SpecReview`
    itself (``title``/``summary``/``items``/``total_minutes``), **not** the
    stage's :class:`~silkscreen.specreview.SpecReviewResult` wrapper. The
    wrapper's three states survive the flattening intact -- an absent key means
    no agenda was asked for, ``null`` means the stage produced nothing usable
    and the warnings say why, and a block means an agenda. Nesting the review
    one level deeper would buy nothing and cost the reader a key that reads
    like an agenda but has no items: the desktop's ``specReviewFrom`` returns
    the first truthy block and ``blockingItems`` then reads ``.items`` off it.
    """
    from silkscreen.agents.specreview import propose_spec_review

    route = session.route
    clauses, sourcing, evidence_warnings = _agenda_evidence(session)
    known = _known_refs(session)
    if known is not None:
        # A kernel item cites the clause by name, which is not a part or a
        # net; without this the ref filter drops exactly the items the case
        # evidence was passed in to produce.
        known = list(dict.fromkeys(known + [str(c.name) for c in clauses]))
    result = propose_spec_review(
        model,
        board_summary=session.intent,
        findings=session.findings,
        unrouted=dict(route.unrouted) if route is not None else None,
        clauses=clauses,
        sourcing=sourcing,
        known_refs=known,
        on_event=emit,
    )
    # Only a *usable* agenda is left on the session, because the session is
    # what the ``spec_review`` deliver destination reads. Storing a failed
    # result there makes ``agenda_from_spec_review`` build an agenda with no
    # items, and ``_book_spec_review`` then answers "the agenda has no blocking
    # item -- nothing needs a meeting" over a run that has blocking findings
    # and unrouted nets: a model failure read as a clean board, which is the
    # quiet zero this repo does not allow. Left unset, deliver falls back to
    # ``agenda_from_result`` and derives the agenda from the findings
    # themselves -- the same answer as never having asked for one. The failure
    # is not hidden: it rides this step's envelope as ``spec_review: null``
    # plus the warning below.
    if result.review is not None:
        session.spec_review = result
    return (
        None if result.review is None else result.review.as_dict()
    ), evidence_warnings + list(result.warnings)


#: Said on the review envelope when the agenda went out without the case's
#: kernel receipt because the background design had not finished. The
#: agenda does not wait for it -- a review step that blocked on a case the
#: engineer never pressed for would be the wrong trade -- so it says so.
AGENDA_BEFORE_CASE = (
    "agenda prepared before the case design finished: no enclosure kernel "
    "clauses were shown to the model"
)
AGENDA_BEFORE_SOURCING = (
    "agenda prepared before the parts sourcing finished: no sourcing gaps "
    "were shown to the model"
)


def _agenda_evidence(session: Session) -> tuple[list[Any], Any, list[str]]:
    """The kernel clauses and sourcing result the agenda may cite, peeked.

    Both come from the session when a step already collected them, and
    otherwise from the background job *if it has finished* -- through
    :func:`_peek`, never a join, so an agenda is never held for a design
    nobody pressed for. A job still running contributes nothing and a
    warning; a job that failed contributes nothing and no warning here,
    because its failure is already on ``background_outcome`` and will be
    on the step that collects it. A session with no job at all (the
    one-shot route's synthetic session, a run that never placed) simply has
    what its fields carry.
    """
    warnings: list[str] = []

    enclosure = session.enclosure
    if enclosure is None and session.case_job is not None:
        if session.case_job.running:
            warnings.append(AGENDA_BEFORE_CASE)
        else:
            _, enclosure, _ = _peek(session.case_job)
    kernel = getattr(enclosure, "kernel", None) if enclosure is not None else None
    clauses = list(getattr(kernel, "clauses", ()) or ()) if kernel is not None else []

    sourcing = session.sourcing
    if sourcing is None and session.sourcing_job is not None:
        if session.sourcing_job.running:
            warnings.append(AGENDA_BEFORE_SOURCING)
        else:
            _, sourcing, _ = _peek(session.sourcing_job)
    return clauses, sourcing, warnings


def _review(session: Session, payload: dict[str, Any], *, model) -> dict[str, Any]:
    started = time.monotonic()
    wants_agenda = _wants_agenda(payload)
    events, emit, enter, tapped, _ = _events_sink(started, model, session=session)
    report = review_stage(
        tapped,
        session.spec,
        facts=session.facts,
        review=True,
        # The same level this session was started at decides the refutation
        # round, exactly as it does on ``/generate``. Reading the session
        # rather than defaulting is the point: a ``thorough`` session that
        # quietly reviewed like a ``fast`` one would report the level it was
        # asked for and do less, which is the one thing the slider exists to
        # prevent.
        refute=profile_for(session.effort).refute,
        emit=emit,
        enter=enter,
    )
    session.review = report
    session.findings = list(report.findings)
    from .app import _finding_dict, _refs_by_spec_name

    refs = _refs_by_spec_name(session.spec, session.board)
    warnings: list[str] = []
    # ``review`` rides the envelope beside ``findings`` because the list alone
    # cannot carry the distinction the overlay has to draw: an empty
    # ``findings`` means the critic found nothing *only* when
    # ``review.status`` is ``ok``. When the critic answered something
    # unreadable, this step still succeeds -- the board is the product, the
    # sourcing-stage convention -- but it says so here and in a warning, so a
    # client cannot render a clean review over a model failure. The desktop's
    # run receipt already writes "the critic raised no findings, which is not
    # a measurement"; this is the fact that sentence needs.
    body: dict[str, Any] = {
        "findings": [_finding_dict(f, refs) for f in session.findings],
        "blockers": [
            str(f.title) for f in session.findings if f.severity.value == "blocker"
        ],
        "review": review_block(report),
    }
    if not report.ok:
        warnings.append(f"the review produced no verdict: {report.note()}")
    if wants_agenda:
        try:
            block, agenda_warnings = _agenda(
                session, model=tapped, emit=emit, enter=enter
            )
        except Exception as exc:  # noqa: BLE001 -- the findings are the product
            body["spec_review"] = None
            warnings.append(
                f"the spec-review agenda could not be prepared: "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            body["spec_review"] = block
            warnings.extend(agenda_warnings)
    if warnings:
        body["warnings"] = warnings
    return _envelope(
        session,
        step="review",
        events=events,
        started=started,
        body=body,
    )


def _sourcing(session: Session, payload: dict[str, Any], *, model) -> dict[str, Any]:
    """Collect the BOM the background started at ``place``.

    Pressing this spends no model call: the lookup has been running since
    placement, and the step waits for it if it is not done. The events on
    the envelope are the lookup's own, so the ``stage.done`` counts -- or
    the ``sourcing.failed`` -- are visible where the BOM is. ``order``
    collects the same job when it is pressed first; this step then reports
    what it found.
    """
    started = time.monotonic()
    result, warnings = _collect_sourcing(session)
    return _envelope(
        session,
        step="sourcing",
        events=session.sourcing_events,
        started=started,
        body={"sourcing": result.as_dict(), "warnings": warnings},
    )


def _order(session: Session, payload: dict[str, Any], *, model) -> dict[str, Any]:
    """Prepare the order and put it where the engineer can find it.

    The ``order`` block is unchanged (manifest, issues, verdict, fab files
    inline) but for one addition: the manifest carries ``bom`` -- the rows
    the background sourcing found, collected here if ``sourcing`` was not
    pressed first -- and the package carries them as ``bom.csv``. Beside it,
    the same package lands on disk as ``<stem>-order.zip`` and
    ``<stem>-order.json`` -- an order that exists only inside an HTTP
    response is one nobody can send -- and the routed board is exported to a
    3D model when a ``kicad-cli`` can be found. Every export that did not
    happen is a ``warnings`` entry naming why; a file is listed only when it
    is on disk and non-empty. Nothing here submits anything, and nothing
    here confirms a part number: ``mpn_status`` reads ``verified`` only when
    the sourcing stage's distributor lookup (Mouser, behind
    ``MOUSER_API_KEY``) listed it, and ``proposed`` otherwise. The package
    carries the per-designator ``bom.csv`` and the assembler's grouped
    ``bom-grouped.csv``; the manifest names both under ``bom_files``.
    """
    started = time.monotonic()
    events, emit, enter, _, _ = _events_sink(started, session=session)
    from silkscreen.order import package_zip

    from .app import _order_block, order_options
    from .kicad_cli import export_models

    options = order_options(payload.get("order") or {})
    # Collected before the order block is built so the BOM's events precede
    # the order's own when this step is the one that waited for them.
    collected_here = session.sourcing is None
    sourcing, warnings = _collect_sourcing(session)
    if collected_here:
        events.extend(session.sourcing_events)
    session.order = _order_block(session.board, session.spec, options)

    manifest = session.order["manifest"]
    manifest["bom"] = [entry.as_dict() for entry in sourcing.parts]
    manifest["bom_files"] = ["bom.csv", "bom-grouped.csv"]
    zip_path = session.path("-order.zip")
    zip_path.write_bytes(
        package_zip(
            [(f["filename"], f["content"]) for f in session.order["files"]]
            + [
                ("bom.csv", bom_csv(sourcing)),
                ("bom-grouped.csv", grouped_bom_csv(sourcing)),
            ],
            manifest,
        )
    )
    manifest_path = session.path("-order.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    session.files["order"] = str(zip_path)
    session.files["order_manifest"] = str(manifest_path)

    board_file = session.files.get("board")
    if board_file is None:
        warnings.append("no 3D model exported: the routed board is not on disk")
    else:
        # ``<stem>-board``, never ``<stem>``: the case step already wrote its
        # assembly to ``<stem>.step``, and an export to the bare stem
        # overwrote it with the board, so "open the case" opened a PCB
        # (seen in session 1e69231541b4, 2026-09-13: ``<stem>.step`` was
        # KiCad's "electronic assembly", written by the order step).
        report = export_models(Path(board_file), session.path("-board"))
        session.files.update(report.files)
        warnings.extend(report.warnings)
    for warning in warnings:
        emit({"event": "order.warning", "stage": "order", "message": warning})

    return _envelope(
        session,
        step="order",
        events=events,
        started=started,
        body={
            "order": session.order,
            "sourcing": sourcing.as_dict(),
            "warnings": warnings,
        },
    )


def _case(session: Session, payload: dict[str, Any], *, model) -> dict[str, Any]:
    style = payload.get("enclosure_style", "")
    if not isinstance(style, str) or len(style) > MAX_ENCLOSURE_STYLE_CHARS:
        raise ValueError(
            "'enclosure_style' must be a string of at most "
            f"{MAX_ENCLOSURE_STYLE_CHARS} characters"
        )
    rigorous = payload.get("enclosure_rigorous", False)
    if not isinstance(rigorous, bool):
        raise ValueError("'enclosure_rigorous' must be a boolean")
    started = time.monotonic()
    warnings: list[str] = []
    # The one place a note typed mid-run is genuinely consumed by a later
    # stage: ``enclosure_style`` is the only free-text field any step payload
    # has, so ``case`` is the only entry in ``amend.CONSUMING_STEPS``. An
    # explicit ``enclosure_style`` in this request wins -- it is the more
    # recent instruction, and it was aimed at this button.
    if not style.strip():
        taken = _amend.take_for(session, "case")
        if taken:
            style = " ".join(a.text for a in taken)[:MAX_ENCLOSURE_STYLE_CHARS]
            warnings.append(
                "the case was designed for "
                + ("your note" if len(taken) == 1 else f"your {len(taken)} notes")
                + " rather than the default, so the case started in the "
                "background at 'place' was discarded and a fresh one designed "
                "-- that is one more model call"
            )
    job = session.case_job
    if style.strip() or rigorous or job is None:
        # The prefetch was the default case; a request that asks for
        # something else gets a fresh design, in line. The stage writes into
        # the session directory under the board's stem, so ``<stem>.step``,
        # ``<stem>-base.stl`` and ``<stem>-lid.stl`` land beside the board.
        events, emit, enter, tapped, _ = _events_sink(started, model, session=session)
        session.enclosure = enclosure_stage(
            tapped,
            session.board,
            enclosure=True,
            enclosure_style=style.strip(),
            rigorous=rigorous,
            output=None,
            emit_stages=False,
            export_dir=session.directory,
            stem=session.stem,
            emit=emit,
            enter=enter,
        )
    else:
        # Collect the design started at ``place``, waiting if it is still
        # running. Its exception -- a model outage, most likely -- is the
        # step's failure to report, not a reason to pretend there is no case.
        events = session.case_events
        try:
            session.enclosure = job.result()
        except Exception as exc:  # noqa: BLE001 -- reported, never hidden
            session.enclosure = None
            warnings.append(_case_failure(exc))
    from .app import _enclosure_dict

    body: dict[str, Any] = {
        "enclosure": _enclosure_dict(session.enclosure, include_brief=True)
    }
    if session.enclosure is not None:
        enclosure = session.enclosure
        exports = getattr(enclosure, "exports", None)
        if exports is None:
            # The kernel built a case but nothing durable was written -- the
            # stage had no directory. The STEP text is still the whole case,
            # so it is written here rather than lost.
            step_path = session.path(".step")
            step_path.write_text(enclosure.step_text, encoding="utf-8")
        else:
            step_path = Path(exports.step)
            session.files["case_base_stl"] = str(exports.base_stl)
            session.files["case_lid_stl"] = str(exports.lid_stl)
        # ``case`` is the STEP assembly: the one file that carries the whole
        # design, that KiCad, FreeCAD and every other CAD tool reads, and that
        # the desktop hands to whatever owns ``.step``.
        session.files["case"] = str(step_path)
        session.files["case_step"] = str(step_path)
        snapshots = getattr(enclosure, "snapshots", ())
        if snapshots:
            session.files["case_snapshots"] = [str(p) for p in snapshots]
        # Desktop mode shows each stage in the tool that reads it: KiCad for
        # the board, FreeCAD for the case. Pressing ``case`` is the ask.
        if session.kicad_live:
            opened, detail = open_in_freecad(step_path)
            body["opened_in_freecad"] = opened
            body["freecad_detail"] = detail
            if not opened and detail:
                note = f"the case was not opened in FreeCAD: {detail}"
                warnings.append(note)
                # The case panel reads the enclosure block's own warnings, so
                # the sentence goes there too rather than only on the envelope.
                block = body.get("enclosure")
                if isinstance(block, dict):
                    block.setdefault("warnings", []).append(note)
    else:
        warnings.append(NO_CASE_WARNING)
    if warnings:
        body["warnings"] = warnings
    return _envelope(
        session, step="case", events=events, started=started, body=body
    )


_RUNNERS = {
    "place": _place,
    "route": _route,
    "review": _review,
    "sourcing": _sourcing,
    "order": _order,
    "case": _case,
}


def advance(
    session_id: str, step: str, payload: dict[str, Any], *, model
) -> dict[str, Any]:
    """Run one approved step against a live session."""
    if step not in STEPS:
        raise StepNotFound(f"no step {step!r}; steps are {', '.join(STEPS)}")
    session = _get(session_id)
    # Checked before the lock, so a cancel is answered immediately rather than
    # after the step already running has finished holding it. This is the
    # saving that cancellation actually buys: every step still to come is a
    # model call or a solver budget that now never happens.
    if session.cancelled.is_set():
        raise StepOrderError(
            f"run {session_id} was cancelled; no further step will run. "
            "Start a new run from the amended intent."
        )
    with session.lock:
        if step in session.done:
            raise StepOrderError(f"step {step!r} already ran for this session")
        needed = _REQUIRES[step]
        if needed not in _reached(session.stage):
            raise StepOrderError(
                f"step {step!r} needs the run to be {needed!r}; it is {session.stage!r}"
            )
        result = _RUNNERS[step](session, payload, model=model)
        session.done.add(step)
        return result


def _reached(stage: str) -> set[str]:
    order = ["new", "proposed", "placed", "routed"]
    return set(order[: order.index(stage) + 1]) if stage in order else set(order)


def _envelope(
    session: Session,
    *,
    step: str,
    events: list[dict[str, Any]],
    started: float,
    body: dict[str, Any],
) -> dict[str, Any]:
    shown, shown_detail = _show(session, step)
    response = {
        "session": session.id,
        "step": step,
        "stage": session.stage,
        "intent": session.intent,
        "files": dict(session.files),
        "next": _next_steps(session, just_ran=step),
        "background": session.background,
        "background_outcome": session.background_outcome,
        # Which effort level this session runs at, and what that level cost
        # the board. On every envelope, not just the first: a strip that
        # joined mid-session still has to be able to say that the board in
        # front of it was placed at ``fast``.
        "effort": (
            None
            if session.effort_receipt is None
            else session.effort_receipt.as_dict()
        ),
        # Everything typed at the strip during this run, applied or not, so a
        # client never has to keep its own copy in step with the server's.
        "amendments": _amend.block(session),
        "cancelled": session.cancelled.is_set(),
        "shown_in_kicad": shown,
        "shown_detail": shown_detail,
        "events": events,
        "duration_s": round(time.monotonic() - started, 3),
    }
    response.update(body)
    return response


def _next_steps(session: Session, *, just_ran: str | None = None) -> list[str]:
    """The steps the caller may take now.

    ``just_ran`` is the step whose envelope is being built: it is marked done
    by ``advance`` only after its body returns, so it is excluded here.
    """
    reached = _reached(session.stage)
    done = session.done | ({just_ran} if just_ran else set())
    return [s for s in STEPS if _REQUIRES[s] in reached and s not in done]


def status(session_id: str) -> dict[str, Any]:
    session = _get(session_id)
    return {
        "session": session.id,
        "stage": session.stage,
        "intent": session.intent,
        "files": dict(session.files),
        "done": sorted(session.done),
        "next": _next_steps(session),
        "background": session.background,
        "background_outcome": session.background_outcome,
        "amendments": _amend.block(session),
        "cancelled": session.cancelled.is_set(),
        # The same block the review envelope carries; the skipped block
        # until that step is pressed, never an absent key a client could
        # read as "reviewed and nothing to say".
        "review": review_block(session.review),
        "kicad_live": session.kicad_live,
        "shown_detail": session.shown_detail,
    }


# ---------------------------------------------------------------- routing


def _parse(path: str) -> tuple[str | None, str | None]:
    parts = [p for p in path.split("?")[0].split("/") if p]
    if not parts or parts[0] != "steps":
        raise StepNotFound(f"no route {path}")
    if len(parts) == 1:
        return None, None
    if len(parts) == 2:
        return parts[1], None
    if len(parts) == 3:
        return parts[1], parts[2]
    raise StepNotFound(f"no route {path}")


def start_once(
    payload: dict[str, Any],
    *,
    model,
    store,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """:func:`start`, answered at most once per ``Idempotency-Key``.

    ``POST /steps`` is the one route here with no defence of its own: every
    other step is guarded by ``advance``'s ``step in session.done`` check under
    the session lock, but a start has no session yet, so a second one is a
    second read, a second plan and a second propose -- several model calls for
    a run the caller already paid for. The desktop's in-flight ref stops a
    double click inside one hook and stops nothing else; a webview reload, a
    second window, or a cancel-then-start (which aborts the fetch but not the
    work: ``useStepRun.fail`` says so) all get past it.

    So the key is the server's half, and it is Stripe's design rather than one
    invented here. ``stripe/_api_requestor.py::request_headers`` sets an
    ``Idempotency-Key`` on every POST for exactly this reason, and the API
    answers a replay with the original response and a request still in flight
    under the same key with a 409. Both halves are here: a finished start is
    replayed verbatim -- which also recovers a run whose session id never
    reached the caller -- and one still running raises
    :class:`StepOrderError`, the 409 this module already speaks.

    A start that *failed* forgets its key: nothing was produced, so the honest
    reading is that the operation never happened and may be attempted again.
    Without a key the behaviour is exactly what it was, because the header is
    optional and no existing client sends one.
    """
    if not idempotency_key:
        return start(payload, model=model, store=store)
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ValueError(
            f"'Idempotency-Key' must be at most {MAX_IDEMPOTENCY_KEY_CHARS} characters"
        )
    with _STARTED_LOCK:
        if idempotency_key in _STARTED:
            answered = _STARTED[idempotency_key]
            if answered is None:
                raise StepOrderError(
                    "a run with this Idempotency-Key is already starting; "
                    "it was not started again. Ask for it once it has finished."
                )
            return answered
        _STARTED[idempotency_key] = None
        while len(_STARTED) > MAX_IDEMPOTENCY_KEYS:
            _STARTED.popitem(last=False)
    try:
        body = start(payload, model=model, store=store)
    except BaseException:
        with _STARTED_LOCK:
            # Only if it is still the in-flight marker: an eviction may have
            # dropped it, and a later key must never be cleared by this one.
            if _STARTED.get(idempotency_key, "") is None:
                del _STARTED[idempotency_key]
        raise
    with _STARTED_LOCK:
        if idempotency_key in _STARTED:
            _STARTED[idempotency_key] = body
    return body


def handle_post(
    path: str,
    payload: dict[str, Any],
    *,
    model,
    store,
    idempotency_key: str = "",
) -> dict[str, Any]:
    session_id, step = _parse(path)
    if session_id is None:
        return start_once(
            payload, model=model, store=store, idempotency_key=idempotency_key
        )
    if step is None:
        raise StepNotFound("POST a step: /steps/<id>/" + "|".join(STEPS))
    if step == VIEW_3D:
        # Not a step: it produces nothing, changes no state, spends no model
        # call, and may be pressed any number of times. So it is answered here
        # rather than through ``advance``, which would refuse the second press
        # with "already ran" and demand a stage transition this has none of.
        return show_board_3d(session_id)
    if step == OPEN_CASE:
        # Not a step either, for the same reasons: nothing is produced and it
        # may be pressed any number of times.
        return open_case_in_freecad(session_id)
    return advance(session_id, step, payload, model=model)


def handle_get(path: str) -> dict[str, Any]:
    session_id, step = _parse(path)
    if session_id is None or step is not None:
        raise StepNotFound("GET /steps/<id>")
    return status(session_id)

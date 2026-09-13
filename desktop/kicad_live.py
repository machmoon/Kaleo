"""Push a silkscreen run into the KiCad window the engineer already has open.

The engine never talks to KiCad: it writes files and stops, and that rule is
what keeps it offline-testable. This module is the other side of that
boundary, and it lives here in ``desktop/`` rather than in the engine for the
same reason ``slackbot/`` and ``meetings/`` do -- it knows KiCad, and it
knows the *files* the engine wrote, and it never imports the engine.

It also runs in its own interpreter, ``.venv-kicad``, because ``kicad-python``
pins ``protobuf<6`` and the Gemini SDK needs ``protobuf>=6.33``. The two cannot
share a venv, which is a happy accident: the process that drives KiCad is
physically unable to import the engine.

What an engineer sees, stage by stage:

* ``schematic`` -- the ``.kicad_sch`` opens in the schematic editor.
* ``placement`` -- the pre-routing ``.placed.kicad_pcb`` opens in the board
  editor. No copper, just parts and ratsnest, which is exactly what a
  placement review wants.
* ``routing`` -- every track and via in the routed ``.kicad_pcb`` is created
  in that *open* board through KiCad's IPC API, so copper appears in the
  window the engineer is already looking at instead of a second file opening.
* ``3d`` -- the routed ``.kicad_pcb`` is opened in the board editor and
  **KiCad's own 3D viewer** is opened on it. "Show me the board in 3D" means
  the viewer the engineer already trusts, reading the same file KiCad reads,
  with the component bodies :mod:`silkscreen.models3d` named on every
  footprint -- not a picture of the board rendered somewhere else.

There is no ``case`` stage. The enclosure is a STEP assembly and the desktop
hands it to whatever owns ``.step``; nothing here launches OpenSCAD (removed
2026-09-08 with the v1 emitter).

The IPC API needs KiCad 9 or newer with *Preferences > Plugins > Enable API
server* on. Nothing here writes to disk: the open board is left modified and
unsaved, because saving it is the engineer's decision, not the tool's.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from kiutils.board import Board as FileBoard
from kiutils.items.brditems import Segment as FileSegment
from kiutils.items.brditems import Via as FileVia

NM_PER_MM = 1_000_000

# The editors are launched directly, not through the KiCad app bundle: opening
# a board via the top-level app lands in the project manager, which owns the
# API socket but has no board editor behind it, so every board request comes
# back "no handler available". A standalone pcbnew serves its own board.
_KICAD_BUNDLE = Path("/Applications/KiCad/KiCad.app/Contents/Applications")
PCBNEW_APP = str(_KICAD_BUNDLE / "pcbnew.app")
EESCHEMA_APP = str(_KICAD_BUNDLE / "eeschema.app")


class BridgeError(RuntimeError):
    """KiCad could not be reached, or the board it has open is not ours."""


# ---------------------------------------------------------------- opening


def open_in_app(path: Path, app: str) -> None:
    """Open ``path`` with a named macOS application (``open -a``)."""
    if sys.platform != "darwin":
        raise BridgeError(
            f"opening {path.name} in {app} is macOS-only here (uses `open -a`)"
        )
    subprocess.run(["open", "-a", app, str(path)], check=True)


def show_schematic(sch_path: Path) -> None:
    _require(sch_path)
    open_in_app(sch_path, EESCHEMA_APP)


def show_placement(placed_pcb: Path) -> None:
    _require(placed_pcb)
    open_in_app(placed_pcb, PCBNEW_APP)


def _require(path: Path) -> None:
    if not path.is_file():
        raise BridgeError(f"no such file: {path}")


# ---------------------------------------------------------------- IPC


#: Seconds a running pcbnew gets to bind its API socket before its absence
#: means the API server is off. A freshly opened editor binds within two or
#: three seconds; one with the server disabled never does.
API_SOCKET_GRACE_S = 10.0

#: The one-line fixes, as the overlay relays them.
API_SERVER_OFF = (
    "KiCad's API server is off: Preferences > Plugins > Enable API server, "
    "then reopen the board"
)
NO_BOARD_EDITOR = "no board editor is running: open the placed board in pcbnew first"
NO_KIPY = "kicad-python is not installed in the bridge venv (see desktop/README.md)"


def _pcbnew_pids() -> list[str]:
    out = subprocess.run(
        ["pgrep", "-x", "pcbnew"], capture_output=True, text=True, check=False
    )
    return out.stdout.split()


def _socket_path(pid: str) -> Path:
    return Path(f"/tmp/kicad/api-{pid}.sock")


#: The socket every KiCad binds regardless of version. It belongs to whichever
#: process started first, which in this flow can be the schematic editor, so it
#: is the fallback rather than the first choice.
SHARED_SOCKET = Path("/tmp/kicad/api.sock")


def _pcbnew_sockets() -> list[str]:
    """The IPC sockets that might front a board editor, best first.

    KiCad 9 binds one socket per process, ``/tmp/kicad/api-<pid>.sock``, next
    to the shared ``api.sock``. Preferring the per-pid socket is what makes
    "push copper into *that* pcbnew" exact: the shared one belongs to whichever
    process started first -- the schematic editor, in this flow, which has no
    board handler and answers every board request "no handler available".

    KiCad 10 does not bind the per-pid sockets at all (measured on 10.0.6:
    pcbnew running, ``/tmp/kicad/`` holds only ``api.lock`` and ``api.sock``).
    Requiring them there means no socket is ever seen, which surfaces as
    ``API_SERVER_OFF`` -- a message that sends the engineer to a preference
    that is already on. So the shared socket is a fallback when no per-pid
    socket exists. It is only ever a *candidate*: ``connect_board`` still
    checks the open board's name, so a shared socket fronting the wrong editor
    is rejected there rather than written into.
    """
    pids = _pcbnew_pids()
    sockets = [
        f"ipc://{_socket_path(pid)}" for pid in pids if _socket_path(pid).exists()
    ]
    if not sockets and pids and SHARED_SOCKET.exists():
        # Only when a board editor is actually running. With no pcbnew, the
        # shared socket can only belong to the schematic editor or the project
        # manager, and pushing copper is not their job -- "nothing was opened
        # to push into" is the honest answer, not a connection to the wrong
        # process.
        sockets.append(f"ipc://{SHARED_SOCKET}")
    return sockets


def connect_board(
    stem: str, timeout_s: float = 60.0, *, socket_grace_s: float | None = None
):
    """The board editor showing ``<stem>*``, as ``(kicad, board)``.

    Waits for the editor to start and finish loading: a pcbnew that has just
    opened a file binds its socket late and answers "busy" for a few seconds,
    and neither is a failure. Pushing copper into whatever board happens to
    be open would be the wrong board more often than not, so the name is
    checked, not assumed.

    Two conditions are decided early rather than after the whole timeout,
    each with the fix in the message: a pcbnew that has been running for
    ``socket_grace_s`` without a socket has the API server off, and no
    pcbnew at all after that long means nothing was opened to push into.
    """
    if socket_grace_s is None:
        socket_grace_s = API_SOCKET_GRACE_S
    deadline = time.monotonic() + timeout_s
    grace_end = time.monotonic() + min(socket_grace_s, timeout_s)
    last = NO_BOARD_EDITOR
    kicad_cls = None
    seen_socket = False
    while time.monotonic() < deadline:
        sockets = _pcbnew_sockets()
        seen_socket = seen_socket or bool(sockets)
        if not seen_socket and time.monotonic() >= grace_end:
            raise BridgeError(API_SERVER_OFF if _pcbnew_pids() else NO_BOARD_EDITOR)
        if sockets and kicad_cls is None:
            try:
                from kipy import KiCad as kicad_cls
            except ImportError as exc:
                raise BridgeError(f"{NO_KIPY}: {exc}") from exc
        for socket in sockets:
            try:
                kicad = kicad_cls(socket_path=socket)
                board = kicad.get_board()
            except Exception as exc:  # noqa: BLE001 - several client error kinds
                last = str(exc)
                continue
            name = Path(board.name or "").name
            # Exact, not a prefix. Stems are intent slugs, so
            # `slug("a 3.3V LDO board")` is a prefix of
            # `slug("a 3.3V LDO board with USB")` -- a prefix match would push
            # this run's copper into the other run's open board, which is the
            # precise accident the name check exists to prevent. Two names are
            # legitimate: the pre-routing board the placement stage opened,
            # and the routed file itself.
            if name in (f"{stem}.placed.kicad_pcb", f"{stem}.kicad_pcb"):
                return kicad, board
            last = f"open board is {name!r}, wanted {stem}.placed.kicad_pcb"
        time.sleep(0.5)
    if not seen_socket:
        raise BridgeError(API_SERVER_OFF if _pcbnew_pids() else NO_BOARD_EDITOR)
    raise BridgeError(f"board not open in KiCad within {timeout_s:.0f}s: {last}")


# ---------------------------------------------------------------- copper


def _copper_from_file(routed_pcb: Path):
    """Tracks and vias the routed file carries, with net names resolved."""
    fb = FileBoard.from_file(str(routed_pcb))
    net_name = {n.number: n.name for n in fb.nets}
    segments: list[tuple[str, float, float, float, float, float, str]] = []
    vias: list[tuple[float, float, float, float, str]] = []
    for item in fb.traceItems:
        if isinstance(item, FileSegment):
            segments.append(
                (
                    item.layer,
                    item.start.X,
                    item.start.Y,
                    item.end.X,
                    item.end.Y,
                    item.width,
                    net_name.get(item.net, ""),
                )
            )
        elif isinstance(item, FileVia):
            vias.append(
                (
                    item.position.X,
                    item.position.Y,
                    item.size,
                    item.drill,
                    net_name.get(item.net, ""),
                )
            )
    return segments, vias


def _nm(mm: float) -> int:
    return round(mm * NM_PER_MM)


def push_copper(board, routed_pcb: Path) -> dict[str, int]:
    """Create the routed file's tracks and vias in the open board.

    Items already present (same endpoints, layer and net) are skipped, so
    running twice does not double the copper. Returns what was created.
    """
    from kipy.board_types import BoardLayer, Track, Via
    from kipy.geometry import Vector2
    from kipy.proto.board.board_types_pb2 import ViaType

    layers = {"F.Cu": BoardLayer.BL_F_Cu, "B.Cu": BoardLayer.BL_B_Cu}
    nets = {n.name: n for n in board.get_nets()}

    have_tracks = {
        (t.layer, t.start.x, t.start.y, t.end.x, t.end.y) for t in board.get_tracks()
    }
    have_vias = {(v.position.x, v.position.y) for v in board.get_vias()}

    segments, vias = _copper_from_file(routed_pcb)
    items = []
    skipped = 0
    for layer_name, x0, y0, x1, y1, width, net in segments:
        layer = layers.get(layer_name)
        if layer is None:
            skipped += 1
            continue
        key = (layer, _nm(x0), _nm(y0), _nm(x1), _nm(y1))
        if key in have_tracks:
            skipped += 1
            continue
        t = Track()
        t.layer = layer
        t.start = Vector2.from_xy(_nm(x0), _nm(y0))
        t.end = Vector2.from_xy(_nm(x1), _nm(y1))
        t.width = _nm(width)
        if net in nets:
            t.net = nets[net]
        items.append(t)
    for x, y, size, drill, net in vias:
        if (_nm(x), _nm(y)) in have_vias:
            skipped += 1
            continue
        v = Via()
        v.position = Vector2.from_xy(_nm(x), _nm(y))
        v.type = ViaType.VT_THROUGH
        v.diameter = _nm(size)
        v.drill_diameter = _nm(drill)
        if net in nets:
            v.net = nets[net]
        items.append(v)

    created = 0
    if items:
        commit = board.begin_commit()
        board.create_items(items)
        board.push_commit(commit, "silkscreen: routing")
        created = len(items)
    return {"created": created, "skipped": skipped}


def show_routing(routed_pcb: Path, *, timeout_s: float = 60.0) -> dict[str, int]:
    """Copper from ``routed_pcb`` appears in the open placed board."""
    _require(routed_pcb)
    stem = routed_pcb.name.split(".")[0]
    _kicad, board = connect_board(stem, timeout_s)
    # A board editor that has just opened a file answers "KiCad is busy" for
    # a few seconds while it finishes loading; that is a wait, not a failure.
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return push_copper(board, routed_pcb)
        except Exception as exc:  # noqa: BLE001
            if "busy" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(1.0)


# ---------------------------------------------------------------- CLI


#: The 3D viewer is a *window* of the running board editor, never a file and
#: never a command. KiCad's own source is unambiguous about this: the action
#: ``common.Control.show3DViewer`` (``common/tool/actions.cpp:1274``, default
#: hotkey Alt+3) dispatches to ``PCB_VIEWER_TOOLS::Show3DViewer``
#: (``pcbnew/tools/pcb_viewer_tools.cpp:75``), whose body is
#: ``frame()->CreateAndShow3D_Frame()`` -- the frame is built by the running
#: editor from the board it already has in memory. There is no viewer binary
#: (``3d-viewer/CMakeLists.txt:118`` builds a STATIC library, and KiCad 10.0.6's
#: bundle ships only ``kicad``/``kicad-cli``/``pcbnew``), ``kicad-cli`` has no
#: GUI subcommand (``kicad/kicad_cli.cpp:143``), and ``pcbnew`` takes a file
#: and nothing else (``common/single_top.cpp:343`` -- positional parameters
#: only, no switches). So the only honest sequence is: open the board in the
#: editor, then ask the editor for the viewer.
#:
#: Two doors to that ask, tried in order:
#:
#: 1. The IPC API's ``RunAction``, reached through ``kipy.KiCad.run_action``.
#:    Structured, locale-independent, and needs no Accessibility grant. KiCad
#:    marks it explicitly unstable ("the TOOL_ACTIONs are specifically *not* an
#:    API", ``api/proto/common/commands/editor_commands.proto``) and refuses it
#:    headless (``pcbnew/api/api_handler_board.cpp``, ``checkForHeadless``),
#:    which is fine: this only ever runs against a GUI editor. It needs the API
#:    server preference on -- the same one ``routing`` already needs.
#: 2. Clicking the menu with AppleScript, which needs neither the API server
#:    nor a stable action name, but is macOS-only, English-menu-only, and
#:    wants an Accessibility grant.
#:
#: Neither is a superset of the other, which is why both are here.
#:
#: The menu moved between releases -- ``View > 3D Viewer`` in KiCad 8 and 9,
#: ``Window > 3D Viewer`` in KiCad 10 (measured on 10.0.6) -- so both are
#: tried rather than one being assumed.
VIEWER_MENUS = ("Window", "View")
VIEWER_ITEM = "3D Viewer"
NO_VIEWER_MENU = (
    "no '3D Viewer' entry in pcbnew's Window or View menu (KiCad too old?)"
)
VIEWER_UNSUPPORTED = "the 3D viewer can only be opened on macOS"
VIEWER_NO_ACCESS = (
    "macOS refused to drive pcbnew's menus: grant Accessibility to the app "
    "running the bridge (System Settings > Privacy & Security > Accessibility)"
)


def _osascript(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )


#: Seconds a just-opened pcbnew gets to put its menu bar up before the click
#: is called a failure. Measured on KiCad 10.0.6 / M-series: a cold launch is
#: ~4 s to a live menu bar, a warm one under 1 s.
EDITOR_MENU_WAIT_S = 25.0


def open_board(pcb: Path) -> None:
    """Make ``pcb`` the board the frontmost pcbnew is showing.

    ``open -a`` is idempotent on a document already open: macOS activates that
    window rather than loading a second copy, so pressing "3D" twice does not
    accumulate windows. When pcbnew is showing a *different* board -- typically
    this run's pre-routing ``.placed.kicad_pcb``, which the placement stage
    opened -- the routed file opens frontmost, which is the right answer: the
    routed ``.kicad_pcb`` on disk is the product, copper included, where the
    placed window's copper only exists as an unsaved IPC push.
    """
    _require(pcb)
    open_in_app(pcb, PCBNEW_APP)


def _await_menu_bar(deadline: float) -> str:
    """pcbnew's menu-bar item names, once one of the viewer menus is up.

    An editor launched from cold has a process before it has a menu bar, and
    a menu bar before it has every menu on it, so this waits for a menu that
    could actually hold the entry rather than for the first readable answer.
    Every way out raises with the fix in the message: no editor, no
    Accessibility grant, or a menu bar with neither menu on it.
    """
    refusal = ""
    readable = ""
    while True:
        if not _pcbnew_pids():
            if time.monotonic() >= deadline:
                raise BridgeError(NO_BOARD_EDITOR)
            time.sleep(0.4)
            continue
        probe = _osascript(
            'tell application "System Events" to tell process "pcbnew" '
            "to get name of every menu bar item of menu bar 1"
        )
        if probe.returncode == 0:
            readable = probe.stdout
            if any(menu in readable for menu in VIEWER_MENUS):
                return readable
        else:
            refusal = probe.stderr.strip()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if readable:
        # The menus were readable throughout and neither one was there. That
        # is a KiCad too old for this entry, not a permissions problem.
        raise BridgeError(NO_VIEWER_MENU)
    raise BridgeError(f"{VIEWER_NO_ACCESS}: {refusal}" if refusal else NO_VIEWER_MENU)


def show_3d(pcb: Path | None = None, *, timeout_s: float = EDITOR_MENU_WAIT_S) -> str:
    """Open KiCad's own 3D viewer on ``pcb``.

    The viewer is a *window of the board editor*, so it can only ever show the
    board that editor has open -- which is why this opens the board first and
    clicks second. Called with no ``pcb`` it falls back to the old behaviour
    (whatever pcbnew already has), which is what the post-routing rider wants,
    since that board is already ours and already carries the pushed copper.

    Returns the menu it was found under. Raises ``BridgeError`` with the fix
    in the message when there is no editor, no entry, or no Accessibility
    permission -- never a silent no-op, because "the viewer did not appear"
    and "the viewer is empty" send an engineer to different places.
    """
    deadline = time.monotonic() + timeout_s
    if pcb is not None:
        open_board(pcb)
    elif not _pcbnew_pids():
        raise BridgeError(NO_BOARD_EDITOR)
    # A short budget, not the whole deadline: with the API server off there is
    # no socket to wait for, and every second spent here is a second the menu
    # fallback does not get.
    if _run_action_3d(min(deadline, time.monotonic() + IPC_VIEWER_BUDGET_S)):
        return "IPC"
    if sys.platform != "darwin":
        # The IPC door is cross-platform; the menu door is not. Say which one
        # was missing rather than "unsupported", which reads as "never works".
        raise BridgeError(f"{API_SERVER_OFF} ({VIEWER_UNSUPPORTED})")
    present = _await_menu_bar(deadline)
    return _click_viewer(present)


#: KiCad's own name for the action. Unstable by KiCad's own warning, which is
#: why a failure here falls through to the menu rather than ending the attempt.
VIEWER_ACTION = "common.Control.show3DViewer"

#: Seconds the IPC door gets before the menu door is tried. A freshly opened
#: editor binds its socket in two or three seconds; one with the API server off
#: never will, and waiting the full deadline for it would starve the fallback.
IPC_VIEWER_BUDGET_S = 6.0


def _run_action_3d(deadline: float) -> bool:
    """Ask the running editor for its 3D viewer over IPC. False if it could
    not be asked -- never an exception, because the menu is still to try."""
    try:
        from kipy import KiCad
        from kipy.proto.common.commands import editor_commands_pb2 as _ec
    except ImportError:
        return False
    ok = _ec.RAS_OK
    while True:
        for socket in _pcbnew_sockets():
            try:
                status = KiCad(socket_path=socket).run_action(VIEWER_ACTION)
            except Exception:  # noqa: BLE001 - several client error kinds
                continue
            if getattr(status, "status", status) == ok:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _click_viewer(present: str) -> str:
    for menu in VIEWER_MENUS:
        if menu not in present:
            continue
        clicked = _osascript(
            'tell application "System Events" to tell process "pcbnew" to click '
            f'menu item "{VIEWER_ITEM}" of menu 1 of menu bar item "{menu}" '
            "of menu bar 1"
        )
        if clicked.returncode == 0:
            return menu
    raise BridgeError(NO_VIEWER_MENU)


def _project_files(pcb: Path) -> dict[str, Path]:
    stem = pcb.name.split(".")[0]
    d = pcb.parent
    return {
        "schematic": d / f"{stem}.kicad_sch",
        "placement": d / f"{stem}.placed.kicad_pcb",
        "routing": d / f"{stem}.kicad_pcb",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kicad_live",
        description="Show a silkscreen run in the open KiCad, one stage at a time.",
    )
    ap.add_argument("pcb", type=Path, help="the run's <stem>.kicad_pcb")
    ap.add_argument(
        "stage",
        choices=["schematic", "placement", "routing", "3d", "replay"],
        help="which stage to show; 'replay' walks them all with a pause between",
    )
    ap.add_argument(
        "--pause", type=float, default=4.0, help="replay: seconds between stages"
    )
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    files = _project_files(args.pcb)
    order = (
        ["schematic", "placement", "routing", "3d"]
        if args.stage == "replay"
        else [args.stage]
    )
    try:
        for i, stage in enumerate(order):
            if stage == "3d":
                # In a replay the editor is already on this run's board with
                # the copper pushed into it, so the viewer is asked for on
                # what is open rather than reopening the file underneath it.
                # Asked for on its own, the board is named, because "show me
                # the board in 3D" means *this* board and the editor might be
                # showing another run, or nothing at all.
                board = None if args.stage == "replay" else files["routing"]
                print(f"3d: opened KiCad's 3D viewer via {show_3d(board)}")
                continue
            path = files[stage]
            if stage == "routing":
                result = show_routing(path, timeout_s=args.timeout)
                print(
                    f"routing: {result['created']} items created, "
                    f"{result['skipped']} already there"
                )
            else:
                shows = {
                    "schematic": show_schematic,
                    "placement": show_placement,
                }
                shows[stage](path)
                print(f"{stage}: opened {path.name}")
            if args.stage == "replay" and i < len(order) - 1:
                time.sleep(args.pause)
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - one relayable line beats a traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

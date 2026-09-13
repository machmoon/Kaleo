"""The KiCad bridge's socket discovery and its follow-on 3D stage.

``desktop/kicad_live.py`` runs in its own interpreter (``.venv-kicad``) and
lives outside every ``testpaths`` directory, so it is exercised from here --
the package that spawns it -- rather than from a test file nothing collects.
Only the parts that need no KiCad are covered: which socket the bridge would
talk to, and which stage rides on which.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))

import kicad_live  # noqa: E402

from service import steps  # noqa: E402


@pytest.fixture
def sockets(tmp_path, monkeypatch):
    """A fake /tmp/kicad holding whatever a test puts in it."""
    monkeypatch.setattr(kicad_live, "SHARED_SOCKET", tmp_path / "api.sock")
    monkeypatch.setattr(
        kicad_live, "_socket_path", lambda pid: tmp_path / f"api-{pid}.sock"
    )
    return tmp_path


def test_per_pid_socket_wins_when_kicad_9_bound_one(sockets, monkeypatch):
    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: ["4242"])
    (sockets / "api-4242.sock").touch()
    (sockets / "api.sock").touch()
    # The shared socket can front the schematic editor, which has no board
    # handler, so a per-pid socket is preferred whenever one exists.
    assert kicad_live._pcbnew_sockets() == [f"ipc://{sockets / 'api-4242.sock'}"]


def test_shared_socket_is_used_when_kicad_10_bound_no_per_pid_socket(
    sockets, monkeypatch
):
    # Measured on KiCad 10.0.6: pcbnew running, /tmp/kicad holds only
    # api.lock and api.sock. Requiring api-<pid>.sock found nothing, so the
    # bridge reported "the API server is off" against a server that was on.
    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: ["4242"])
    (sockets / "api.sock").touch()
    assert kicad_live._pcbnew_sockets() == [f"ipc://{sockets / 'api.sock'}"]


def test_no_socket_at_all_stays_empty(sockets, monkeypatch):
    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: ["4242"])
    assert kicad_live._pcbnew_sockets() == []


@pytest.fixture
def no_ipc(monkeypatch):
    """The IPC door shut, so the tests below exercise the menu door.

    ``kipy`` is not installed in this venv on purpose (it pins ``protobuf<6``
    against the Gemini SDK's ``>=6.33``), so the import inside
    ``_run_action_3d`` already fails here. Pinning it anyway keeps the test
    honest on a machine where someone installed it.
    """
    monkeypatch.setattr(kicad_live, "_run_action_3d", lambda deadline: False)


def test_three_d_refuses_off_macos(monkeypatch, no_ipc):
    monkeypatch.setattr(kicad_live.sys, "platform", "linux")
    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: ["1"])
    # Off macOS the menu door does not exist, so the refusal names the door
    # that does -- the API server -- rather than saying "unsupported", which
    # reads as "this never works anywhere".
    with pytest.raises(kicad_live.BridgeError, match="only be opened on macOS"):
        kicad_live.show_3d()
    with pytest.raises(kicad_live.BridgeError, match="API server"):
        kicad_live.show_3d()


def test_three_d_refuses_with_no_board_editor(monkeypatch, no_ipc):
    monkeypatch.setattr(kicad_live.sys, "platform", "darwin")
    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: [])
    with pytest.raises(kicad_live.BridgeError, match="no board editor"):
        kicad_live.show_3d()


def test_three_d_opens_the_named_board_before_asking_for_the_viewer(
    monkeypatch, tmp_path
):
    """The whole point of naming a board: the viewer is a window of the board
    editor, so it can only ever show what that editor has open. Asked for a
    board, the bridge opens it *first* and clicks second."""
    pcb = tmp_path / "demo.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    opened: list[tuple[Path, str]] = []
    monkeypatch.setattr(kicad_live.sys, "platform", "darwin")
    monkeypatch.setattr(
        kicad_live, "open_in_app", lambda path, app: opened.append((path, app))
    )
    monkeypatch.setattr(kicad_live, "_run_action_3d", lambda deadline: True)
    assert kicad_live.show_3d(pcb) == "IPC"
    assert opened == [(pcb, kicad_live.PCBNEW_APP)]


def test_three_d_without_a_board_shows_whatever_is_open(monkeypatch):
    """The post-routing rider passes no board: that editor is already on this
    run's board and already carries the copper pushed into it, so reopening
    the file underneath it would be the wrong move."""
    monkeypatch.setattr(kicad_live.sys, "platform", "darwin")
    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: ["7"])
    monkeypatch.setattr(
        kicad_live,
        "open_in_app",
        lambda path, app: pytest.fail("nothing may be reopened here"),
    )
    monkeypatch.setattr(kicad_live, "_run_action_3d", lambda deadline: True)
    assert kicad_live.show_3d() == "IPC"


def test_three_d_names_a_missing_board_rather_than_opening_nothing(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(kicad_live.sys, "platform", "darwin")
    with pytest.raises(kicad_live.BridgeError, match="no such file"):
        kicad_live.show_3d(tmp_path / "gone.kicad_pcb")


def test_the_bridge_has_no_openscad_stage_left(monkeypatch, tmp_path):
    """OpenSCAD left the product path on 2026-09-08 with the v1 emitter. The
    ``case`` stage went with it: a STEP assembly is handed to the OS, and
    nothing here launches a CAD GUI."""
    assert "case" not in steps._BRIDGE_STAGE
    assert not hasattr(kicad_live, "show_case")
    assert not hasattr(kicad_live, "OPENSCAD_BIN")
    assert "case" not in kicad_live._project_files(tmp_path / "b.kicad_pcb")


def test_the_viewer_rides_on_routing_and_nothing_else():
    # Opened before routing it would show a board with no copper on it.
    assert steps._FOLLOW_STAGE == {"routing": "3d"}
    assert steps._BRIDGE_STAGE["route"] == "routing"


def test_follow_is_silent_when_no_bridge_is_installed(monkeypatch, tmp_path):
    # No .venv-kicad means bridge_command returns None; the follow-up must
    # not raise into the step that already succeeded.
    monkeypatch.setattr(steps, "bridge_command", lambda pcb, stage: None)

    class FakeSession:
        def path(self, suffix):
            return tmp_path / f"board{suffix}"

    steps._follow(FakeSession(), "routing")  # must not raise


def test_the_viewer_follows_a_bridge_that_outlived_the_grace_period(
    monkeypatch, tmp_path
):
    """The slow path is the real one: KiCad takes longer than the grace.

    ``_show`` returns "shown" as soon as the bridge outlives BRIDGE_GRACE_S
    and hands it to ``_watch_bridge``. Running the follow-up only on the fast
    branch meant the 3D viewer opened for a toy board and never for a board
    big enough to take KiCad more than twelve seconds -- which is every board
    the routing stage actually produces.
    """
    spawned: list[str] = []
    monkeypatch.setattr(
        steps, "bridge_command", lambda pcb, stage: ["/bin/true", stage]
    )

    class FakePopen:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    class FakeSession:
        shown_detail = None

        def path(self, suffix):
            return tmp_path / f"board{suffix}"

    def record(argv, **kw):
        spawned.append(argv[1])
        return FakePopen()

    monkeypatch.setattr(steps.subprocess, "Popen", record)
    steps._watch_bridge(FakeSession(), "routing", FakePopen())
    assert spawned == ["3d"]


def test_a_bridge_that_failed_slowly_opens_no_viewer(monkeypatch, tmp_path):
    """A stage that never reached KiCad has nothing to show in 3D."""
    spawned: list[str] = []
    monkeypatch.setattr(
        steps, "bridge_command", lambda pcb, stage: ["/bin/true", stage]
    )
    monkeypatch.setattr(
        steps.subprocess, "Popen", lambda argv, **kw: spawned.append(argv[1])
    )

    class FailedPopen:
        returncode = 1

        def communicate(self, timeout=None):
            return "", "error: no board editor is running"

    class FakeSession:
        shown_detail = None

        def path(self, suffix):
            return tmp_path / f"board{suffix}"

    session = FakeSession()
    steps._watch_bridge(session, "routing", FailedPopen())
    assert spawned == []
    assert session.shown_detail is not None


def test_the_board_name_check_is_exact_not_a_prefix(monkeypatch):
    """Copper must never land in a different run's board.

    Stems are intent slugs, so `a-3-3v-ldo-board` is a prefix of
    `a-3-3v-ldo-board-with-usb`. Under a prefix match, a session whose board
    was open in pcbnew received another session's tracks.
    """
    seen: list[str] = []

    class FakeBoard:
        def __init__(self, name):
            self.name = name

    class FakeKiCad:
        def __init__(self, socket_path):
            seen.append(socket_path)

        def get_board(self):
            return FakeBoard("/tmp/a-3-3v-ldo-board-with-usb.placed.kicad_pcb")

    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: ["7"])
    monkeypatch.setattr(kicad_live, "_pcbnew_sockets", lambda: ["ipc:///tmp/x.sock"])
    monkeypatch.setitem(sys.modules, "kipy", type(sys)("kipy"))
    sys.modules["kipy"].KiCad = FakeKiCad

    with pytest.raises(kicad_live.BridgeError) as caught:
        kicad_live.connect_board(
            "a-3-3v-ldo-board", timeout_s=0.6, socket_grace_s=0.0
        )
    assert "a-3-3v-ldo-board-with-usb" in str(caught.value)


def test_the_placed_board_the_placement_stage_opened_is_accepted(monkeypatch):
    class FakeBoard:
        name = "/tmp/a-3-3v-ldo-board.placed.kicad_pcb"

    class FakeKiCad:
        def __init__(self, socket_path):
            pass

        def get_board(self):
            return FakeBoard()

    monkeypatch.setattr(kicad_live, "_pcbnew_pids", lambda: ["7"])
    monkeypatch.setattr(kicad_live, "_pcbnew_sockets", lambda: ["ipc:///tmp/x.sock"])
    monkeypatch.setitem(sys.modules, "kipy", type(sys)("kipy"))
    sys.modules["kipy"].KiCad = FakeKiCad

    _kicad, board = kicad_live.connect_board(
        "a-3-3v-ldo-board", timeout_s=2.0, socket_grace_s=0.0
    )
    assert board.name.endswith("a-3-3v-ldo-board.placed.kicad_pcb")

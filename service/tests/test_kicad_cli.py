"""``service/kicad_cli.py``: finding the binary and exporting a 3D model.

The exporter is driven by a fake ``kicad-cli`` -- a shell script that parses
``-o`` and does what ``FAKE_MODE`` says -- so every failure path runs without
KiCad. One test is gated on the real binary, the ngspice convention:
it skips without one, so a green run does **not** by itself mean a real export
was exercised.
"""

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from service import kicad_cli

FIXTURE = Path(__file__).resolve().parents[2] / "engine/tests/fixtures/ref.kicad_pcb"

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake kicad-cli is a POSIX shell script"
)

FAKE_SCRIPT = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
case "$FAKE_MODE" in
  ok) printf 'glTF-or-STEP' > "$out" ;;
  empty) : > "$out" ;;
  fail) echo "Error: board outline is malformed" >&2; exit 3 ;;
  hang) sleep 5 ;;
esac
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "kicad-cli"
    script.write_text(FAKE_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_MODE", "ok")
    return script, log


# ---------------------------------------------------------------- finder


def test_env_var_wins_and_a_missing_env_var_target_is_none(tmp_path):
    binary = tmp_path / "kicad-cli"
    binary.write_text("")
    assert kicad_cli.find_kicad_cli({"KICAD_CLI": str(binary)}) == str(binary)
    # Explicitly configured and wrong: not silently another binary.
    assert kicad_cli.find_kicad_cli({"KICAD_CLI": str(tmp_path / "nope")}) is None


def test_path_is_searched_when_nothing_is_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/bin/kicad-cli")
    assert kicad_cli.find_kicad_cli({}) == "/opt/bin/kicad-cli"


def test_fixed_locations_and_windows_glob_are_tried_in_order(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(kicad_cli, "_FIXED_CANDIDATES", ("/nowhere/kicad-cli",))
    monkeypatch.setattr(
        kicad_cli.glob,
        "glob",
        lambda pattern: [
            "C:/Program Files/KiCad/8.0/bin/kicad-cli.exe",
            "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe",
        ],
    )
    newest = "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe"
    assert kicad_cli.find_kicad_cli({}) == newest
    monkeypatch.setattr(kicad_cli.glob, "glob", lambda pattern: [])
    assert kicad_cli.find_kicad_cli({}) is None


# ---------------------------------------------------------------- exporter


def test_missing_binary_is_a_warning_with_no_files(tmp_path):
    report = kicad_cli.export_models(
        FIXTURE, tmp_path / "board", environ={"KICAD_CLI": str(tmp_path / "nope")}
    )
    assert report.files == {}
    assert len(report.warnings) == 1
    assert "KICAD_CLI" in report.warnings[0] and "not a file" in report.warnings[0]


def test_no_binary_anywhere_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(kicad_cli, "_FIXED_CANDIDATES", ())
    monkeypatch.setattr(kicad_cli.glob, "glob", lambda pattern: [])
    report = kicad_cli.export_models(FIXTURE, tmp_path / "board", environ={})
    assert report.files == {}
    assert report.warnings == [
        "no 3D model exported: kicad-cli not found (install KiCad or set KICAD_CLI)"
    ]


@pytest.fixture(autouse=True)
def _out_dir(tmp_path):
    (tmp_path / "out").mkdir(exist_ok=True)


@posix_only
def test_fake_export_records_both_files_and_the_argv(fake, tmp_path):
    script, log = fake
    report = kicad_cli.export_models(
        FIXTURE, tmp_path / "out" / "board", binary=str(script)
    )
    assert report.warnings == []
    assert set(report.files) == {"model_glb", "model_step"}
    assert Path(report.files["model_glb"]).name == "board.glb"
    assert Path(report.files["model_step"]).name == "board.step"
    for path in report.files.values():
        assert Path(path).stat().st_size > 0
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("pcb export glb ")
    assert lines[1].startswith("pcb export step ")
    for line in lines:
        assert str(FIXTURE) in line and "--force" in line and "--include-tracks" in line


@posix_only
def test_nonzero_exit_names_the_code_and_the_stderr_tail(fake, tmp_path, monkeypatch):
    script, _ = fake
    monkeypatch.setenv("FAKE_MODE", "fail")
    report = kicad_cli.export_models(
        FIXTURE, tmp_path / "out" / "board", binary=str(script)
    )
    assert report.files == {}
    assert len(report.warnings) == 2
    for fmt, warning in zip(("glb", "step"), report.warnings, strict=True):
        assert warning.startswith(f"{fmt} export failed: kicad-cli exited 3")
        assert "board outline is malformed" in warning


@posix_only
def test_an_empty_output_is_not_a_success(fake, tmp_path, monkeypatch):
    script, _ = fake
    monkeypatch.setenv("FAKE_MODE", "empty")
    report = kicad_cli.export_models(
        FIXTURE, tmp_path / "out" / "board", binary=str(script)
    )
    assert report.files == {}
    assert all("wrote no file" in w and "exit 0" in w for w in report.warnings)
    assert (tmp_path / "out" / "board.glb").stat().st_size == 0


@posix_only
def test_a_hang_is_cut_off_by_the_timeout(fake, tmp_path, monkeypatch):
    script, _ = fake
    monkeypatch.setenv("FAKE_MODE", "hang")
    report = kicad_cli.export_models(
        FIXTURE, tmp_path / "out" / "board", binary=str(script), timeout_s=0.3
    )
    assert report.files == {}
    assert all("timed out after 0.3 s" in w for w in report.warnings)


@posix_only
def test_a_binary_that_cannot_start_is_a_warning(tmp_path):
    not_executable = tmp_path / "kicad-cli"
    not_executable.write_text("not a program")
    report = kicad_cli.export_models(
        FIXTURE, tmp_path / "out" / "board", binary=str(not_executable)
    )
    assert report.files == {}
    assert all("could not start kicad-cli" in w for w in report.warnings)


# ---------------------------------------------------------------- real binary

REAL = kicad_cli.find_kicad_cli(
    {k: v for k, v in os.environ.items() if k != kicad_cli.ENV_VAR}
)


@pytest.mark.skipif(REAL is None, reason="kicad-cli not installed")
def test_real_kicad_cli_exports_the_fixture_board(tmp_path):
    # kicad-cli writes a ``.kicad_prl`` beside whatever board it opens, so the
    # fixture is copied out rather than exported in place.
    board = tmp_path / "ref.kicad_pcb"
    shutil.copyfile(FIXTURE, board)
    report = kicad_cli.export_models(board, tmp_path / "out" / "ref", binary=REAL)
    assert report.warnings == [], report.warnings
    glb = Path(report.files["model_glb"])
    step = Path(report.files["model_step"])
    assert glb.read_bytes()[:4] == b"glTF"
    assert step.read_bytes().startswith(b"ISO-10303-21")

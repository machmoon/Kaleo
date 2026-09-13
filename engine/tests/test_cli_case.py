"""The ``silkscreen case`` subcommand and the generate command's --case flags.

Everything here is offline: the ``--no-model`` path is deterministic by
contract (no API call at all) and the generate-command flag tests monkeypatch
``generate_pcb`` so no model or key is ever touched.

The build123d kernel is the only enclosure engine (docs/ai-cad-plan.md v3,
2026-09-08), so the tests that actually build gate on it the ngspice way, and
one test pins the refusal that replaced the OpenSCAD fallback.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from silkscreen import cli
from silkscreen.enclosure.cad import kernel_available

needs_build123d = pytest.mark.skipif(
    not kernel_available(), reason="build123d (the 'cad' extra) is not installed"
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ref.kicad_pcb"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="board fixture not present"
)

# The raw fixture has no Edge.Cuts outline (test_enclosure_geometry.py proves
# board_envelope refuses it), so the case tests draw one the same way
# set_board_outline does: four gr_lines closing a rectangle.
OUTLINE_X0, OUTLINE_Y0, OUTLINE_X1, OUTLINE_Y1 = -4.0, -7.0, 15.0, 19.0


def _outlined_fixture(tmp_path: Path) -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    corners = [
        (OUTLINE_X0, OUTLINE_Y0),
        (OUTLINE_X1, OUTLINE_Y0),
        (OUTLINE_X1, OUTLINE_Y1),
        (OUTLINE_X0, OUTLINE_Y1),
    ]
    lines = []
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        lines.append(
            f'  (gr_line (start {sx} {sy}) (end {ex} {ey}) '
            f'(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'
        )
    body = text.rstrip()
    assert body.endswith(")")
    out = tmp_path / "outlined.kicad_pcb"
    out.write_text(body[:-1] + "\n" + "\n".join(lines) + "\n)\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------- case


@needs_build123d
def test_no_model_writes_a_step_assembly_and_its_stls(tmp_path, capsys):
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.step"

    code = cli.main(["case", str(board), "-o", str(out), "--no-model"])

    assert code == 0
    # ISO 10303-21 is the STEP wrapper; a truncated export would not have it.
    text = out.read_text(encoding="utf-8")
    assert text.startswith("ISO-10303-21;")
    assert "END-ISO-10303-21;" in text
    assert (tmp_path / "case-base.stl").exists()
    assert (tmp_path / "case-lid.stl").exists()
    captured = capsys.readouterr()
    assert str(out) in captured.out
    # The kernel report is the receipt, one signed margin per clause.
    assert "Kernel report:" in captured.out
    assert "engine: kernel (build123d)" in captured.out


@needs_build123d
def test_no_model_needs_no_api_key(tmp_path, monkeypatch):
    """The offline path must never construct a model, key or no key."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def boom(*a, **kw):  # pragma: no cover - only fires on regression
        raise AssertionError("--no-model constructed a model")

    monkeypatch.setattr(cli, "GeminiModel", boom)
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.step"
    assert cli.main(["case", str(board), "-o", str(out), "--no-model"]) == 0
    assert out.exists()


def test_a_board_without_an_outline_is_refused(tmp_path, capsys):
    out = tmp_path / "case.step"
    code = cli.main(["case", str(FIXTURE), "-o", str(out), "--no-model"])
    assert code == 1
    assert not out.exists()
    assert "Edge.Cuts" in capsys.readouterr().err


def test_without_the_kernel_the_case_command_refuses_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    """The decision this file's deletions came from: a silent degrade to a
    worse case is the one unacceptable outcome, so with no ``cad`` extra the
    command refuses in words naming what to install and writes nothing."""
    import silkscreen.enclosure.cad as cad_mod

    monkeypatch.setattr(cad_mod, "kernel_available", lambda: False)
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.step"

    code = cli.main(["case", str(board), "-o", str(out), "--no-model"])

    assert code == 2
    err = capsys.readouterr().err
    assert "build123d" in err
    assert 'pip install -e ".[cad]"' in err
    assert not out.exists()
    assert not list(tmp_path.glob("case*"))


def test_case_subcommand_threads_the_rigorous_flag(tmp_path, monkeypatch):
    """``silkscreen case --rigorous`` reaches propose_enclosure; the default
    stays fast. The CLI imports propose_enclosure at call time, so patching
    the module attribute intercepts it."""
    seen = _install_kernel_case(monkeypatch, report=_kernel_report())
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.step"

    assert cli.main(["case", str(board), "-o", str(out), "--rigorous"]) == 0
    assert seen["rigorous"] is True
    assert cli.main(["case", str(board), "-o", str(out)]) == 0
    assert seen["rigorous"] is False


# ------------------------------------------------- generate command flags


@needs_build123d
def test_board_only_case_into_a_missing_directory_writes_the_board(
    tmp_path, monkeypatch, capsys
):
    """Regression: ``--board-only --case`` with an output directory that does
    not exist yet must complete and write the board. The case write used to
    run outside the stage's try, before ``_finish``, and its
    FileNotFoundError killed the whole run."""
    import json

    from test_agents import _scripted_pipeline_model
    from test_enclosure_agent import GOOD_ENCLOSURE

    monkeypatch.setenv("SILKSCREEN_ENGINE", "sdk")
    model = _scripted_pipeline_model()
    model.by_marker["ENCLOSURE-SPEC v1"] = json.dumps(GOOD_ENCLOSURE)
    monkeypatch.setattr(cli, "GeminiModel", lambda name: model)

    out = tmp_path / "does" / "not" / "exist" / "board.kicad_pcb"
    assert not out.parent.exists()
    code = cli.main(
        ["a 3.3V motor driver board", "-o", str(out), "--board-only", "--case"]
    )

    assert code == 0
    assert out.exists()
    # --board-only promises only the routed board; the receipt still prints.
    assert not list(out.parent.glob("enclosure*"))
    captured = capsys.readouterr()
    assert "Kernel report:" in captured.out
    assert str(out) in captured.out


def _fake_result(enclosure=None, sourcing=None, review=None):
    from silkscreen.agents.review import ReviewReport, ReviewStatus

    return SimpleNamespace(
        summary=lambda: "1 part board",
        board=SimpleNamespace(parts=[], warnings=[]),
        findings=[],
        # The CLI reads this to decide whether an empty ``findings`` means the
        # critic found nothing or never delivered a verdict, so the stub has
        # to carry it too; these tests are about --case and --bom, so the
        # review here is a real one that found nothing.
        review=review or ReviewReport(status=ReviewStatus.OK),
        route=None,
        artifacts=[],
        project_path=None,
        enclosure=enclosure,
        sourcing=sourcing,
        # Every field the CLI reads has to be here, or the stub passes while
        # the real result would not. These two are the newest: a datasheet
        # that could not be read (printed above the review, because it
        # changes what the review is worth) and the plan the run designed
        # against.
        unread_datasheets=[],
        plan=None,
    )


@pytest.fixture()
def captured_generate(monkeypatch):
    seen = {}

    def fake_generate_pcb(model, intent, **kw):
        seen.update(kw)
        return _fake_result()

    monkeypatch.setattr(cli, "generate_pcb", fake_generate_pcb)
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())
    return seen


def test_case_flag_opts_in_to_the_enclosure_kwargs(
    tmp_path, captured_generate, capsys
):
    out = tmp_path / "board.kicad_pcb"
    code = cli.main(
        ["an ldo board", "-o", str(out), "--case", "--case-style", "usb left"]
    )
    assert code == 0
    assert captured_generate["enclosure"] is True
    assert captured_generate["enclosure_style"] == "usb left"
    # Fast is the default: rigor is opt-in via --rigorous.
    assert captured_generate["enclosure_rigorous"] is False
    # A run whose stage failed says so, and still exits cleanly.
    assert "without one" in capsys.readouterr().err


def test_rigorous_flag_opts_in_to_the_strict_loop(tmp_path, captured_generate):
    out = tmp_path / "board.kicad_pcb"
    code = cli.main(["an ldo board", "-o", str(out), "--case", "--rigorous"])
    assert code == 0
    assert captured_generate["enclosure"] is True
    assert captured_generate["enclosure_rigorous"] is True


def test_without_case_the_kwargs_are_absent(tmp_path, captured_generate):
    """The default call must stay byte-for-byte what it always was."""
    out = tmp_path / "board.kicad_pcb"
    assert cli.main(["an ldo board", "-o", str(out)]) == 0
    assert "enclosure" not in captured_generate
    assert "enclosure_style" not in captured_generate
    assert "enclosure_rigorous" not in captured_generate
    assert "sourcing" not in captured_generate


def test_case_success_prints_the_kernel_receipt(tmp_path, monkeypatch, capsys):
    enclosure = SimpleNamespace(
        repair_rounds=2, step_text="ISO-10303-21;\n",
        kernel=_kernel_report(failing=("lid_mates",)),
    )
    monkeypatch.setattr(
        cli, "generate_pcb", lambda model, intent, **kw: _fake_result(enclosure)
    )
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())

    code = cli.main(["an ldo", "-o", str(tmp_path / "b.kicad_pcb"), "--case"])

    assert code == 0
    captured = capsys.readouterr()
    assert "Kernel report:" in captured.out
    assert "PASS board_clash margin=+1.000 mm" in captured.out
    assert "FAIL lid_mates margin=-0.120 mm" in captured.out, (
        "a violated clause must keep its sign"
    )
    assert "repair rounds: 2" in captured.err


def test_the_cli_prints_that_the_review_produced_no_verdict(
    tmp_path, monkeypatch, capsys
):
    """The CLI used to print nothing at all for an empty finding list.

    Silence there is indistinguishable from a board with nothing to flag,
    which is precisely what a critic that answered gibberish must not look
    like on a terminal.
    """
    from silkscreen.agents.review import ReviewReport, ReviewStatus

    failed = ReviewReport(status=ReviewStatus.FAILED, detail="not JSON")
    monkeypatch.setattr(
        cli,
        "generate_pcb",
        lambda model, intent, **kw: _fake_result(review=failed),
    )
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())

    assert cli.main(["an ldo", "-o", str(tmp_path / "b.kicad_pcb")]) == 0
    out = capsys.readouterr().out
    assert "Review: the review failed to produce a readable answer" in out
    assert "nothing is known about this board" in out


def test_the_cli_says_nothing_extra_for_a_review_that_found_nothing(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        cli, "generate_pcb", lambda model, intent, **kw: _fake_result()
    )
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())
    assert cli.main(["an ldo", "-o", str(tmp_path / "b.kicad_pcb")]) == 0
    assert "Review:" not in capsys.readouterr().out


# ------------------------------------------------------------ kernel path


def _kernel_report(*, failing=()):
    from silkscreen.enclosure.kernel import Clause, KernelReport

    return KernelReport(
        clauses=(
            Clause("board_clash", "board_clash" not in failing, 1_000_000,
                   "cavity clears the board by 1.000 mm"),
            Clause("lid_mates", "lid_mates" not in failing, -120_000,
                   "lip gap 0.120 mm over slack"),
        ),
        warnings=("critic: nothing to report",),
    )


def _install_kernel_case(monkeypatch, *, report, render=None):
    """propose_enclosure answers with a kernel-built proposal; the exporters
    the CLI imports lazily from cad/snapshot are replaced at their modules."""
    import silkscreen.agents.enclosure as agent_enclosure
    import silkscreen.enclosure.cad as cad
    import silkscreen.enclosure.snapshot as snapshot
    from silkscreen.agents.enclosure import EnclosureProposal
    from silkscreen.enclosure.cad import ExportPaths
    from silkscreen.enclosure.ir import parse_enclosure_spec

    fake_model = object()
    seen = {}

    def fake_propose(model, envelope, *, style_hint="", rigorous=False, **kw):
        seen["rigorous"] = rigorous
        spec = parse_enclosure_spec({})
        return EnclosureProposal(
            spec=spec, repair_rounds=0,
            model=fake_model, kernel=report, brief="brief",
        )

    def fake_export(model, directory, stem="enclosure"):
        assert model is fake_model
        directory = Path(directory)
        seen["export"] = (directory, stem)
        paths = ExportPaths(
            step=directory / f"{stem}.step",
            base_stl=directory / f"{stem}-base.stl",
            lid_stl=directory / f"{stem}-lid.stl",
        )
        for path, text in (
            (paths.step, "ISO-10303-21;\n"),
            (paths.base_stl, "solid base\nendsolid base\n"),
            (paths.lid_stl, "solid lid\nendsolid lid\n"),
        ):
            path.write_text(text)
        return paths

    def fake_render(model, out_dir, **kw):
        png = Path(out_dir) / "case-iso.png"
        png.write_bytes(b"\x89PNG")
        return (png,)

    monkeypatch.setattr(agent_enclosure, "propose_enclosure", fake_propose)
    monkeypatch.setattr(cad, "export_model", fake_export)
    monkeypatch.setattr(snapshot, "render_packet", render or fake_render)
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())
    return seen


def test_kernel_case_writes_step_and_stls_beside_the_output(
    tmp_path, monkeypatch, capsys
):
    seen = _install_kernel_case(monkeypatch, report=_kernel_report())
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.step"

    code = cli.main(["case", str(board), "-o", str(out)])

    assert code == 0
    assert seen["export"] == (tmp_path, "case")
    for name in ("case.step", "case-base.stl", "case-lid.stl", "case-iso.png"):
        assert (tmp_path / name).exists(), name
    # No .scad anywhere: the OpenSCAD preview wrapper is gone.
    assert not list(tmp_path.glob("*.scad"))
    captured = capsys.readouterr()
    assert "engine: kernel" in captured.out
    for name in ("case.step", "case-base.stl", "case-lid.stl", "case-iso.png"):
        assert str(tmp_path / name) in captured.out
    # The kernel report text, one signed-margin line per clause, is printed.
    assert "Kernel report:" in captured.out
    assert "PASS board_clash margin=+1.000 mm" in captured.out
    assert "PASS lid_mates margin=-0.120 mm" in captured.out
    assert "WARN critic: nothing to report" in captured.out


def test_failing_kernel_report_is_an_error_only_in_rigorous_mode(
    tmp_path, monkeypatch, capsys
):
    report = _kernel_report(failing=("lid_mates",))
    _install_kernel_case(monkeypatch, report=report)
    board = _outlined_fixture(tmp_path)
    out = tmp_path / "case.step"

    code = cli.main(["case", str(board), "-o", str(out), "--rigorous"])
    assert code == 1
    captured = capsys.readouterr()
    assert "FAIL lid_mates" in captured.out
    assert "kernel clauses failed: lid_mates" in captured.err
    # The files were still written first: the report is about them.
    assert (tmp_path / "case.step").exists()

    code = cli.main(["case", str(board), "-o", str(out)])
    assert code == 0
    captured = capsys.readouterr()
    assert "FAIL lid_mates" in captured.out
    assert "--rigorous makes this an error" in captured.err


def test_kernel_case_snapshot_failure_is_a_note_not_an_error(
    tmp_path, monkeypatch, capsys
):
    def broken(model, out_dir, **kw):
        raise NotImplementedError("no renderer")

    _install_kernel_case(monkeypatch, report=_kernel_report(), render=broken)
    board = _outlined_fixture(tmp_path)
    code = cli.main(["case", str(board), "-o", str(tmp_path / "case.step")])
    assert code == 0
    captured = capsys.readouterr()
    assert "snapshots not rendered" in captured.err
    assert "engine: kernel" in captured.out


def test_generate_tail_prints_the_kernel_report(tmp_path, monkeypatch, capsys):
    enclosure = SimpleNamespace(
        repair_rounds=0, step_text="ISO-10303-21;\n", kernel=_kernel_report(),
    )
    monkeypatch.setattr(
        cli, "generate_pcb", lambda model, intent, **kw: _fake_result(enclosure)
    )
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())
    code = cli.main(["an ldo", "-o", str(tmp_path / "b.kicad_pcb"), "--case"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Kernel report:" in out
    assert "PASS board_clash margin=+1.000 mm" in out



# ------------------------------------------------------------- --bom flag


def test_bom_flag_opts_in_to_sourcing_only(tmp_path, captured_generate, capsys):
    """``--bom`` turns the sourcing switch on and touches nothing else."""
    out = tmp_path / "board.kicad_pcb"
    code = cli.main(["an ldo board", "-o", str(out), "--bom"])
    assert code == 0
    assert captured_generate["sourcing"] is True
    assert "enclosure" not in captured_generate
    # A pipeline that answered without the stage says so, and still exits 0.
    assert "sourcing did not run" in capsys.readouterr().err


def test_bom_and_case_flags_compose(tmp_path, captured_generate):
    out = tmp_path / "board.kicad_pcb"
    assert cli.main(["an ldo board", "-o", str(out), "--bom", "--case"]) == 0
    assert captured_generate["sourcing"] is True
    assert captured_generate["enclosure"] is True


def test_bom_success_prints_the_counts_line(tmp_path, monkeypatch, capsys):
    """The receipt's vocabulary is the honest one: proposed, verified,
    unresolved -- never "found" or "confirmed" for a part number nobody
    checked."""
    from silkscreen.sourcing import SourcingEntry, SourcingResult

    sourcing = SourcingResult(
        [
            SourcingEntry("U1", "AMS1117-3.3", "device", "SOT-223-3_TabPin2",
                          manufacturer="AMS", mpn="AMS1117-3.3",
                          mpn_status="proposed",
                          datasheet_url="https://vendor.example/a.pdf",
                          datasheet_status="verified"),
            SourcingEntry("C1", "22uF", "capacitor", "C_1206",
                          mpn="CL31A226KAHNNNE", mpn_status="proposed"),
            SourcingEntry("R1", "10k", "resistor", "R_0603"),
        ],
        warnings=["R1: no part number proposed"],
    )
    monkeypatch.setattr(
        cli, "generate_pcb",
        lambda model, intent, **kw: _fake_result(sourcing=sourcing),
    )
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())

    out = tmp_path / "board.kicad_pcb"
    assert cli.main(["an ldo board", "-o", str(out), "--bom"]) == 0
    captured = capsys.readouterr()
    assert (
        "BOM: 3 parts, 2 part number(s) proposed, 1 datasheet(s) verified, "
        "1 unresolved"
    ) in captured.out
    assert "note: R1: no part number proposed" in captured.err
    assert "bom.csv not written" not in captured.err


def test_board_only_bom_says_the_file_was_not_written(tmp_path, monkeypatch, capsys):
    """``--board-only --bom`` still pays for sourcing (the counts print) but
    writes no bom.csv; the receipt says so rather than leaving the buyer to
    look for a file that was never promised."""
    from silkscreen.sourcing import SourcingEntry, SourcingResult

    sourcing = SourcingResult([SourcingEntry("R1", "10k", "resistor", "R_0603")])
    monkeypatch.setattr(
        cli, "generate_pcb",
        lambda model, intent, **kw: _fake_result(sourcing=sourcing),
    )
    monkeypatch.setattr(cli, "GeminiModel", lambda name: object())

    out = tmp_path / "board.kicad_pcb"
    assert cli.main(["an ldo board", "-o", str(out), "--bom", "--board-only"]) == 0
    captured = capsys.readouterr()
    assert "BOM: 1 parts" in captured.out
    assert "bom.csv not written (--board-only)" in captured.err


def test_without_bom_no_counts_line_is_printed(tmp_path, captured_generate, capsys):
    out = tmp_path / "board.kicad_pcb"
    assert cli.main(["an ldo board", "-o", str(out)]) == 0
    captured = capsys.readouterr()
    assert "BOM:" not in captured.out
    assert "BOM:" not in captured.err

"""The board seated in its case: :mod:`silkscreen.enclosure.assembly`.

**Three gates, and a green suite proves none of them ran.** The kernel tests
skip without build123d (``needs_build123d``, the ``test_spice.py`` convention
applied to the ``cad`` extra); the end-to-end assembly tests skip without
``kicad-cli`` on the machine, because the board model is KiCad's own 3D
export and nothing here invents one; and the cross-check skips without
``freecadcmd``. So a passing run on a bare CI machine says nothing about
whether a board was ever seated in a case -- read the skip counts.

Oracle discipline, the ``test_kicad.py`` rule: the seating transform's
expected value is computed here from raw millimetre literals and the frame
map written out by hand, never by calling :func:`~.assembly.seat_offset_mm`'s
own arithmetic; and every geometric claim is a measurement on the B-rep
(volumes, bounding boxes, rays) rather than a re-reading of the code that
made it. The negative controls matter as much as the passes: a clause that
cannot fail is the receipt this package deleted an entire emitter over
(docs/ai-cad-plan.md v3), so ``test_seated_clash_fails_...`` pushes the board
through the floor and insists the report says so, with a negative margin.

The fixture ``fixtures/assembly_board.kicad_pcb`` is a real generated LDO
board (AMS1117-3.3 in SOT-223, three capacitors, one resistor). Its five
footprints name ``${KISYS3DMOD}`` 3D models, which is what makes the KiCad
export carry component solids at all -- ``ref.kicad_pcb`` names
``${KICAD8_3DMODEL_DIR}``, which KiCad 10 does not resolve, and exports as a
bare substrate.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from silkscreen.enclosure import assembly as A
from silkscreen.enclosure import freecad_check as F
from silkscreen.enclosure.board_shape import board_envelope
from silkscreen.enclosure.cad import build_enclosure, kernel_available
from silkscreen.enclosure.errors import (
    AssemblyError,
    BoardModelUnavailable,
    FreeCADUnavailable,
)
from silkscreen.enclosure.ir import parse_enclosure_spec

BOARD = Path(__file__).parent / "fixtures" / "assembly_board.kicad_pcb"


def _scratch_board(tmp: Path) -> Path:
    """A copy of the fixture in a temporary directory.

    kicad-cli writes a ``.kicad_prl`` beside the project it opens, so it is
    never pointed at the checked-in fixture: a test run must not leave a file
    in ``fixtures/``.
    """
    out = tmp / "board.kicad_pcb"
    out.write_bytes(BOARD.read_bytes())
    return out
PACKAGE = Path(A.__file__).resolve().parent.parent  # silkscreen/

needs_build123d = pytest.mark.skipif(
    not kernel_available(), reason="build123d not installed (pip install -e '.[cad]')"
)
needs_kicad_cli = pytest.mark.skipif(
    A.kicad_cli() is None, reason="kicad-cli not installed"
)
needs_freecad = pytest.mark.skipif(
    F.freecad_cmd() is None, reason="freecadcmd not installed"
)


# ------------------------------------------------------- the hard constraint


def test_nothing_in_the_package_imports_freecad():
    """FreeCAD and build123d each carry their own OCCT; two in one process is
    undefined behaviour. FreeCAD is reached only by running ``freecadcmd``
    against a script file, so no module may import it.

    Checked with ``ast`` rather than a text search on purpose: the script
    ``freecad_check`` hands to the subprocess is a *string* containing
    ``import FreeCAD``, and that one is correct -- it runs in FreeCAD's own
    interpreter. A grep could not tell the two apart.
    """
    banned = {"FreeCAD", "FreeCADGui", "Import", "Part", "Mesh", "ImportGui"}
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            if names & banned:
                offenders.append(f"{path}:{node.lineno}: {sorted(names & banned)}")
    assert not offenders, "FreeCAD must only ever be a subprocess:\n" + "\n".join(
        offenders
    )


def test_the_subprocess_script_is_the_documented_headless_api():
    """The script really does drive FreeCAD's own headless API, and prints its
    answer behind a marker (FreeCAD writes its banner to the same stream)."""
    assert "import FreeCAD, Import" in F._SCRIPT
    assert "FreeCAD.newDocument" in F._SCRIPT
    assert "Import.insert" in F._SCRIPT
    assert "ImportGui" not in F._SCRIPT  # the GUI half needs a display


# ------------------------------------------------------------- the refusals


def test_missing_kicad_cli_refuses_and_names_the_command(monkeypatch):
    monkeypatch.setenv(A.KICAD_CLI_ENV, "/nowhere/kicad-cli")
    assert A.kicad_cli() is None
    with pytest.raises(BoardModelUnavailable) as exc:
        A.export_board_step(BOARD, "/nowhere/out.step")
    assert "kicad-cli pcb export step" in str(exc.value)


def test_a_failing_kicad_cli_is_a_refusal_not_an_empty_file(tmp_path):
    fake = tmp_path / "fake-cli"
    fake.write_text("#!/bin/sh\necho 'no such board' >&2\nexit 3\n")
    fake.chmod(0o755)
    with pytest.raises(BoardModelUnavailable) as exc:
        A.export_board_step(BOARD, tmp_path / "out.step", cli=str(fake))
    assert "exit 3" in str(exc.value)
    assert "no such board" in str(exc.value)


def test_a_silent_kicad_cli_is_a_refusal_too(tmp_path):
    """Exit 0 and no file is the quiet zero this package refuses: an agent
    handed an empty board model concludes the case is empty."""
    fake = tmp_path / "quiet-cli"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    with pytest.raises(BoardModelUnavailable) as exc:
        A.export_board_step(BOARD, tmp_path / "out.step", cli=str(fake))
    assert "is empty" in str(exc.value)


def test_missing_freecad_refuses_and_names_the_binary(monkeypatch, tmp_path):
    monkeypatch.setenv(F.FREECAD_CMD_ENV, "/nowhere/freecadcmd")
    assert F.freecad_cmd() is None
    step = tmp_path / "x.step"
    step.write_text("ISO-10303-21;\n")
    with pytest.raises(FreeCADUnavailable) as exc:
        F.crosscheck_step(step)
    assert "freecadcmd" in str(exc.value)


def test_a_freecad_that_prints_nothing_is_a_refusal_not_an_empty_list(tmp_path):
    """An empty list means "read the file, found no solid" -- a different and
    real fact. It must never also mean "could not check"."""
    fake = tmp_path / "quiet-freecad"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    step = tmp_path / "x.step"
    step.write_text("ISO-10303-21;\n")
    with pytest.raises(FreeCADUnavailable) as exc:
        F.crosscheck_step(step, cmd=str(fake))
    assert "printed no" in str(exc.value)


# ------------------------------------------------------------ the transform


class _FakeModel:
    """Only what :func:`seat_offset_mm` reads."""

    def __init__(self, boxes):
        self.board_boxes = boxes


class _FakeEnvelope:
    def __init__(self, x_min_nm, y_max_nm):
        self.x_min_nm = x_min_nm
        self.y_max_nm = y_max_nm


def test_seat_offset_is_the_frame_map_written_out_by_hand():
    """Board outline x -2..14.2, y -2..17.85 mm in KiCad; the emitter put the
    substrate at x0 3.6, y0 3.6, bottom 6.0 mm in the assembled frame.

    kicad-cli negates Y and puts z = 0 at the board's underside
    (``step_pcb_model.cpp``), so the STEP holds that board at x -2..14.2,
    y -17.85..2, z 0..t. Seating it is therefore a translation of
    (3.6 - -2, 3.6 + 17.85, 6.0) = (5.6, 21.45, 6.0), computed here from the
    literals rather than from the module.
    """
    nm = 1_000_000
    model = _FakeModel(
        [("board", "substrate", int(3.6 * nm), int(3.6 * nm), int(6.0 * nm),
          int(19.8 * nm), int(23.45 * nm), int(7.6 * nm))]
    )
    env = _FakeEnvelope(int(-2.0 * nm), int(17.85 * nm))
    dx, dy, dz = A.seat_offset_mm(model, env)
    assert (round(dx, 6), round(dy, 6), round(dz, 6)) == (5.6, 21.45, 6.0)


def test_a_model_with_no_substrate_box_refuses_rather_than_guessing():
    with pytest.raises(AssemblyError):
        A.seat_offset_mm(_FakeModel([]), _FakeEnvelope(0, 0))


def test_clause_names_are_frozen():
    """The clause names are the vocabulary a repair prompt speaks; they are a
    contract, like ``kernel.CLAUSES``."""
    assert A.ASSEMBLY_CLAUSES == (
        "board_imported",
        "board_registration",
        "board_seated",
        "seated_clash",
        "seated_headroom",
        "part_height_claim",
        "cutout_clear_of_board",
    )
    assert tuple(A._EVALUATORS) == A.ASSEMBLY_CLAUSES


# --------------------------------------------------------------- the kernel


@needs_build123d
def test_a_step_that_is_not_a_step_raises(tmp_path):
    junk = tmp_path / "junk.step"
    junk.write_text("this is not a STEP file\n")
    with pytest.raises(AssemblyError):
        A.import_board_step(junk)


@pytest.fixture(scope="module")
def seated(tmp_path_factory):
    """The fixture board's case with the fixture board seated in it."""
    if not kernel_available() or A.kicad_cli() is None:
        pytest.skip("needs build123d and kicad-cli")
    tmp = tmp_path_factory.mktemp("assembly")
    board = _scratch_board(tmp)
    envelope = board_envelope(str(board))
    spec = parse_enclosure_spec({})
    model = build_enclosure(spec, envelope)
    step = A.export_board_step(board, tmp / "board.step")
    return spec, envelope, model, A.build_assembly(model, envelope, step), tmp


@needs_build123d
@needs_kicad_cli
def test_the_board_step_carries_the_component_models(seated):
    """Not a formality: without resolvable 3D models the export is a bare
    substrate and every part clause below would pass vacuously."""
    _spec, _env, _model, asm, _tmp = seated
    labels = {label for label, _solid in asm.components}
    assert "SOT-223" in labels
    assert len(asm.components) == 5
    assert asm.pcb_label.endswith("_PCB")
    assert asm.pcb.volume > 100  # mm³; the substrate, not a chip capacitor


@needs_build123d
@needs_kicad_cli
def test_the_seated_board_lands_where_the_emitter_put_the_keep_out(seated):
    """Measured on the imported B-rep against the emitter's own substrate box
    -- the transform's receipt, independent of the clause that reports it."""
    _spec, _env, model, asm, _tmp = seated
    _ref, _side, x0, y0, z0, x1, y1, _z1 = model.board_boxes[0]
    bb = asm.pcb.bounding_box(optimal=True)
    assert abs(bb.min.X - x0 / 1e6) < 0.01
    assert abs(bb.min.Y - y0 / 1e6) < 0.01
    assert abs(bb.max.X - x1 / 1e6) < 0.01
    assert abs(bb.max.Y - y1 / 1e6) < 0.01
    # Seated on the standoff tops, TurboCase's rule: the underside, not the
    # top surface, meets the emitter's board_bottom.
    assert abs(bb.min.Z - z0 / 1e6) < 0.01


@needs_build123d
@needs_kicad_cli
def test_every_assembly_clause_passes_on_a_generated_board(seated):
    spec, _env, _model, asm, _tmp = seated
    report = A.verify_assembly(asm, spec)
    assert report.passed, report.text()
    assert [c.name for c in report.clauses] == list(A.ASSEMBLY_CLAUSES)
    # Every clause states what it measured; none is a bare boolean.
    for clause in report.clauses:
        assert clause.detail
        assert isinstance(clause.margin_nm, int)


@needs_build123d
@needs_kicad_cli
def test_seated_clash_fails_when_the_board_is_pushed_through_the_floor(seated):
    """The negative control. A clause that cannot fail is not a check."""
    from build123d import Compound, Location

    spec, env, model, asm, _tmp = seated
    drop = Location((0, 0, -3.0))
    sunk = A.Assembly(
        model=model,
        envelope=env,
        board=Compound(children=[s.moved(drop) for _l, s in
                                 [(asm.pcb_label, asm.pcb), *asm.components]]),
        pcb=asm.pcb.moved(drop),
        pcb_label=asm.pcb_label,
        components=tuple((label, s.moved(drop)) for label, s in asm.components),
        offset_mm=asm.offset_mm,
        source=asm.source,
    )
    report = A.verify_assembly(sunk, spec)
    assert "seated_clash" in report.failed
    assert report.margins_nm["seated_clash"] < 0
    assert "board_seated" in report.failed  # it is not on the standoffs either


@needs_build123d
@needs_kicad_cli
def test_part_height_claim_fails_when_a_part_is_taller_than_the_table_said(
    seated,
):
    """The clause this module exists for: the height table under-claims a
    part, the lid is built to the claim, and the real component pokes through
    it. Simulated by shrinking the keep-out box the case was designed around,
    which is what an under-claim *is*."""
    spec, env, model, asm, _tmp = seated
    shrunk = []
    for box in model.board_boxes:
        ref, side, x0, y0, z0, x1, y1, z1 = box
        if side == "top":
            z1 = z0 + (z1 - z0) // 4  # the table claimed a quarter of the height
        shrunk.append((ref, side, x0, y0, z0, x1, y1, z1))
    from dataclasses import replace

    report = A.verify_assembly(
        A.Assembly(
            model=replace(model, board_boxes=tuple(shrunk)),
            envelope=env,
            board=asm.board,
            pcb=asm.pcb,
            pcb_label=asm.pcb_label,
            components=asm.components,
            offset_mm=asm.offset_mm,
            source=asm.source,
        ),
        spec,
    )
    assert "part_height_claim" in report.failed
    assert report.margins_nm["part_height_claim"] < 0


@needs_build123d
@needs_kicad_cli
def test_the_exported_assembly_is_one_labelled_step(seated):
    """Measured on the bytes that leave: ISO 10303-21 is ASCII, so the labels
    the engineer will see in their CAD tool are readable right here."""
    _spec, _env, _model, asm, tmp = seated
    path = A.export_assembly(asm, tmp, "assembly")
    text = path.read_text(encoding="utf-8", errors="replace")
    assert text.startswith("ISO-10303-21;")
    assert path.stat().st_size > 100_000  # a case plus five component solids
    for label in ("base", "lid", "board", "SOT-223"):
        assert f"PRODUCT('{label}'" in text, label


# --------------------------------------------------- the independent reader


@needs_build123d
@needs_kicad_cli
@needs_freecad
def test_freecad_reads_back_the_same_assembly(seated):
    """A second program, a second OCCT build, out of process. The point is not
    that FreeCAD is needed -- it is not, build123d imports and places the
    board on its own -- but that the file we ship measures the same in
    somebody else's kernel as it does in ours."""
    _spec, _env, model, asm, tmp = seated
    path = A.export_assembly(asm, tmp, "crosscheck")
    rows = {row["label"]: row for row in F.crosscheck_step(path)}
    assert "base" in rows and "lid" in rows
    assert rows["base"]["solids"] == 1
    assert abs(rows["base"]["volume_mm3"] - model.base.volume) < 1e-3
    # The board is in the file, at the seated height, not at the origin.
    board_rows = [
        r for label, r in rows.items() if label.endswith("_PCB")
    ]
    assert board_rows, sorted(rows)
    z_min = board_rows[0]["bbox_mm"][2]
    assert abs(z_min - model.board_boxes[0][4] / 1e6) < 0.01


# ------------------------------------------------------------ the CLI seam


@needs_build123d
@needs_kicad_cli
def test_cli_case_assemble_writes_the_assembly(tmp_path):
    """End to end through the shipped entry point, because that is what an
    engineer runs."""
    out = tmp_path / "case.step"
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "silkscreen", "case", str(_scratch_board(tmp_path)),
         "--no-model", "--assemble", "-o", str(out)],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONPATH": str(PACKAGE.parent)},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "case-assembly.step").exists()
    assert (tmp_path / "case-board.step").exists()
    assert "Assembly report:" in done.stdout
    for name in A.ASSEMBLY_CLAUSES:
        assert name in done.stdout


def test_cli_case_assemble_refuses_without_kicad_cli(tmp_path, monkeypatch):
    """The refusal names the command. Runs everywhere: it never gets as far as
    needing a kernel if there is none, and the message is the product."""
    if not kernel_available():
        pytest.skip("needs build123d to reach the assembly step")
    out = tmp_path / "case.step"
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "silkscreen", "case", str(_scratch_board(tmp_path)),
         "--no-model", "--assemble", "-o", str(out)],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONPATH": str(PACKAGE.parent),
             A.KICAD_CLI_ENV: "/nowhere/kicad-cli", "PATH": "/nonexistent"},
        check=False,
    )
    assert done.returncode == 1
    assert "kicad-cli pcb export step" in done.stderr
    assert (tmp_path / "case.step").exists()  # the case is still the product


@needs_build123d
@needs_kicad_cli
def test_a_board_whose_models_are_missing_says_so_rather_than_passing_quietly(
    tmp_path,
):
    """KiCad exports a bare substrate when a footprint's 3D model does not
    resolve (``ref.kicad_pcb`` names ``${KICAD8_3DMODEL_DIR}``, which KiCad 10
    does not have). The part clauses then have nothing real to measure, and
    the report must be readable as that rather than as five parts that fit.
    """
    stripped = tmp_path / "no-models.kicad_pcb"
    stripped.write_text(
        "\n".join(
            line for line in BOARD.read_text(encoding="utf-8").splitlines()
            if '(model "' not in line
        ),
        encoding="utf-8",
    )
    envelope = board_envelope(str(stripped))
    spec = parse_enclosure_spec({})
    model = build_enclosure(spec, envelope)
    step = A.export_board_step(stripped, tmp_path / "bare.step")
    asm = A.build_assembly(model, envelope, step)
    assert asm.components == ()
    assert any("no component solid" in w for w in asm.warnings)
    report = A.verify_assembly(asm, spec)
    assert report.passed, report.text()
    by_name = {c.name: c for c in report.clauses}
    assert by_name["seated_headroom"].detail.startswith("nothing to check")
    assert by_name["part_height_claim"].detail.startswith("nothing to check")
    assert any("no component solid" in w for w in report.warnings)
    # The board itself is still real and still measured.
    assert by_name["board_registration"].passed
    assert by_name["board_seated"].passed

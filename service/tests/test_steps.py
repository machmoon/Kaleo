"""The approval-gated step routes (``service/steps.py``).

Offline: the scripted model from ``test_app`` answers propose and review, the
placer and router are deterministic, the datasheet probe is pinned to a fake
(``steps.PROBE``) so no sourcing thread ever opens a socket, and the bridge is
pointed at nothing so ``shown_in_kicad`` is honestly false. Files are written
into a temp dir so the tests can assert the on-disk layout
``desktop/kicad_live.py`` expects.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.agents.sourcing import SOURCING_MARKER

from service import steps
from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import scripted, url

#: The enclosure agent's prompt marker (the engine suite's convention).
CASE_MARKER = "ENCLOSURE-SPEC v1"
GOOD_ENCLOSURE = {
    "wall_mm": 2.0,
    "clearance_mm": 1.0,
    "corner_radius_mm": 2.0,
    "lid": "friction",
    "cutouts": [],
    "standoffs": True,
    "vents": False,
    "label": "silkscreen",
}


#: The URL the fake probe reports as a real PDF; every other URL is a page.
VERIFIED_URL = "https://example.test/ams1117.pdf"
#: A sourcing answer covering every ref in ``test_app.CIRCUIT`` (U1, C1, C2):
#: one verified datasheet, one that is not a PDF, one honest null.
SOURCING = {
    "parts": [
        {
            "ref": "U1",
            "manufacturer": "Advanced Monolithic Systems",
            "mpn": "AMS1117-3.3",
            "datasheet_url": VERIFIED_URL,
            "note": None,
        },
        {
            "ref": "C1",
            "manufacturer": "Samsung",
            "mpn": "CL21A106KOQNNNE",
            "datasheet_url": "https://example.test/viewer.html",
            "note": "X5R; check the voltage derating",
        },
        {
            "ref": "C2",
            "manufacturer": None,
            "mpn": None,
            "datasheet_url": None,
            "note": None,
        },
    ]
}


def fake_probe(url: str) -> str:
    """Offline stand-in for ``probe_pdf``: one URL is a PDF, the rest are pages."""
    return "verified" if url == VERIFIED_URL else "not_pdf"


def sourced():
    """The shared scripted model plus a sourcing answer."""
    return ScriptedModel(
        by_marker={**scripted().by_marker, SOURCING_MARKER: json.dumps(SOURCING)}
    )


class _Gated:
    """A model that answers the case, and the sourcing, only once ``gate`` is set.

    Every request gets its own model from the factory, so the calls of all
    of them land in one shared ``log``; the gate holds the background work
    open long enough for a test to observe it running.
    """

    def __init__(self, gate: threading.Event, log: list) -> None:
        self.gate = gate
        self.inner = ScriptedModel(
            by_marker={
                **scripted().by_marker,
                CASE_MARKER: json.dumps(GOOD_ENCLOSURE),
                SOURCING_MARKER: json.dumps(SOURCING),
            },
            calls=log,
        )

    def generate(self, prompt, **kwargs):
        if CASE_MARKER in prompt or SOURCING_MARKER in prompt:
            assert self.gate.wait(timeout=10), "the test never opened the gate"
        return self.inner.generate(prompt, **kwargs)


def _case_calls(log: list) -> list[dict]:
    return [c for c in log if CASE_MARKER in c["prompt"]]


def _sourcing_calls(log: list) -> list[dict]:
    return [c for c in log if SOURCING_MARKER in c["prompt"]]


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "missing-python"))
    # Pinned to nothing so the order step never runs a real KiCad here; the
    # export itself is covered by test_kicad_cli.py.
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "missing-kicad-cli"))
    # The background sourcing probes every datasheet URL the model names;
    # pinned here so no test can reach the network, whatever the model says.
    monkeypatch.setattr(steps, "PROBE", fake_probe)
    steps.reset_sessions()
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None
    steps.reset_sessions()


def post(srv, path, payload):
    req = urllib.request.Request(
        url(srv, path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(srv, path):
    try:
        with urllib.request.urlopen(url(srv, path)) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _start(srv, **extra):
    status, body = post(
        srv, "/steps", {"intent": "build me a toy car", "time_limit_s": 5, **extra}
    )
    assert status == 200, body
    return body


def test_start_proposes_and_writes_the_schematic(server, tmp_path):
    body = _start(server)
    assert body["step"] == "propose" and body["stage"] == "proposed"
    assert body["parts"] > 0 and body["schematic"]["nets"]
    assert body["next"] == ["place"]
    assert body["shown_in_kicad"] is False
    sch = tmp_path / "steps" / body["session"] / "build-me-a-toy-car.kicad_sch"
    assert sch.is_file() and body["files"]["schematic"] == str(sch)
    assert (sch.parent / "build-me-a-toy-car.kicad_pro").is_file()
    assert any(
        e["event"] == "stage.done" and e["stage"] == "propose" for e in body["events"]
    )


def test_steps_run_in_order_and_write_the_bridge_layout(server, tmp_path):
    sid = _start(server)["session"]
    d = tmp_path / "steps" / sid

    status, placed = post(server, f"/steps/{sid}/place", {})
    assert status == 200, placed
    assert placed["stage"] == "placed" and placed["placements"]["parts"]
    assert (d / "build-me-a-toy-car.placed.kicad_pcb").is_file()
    assert "(segment" not in placed["kicad_pcb"]
    # The case and the sourcing need only the placed board, so both open here.
    assert placed["next"] == ["route", "case", "sourcing"]

    status, routed = post(server, f"/steps/{sid}/route", {})
    assert status == 200, routed
    assert routed["stage"] == "routed"
    assert routed["routing"]["tracks"] > 0
    assert "(segment" in routed["kicad_pcb"]
    assert (d / "build-me-a-toy-car.kicad_pcb").is_file()
    # The case leads once copper exists: the desktop draws next[0] as the
    # primary button, and review is a choice beside it, not the default.
    assert routed["next"] == ["case", "review", "sourcing", "order"]

    status, reviewed = post(server, f"/steps/{sid}/review", {})
    assert status == 200, reviewed
    assert isinstance(reviewed["findings"], list)
    # The critic proposed them; no rule measured the board on this route, and
    # the wire says which rather than leaving the panel to assume.
    assert all(f["origin"] == "suggested" for f in reviewed["findings"])
    assert all(
        f["rule"] is None and f["evidence"] is None for f in reviewed["findings"]
    )

    status, ordered = post(server, f"/steps/{sid}/order", {})
    assert status == 200, ordered
    assert "orderable" in ordered["order"]

    status, state = get(server, f"/steps/{sid}")
    assert status == 200
    assert state["done"] == ["order", "place", "review", "route"]
    assert state["next"] == ["case", "sourcing"]


def test_a_step_out_of_order_is_a_409_not_a_guess(server):
    sid = _start(server)["session"]
    status, body = post(server, f"/steps/{sid}/route", {})
    assert status == 409 and "needs the run to be 'placed'" in body["error"]
    post(server, f"/steps/{sid}/place", {})
    status, body = post(server, f"/steps/{sid}/place", {})
    assert status == 409 and "already ran" in body["error"]


def test_unknown_session_or_step_is_a_404(server):
    status, body = post(server, "/steps/nope/place", {})
    assert status == 404 and "nope" in body["error"]
    sid = _start(server)["session"]
    status, body = post(server, f"/steps/{sid}/fly", {})
    assert status == 404
    status, body = get(server, "/steps/nope")
    assert status == 404


def test_field_validation_is_a_400(server):
    status, body = post(server, "/steps", {"intent": ""})
    assert status == 400 and "intent" in body["error"]
    status, body = post(server, "/steps", {"intent": "x", "kicad_live": "yes"})
    assert status == 400 and "kicad_live" in body["error"]


def test_case_writes_the_step_beside_the_board(server, tmp_path, monkeypatch):
    """A stage that produced a case but wrote nothing durable (no export
    directory) still has a whole case in ``step_text``, so the step writes it
    here rather than losing it. The case step's model call is scripted here
    rather than in the shared model: the enclosure prompt is a different
    agent."""
    from types import SimpleNamespace

    from silkscreen.units import mm

    fake = SimpleNamespace(
        spec=SimpleNamespace(cutouts=[], lid="friction", wall_nm=mm(2)),
        step_text="ISO-10303-21;\n",
        repair_rounds=0,
        exports=None,
    )
    monkeypatch.setattr(steps, "enclosure_stage", lambda *a, **k: fake)
    monkeypatch.setattr(
        "service.app._enclosure_dict", lambda e, **kw: {"step": e.step_text}
    )

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    post(server, f"/steps/{sid}/route", {})
    status, body = post(server, f"/steps/{sid}/case", {"enclosure_style": "rounded"})
    assert status == 200, body
    step = tmp_path / "steps" / sid / "build-me-a-toy-car.step"
    assert step.read_text() == "ISO-10303-21;\n"
    assert body["files"]["case"] == str(step)
    assert body["files"]["case_step"] == str(step)
    # Nothing durable was exported, so no STL slot is invented.
    assert "case_base_stl" not in body["files"]
    assert "case_lid_stl" not in body["files"]
    assert not list((tmp_path / "steps" / sid).glob("*.scad"))


def test_case_kernel_files_land_beside_the_board(server, tmp_path, monkeypatch):
    """The kernel path: the stage is handed the session directory and stem,
    and every file it exports is registered under the frozen ``files`` keys."""
    from silkscreen.agents.stages import EnclosureResult
    from silkscreen.enclosure.cad import ExportPaths
    from silkscreen.enclosure.kernel import Clause, KernelReport

    seen = {}

    def fake_stage(model, board, **kw):
        seen.update(kw)
        directory = Path(kw["export_dir"])
        stem = kw["stem"]
        exports = ExportPaths(
            step=directory / f"{stem}.step",
            base_stl=directory / f"{stem}-base.stl",
            lid_stl=directory / f"{stem}-lid.stl",
        )
        for path in (exports.step, exports.base_stl, exports.lid_stl):
            path.write_text(f"// {path.name}\n")
        snapshot = directory / f"{stem}-iso.png"
        snapshot.write_bytes(b"\x89PNG")
        return EnclosureResult(
            spec=None,
            step_text=exports.step.read_text(),
            repair_rounds=0,
            kernel=KernelReport(
                clauses=(Clause("board_clash", True, 500_000, "clear by 0.5 mm"),)
            ),
            exports=exports,
            snapshots=(snapshot,),
            brief="the facts block",
        )

    monkeypatch.setattr(steps, "enclosure_stage", fake_stage)

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    post(server, f"/steps/{sid}/route", {})
    # A style is a new ask, so the stage runs in line here (the prefetch is
    # the default case) and the kwargs it is handed can be observed.
    status, body = post(server, f"/steps/{sid}/case", {"enclosure_style": "rounded"})
    assert status == 200, body

    where = tmp_path / "steps" / sid
    stem = "build-me-a-toy-car"
    assert seen["export_dir"] == where
    assert seen["stem"] == stem
    assert seen["output"] is None and seen["emit_stages"] is False
    assert body["files"]["case"] == str(where / f"{stem}.step")
    assert body["files"]["case_step"] == str(where / f"{stem}.step")
    assert body["files"]["case_base_stl"] == str(where / f"{stem}-base.stl")
    assert body["files"]["case_lid_stl"] == str(where / f"{stem}-lid.stl")
    assert body["files"]["case_snapshots"] == [str(where / f"{stem}-iso.png")]
    for key in ("case", "case_step", "case_base_stl", "case_lid_stl"):
        assert Path(body["files"][key]).exists(), key
    # The exported STEP is what the case slot points at, not rewritten over.
    assert (where / f"{stem}.step").read_text() == f"// {stem}.step\n"
    assert not list(where.glob("*.scad"))

    enclosure = body["enclosure"]
    assert enclosure["kernel"]["passed"] is True
    assert enclosure["kernel"]["clauses"][0]["margin_mm"] == 0.5
    assert enclosure["files"]["step"] == str(where / f"{stem}.step")
    assert enclosure["files"]["snapshots"] == [str(where / f"{stem}-iso.png")]
    # The steps route, and only it, carries the brief.
    assert enclosure["brief"] == "the facts block"

    # GET /steps/<id> reports the same files.
    status, state = get(server, f"/steps/{sid}")
    assert status == 200
    assert state["files"]["case_snapshots"] == [str(where / f"{stem}-iso.png")]


def _routed(server):
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    status, routed = post(server, f"/steps/{sid}/route", {})
    assert status == 200, routed
    return sid


def test_order_writes_the_package_and_names_it(server, tmp_path):
    import zipfile

    from silkscreen.order import MANIFEST_FILENAME

    sid = _routed(server)
    status, body = post(server, f"/steps/{sid}/order", {"order": {"quantity": 10}})
    assert status == 200, body
    d = tmp_path / "steps" / sid
    zip_path = d / "build-me-a-toy-car-order.zip"
    manifest_path = d / "build-me-a-toy-car-order.json"
    assert body["files"]["order"] == str(zip_path)
    assert body["files"]["order_manifest"] == str(manifest_path)
    assert "order" in body["files"] and "board" in body["files"]

    # The inline block is untouched, and the files on disk are the same order.
    assert body["order"]["manifest"]["options"]["quantity"] == 10
    manifest = json.loads(manifest_path.read_text())
    assert manifest == body["order"]["manifest"]
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert MANIFEST_FILENAME in names
        assert json.loads(archive.read(MANIFEST_FILENAME)) == manifest
        for entry in body["order"]["files"]:
            assert archive.read(entry["filename"]).decode() == entry["content"]
    assert manifest["requires_human_approval"] is True

    status, state = get(server, f"/steps/{sid}")
    assert state["files"]["order"] == str(zip_path)


def test_order_without_kicad_cli_warns_and_still_succeeds(server):
    sid = _routed(server)
    status, body = post(server, f"/steps/{sid}/order", {})
    assert status == 200, body
    assert "model_glb" not in body["files"] and "model_step" not in body["files"]
    exports = [w for w in body["warnings"] if "3D model" in w]
    assert len(exports) == 1
    assert "no 3D model exported" in exports[0]
    assert "KICAD_CLI" in exports[0]
    assert any(e["event"] == "order.warning" for e in body["events"])


def test_order_records_the_3d_model_when_the_export_produced_one(
    server, tmp_path, monkeypatch
):
    from service import kicad_cli

    def fake_export(board, stem, **kwargs):
        assert str(board).endswith("build-me-a-toy-car.kicad_pcb")
        glb = Path(f"{stem}.glb")
        glb.write_bytes(b"glTF")
        return kicad_cli.ExportReport(
            files={"model_glb": str(glb)},
            warnings=["step export failed: kicad-cli exited 3: x"],
        )

    monkeypatch.setattr(kicad_cli, "export_models", fake_export)
    sid = _routed(server)
    status, body = post(server, f"/steps/{sid}/order", {})
    assert status == 200, body
    # ``-board``: the bare stem is the case's ``<stem>.step``, which a board
    # export must never overwrite.
    assert body["files"]["model_glb"] == str(
        tmp_path / "steps" / sid / "build-me-a-toy-car-board.glb"
    )
    assert "model_step" not in body["files"]
    assert "step export failed: kicad-cli exited 3: x" in body["warnings"]


def test_case_is_designed_in_the_background_from_the_placed_board(server):
    """Place starts the default case; pressing case collects it, spending no
    second model call. The envelope and the status route say it is running."""
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))

    sid = _start(server)["session"]
    status, placed = post(server, f"/steps/{sid}/place", {})
    assert status == 200, placed
    assert "case" in placed["next"]
    assert placed["background"] == ["sourcing", "case"]
    status, state = get(server, f"/steps/{sid}")
    assert status == 200 and state["background"] == ["sourcing", "case"]

    gate.set()
    status, cased = post(server, f"/steps/{sid}/case", {})
    assert status == 200, cased
    assert cased["enclosure"] is not None
    assert cased["enclosure"]["step"].startswith("ISO-10303-21;")
    assert "case" not in cased["background"]
    assert "warnings" not in cased
    assert len(_case_calls(log)) == 1, "the background design is the answer"
    # The case's own stage events ride the envelope of the step that shows them.
    assert any(
        e["event"] == "stage.done" and e.get("stage") == "enclosure"
        for e in cased["events"]
    )
    status, state = get(server, f"/steps/{sid}")
    assert "case" not in state["background"] and "case" in state["done"]
    # Copper was never needed: route is still open.
    assert state["next"] == ["route", "sourcing"]


def test_case_with_a_style_designs_afresh_instead_of_the_prefetch(server):
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))
    gate.set()

    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    status, cased = post(server, f"/steps/{sid}/case", {"enclosure_style": "rounded"})
    assert status == 200, cased
    assert cased["enclosure"] is not None
    calls = _case_calls(log)
    assert len(calls) == 2, "the prefetch was the default; a style is a new ask"
    # Which of the two lands in the shared log first is a race between the
    # background design and the styled one, so the invariant is counted rather
    # than indexed: exactly one call asked for the style, and one did not.
    styled = [c for c in calls if "rounded" in c["prompt"]]
    assert len(styled) == 1, "exactly one of the two calls carried the style"


def test_a_failing_background_case_is_a_warning_never_a_quiet_success(server):
    """The shared scripted model has no case answer, so the background design
    dies with a ModelError; the case step must say so."""
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    status, cased = post(server, f"/steps/{sid}/case", {})
    assert status == 200, cased
    assert cased["enclosure"] is None
    assert any("background" in w and "ModelError" in w for w in cased["warnings"])
    assert any("without a case" in w for w in cased["warnings"])
    assert "case" not in cased["files"]


def test_bridge_command_is_none_without_a_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "nope"))
    assert steps.bridge_command(tmp_path / "b.kicad_pcb", "routing") is None


def test_bridge_command_names_the_stage_and_the_board(tmp_path, monkeypatch):
    py = tmp_path / "python"
    py.write_text("")
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(py))
    argv = steps.bridge_command(tmp_path / "b.kicad_pcb", "routing")
    assert argv is not None
    assert argv[0] == str(py) and argv[1].endswith("desktop/kicad_live.py")
    assert argv[2:] == [str(tmp_path / "b.kicad_pcb"), "routing"]


def test_slug_is_a_safe_file_stem():
    assert steps.slug("Build me a toy car!") == "build-me-a-toy-car"
    assert steps.slug("???") == "board"
    assert len(steps.slug("x" * 100)) <= 40


# ---------------------------------------------------------------- sourcing


def test_sourcing_runs_in_the_background_and_the_step_collects_it(server):
    """Place starts the lookup; pressing sourcing collects it with no second
    model call, and every row carries the honest statuses."""
    gate = threading.Event()
    log: list = []
    Handler.model_factory = staticmethod(lambda: _Gated(gate, log))

    sid = _start(server)["session"]
    status, placed = post(server, f"/steps/{sid}/place", {})
    assert status == 200, placed
    assert "sourcing" in placed["next"]
    assert "sourcing" in placed["background"]

    gate.set()
    status, body = post(server, f"/steps/{sid}/sourcing", {})
    assert status == 200, body
    assert "sourcing" not in body["background"]
    assert len(_sourcing_calls(log)) == 1, "the background lookup is the answer"

    block = body["sourcing"]
    rows = {p["ref"]: p for p in block["parts"]}
    assert set(rows) == {"U1", "C1", "C2"}
    assert rows["U1"]["mpn"] == "AMS1117-3.3"
    assert rows["U1"]["mpn_status"] == "proposed"
    assert rows["U1"]["datasheet_status"] == "verified"
    assert rows["U1"]["kind"] == "device"
    assert rows["C1"]["datasheet_status"] == "not_pdf"
    assert rows["C1"]["note"] == "X5R; check the voltage derating"
    assert rows["C1"]["kind"] == "capacitor"
    assert rows["C2"]["mpn"] is None and rows["C2"]["mpn_status"] == "none"
    assert rows["C2"]["datasheet_status"] == "none"
    for row in rows.values():
        assert row["package"], "every row names its land pattern"
        assert "model3d" in row and "model3d_note" in row
    assert block["verified"] == 1
    assert block["proposed"] == 2
    assert block["unresolved"] == 1
    assert block["warnings"] == [] and body["warnings"] == []

    # The lookup's own events ride the envelope of the step that shows them.
    assert any(
        e["event"] == "stage.done" and e.get("stage") == "sourcing"
        for e in body["events"]
    )
    assert [e["ref"] for e in body["events"] if e["event"] == "sourcing.part"] == [
        "U1",
        "C1",
        "C2",
    ]
    assert Path(body["files"]["bom"]).name == "build-me-a-toy-car-bom.csv"
    csv_text = Path(body["files"]["bom"]).read_text()
    assert csv_text.splitlines()[0].startswith("ref,value,kind,package,")
    assert "AMS1117-3.3" in csv_text

    status, state = get(server, f"/steps/{sid}")
    assert "sourcing" in state["done"] and "sourcing" not in state["background"]
    assert state["files"]["bom"] == body["files"]["bom"]


def test_order_carries_the_bom_in_the_manifest_and_the_zip(server, tmp_path):
    """Order pressed before sourcing collects the lookup itself: the manifest
    has the rows, the zip has ``bom.csv``, and the CSV is on disk."""
    import zipfile

    from silkscreen.order import MANIFEST_FILENAME

    Handler.model_factory = staticmethod(sourced)
    sid = _routed(server)
    status, body = post(server, f"/steps/{sid}/order", {})
    assert status == 200, body

    bom = body["order"]["manifest"]["bom"]
    assert [row["ref"] for row in bom] == ["U1", "C1", "C2"]
    assert bom[0]["mpn_status"] == "proposed"
    assert bom[0]["datasheet_status"] == "verified"
    assert body["sourcing"]["parts"] == bom
    assert body["sourcing"]["verified"] == 1

    d = tmp_path / "steps" / sid
    bom_path = d / "build-me-a-toy-car-bom.csv"
    assert body["files"]["bom"] == str(bom_path) and bom_path.is_file()
    manifest = json.loads((d / "build-me-a-toy-car-order.json").read_text())
    assert manifest["bom"] == bom
    with zipfile.ZipFile(d / "build-me-a-toy-car-order.zip") as archive:
        assert "bom.csv" in archive.namelist()
        assert archive.read("bom.csv").decode() == bom_path.read_text()
        assert json.loads(archive.read(MANIFEST_FILENAME))["bom"] == bom
    # The lookup's events are shown by the step that waited for them.
    assert any(
        e["event"] == "stage.done" and e.get("stage") == "sourcing"
        for e in body["events"]
    )

    # Sourcing pressed afterwards reports the same rows; nothing is redone.
    status, again = post(server, f"/steps/{sid}/sourcing", {})
    assert status == 200, again
    assert again["sourcing"]["parts"] == bom
    assert again["files"]["bom"] == str(bom_path)


class _SourcingOutage:
    """The shared scripted model, except that the sourcing call is down.

    The outage is explicit rather than an answer left out of ``by_marker``:
    the shared model gained a sourcing answer once the pipeline tests
    needed one, and a test that leaned on its absence started passing for
    the wrong reason.
    """

    def __init__(self) -> None:
        self.inner = scripted()

    def generate(self, prompt, **kwargs):
        from silkscreen.agents.model import ModelError
        from silkscreen.agents.sourcing import SOURCING_MARKER

        if SOURCING_MARKER in prompt:
            raise ModelError("sourcing model is down (test outage)")
        return self.inner.generate(prompt, **kwargs)


def test_a_failing_background_sourcing_is_a_warning_with_the_plain_rows(server):
    """The sourcing model dies with a ModelError in the background; the step
    must say so and still list every part, with nothing claimed for it."""
    Handler.model_factory = staticmethod(_SourcingOutage)
    sid = _start(server)["session"]
    post(server, f"/steps/{sid}/place", {})
    status, body = post(server, f"/steps/{sid}/sourcing", {})
    assert status == 200, body
    rows = body["sourcing"]["parts"]
    assert [row["ref"] for row in rows] == ["U1", "C1", "C2"]
    assert all(row["mpn"] is None and row["mpn_status"] == "none" for row in rows)
    assert all(row["datasheet_status"] == "none" for row in rows)
    assert body["sourcing"]["proposed"] == 0
    assert body["sourcing"]["unresolved"] == 3
    assert any("background" in w and "ModelError" in w for w in body["warnings"])
    assert body["sourcing"]["warnings"] == body["warnings"]
    assert Path(body["files"]["bom"]).is_file()

    # The order still has a BOM, and repeats the warning where it is read.
    post(server, f"/steps/{sid}/route", {})
    status, ordered = post(server, f"/steps/{sid}/order", {})
    assert status == 200, ordered
    assert [row["ref"] for row in ordered["order"]["manifest"]["bom"]] == [
        "U1",
        "C1",
        "C2",
    ]
    assert any("ModelError" in w for w in ordered["warnings"])


def test_sourcing_before_place_is_a_409(server):
    sid = _start(server)["session"]
    status, body = post(server, f"/steps/{sid}/sourcing", {})
    assert status == 409 and "needs the run to be 'placed'" in body["error"]


# ---------------------------------------------------------------- bridge truthfulness

_POSIX = pytest.mark.skipif(
    os.name != "posix", reason="the fake bridge is a shell script"
)


def _fake_bridge(tmp_path, body: str) -> Path:
    """An interpreter stand-in: ignores its arguments and runs ``body``."""
    script = tmp_path / "fake-bridge"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return script


@_POSIX
def test_a_bridge_that_fails_makes_shown_false_and_says_why(
    server, tmp_path, monkeypatch
):
    bridge = _fake_bridge(
        tmp_path,
        "echo 'error: KiCad'\"'\"'s API server is off: Preferences > Plugins > "
        "Enable API server' >&2\nexit 1",
    )
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(bridge))
    body = _start(server, kicad_live=True)
    assert body["shown_in_kicad"] is False
    assert body["shown_detail"].startswith("schematic was not shown in KiCad: ")
    assert "Enable API server" in body["shown_detail"]
    assert "error:" not in body["shown_detail"]
    status, state = get(server, f"/steps/{body['session']}")
    assert status == 200 and state["shown_detail"] == body["shown_detail"]


@_POSIX
def test_a_bridge_that_finishes_cleanly_is_shown(server, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SILKSCREEN_KICAD_LIVE_PYTHON", str(_fake_bridge(tmp_path, "exit 0"))
    )
    body = _start(server, kicad_live=True)
    assert body["shown_in_kicad"] is True and body["shown_detail"] is None
    _, state = get(server, f"/steps/{body['session']}")
    assert state["shown_detail"] is None


@_POSIX
def test_a_bridge_that_outlives_the_grace_reports_through_status(
    server, tmp_path, monkeypatch
):
    monkeypatch.setattr(steps, "BRIDGE_GRACE_S", 0.2)
    bridge = _fake_bridge(
        tmp_path, "sleep 1\necho 'error: board never loaded' >&2\nexit 1"
    )
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(bridge))
    body = _start(server, kicad_live=True)
    # Still running when the step answered: honestly "started", nothing known.
    assert body["shown_in_kicad"] is True and body["shown_detail"] is None
    deadline = time.monotonic() + 5
    detail = None
    while time.monotonic() < deadline and detail is None:
        _, state = get(server, f"/steps/{body['session']}")
        detail = state["shown_detail"]
        time.sleep(0.1)
    assert detail == "schematic was not shown in KiCad: board never loaded"


def test_no_bridge_is_a_named_reason_not_a_bare_false(server):
    body = _start(server, kicad_live=True)
    assert body["shown_in_kicad"] is False
    assert ".venv-kicad" in body["shown_detail"]
    assert "SILKSCREEN_KICAD_LIVE_PYTHON" in body["shown_detail"]


def test_a_session_that_did_not_ask_for_kicad_has_no_detail(server):
    body = _start(server)
    assert body["shown_in_kicad"] is False and body["shown_detail"] is None


def test_bridge_failure_is_the_last_stderr_line_without_the_prefix():
    assert (
        steps._bridge_failure(
            "routing", "warning: x\nerror: no board editor is running\n", 1
        )
        == "routing was not shown in KiCad: no board editor is running"
    )
    assert steps._bridge_failure("routing", "", 3) == (
        "routing was not shown in KiCad: the bridge exited 3 without saying why"
    )


# ---------------------------------------------------------------- desktop/kicad_live.py


def _kicad_live():
    """The bridge module, loaded from ``desktop/`` (it is not a package)."""
    import importlib.util

    path = Path(steps._REPO_ROOT) / "desktop" / "kicad_live.py"
    spec = importlib.util.spec_from_file_location("kicad_live_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_fails_fast_when_pcbnew_runs_without_an_api_socket(monkeypatch):
    kl = _kicad_live()
    monkeypatch.setattr(kl, "_pcbnew_pids", lambda: ["4242"])
    monkeypatch.setattr(kl, "_socket_path", lambda pid: Path("/nonexistent/api.sock"))
    # "Without an api socket" means without any: KiCad 10 binds only the
    # shared one, so leaving the real /tmp/kicad/api.sock in play makes this
    # pass on CI and fail on a developer machine with pcbnew open.
    monkeypatch.setattr(kl, "SHARED_SOCKET", Path("/nonexistent/api.sock"))
    started = time.monotonic()
    with pytest.raises(kl.BridgeError) as caught:
        kl.connect_board("board", timeout_s=60.0, socket_grace_s=0.0)
    assert time.monotonic() - started < 5
    assert "Enable API server" in str(caught.value)


def test_bridge_fails_fast_when_no_board_editor_is_running(monkeypatch):
    kl = _kicad_live()
    monkeypatch.setattr(kl, "_pcbnew_pids", lambda: [])
    with pytest.raises(kl.BridgeError) as caught:
        kl.connect_board("board", timeout_s=60.0, socket_grace_s=0.0)
    assert "open the placed board in pcbnew" in str(caught.value)


def test_bridge_main_relays_one_stderr_line(monkeypatch, tmp_path, capsys):
    kl = _kicad_live()
    monkeypatch.setattr(kl, "_pcbnew_pids", lambda: ["4242"])
    monkeypatch.setattr(kl, "_socket_path", lambda pid: Path("/nonexistent/api.sock"))
    monkeypatch.setattr(kl, "SHARED_SOCKET", Path("/nonexistent/api.sock"))
    monkeypatch.setattr(kl, "API_SOCKET_GRACE_S", 0.0)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert kl.main([str(pcb), "routing", "--timeout", "1"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("error: ")
    assert "Enable API server" in err[0]


@pytest.fixture(autouse=True)
def _no_distributor_key(monkeypatch):
    """The sourcing thread asks Mouser only when ``MOUSER_API_KEY`` is set;
    a developer's real key must never turn these tests into network calls."""
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)


def test_order_carries_the_grouped_bom_beside_the_per_ref_one(server, tmp_path):
    """The assembler's sheet: one line per part with a quantity, in the zip,
    named in the manifest, and on disk next to ``<stem>-bom.csv``."""
    import zipfile

    from silkscreen.order import MANIFEST_FILENAME

    Handler.model_factory = staticmethod(sourced)
    sid = _routed(server)
    status, body = post(server, f"/steps/{sid}/order", {})
    assert status == 200, body

    d = tmp_path / "steps" / sid
    grouped_path = d / "build-me-a-toy-car-bom-grouped.csv"
    assert body["files"]["bom_grouped"] == str(grouped_path)
    assert grouped_path.is_file()
    text = grouped_path.read_text()
    lines = text.splitlines()
    assert lines[0] == (
        "designators,qty,value,package,manufacturer,mpn,mpn_status,"
        "distributor_sku,dnp"
    )
    # Every designator on the board appears exactly once across the groups.
    refs = [ref for line in lines[1:] for ref in line.split(",")[0].split(" ")]
    bom = body["order"]["manifest"]["bom"]
    assert sorted(refs) == sorted(row["ref"] for row in bom)
    assert sum(int(line.split(",")[1]) for line in lines[1:]) == len(refs)
    # No key was set, so nothing reads stronger than proposed.
    assert "verified" not in {line.split(",")[6] for line in lines[1:]}

    manifest = body["order"]["manifest"]
    assert manifest["bom_files"] == ["bom.csv", "bom-grouped.csv"]
    with zipfile.ZipFile(d / "build-me-a-toy-car-order.zip") as archive:
        assert {"bom.csv", "bom-grouped.csv"} <= set(archive.namelist())
        assert archive.read("bom-grouped.csv").decode() == text
        packed = json.loads(archive.read(MANIFEST_FILENAME))
        assert packed["bom_files"] == manifest["bom_files"]
    assert all(row["mpn_status"] != "verified" for row in manifest["bom"])
    assert all(row["verify_error"] is None for row in manifest["bom"])

    status, state = get(server, f"/steps/{sid}")
    assert state["files"]["bom_grouped"] == str(grouped_path)


class _NamedModel:
    """A model that reports its own name, the way ``GeminiModel`` does."""

    def __init__(self, model: str = "gemini-3.7-flash") -> None:
        self.model = model
        self.inner = sourced()

    def generate(self, prompt, **kwargs):
        return self.inner.generate(prompt, **kwargs)


def test_a_step_names_the_model_that_did_the_work_and_counts_its_calls(server):
    """The step envelope carries the pipeline's own ``model.call`` frames.

    Without them a step run reported no model name and no call count, and a
    client could only say the engine had not told it. The deterministic half
    stays deterministic: the placer is CP-SAT, it calls no model, and its
    envelope must carry no ``model.call`` at all -- a stage labelled with a
    model it never used is the same lie in the other direction.
    """
    Handler.model_factory = staticmethod(_NamedModel)
    body = _start(server)
    calls = [e for e in body["events"] if e["event"] == "model.call"]
    assert calls, "propose called a model; the envelope must say which"
    assert {c["model"] for c in calls} == {"gemini-3.7-flash"}
    assert all(c["ok"] and c["layer"] == "worker" for c in calls)
    assert any(c["stage"] == "propose" for c in calls)
    assert len({c["call_id"] for c in calls}) == len(calls), "call ids collide"

    status, placed = post(server, f"/steps/{body['session']}/place", {})
    assert status == 200, placed
    assert [e for e in placed["events"] if e["event"] == "model.call"] == []


# ------------------------------------------------- the spec-review agenda

#: An agenda about a part ``test_app.CIRCUIT`` actually contains, so the
#: hallucination filter in ``parse_spec_review`` keeps the item.
AGENDA = {
    "title": "Toy car board spec review",
    "summary": "Two open questions the run could not settle on its own.",
    "items": [
        {
            "topic": "Regulator thermal budget",
            "why": "U1 dissipation at the stated load was never bounded",
            "minutes": 20,
            "blocking": True,
            "refs": ["U1"],
        },
        {
            "topic": "Bulk capacitor value",
            "why": "C1 was chosen without a ripple target",
            "minutes": 10,
            "blocking": False,
            "refs": ["C1"],
        },
    ],
    "decisions_needed": ["pick the regulator package"],
    "prepared_from": ["review"],
}


def with_agenda():
    """The shared scripted model, plus an answer for the spec-review prompt."""
    from silkscreen.agents.specreview import SPECREVIEW_MARKER

    return ScriptedModel(
        by_marker={**scripted().by_marker, SPECREVIEW_MARKER: json.dumps(AGENDA)}
    )


def _reviewed(srv, payload):
    """Run a session to ``review``, passing ``payload`` to the review step."""
    sid = _start(srv)["session"]
    assert post(srv, f"/steps/{sid}/place", {})[0] == 200
    assert post(srv, f"/steps/{sid}/route", {})[0] == 200
    status, body = post(srv, f"/steps/{sid}/review", payload)
    assert status == 200, body
    return sid, body


@pytest.mark.parametrize("payload", [{"spec_review": True}, {"summary": "structured"}])
def test_review_puts_the_agenda_itself_on_the_envelope(server, monkeypatch, payload):
    """``spec_review`` is the ``SpecReview``, not the stage's result wrapper.

    This is the seam between ``agents/specreview.py`` and the desktop: the
    engine's ``SpecReviewResult.as_dict()`` is
    ``{ok, review, needs_meeting, warnings}``, while ``StepResponse.spec_review``
    in ``app/src/lib/silkscreen/types.ts`` is a ``SpecReviewBlock`` with
    ``items``. Sending the wrapper made ``specReviewFrom`` return a truthy
    object with no ``items`` and ``blockingItems`` threw
    ``Cannot read properties of undefined (reading 'filter')`` -- the deliver
    panel, not a bad agenda. The flattened block keeps all three states: absent
    (not asked for), ``null`` (nothing usable, warnings say why), a block.

    Both request spellings are pinned because ``_wants_agenda`` accepts two --
    the explicit flag and the desktop's Structured toggle.
    """
    monkeypatch.setattr(Handler, "model_factory", staticmethod(with_agenda))
    _, body = _reviewed(server, payload)

    block = body["spec_review"]
    assert set(block) >= {"title", "summary", "items", "total_minutes"}
    # The wrapper's keys must not be here; "review" nested inside would be the
    # exact shape the desktop cannot read.
    assert "review" not in block and "ok" not in block
    assert block["title"] == AGENDA["title"]
    assert [i["topic"] for i in block["items"]] == [
        i["topic"] for i in AGENDA["items"]
    ]
    assert block["total_minutes"] == 30
    assert block["needs_meeting"] is True
    assert [i["blocking"] for i in block["items"]] == [True, False]
    # The findings are still the product of the step.
    assert isinstance(body["findings"], list)


def test_no_agenda_is_asked_for_unless_the_caller_says_so(server, monkeypatch):
    """Default prose: no key at all, and no model call spent on one."""
    monkeypatch.setattr(Handler, "model_factory", staticmethod(with_agenda))
    _, body = _reviewed(server, {})
    assert "spec_review" not in body
    _, prose = _reviewed(server, {"summary": "prose"})
    assert "spec_review" not in prose


def test_an_unusable_agenda_is_null_with_a_warning_not_a_silent_gap(
    server, monkeypatch
):
    """A model that never returns valid JSON loses the agenda, not the run.

    ``null`` plus a warning, which is distinguishable from "not asked for"
    (key absent) and from an agenda with nothing blocking (a block whose
    ``needs_meeting`` is false).
    """
    from silkscreen.agents.specreview import SPECREVIEW_MARKER

    def broken():
        return ScriptedModel(
            by_marker={**scripted().by_marker, SPECREVIEW_MARKER: "not json at all"}
        )

    monkeypatch.setattr(Handler, "model_factory", staticmethod(broken))
    _, body = _reviewed(server, {"spec_review": True})
    assert body["spec_review"] is None
    assert body["warnings"] and any(
        "spec review agenda" in w or "spec-review agenda" in w
        for w in body["warnings"]
    )
    assert isinstance(body["findings"], list)


def test_a_bad_spec_review_flag_is_a_400_not_a_guess(server):
    sid = _start(server)["session"]
    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    assert post(server, f"/steps/{sid}/route", {})[0] == 200
    status, body = post(server, f"/steps/{sid}/review", {"spec_review": "yes"})
    assert status == 400 and "spec_review" in body["error"]


def test_known_refs_carries_net_names_not_only_part_refs():
    """The agenda's ref filter must know what the evidence is made of.

    Proven end to end on 2026-09-06: with part refs alone, an ESP32-S3 run
    carrying a blocker and five unrouted nets came back with an EMPTY agenda,
    because every item the model wrote cited a net -- ``ESP_EN``, ``I2C_SDA``
    -- and the filter dropped each one as "the board does not contain". The
    filter fired hardest exactly where a meeting was most warranted, which is
    worse than not filtering at all.
    """

    class _Part:
        def __init__(self, ref):
            self.ref = ref

    class _Board:
        parts = [_Part("U1"), _Part("C3")]
        nets = ["ESP_EN", "I2C_SDA", "GND"]

    class _Route:
        unrouted = {"ESP_EN": "no channel", "USB_DN": "no channel"}
        routed = ["GND"]

    session = steps.Session(
        id="s", intent="", stem="b", directory=Path("."),
        kicad_live=False, time_limit_s=None,
    )
    session.board = _Board()
    session.route = _Route()

    known = steps._known_refs(session)
    assert "U1" in known and "C3" in known          # parts, as before
    assert "ESP_EN" in known and "I2C_SDA" in known  # nets the evidence names
    assert "USB_DN" in known                         # unrouted-only nets too
    assert known.count("GND") == 1                   # deduped, order kept
    assert known.index("U1") < known.index("ESP_EN")

    # No board at all is None, not an empty list: an empty list would claim
    # the board contains nothing and drop every item ever written.
    empty = steps.Session(
        id="s2", intent="", stem="b", directory=Path("."),
        kicad_live=False, time_limit_s=None,
    )
    assert steps._known_refs(empty) is None


def test_the_review_step_says_when_the_critic_produced_no_verdict(
    tmp_path, monkeypatch
):
    """``findings: []`` alone cannot tell the overlay what happened.

    The step envelope has to carry the difference between "the critic reviewed
    this board and found nothing" and "the critic answered something
    unreadable", because a client that sees only an empty list renders a clean
    review over a model failure -- the same defect one hop downstream from
    ``review_circuit``. The board is still the product, so the step still
    succeeds; it just refuses to claim a verdict it does not have.
    """
    import json as _json

    def unreadable_review():
        # Spread the shared script and override only the critic. Building the
        # marker map from scratch meant this test had to be edited every time
        # a stage was added -- and it was the one test that broke when the
        # planning stage landed.
        return ScriptedModel(
            by_marker={
                **scripted().by_marker,
                "reviewing a circuit someone else designed": _json.dumps(
                    {"verdict": "looks fine to me"}
                ),
            }
        )

    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "missing-kicad-cli"))
    monkeypatch.setattr(steps, "PROBE", fake_probe)
    steps.reset_sessions()
    Handler.model_factory = staticmethod(unreadable_review)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        sid = _start(srv)["session"]
        assert post(srv, f"/steps/{sid}/place", {})[0] == 200
        assert post(srv, f"/steps/{sid}/route", {})[0] == 200
        status, reviewed = post(srv, f"/steps/{sid}/review", {})
        assert status == 200, reviewed
        assert reviewed["findings"] == [] and reviewed["blockers"] == []
        assert reviewed["review"]["status"] == "failed"
        assert reviewed["review"]["ran"] is True
        assert "nothing is known about this board" in reviewed["review"]["note"]
        # And a warning nobody has to dig for.
        assert any(
            "produced no verdict" in w for w in reviewed.get("warnings", [])
        )
    finally:
        srv.shutdown()
        srv.server_close()
        Handler.store = None
        steps.reset_sessions()


def test_status_carries_the_review_block_before_and_after_the_step(server):
    """``GET /steps/<id>`` says ``skipped`` until the review step is pressed
    and then repeats the step's own block, key for key -- never an absent key
    a client could read as "reviewed, nothing to say"."""
    sid = _start(server)["session"]
    status, state = get(server, f"/steps/{sid}")
    assert status == 200, state
    assert state["review"] == {
        "status": "skipped",
        "ran": False,
        "detail": None,
        "note": "the review did not run, so nothing is known about this board",
        "dropped": [],
        "merged": [],
    }
    assert state["background_outcome"] == {}
    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    assert post(server, f"/steps/{sid}/route", {})[0] == 200
    status, reviewed = post(server, f"/steps/{sid}/review", {})
    assert status == 200, reviewed
    assert reviewed["review"]["status"] == "ok" and reviewed["review"]["ran"] is True
    assert reviewed["review"]["detail"] is None
    status, state = get(server, f"/steps/{sid}")
    assert state["review"] == reviewed["review"]


# ---------------------------------------------------------------- view3d
#
# "Show me the board in 3D" means KiCad's own 3D viewer, on the routed board
# KiCad itself reads. These pin the route's contract without a KiCad: that it
# is not a step (no state, repeatable, no model call), and that a failure
# always comes back in words naming the fix rather than as a bare false.


def test_view3d_is_not_a_step(tmp_path, monkeypatch):
    # In STEPS it would be refused the second time it was pressed ("already
    # ran") and would demand a stage transition it has none of.
    assert steps.VIEW_3D not in steps.STEPS
    assert steps.VIEW_3D not in steps._REQUIRES


def test_view3d_says_there_is_no_board_yet_rather_than_a_bare_false(monkeypatch):
    steps.reset_sessions()
    session = steps.Session(
        id="s1",
        intent="x",
        stem="b",
        directory=Path(tempfile.mkdtemp()),
        kicad_live=True,
        time_limit_s=None,
    )
    steps._SESSIONS[session.id] = session
    body = steps.show_board_3d("s1")
    assert body["opened"] is False
    assert "no board yet" in body["detail"]


def test_view3d_names_the_missing_bridge_instead_of_failing_silently(
    monkeypatch, tmp_path
):
    steps.reset_sessions()
    session = steps.Session(
        id="s2",
        intent="x",
        stem="b",
        directory=tmp_path,
        kicad_live=True,
        time_limit_s=None,
    )
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    steps._SESSIONS[session.id] = session
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "nope"))
    body = steps.show_board_3d("s2")
    assert body["opened"] is False
    assert body["detail"] == steps.NO_BRIDGE_DETAIL


def test_view3d_relays_the_bridges_own_words_when_kicad_refused(monkeypatch, tmp_path):
    """The reason is the product: "the API server is off" and "no Accessibility
    grant" send an engineer to two different preferences, and a bare false
    sends them to neither."""
    steps.reset_sessions()
    session = steps.Session(
        id="s3",
        intent="x",
        stem="b",
        directory=tmp_path,
        kicad_live=True,
        time_limit_s=None,
    )
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    steps._SESSIONS[session.id] = session
    monkeypatch.setattr(steps, "bridge_command", lambda pcb, stage: ["/bin/sh", "-c",
        "echo \"error: KiCad's API server is off\" >&2; exit 1"])
    body = steps.show_board_3d("s3")
    assert body["opened"] is False
    assert "API server is off" in body["detail"]


def test_view3d_reports_the_board_it_opened_when_kicad_answered(monkeypatch, tmp_path):
    steps.reset_sessions()
    session = steps.Session(
        id="s4",
        intent="x",
        stem="b",
        directory=tmp_path,
        kicad_live=True,
        time_limit_s=None,
    )
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    steps._SESSIONS[session.id] = session
    seen: list[str] = []

    def fake(pcb, stage):
        seen.append(f"{pcb.name}:{stage}")
        return ["/bin/sh", "-c", "echo 3d: opened the 3D viewer via IPC"]

    monkeypatch.setattr(steps, "bridge_command", fake)
    body = steps.show_board_3d("s4")
    assert body["opened"] is True
    assert "3D viewer" in body["detail"]
    # The routed board, not the pre-routing placed one: the routed file on
    # disk is the product, copper included.
    assert seen == ["b.kicad_pcb:3d"]


# ---------------------------------------------------------------- FreeCAD
#
# macOS has no handler for ``.step`` even with FreeCAD installed (FreeCAD's
# Info.plist declares only its own types), so handing the path to "whatever
# owns .step" opened nothing. The case is opened in FreeCAD by name, and a
# machine without FreeCAD says so in words.


def _case_session(tmp_path, *, step=True, kicad_live=True):
    steps.reset_sessions()
    session = steps.Session(
        id="fc1",
        intent="x",
        stem="b",
        directory=tmp_path,
        kicad_live=kicad_live,
        time_limit_s=None,
    )
    if step:
        model = tmp_path / "b.step"
        model.write_text("ISO-10303-21;\n", encoding="utf-8")
        session.files["case"] = session.files["case_step"] = str(model)
    steps._SESSIONS[session.id] = session
    return session


def test_open_case_is_not_a_step():
    assert steps.OPEN_CASE not in steps.STEPS
    assert steps.OPEN_CASE not in steps._REQUIRES


def test_open_case_says_there_is_no_case_yet(tmp_path):
    _case_session(tmp_path, step=False)
    body = steps.open_case_in_freecad("fc1")
    assert body == {"opened": False, "detail": "there is no case yet: run case first"}


def test_open_case_names_a_missing_freecad_instead_of_doing_nothing(
    tmp_path, monkeypatch
):
    _case_session(tmp_path)
    monkeypatch.delenv(steps.FREECAD_APP_ENV, raising=False)
    monkeypatch.setattr(steps, "FREECAD_APP_CANDIDATES", (str(tmp_path / "none.app"),))
    monkeypatch.setattr(steps, "FREECAD_EXECUTABLES", ("no-such-freecad-binary",))
    body = steps.open_case_in_freecad("fc1")
    assert body["opened"] is False
    assert body["detail"] == steps.FREECAD_NOT_INSTALLED
    assert "FreeCAD is not installed" in body["detail"]


def test_open_case_names_a_misconfigured_override(tmp_path, monkeypatch):
    _case_session(tmp_path)
    monkeypatch.setenv(steps.FREECAD_APP_ENV, str(tmp_path / "gone.app"))
    body = steps.open_case_in_freecad("fc1")
    assert body["opened"] is False
    assert steps.FREECAD_APP_ENV in body["detail"]


def _fake_bundle(tmp_path, executable="FreeCAD"):
    import plistlib

    app = tmp_path / "FreeCAD.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump({"CFBundleExecutable": executable}, fh)
    binary = app / "Contents" / "MacOS" / executable
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    return app, binary


class _FakeFreeCAD:
    """A spawned FreeCAD: ``code`` None stays up, an int exits with it."""

    def __init__(self, seen, code=None, stderr=""):
        self.seen, self.code, self.stderr = seen, code, stderr

    def __call__(self, argv, **kwargs):
        self.seen.append(list(argv))
        outer = self

        class Proc:
            returncode = outer.code

            def communicate(self, timeout=None):
                if outer.code is None and timeout is not None:
                    raise subprocess.TimeoutExpired(argv, timeout)
                return "", outer.stderr

        return Proc()


@pytest.mark.skipif(not sys.platform.startswith("darwin"), reason="macOS bundle path")
def test_open_case_passes_the_step_as_an_argument_not_an_open_event(
    tmp_path, monkeypatch
):
    """``open -a FreeCAD x.step`` exits 0 and imports nothing: FreeCAD's
    QFileOpenEvent handler opens only .FCStd. The path must reach argv, and
    ``-n`` because ``--args`` is dropped when ``open`` only activates a
    FreeCAD that is already running."""
    _case_session(tmp_path)
    app, _ = _fake_bundle(tmp_path)
    monkeypatch.setenv(steps.FREECAD_APP_ENV, str(app))
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(steps.subprocess, "run", fake_run)
    body = steps.open_case_in_freecad("fc1")
    assert body["opened"] is True
    step = str((tmp_path / "b.step").resolve())
    assert seen == [["/usr/bin/open", "-n", "-a", str(app), "--args", step]]


@pytest.mark.skipif(not sys.platform.startswith("darwin"), reason="macOS bundle path")
def test_open_case_relays_launchservices_own_refusal(tmp_path, monkeypatch):
    _case_session(tmp_path)
    app, _ = _fake_bundle(tmp_path)
    monkeypatch.setenv(steps.FREECAD_APP_ENV, str(app))
    monkeypatch.setattr(
        steps.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, "", "Unable to find application named 'FreeCAD'\n"
        ),
    )
    body = steps.open_case_in_freecad("fc1")
    assert body["opened"] is False
    assert "Unable to find application named 'FreeCAD'" in body["detail"]


def test_open_case_relays_freecads_own_words_when_it_dies_at_once(
    tmp_path, monkeypatch
):
    _case_session(tmp_path)
    exe = tmp_path / "freecad"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(steps.FREECAD_APP_ENV, str(exe))
    monkeypatch.setattr(
        steps.subprocess,
        "Popen",
        _FakeFreeCAD([], code=1, stderr="boot\nLibrary not loaded: libQt6Core\n"),
    )
    body = steps.open_case_in_freecad("fc1")
    assert body["opened"] is False
    assert body["detail"].endswith("Library not loaded: libQt6Core")


def test_open_case_runs_an_executable_with_the_file_elsewhere(tmp_path, monkeypatch):
    _case_session(tmp_path)
    exe = tmp_path / "freecad"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(steps.FREECAD_APP_ENV, str(exe))
    seen: list[list[str]] = []
    monkeypatch.setattr(steps.subprocess, "Popen", _FakeFreeCAD(seen))
    body = steps.open_case_in_freecad("fc1")
    assert body["opened"] is True
    assert seen == [[str(exe), str(tmp_path / "b.step")]]


def test_case_step_in_desktop_mode_says_in_words_when_freecad_is_absent(
    server, tmp_path, monkeypatch
):
    """Pressing case in desktop mode asks for FreeCAD; with none installed the
    case panel's own warnings (the enclosure block) carry the sentence."""
    from types import SimpleNamespace

    from silkscreen.units import mm

    fake = SimpleNamespace(
        spec=SimpleNamespace(cutouts=[], lid="friction", wall_nm=mm(2)),
        step_text="ISO-10303-21;\n",
        repair_rounds=0,
        exports=None,
    )
    monkeypatch.setattr(steps, "enclosure_stage", lambda *a, **k: fake)
    monkeypatch.setattr(
        "service.app._enclosure_dict",
        lambda e, **kw: {"step": e.step_text, "warnings": []},
    )
    monkeypatch.delenv(steps.FREECAD_APP_ENV, raising=False)
    monkeypatch.setattr(steps, "FREECAD_APP_CANDIDATES", (str(tmp_path / "none.app"),))
    monkeypatch.setattr(steps, "FREECAD_EXECUTABLES", ("no-such-freecad-binary",))

    sid = _start(server, kicad_live=True)["session"]
    post(server, f"/steps/{sid}/place", {})
    status, body = post(server, f"/steps/{sid}/case", {"enclosure_style": "rounded"})
    assert status == 200, body
    assert body["opened_in_freecad"] is False
    assert any("FreeCAD is not installed" in w for w in body["warnings"])
    assert any("FreeCAD is not installed" in w for w in body["enclosure"]["warnings"])


def test_open_case_route_answers_over_http(server, tmp_path, monkeypatch):
    monkeypatch.delenv(steps.FREECAD_APP_ENV, raising=False)
    monkeypatch.setattr(steps, "FREECAD_APP_CANDIDATES", (str(tmp_path / "none.app"),))
    monkeypatch.setattr(steps, "FREECAD_EXECUTABLES", ("no-such-freecad-binary",))
    sid = _start(server)["session"]
    status, body = post(server, f"/steps/{sid}/open_case", {})
    assert status == 200, body
    assert body == {"opened": False, "detail": "there is no case yet: run case first"}


# --------------------------------------------------------- idempotent starts
#
# ``POST /steps`` is the only route here that is not guarded by ``advance``'s
# "already ran" check, and a second start is a second read, plan and propose.
# The header, its replay and its 409 are Stripe's design: stripe-python sets an
# ``Idempotency-Key`` on every POST (``stripe/_api_requestor.py``) precisely so
# a repeat cannot become a second charge. The property each test asserts is the
# number of model calls, not the shape of the body.


def post_keyed(srv, path, payload, key):
    req = urllib.request.Request(
        url(srv, path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Idempotency-Key": key},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def counted(server, monkeypatch):
    """The server, with every model call this run makes in one list."""
    log: list = []
    monkeypatch.setattr(
        Handler,
        "model_factory",
        staticmethod(lambda: ScriptedModel(by_marker=scripted().by_marker, calls=log)),
    )
    return server, log


def test_a_repeated_start_under_one_key_is_replayed_not_re_run(counted):
    srv, log = counted
    body = {"intent": "build me a toy car", "time_limit_s": 5}

    first_status, first = post_keyed(srv, "/steps", body, "press-1")
    assert first_status == 200, first
    spent = len(log)
    assert spent > 0

    second_status, second = post_keyed(srv, "/steps", body, "press-1")

    assert second_status == 200, second
    # The same run, not a second one: same session id, and not one more call.
    assert second["session"] == first["session"]
    assert second == first
    assert len(log) == spent


def test_two_presses_without_a_key_are_two_runs_which_is_the_old_behaviour(counted):
    srv, log = counted
    body = {"intent": "build me a toy car", "time_limit_s": 5}

    _, first = post(srv, "/steps", body)
    spent = len(log)
    _, second = post(srv, "/steps", body)

    assert first["session"] != second["session"]
    assert len(log) > spent


def test_different_keys_are_different_runs(counted):
    srv, log = counted
    body = {"intent": "build me a toy car", "time_limit_s": 5}

    _, first = post_keyed(srv, "/steps", body, "press-1")
    spent = len(log)
    _, second = post_keyed(srv, "/steps", body, "press-2")

    assert first["session"] != second["session"]
    assert len(log) > spent


def test_a_key_still_in_flight_is_a_409_rather_than_a_second_run(server, monkeypatch):
    """The second press lands while the first is still proposing.

    Stripe answers a key whose request has not finished with a 409 rather than
    running it again, and so does this: the run is under way and its envelope
    does not exist yet, so there is nothing to replay and nothing to repeat.
    """
    gate = threading.Event()
    log: list = []

    class _Held:
        def __init__(self) -> None:
            self.inner = ScriptedModel(by_marker=scripted().by_marker, calls=log)

        def generate(self, prompt, **kwargs):
            assert gate.wait(timeout=10), "the test never opened the gate"
            return self.inner.generate(prompt, **kwargs)

    monkeypatch.setattr(Handler, "model_factory", staticmethod(_Held))
    body = {"intent": "build me a toy car", "time_limit_s": 5}
    answers: list = []
    first = threading.Thread(
        target=lambda: answers.append(post_keyed(server, "/steps", body, "press-1"))
    )
    first.start()
    # The first request is inside the held model call, so the key is recorded
    # and unanswered -- which is the state this test exists to exercise.
    for _ in range(200):
        with steps._STARTED_LOCK:
            if "press-1" in steps._STARTED:
                break
        time.sleep(0.01)
    else:  # pragma: no cover - the first request never reached the model
        gate.set()
        first.join(timeout=10)
        pytest.fail("the first start never registered its key")

    status, refused = post_keyed(server, "/steps", body, "press-1")
    gate.set()
    first.join(timeout=30)

    assert status == 409
    assert "already starting" in refused["error"]
    assert answers and answers[0][0] == 200
    # One run reached the model and one session exists; the refused press
    # neither proposed nor registered anything.
    assert log, "the held run never called the model"
    assert list(steps._SESSIONS) == [answers[0][1]["session"]]


def test_a_start_that_failed_may_be_started_again_under_the_same_key(server):
    """Nothing was produced, so the key must not lock the caller out."""
    status, body = post_keyed(server, "/steps", {"intent": "   "}, "press-1")
    assert status == 400 and "intent" in body["error"]

    status, ok = post_keyed(
        server, "/steps", {"intent": "build me a toy car", "time_limit_s": 5}, "press-1"
    )
    assert status == 200, ok
    assert ok["stage"] == "proposed"


def test_an_over_long_key_is_a_400_before_any_work(counted):
    srv, log = counted
    status, body = post_keyed(
        srv,
        "/steps",
        {"intent": "build me a toy car", "time_limit_s": 5},
        "k" * (steps.MAX_IDEMPOTENCY_KEY_CHARS + 1),
    )
    assert status == 400 and "Idempotency-Key" in body["error"]
    assert log == []

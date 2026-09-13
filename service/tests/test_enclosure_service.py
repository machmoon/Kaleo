"""The additive ``enclosure`` request opt-in and response key (workstream D).

Workstream C's ``generate_pcb`` kwargs land concurrently, so these tests stub
``generate_pcb`` at the service boundary (the existing convention): the stub
strips the enclosure kwargs, runs the real pipeline with the scripted model,
and reattaches an ``enclosure`` result built from the real result type.
The response shape asserted here is the contract in docs/ai-cad-plan.md; the
v1 ``scad``/``params``/``fit``/``engine`` keys were removed with the OpenSCAD
emitter on 2026-09-08 (plan v3) and ``step`` carries the case instead.
"""

import json
import threading
from types import SimpleNamespace

import pytest
from silkscreen.agents.stages import EnclosureResult
from silkscreen.enclosure.cad import ExportPaths
from silkscreen.enclosure.kernel import Clause, KernelReport

from service.app import Handler, _enclosure_dict, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import post, post_stream, scripted


@pytest.fixture
def server():
    """The scripted-model server, same wiring as test_app's fixture.

    Defined locally rather than imported: ruff reads an imported fixture as an
    unused name that every test then shadows (F811).
    """
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None

REQUEST = {"intent": "a regulator", "time_limit_s": 5}

#: ISO 10303-21 is ASCII and self-contained, which is what lets a whole case
#: ride the one-shot JSON response.
STEP = "ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


class _WithEnclosure:
    """A PipelineResult plus the enclosure attribute C's contract adds."""

    def __init__(self, result, enclosure):
        self._result = result
        self.enclosure = enclosure

    def __getattr__(self, name):
        return getattr(self._result, name)


def _stub_pipeline(monkeypatch, enclosure, seen=None, events=()):
    """Run the real pipeline minus the enclosure kwargs, then reattach."""
    import service.app as app

    real = app.generate_pcb

    def fake(model, intent, **kw):
        kw.pop("enclosure", None)
        kw.pop("enclosure_style", None)
        kw.pop("enclosure_rigorous", None)
        if seen is not None:
            seen.update(kw)
        on_event = kw.get("on_event")
        if on_event is not None:
            for event in events:
                on_event(dict(event))
        return _WithEnclosure(real(model, intent, **kw), enclosure)

    monkeypatch.setattr(app, "generate_pcb", fake)
    return fake


#: The key set of the ``enclosure`` block (docs/ai-cad-plan.md v3). ``brief``
#: joins it only on the steps route.
ENCLOSURE_KEYS = {"step", "warnings", "repair_rounds", "kernel", "files"}

KERNEL = KernelReport(
    clauses=(
        Clause("board_clash", True, 1_000_000, "cavity clears the board by 1.000 mm"),
        Clause("headroom", False, -250_000, "lid 0.250 mm below the tallest part"),
    ),
    warnings=("critic: nothing to report",),
)


def _success_enclosure():
    """The real result type on the one-shot route: STEP text, no files."""
    return EnclosureResult(
        spec=None, step_text=STEP, repair_rounds=1, kernel=KERNEL
    )


def _kernel_enclosure(directory):
    exports = ExportPaths(
        step=directory / "board.step",
        base_stl=directory / "board-base.stl",
        lid_stl=directory / "board-lid.stl",
    )
    return EnclosureResult(
        spec=None, step_text=STEP, repair_rounds=1,
        kernel=KERNEL, exports=exports,
        snapshots=(directory / "board-iso.png", directory / "board-top.png"),
        brief="Board outline: 48.2 x 30.0 mm ...",
    )


def test_enclosure_block_matches_the_frozen_shape(monkeypatch, server):
    _stub_pipeline(monkeypatch, _success_enclosure())
    status, body = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "usb left"}
    )
    assert status == 200
    enclosure = body["enclosure"]
    # Exactly the contract keys -- the one-shot response must never grow raw
    # model output, and an extra key here would be where it leaked.
    assert set(enclosure) == ENCLOSURE_KEYS
    assert enclosure["step"] == STEP
    assert enclosure["warnings"] == ["critic: nothing to report"]
    assert enclosure["repair_rounds"] == 1
    # The kernel report is the only receipt; the one-shot route writes nothing.
    assert enclosure["kernel"]["passed"] is False
    assert enclosure["files"] == {
        "step": None, "base_stl": None, "lid_stl": None, "snapshots": [],
    }


def test_enclosure_dict_kernel_shape_is_exact(tmp_path):
    """The whole block, key for key, for the desktop UI built against it."""
    block = _enclosure_dict(_kernel_enclosure(tmp_path))
    assert block == {
        "step": STEP,
        "warnings": ["critic: nothing to report"],
        "repair_rounds": 1,
        "kernel": {
            "passed": False,
            "clauses": [
                {
                    "name": "board_clash",
                    "passed": True,
                    "margin_mm": 1.0,
                    "detail": "cavity clears the board by 1.000 mm",
                },
                {
                    "name": "headroom",
                    "passed": False,
                    "margin_mm": -0.25,
                    "detail": "lid 0.250 mm below the tallest part",
                },
            ],
            "warnings": ["critic: nothing to report"],
            "thin_band_mm": 0.2,
        },
        "files": {
            "step": str(tmp_path / "board.step"),
            "base_stl": str(tmp_path / "board-base.stl"),
            "lid_stl": str(tmp_path / "board-lid.stl"),
            "snapshots": [
                str(tmp_path / "board-iso.png"), str(tmp_path / "board-top.png")
            ],
        },
    }
    # The brief is opt-in, for the steps route only.
    assert "brief" not in block
    with_brief = _enclosure_dict(_kernel_enclosure(tmp_path), include_brief=True)
    assert set(with_brief) == ENCLOSURE_KEYS | {"brief"}
    assert with_brief["brief"] == "Board outline: 48.2 x 30.0 mm ..."


def test_kernel_block_states_the_thin_margin_band_in_mm(tmp_path):
    """The band a client marks near-zero clauses with is a mechanical number,
    so the engine states it; the client never invents one.

    The expected value is read from ``rules`` and converted independently of
    ``_enclosure_dict``'s own rounding, and it is absent -- not null -- when
    there is no kernel receipt at all.
    """
    from silkscreen.enclosure import rules

    kernel = _enclosure_dict(_kernel_enclosure(tmp_path))["kernel"]
    assert kernel["thin_band_mm"] == pytest.approx(rules.THIN_MARGIN_NM / 1e6)
    assert kernel["thin_band_mm"] == 0.2

    # A stage that produced no report carries no band and no null band.
    no_report = SimpleNamespace(step_text=STEP, repair_rounds=0, kernel=None)
    assert _enclosure_dict(no_report)["kernel"] is None


def test_enclosure_dict_tolerates_a_minimal_result():
    """A consumer-side fake carrying only the required fields serialises."""
    minimal = SimpleNamespace(step_text=STEP, repair_rounds=0)
    block = _enclosure_dict(minimal)
    assert set(block) == ENCLOSURE_KEYS
    assert block["kernel"] is None and block["warnings"] == []
    assert block["files"]["snapshots"] == []


def test_one_shot_route_never_carries_the_brief(monkeypatch, server, tmp_path):
    """Even a kernel-built result reaches /generate without prompt text, and
    its file paths ride as strings so the JSON stays strict."""
    _stub_pipeline(monkeypatch, _kernel_enclosure(tmp_path))
    status, body = post(server, {**REQUEST, "enclosure": True})
    assert status == 200
    enclosure = body["enclosure"]
    assert set(enclosure) == ENCLOSURE_KEYS
    assert "brief" not in enclosure
    assert "Board outline" not in json.dumps(body)
    assert enclosure["kernel"]["passed"] is False
    assert [c["name"] for c in enclosure["kernel"]["clauses"]] == [
        "board_clash", "headroom"
    ]
    assert enclosure["files"]["step"] == str(tmp_path / "board.step")
    json.dumps(body, allow_nan=False)


def test_enclosure_kwargs_reach_the_pipeline(monkeypatch, server):
    import service.app as app

    real = app.generate_pcb
    seen = {}

    def fake(model, intent, **kw):
        seen["enclosure"] = kw.pop("enclosure", None)
        seen["enclosure_style"] = kw.pop("enclosure_style", None)
        seen["enclosure_rigorous"] = kw.pop("enclosure_rigorous", None)
        return _WithEnclosure(real(model, intent, **kw), _success_enclosure())

    monkeypatch.setattr(app, "generate_pcb", fake)
    status, _ = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "  usb left  "}
    )
    assert status == 200
    assert seen["enclosure"] is True
    assert seen["enclosure_style"] == "usb left"
    # Fast is the wire default: rigor is the caller's opt-in.
    assert seen["enclosure_rigorous"] is False

    status, _ = post(
        server, {**REQUEST, "enclosure": True, "enclosure_rigorous": True}
    )
    assert status == 200
    assert seen["enclosure_rigorous"] is True


def test_enclosure_failure_degrades_to_null_plus_warning(monkeypatch, server):
    """Decision 5: an exhausted repair budget never fails the run."""
    _stub_pipeline(monkeypatch, None)
    status, body = post(server, {**REQUEST, "enclosure": True})
    assert status == 200
    assert body["enclosure"] is None
    assert any("enclosure" in w for w in body["warnings"])
    # The board itself is still the product.
    assert body["kicad_pcb"].startswith("(kicad_pcb")


def test_without_opt_in_the_response_is_unchanged(monkeypatch, server):
    seen = {}
    import service.app as app

    real = app.generate_pcb

    def spy(model, intent, **kw):
        seen.update(kw)
        return real(model, intent, **kw)

    monkeypatch.setattr(app, "generate_pcb", spy)
    status, body = post(server, dict(REQUEST))
    assert status == 200
    assert "enclosure" not in body
    assert "enclosure" not in seen
    assert "enclosure_style" not in seen
    assert "enclosure_rigorous" not in seen


def test_enclosure_must_be_a_boolean(monkeypatch, server):
    import service.app as app

    def untouched(*a, **kw):  # pragma: no cover - only fires on regression
        raise AssertionError("a field-level 400 must not run the pipeline")

    monkeypatch.setattr(app, "generate_pcb", untouched)
    status, body = post(server, {**REQUEST, "enclosure": "yes"})
    assert status == 400
    assert "'enclosure'" in body["error"]


def test_enclosure_rigorous_must_be_a_boolean(monkeypatch, server):
    """The known-issue-10 taxonomy: a field-level 400, before the pipeline."""
    import service.app as app

    def untouched(*a, **kw):  # pragma: no cover - only fires on regression
        raise AssertionError("a field-level 400 must not run the pipeline")

    monkeypatch.setattr(app, "generate_pcb", untouched)
    status, body = post(
        server, {**REQUEST, "enclosure": True, "enclosure_rigorous": "yes"}
    )
    assert status == 400
    assert "'enclosure_rigorous'" in body["error"]


def test_enclosure_style_must_be_a_string(monkeypatch, server):
    import service.app as app

    monkeypatch.setattr(app, "generate_pcb", lambda *a, **kw: 1 / 0)
    status, body = post(server, {**REQUEST, "enclosure": True, "enclosure_style": 7})
    assert status == 400
    assert "'enclosure_style'" in body["error"]


def test_enclosure_style_length_is_capped(monkeypatch, server):
    # The stub wraps the real pipeline first, so the at-limit request below
    # can succeed through it after the over-limit one is refused.
    _stub_pipeline(monkeypatch, None)
    status, body = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "x" * 501}
    )
    assert status == 400
    assert "500" in body["error"]
    # Exactly at the limit is fine (and reaches the pipeline).
    status, _ = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "x" * 500}
    )
    assert status == 200


ENCLOSURE_EVENTS = (
    {"event": "stage.start", "stage": "enclosure"},
    {
        "event": "enclosure.round",
        "round": 1,
        "errors": 2,
        "first_error": "'wall_mm' is 0.4 mm, below the printable FDM minimum",
    },
    {
        "event": "stage.done",
        "stage": "enclosure",
        "cutouts": 1,
        "lid": "lip",
        "wall_mm": 2.0,
        "repair_rounds": 1,
        "rendered": False,
    },
)


def test_stream_forwards_enclosure_events_and_result(monkeypatch, server):
    _stub_pipeline(monkeypatch, _success_enclosure(), events=ENCLOSURE_EVENTS)
    status, _, frames = post_stream(
        server, {**REQUEST, "enclosure": True}, path="/generate/stream"
    )
    assert status == 200
    by_event = [f["event"] for f in frames]
    assert "enclosure.round" in by_event
    starts = [
        f for f in frames
        if f["event"] == "stage.start" and f.get("stage") == "enclosure"
    ]
    assert len(starts) == 1
    done = next(
        f for f in frames
        if f["event"] == "stage.done" and f.get("stage") == "enclosure"
    )
    assert done["lid"] == "lip"
    assert frames[-1]["event"] == "run.done"
    assert frames[-1]["result"]["enclosure"]["step"] == STEP


def test_stream_validation_failure_is_a_400_frame(monkeypatch, server):
    """The shared taxonomy: the stream reports the same field-level 400."""
    status, _, frames = post_stream(
        server,
        {**REQUEST, "enclosure": "yes"},
        path="/generate/stream",
    )
    assert status == 200, "headers are already sent; failure lives in the frames"
    assert frames[-1]["event"] == "run.error"
    assert frames[-1]["status"] == 400
    assert "'enclosure'" in frames[-1]["error"]


def test_real_pipeline_end_to_end(monkeypatch, server):
    """No stub: the request runs C's actual enclosure stage.

    The scripted model answers the frozen ``ENCLOSURE-SPEC v1`` marker with a
    valid spec, so this covers the whole real path -- request opt-in through
    ``generate_pcb(enclosure=True)`` to the stage's measured envelope, the
    kernel's receipt, and this service's response block. It needs the real
    kernel, which is the only enclosure engine, so it gates on the ``cad``
    extra the way the ngspice tests gate on a simulator.
    """
    from silkscreen.enclosure.cad import kernel_available

    if not kernel_available():
        pytest.skip("build123d (the 'cad' extra) is not installed")

    import json as _json

    from silkscreen.agents.model import ScriptedModel

    from service.tests.test_app import CIRCUIT, DATASHEET, REVIEW

    def enclosure_scripted():
        return ScriptedModel(
            by_marker={
                "designing a printed circuit board": _json.dumps(CIRCUIT),
                "reviewing a circuit someone else designed": _json.dumps(REVIEW),
                "reading an electronic component datasheet": _json.dumps(
                    DATASHEET
                ),
                "ENCLOSURE-SPEC v1": _json.dumps(
                    {"wall_mm": 2.0, "lid": "lip", "cutouts": []}
                ),
            }
        )

    monkeypatch.setattr(
        Handler, "model_factory", staticmethod(enclosure_scripted)
    )
    status, body = post(
        server, {**REQUEST, "enclosure": True, "enclosure_style": "rounded"}
    )
    assert status == 200
    enclosure = body["enclosure"]
    assert enclosure is not None
    assert enclosure["step"].startswith("ISO-10303-21;")
    assert "END-ISO-10303-21;" in enclosure["step"]
    assert enclosure["repair_rounds"] == 0
    assert enclosure["kernel"] is not None
    assert {c["name"] for c in enclosure["kernel"]["clauses"]}
    assert enclosure["files"]["step"] is None, "the one-shot route writes nothing"
    # The board is untouched by the addition.
    assert body["kicad_pcb"].startswith("(kicad_pcb")


def test_success_response_is_strict_json(monkeypatch, server):
    """The whole response, enclosure included, survives strict serialisation."""
    _stub_pipeline(monkeypatch, _success_enclosure())
    status, body = post(server, {**REQUEST, "enclosure": True})
    assert status == 200
    json.dumps(body, allow_nan=False)

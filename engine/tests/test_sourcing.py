"""Sourcing: the BOM rows, the model's answer, the datasheet probe, the stage.

ScriptedModel-driven and offline throughout, per the seam in
:mod:`silkscreen.agents.model`. The only sockets opened are to a
``http.server`` on a loopback port this file starts itself; the SSRF guard
that would otherwise refuse loopback is swapped out for exactly those tests
and left in place for the one that checks it refuses the metadata service.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from silkscreen.agents import ModelError, ScriptedModel
from silkscreen.agents import sourcing as sourcing_agent
from silkscreen.agents.sourcing import (
    SOURCING_MARKER,
    SOURCING_PROMPT,
    probe_pdf,
    propose_sourcing,
)
from silkscreen.agents.stages import (
    SourcingJob,
    sourcing_stage,
    start_sourcing_stage,
)
from silkscreen.board import BoardResult, PlacedPart
from silkscreen.footprints import Footprint
from silkscreen.sourcing import (
    CSV_HEADER,
    KINDS,
    SourcingEntry,
    SourcingResult,
    SourcingValidationError,
    bom_csv,
    bom_rows,
    kind_for_ref,
    parse_sourcing_response,
)

# ---------------------------------------------------------------- fixtures

PDF_URL = "https://vendor.example/ds/ams1117.pdf"
HTML_URL = "https://vendor.example/ds/drv8837"


def _board() -> BoardResult:
    """A placed board built by hand: no solver, so the rows are known."""
    parts = [
        PlacedPart("U1", Footprint("SOT-223-3_TabPin2"), value="AMS1117-3.3"),
        PlacedPart("U2", Footprint("SOIC-8"), value="DRV8837"),
        PlacedPart("C1", Footprint("C_1206"), value="22uF"),
        PlacedPart("R1", Footprint("C_0603"), value="10k"),
        PlacedPart("Y1", Footprint("C_1210"), value="8MHz"),
    ]
    return BoardResult(
        parts=parts, nets=[], width_nm=0, height_nm=0, solver_status="FEASIBLE"
    )


def _good_answer() -> str:
    return json.dumps(
        {
            "parts": [
                {
                    "ref": "U1",
                    "manufacturer": "Advanced Monolithic Systems",
                    "mpn": "AMS1117-3.3",
                    "datasheet_url": PDF_URL,
                    "note": None,
                },
                {
                    "ref": "U2",
                    "manufacturer": "Texas Instruments",
                    "mpn": "DRV8837DSGR",
                    "datasheet_url": HTML_URL,
                    "note": "WSON, not SOIC -- check the package",
                },
                {
                    "ref": "C1",
                    "manufacturer": "Samsung",
                    "mpn": "CL31A226KAHNNNE",
                    "datasheet_url": None,
                    "note": None,
                },
                {"ref": "R1", "manufacturer": None, "mpn": None,
                 "datasheet_url": None, "note": None},
                {"ref": "Y1", "manufacturer": None, "mpn": None,
                 "datasheet_url": None, "note": "no 2-pin 1210 crystal known"},
            ]
        }
    )


def _fake_probe(url: str) -> str:
    return "verified" if url == PDF_URL else "not_pdf"


def _model(*responses: str) -> ScriptedModel:
    """Answers in order, so a repair round gets the *second* response."""
    return ScriptedModel(responses=list(responses))


# ---------------------------------------------------------------- rows


def test_kind_follows_the_ref_prefix():
    assert kind_for_ref("R12") == "resistor"
    assert kind_for_ref("C1") == "capacitor"
    assert kind_for_ref("L3") == "inductor"
    assert kind_for_ref("D2") == "diode"
    assert kind_for_ref("Y1") == "crystal"
    assert kind_for_ref("U1") == "device"
    assert kind_for_ref("J4") == "device"
    assert kind_for_ref("r7") == "resistor"


def test_the_new_ir_kinds_reach_the_bom_rather_than_landing_on_device():
    """``netlist.KINDS`` gained ``switch`` and ``testpoint`` and names *this*
    table as the reason they are their own kinds rather than sub-cases of
    ``connector`` -- "sourcing.bom_rows, which reads the ref prefix". The
    table did not gain the rows, so the stated reason was unmet and a button
    and a bare pad both read as ``device``, the same word a microcontroller
    gets. The two vocabularies have to agree or one of them is lying."""
    from silkscreen.netlist import REF_PREFIX

    assert kind_for_ref("SW1") == "switch"
    assert kind_for_ref("TP7") == "testpoint"
    assert kind_for_ref("sw2") == "switch"
    # Every packaged IR kind whose prefix is not one of the passive letters
    # has to survive the round trip prefix -> kind.
    for kind in ("switch", "testpoint"):
        assert kind_for_ref(f"{REF_PREFIX[kind]}1") == kind
        assert kind in KINDS


def test_bom_rows_one_per_part_in_board_order_with_models():
    rows = bom_rows(_board())
    assert [r.ref for r in rows] == ["U1", "U2", "C1", "R1", "Y1"]
    assert [r.kind for r in rows] == [
        "device", "device", "capacitor", "resistor", "crystal"
    ]
    assert rows[0].value == "AMS1117-3.3" and rows[0].package == "SOT-223-3_TabPin2"
    # Statuses say nothing was looked up yet.
    assert all(r.mpn_status == "none" and r.datasheet_status == "none" for r in rows)
    assert rows[0].model3d == "${KISYS3DMOD}/Package_TO_SOT_SMD.3dshapes/SOT-223.step"
    assert rows[2].model3d == (
        "${KISYS3DMOD}/Capacitor_SMD.3dshapes/C_1206_3216Metric.step"
    )
    assert rows[3].model3d == (
        "${KISYS3DMOD}/Resistor_SMD.3dshapes/R_0603_1608Metric.step"
    )
    # A row with no model carries the reason, never a silent None.
    assert rows[4].model3d is None
    assert "crystal" in rows[4].model3d_note
    assert all(r.model3d_note is None for r in rows if r.model3d is not None)


def test_bom_rows_from_a_solved_board():
    """The real emitter's footprint names, through the real placer."""
    from silkscreen.board import build_board
    from silkscreen.netlist import parse_circuit_spec
    from test_agents import GOOD_CIRCUIT

    board = build_board(parse_circuit_spec(json.dumps(GOOD_CIRCUIT)), time_limit_s=5.0)
    rows = bom_rows(board)
    assert [r.ref for r in rows] == [p.ref for p in board.parts]
    by_ref = {r.ref: r for r in rows}
    assert by_ref["U1"].package == "SOT-223-3_TabPin2"
    assert by_ref["U1"].model3d is not None
    assert by_ref["C3"].package == "C_0603"
    assert by_ref["C3"].model3d is not None
    # Every row either has a model or says why not.
    for row in rows:
        assert (row.model3d is None) == (row.model3d_note is not None), row


def test_entry_as_dict_carries_every_field():
    entry = bom_rows(_board())[0]
    d = entry.as_dict()
    assert set(d) == {
        "ref", "value", "kind", "package", "manufacturer", "mpn", "mpn_status",
        "datasheet_url", "datasheet_status", "distributor", "distributor_sku",
        "distributor_url", "verify_error", "model3d", "model3d_note", "note",
    }
    json.dumps(d)


# ---------------------------------------------------------------- parsing


def test_parse_accepts_a_fenced_answer_and_reads_empty_as_null():
    raw = "```json\n" + json.dumps({"parts": [
        {"ref": "U1", "manufacturer": "  ", "mpn": "AMS1117-3.3",
         "datasheet_url": "", "note": "x" * 300},
    ]}) + "\n```"
    found = parse_sourcing_response(raw, ["U1"])
    assert found == {
        "U1": {
            "manufacturer": None,
            "mpn": "AMS1117-3.3",
            "datasheet_url": None,
            "note": "x" * 200,
        }
    }


def test_parse_batches_every_failure_into_one_error():
    raw = json.dumps({"parts": [
        {"ref": "U1", "mpn": 12345, "datasheet_url": "ftp://x/ds.pdf"},
        {"ref": "U1", "mpn": "dup"},
        {"ref": "Q9", "mpn": "nope"},
        {"ref": "C1", "mpn": "m" * 65},
        "not an object",
        {"manufacturer": "no ref"},
    ]})
    with pytest.raises(SourcingValidationError) as info:
        parse_sourcing_response(raw, ["U1", "C1", "R1"])
    errors = info.value.errors
    text = "\n".join(errors)
    assert "U1: \"mpn\" must be a string or null, got int" in text
    assert "ftp://x/ds.pdf" in text and "not an http(s) URL" in text
    assert "'U1' appears more than once" in text
    assert "'Q9' is not a part on this board" in text
    assert "C1: mpn is 65 characters" in text
    assert "parts[4] must be an object" in text
    assert 'parts[5] needs a "ref" string' in text
    assert "R1: missing from the response" in text
    assert len(errors) == 8
    assert isinstance(info.value, ValueError)


def test_parse_rejects_non_json_and_wrong_shapes_as_validation_errors():
    with pytest.raises(SourcingValidationError) as info:
        parse_sourcing_response("Sure! Here are the parts.", ["U1"])
    assert "not JSON" in info.value.errors[0]
    with pytest.raises(SourcingValidationError) as info:
        parse_sourcing_response("[1, 2]", ["U1"])
    assert "JSON object" in info.value.errors[0]
    with pytest.raises(SourcingValidationError) as info:
        parse_sourcing_response(json.dumps({"parts": {"U1": {}}}), ["U1"])
    assert '"parts" must be a list' in info.value.errors[0]


# ---------------------------------------------------------------- csv


def test_bom_csv_is_deterministic_with_the_frozen_header():
    result = propose_sourcing(_model(_good_answer()), bom_rows(_board()),
                              probe=_fake_probe)
    text = bom_csv(result)
    assert text == bom_csv(result)
    lines = text.split("\n")
    assert lines[0] == ",".join(CSV_HEADER)
    assert lines[0] == (
        "ref,value,kind,package,manufacturer,mpn,mpn_status,"
        "datasheet_url,datasheet_status,model3d"
    )
    assert lines[-1] == ""  # trailing newline, "\n" line ends
    assert "\r" not in text
    assert lines[1].startswith("U1,AMS1117-3.3,device,SOT-223-3_TabPin2,")
    assert f"AMS1117-3.3,proposed,{PDF_URL},verified," in lines[1]
    # None is an empty cell, never the word None.
    assert lines[4].startswith("R1,10k,resistor,C_0603,,,none,,none,")
    assert "None" not in text


def test_bom_csv_quotes_a_comma_in_a_value():
    row = SourcingEntry(ref="U1", value="a, b", kind="device", package="SOIC-8")
    text = bom_csv(SourcingResult([row]))
    assert '"a, b"' in text
    assert text.count("\n") == 2


# ---------------------------------------------------------------- proposal


def test_propose_fills_statuses_and_probes_only_proposed_urls():
    model = _model(_good_answer())
    probed: list[str] = []

    def probe(url: str) -> str:
        probed.append(url)
        return _fake_probe(url)

    events: list[dict] = []
    result = propose_sourcing(
        model, bom_rows(_board()), probe=probe, on_event=events.append
    )

    assert len(model.calls) == 1
    prompt = model.calls[0]["prompt"]
    assert SOURCING_MARKER in prompt and SOURCING_MARKER in SOURCING_PROMPT
    assert "null beats invention" in prompt
    for ref in ("U1", "U2", "C1", "R1", "Y1"):
        assert f"- {ref}:" in prompt
    assert "package=SOT-223-3_TabPin2" in prompt

    assert sorted(probed) == sorted([PDF_URL, HTML_URL])
    by_ref = {e.ref: e for e in result.parts}
    assert by_ref["U1"].mpn == "AMS1117-3.3"
    assert by_ref["U1"].mpn_status == "proposed"
    assert by_ref["U1"].datasheet_status == "verified"
    assert by_ref["U1"].manufacturer == "Advanced Monolithic Systems"
    assert by_ref["U2"].datasheet_status == "not_pdf"
    assert by_ref["U2"].note == "WSON, not SOIC -- check the package"
    assert by_ref["C1"].mpn_status == "proposed"
    assert by_ref["C1"].datasheet_status == "none"
    assert by_ref["R1"].mpn_status == "none" and by_ref["R1"].mpn is None
    assert by_ref["Y1"].mpn_status == "none"
    assert by_ref["Y1"].note == "no 2-pin 1210 crystal known"
    # What the board knew survives the model's answer.
    assert by_ref["U1"].model3d == bom_rows(_board())[0].model3d
    assert by_ref["Y1"].model3d_note is not None

    assert result.verified == 1 and result.proposed == 3 and result.unresolved == 2
    assert result.warnings == []
    d = result.as_dict()
    assert (d["verified"], d["proposed"], d["unresolved"]) == (1, 3, 2)
    assert [p["ref"] for p in d["parts"]] == ["U1", "U2", "C1", "R1", "Y1"]

    assert [e["event"] for e in events] == ["sourcing.part"] * 5
    assert events[0] == {
        "event": "sourcing.part", "ref": "U1",
        "mpn_status": "proposed", "datasheet_status": "verified",
    }
    for event in events:
        for value in event.values():
            assert not (isinstance(value, str) and len(value) > 200)


def test_propose_repairs_once_with_the_batched_errors():
    bad = json.dumps({"parts": [
        {"ref": "U1", "mpn": "AMS1117-3.3", "datasheet_url": "ftp://x"},
        {"ref": "Q9", "mpn": "x"},
    ]})
    model = _model(bad, _good_answer())
    events: list[dict] = []
    result = propose_sourcing(
        model, bom_rows(_board()), probe=_fake_probe, on_event=events.append
    )
    assert len(model.calls) == 2
    repair = model.calls[1]["prompt"]
    assert SOURCING_MARKER in repair
    assert "Fix ALL of these" in repair
    assert "ftp://x" in repair and "'Q9' is not a part" in repair
    assert "U2: missing from the response" in repair
    assert result.warnings == []
    assert result.proposed == 3
    rounds = [e for e in events if e["event"] == "sourcing.round"]
    assert len(rounds) == 1
    # A bad URL, an unknown ref, and four refs the answer left out.
    assert rounds[0]["round"] == 1 and rounds[0]["errors"] == 6


def test_propose_gives_up_honestly_after_the_budget():
    model = _model("not json", "still not json", _good_answer())
    events: list[dict] = []
    rows = bom_rows(_board())
    result = propose_sourcing(
        model, rows, probe=_fake_probe, max_repairs=1, on_event=events.append
    )
    # Two attempts, never the third that would have succeeded.
    assert len(model.calls) == 2
    assert result.parts == rows
    assert result.proposed == 0 and result.verified == 0
    assert result.unresolved == len(rows)
    assert len(result.warnings) == 1
    assert "not sourced" in result.warnings[0]
    assert "2 attempt(s)" in result.warnings[0]
    assert "not JSON" in result.warnings[0]
    assert [e["event"] for e in events] == (
        ["sourcing.round", "sourcing.round"] + ["sourcing.part"] * 5
    )
    assert all(
        e["mpn_status"] == "none" and e["datasheet_status"] == "none"
        for e in events if e["event"] == "sourcing.part"
    )


def test_propose_lets_a_model_outage_propagate():
    with pytest.raises(ModelError):
        propose_sourcing(ScriptedModel(), bom_rows(_board()), probe=_fake_probe)


def test_propose_with_no_rows_never_calls_the_model():
    model = ScriptedModel()
    result = propose_sourcing(model, [], probe=_fake_probe)
    assert result.parts == [] and result.warnings == []
    assert model.calls == []


def test_a_probe_outside_the_vocabulary_is_a_programming_error():
    with pytest.raises(ValueError, match="expected one of"):
        propose_sourcing(
            _model(_good_answer()), bom_rows(_board()), probe=lambda url: "ok"
        )


# ---------------------------------------------------------------- probe_pdf


class _Datasheets(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 -- http.server's spelling
        if self.path == "/ds.pdf":
            body = b"%PDF-1.4\n" + b"x" * 4096
            ctype = "application/pdf"
        elif self.path == "/page.pdf":
            body = b"<!doctype html><html><body>viewer</body></html>"
            ctype = "text/html"
        elif self.path == "/moved.pdf":
            self.send_response(302)
            self.send_header("Location", "/ds.pdf")
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server(monkeypatch):
    """A loopback http.server, with the SSRF guard stood down for it.

    The guard refuses every non-global address by design, loopback included;
    these tests are about what the probe makes of the bytes, so the guard is
    replaced for their duration and exercised on its own below.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Datasheets)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(sourcing_agent, "_validate_url", lambda url: None)
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_probe_reports_a_pdf_as_verified(local_server):
    assert probe_pdf(f"{local_server}/ds.pdf", timeout_s=5.0) == "verified"


def test_probe_reports_an_html_viewer_page_as_not_pdf(local_server):
    assert probe_pdf(f"{local_server}/page.pdf", timeout_s=5.0) == "not_pdf"


def test_probe_follows_a_redirect_to_the_pdf(local_server):
    assert probe_pdf(f"{local_server}/moved.pdf", timeout_s=5.0) == "verified"


def test_probe_reports_a_404_as_unreachable(local_server):
    assert probe_pdf(f"{local_server}/missing.pdf", timeout_s=5.0) == "unreachable"


def test_probe_reports_a_closed_port_as_unreachable(monkeypatch):
    monkeypatch.setattr(sourcing_agent, "_validate_url", lambda url: None)
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    assert probe_pdf(f"http://127.0.0.1:{port}/ds.pdf", timeout_s=2.0) == "unreachable"


def test_probe_refuses_the_metadata_service_and_bad_urls_without_raising():
    """The real guard: a link-local literal never gets a connection."""
    assert probe_pdf("http://169.254.169.254/latest/meta-data/") == "unreachable"
    assert probe_pdf("http://127.0.0.1/ds.pdf") == "unreachable"
    assert probe_pdf("ftp://vendor.example/ds.pdf") == "unreachable"
    assert probe_pdf("not a url") == "unreachable"
    assert probe_pdf("") == "unreachable"


# ---------------------------------------------------------------- stage + job


def test_sourcing_stage_emits_open_parts_close_with_counts():
    events: list[dict] = []
    entered: list[str] = []
    result = sourcing_stage(
        _model(_good_answer()), _board(),
        emit=events.append, enter=entered.append, probe=_fake_probe,
    )
    assert entered == ["sourcing"]
    assert [e["event"] for e in events] == (
        ["stage.start"] + ["sourcing.part"] * 5 + ["stage.done"]
    )
    assert events[0] == {"event": "stage.start", "stage": "sourcing"}
    assert events[-1] == {
        "event": "stage.done", "stage": "sourcing", "parts": 5,
        "verified": 1, "proposed": 3, "unresolved": 2,
    }
    assert result.verified == 1


def test_sourcing_stage_never_fails_the_run_on_a_value_error():
    events: list[dict] = []
    board = _board()
    result = sourcing_stage(
        _model(_good_answer()), board,
        emit=events.append, enter=lambda s: None, probe=lambda url: "bogus",
    )
    assert [e["event"] for e in events] == ["stage.start", "sourcing.failed"]
    assert "bogus" in events[1]["error"]
    assert result.parts == bom_rows(board)
    assert len(result.warnings) == 1 and "not sourced" in result.warnings[0]
    assert result.proposed == 0


def test_sourcing_stage_lets_a_model_outage_propagate():
    with pytest.raises(ModelError):
        sourcing_stage(
            ScriptedModel(), _board(),
            emit=lambda e: None, enter=lambda s: None, probe=_fake_probe,
        )


def test_sourcing_job_joins_to_the_same_result_as_the_stage_in_line():
    events: list[dict] = []
    entered: list[str] = []
    lock = threading.Lock()

    def emit(event):
        with lock:
            events.append(event)

    job = start_sourcing_stage(
        _model(_good_answer()), _board(),
        emit=emit, enter=entered.append, probe=_fake_probe,
    )
    assert isinstance(job, SourcingJob)
    result = job.result()
    assert not job.running
    assert result is not None
    assert result.as_dict() == sourcing_stage(
        _model(_good_answer()), _board(),
        emit=lambda e: None, enter=lambda s: None, probe=_fake_probe,
    ).as_dict()
    assert entered == ["sourcing"]
    assert events[0] == {"event": "stage.start", "stage": "sourcing"}
    assert events[-1]["event"] == "stage.done"
    # Joining twice is the same answer, not a second run.
    assert job.result() is result


def test_sourcing_job_reraises_a_model_error_at_the_join():
    job = start_sourcing_stage(
        ScriptedModel(), _board(),
        emit=lambda e: None, enter=lambda s: None, probe=_fake_probe,
    )
    with pytest.raises(ModelError):
        job.result()
    assert not job.running


def test_sourcing_job_reraises_a_callback_that_hung_up():
    class Gone(RuntimeError):
        pass

    def hang_up(event):
        if event["event"] == "stage.start":
            raise Gone("client disconnected")

    job = start_sourcing_stage(
        _model(_good_answer()), _board(),
        emit=hang_up, enter=lambda s: None, probe=_fake_probe,
    )
    with pytest.raises(Gone):
        job.result()


def test_a_never_started_job_settles_to_none():
    job = SourcingJob()
    assert not job.running
    assert job.result() is None


def test_sourcing_job_daemon_flag_marks_the_thread():
    job = start_sourcing_stage(
        _model(_good_answer()), _board(),
        emit=lambda e: None, enter=lambda s: None, probe=_fake_probe, daemon=True,
    )
    assert job._thread.daemon
    job.wait()


# ---------------------------------------------------------------- probe budget


@pytest.fixture(autouse=True)
def _no_distributor_key(monkeypatch):
    """A developer's real key must never turn these tests into network calls."""
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)


def test_probe_datasheets_runs_a_few_at_a_time_under_one_budget():
    import time

    started = time.monotonic()
    ticks: list[float] = []

    def slow(url: str) -> str:
        ticks.append(time.monotonic() - started)
        time.sleep(0.3)
        return "verified"

    urls = [f"https://vendor.example/{i}.pdf" for i in range(4)]
    statuses, warnings = sourcing_agent.probe_datasheets(
        urls, slow, budget_s=5.0, workers=4
    )
    assert statuses == {u: "verified" for u in urls}
    assert warnings == []
    # Four probes ran together, not one after the other (1.2 s sequential).
    assert len(ticks) == 4
    assert time.monotonic() - started < 0.9


def test_probe_datasheets_marks_the_rows_the_budget_never_reached_unprobed():
    import threading

    release = threading.Event()

    def stuck(url: str) -> str:
        if url.endswith("/fast.pdf"):
            return "verified"
        release.wait(timeout=5.0)
        return "verified"

    urls = ["https://vendor.example/fast.pdf", "https://vendor.example/slow.pdf"]
    try:
        statuses, warnings = sourcing_agent.probe_datasheets(
            urls, stuck, budget_s=0.2, workers=2
        )
    finally:
        release.set()
    assert statuses == {urls[0]: "verified", urls[1]: "unprobed"}
    assert warnings == [
        "datasheet probes stopped after 0.2 s: 1 of 2 URL(s) left unprobed"
    ]


def test_probe_datasheets_asks_each_distinct_url_once():
    seen: list[str] = []

    def probe(url: str) -> str:
        seen.append(url)
        return "not_pdf"

    statuses, _ = sourcing_agent.probe_datasheets(
        [PDF_URL, PDF_URL, HTML_URL], probe, budget_s=1.0
    )
    assert sorted(seen) == sorted([PDF_URL, HTML_URL])
    assert statuses == {PDF_URL: "not_pdf", HTML_URL: "not_pdf"}


def test_propose_marks_over_budget_datasheets_unprobed_and_warns():
    import threading

    release = threading.Event()

    def stuck(url: str) -> str:
        if url == PDF_URL:
            return "verified"
        release.wait(timeout=5.0)
        return "not_pdf"

    try:
        result = propose_sourcing(
            _model(_good_answer()), bom_rows(_board()), probe=stuck, budget_s=0.2
        )
    finally:
        release.set()
    by_ref = {e.ref: e for e in result.parts}
    assert by_ref["U1"].datasheet_status == "verified"
    assert by_ref["U2"].datasheet_status == "unprobed"
    assert by_ref["U2"].mpn_status == "proposed"  # the MPN is unaffected
    assert by_ref["C1"].datasheet_status == "none"  # no URL was proposed at all
    assert len(result.warnings) == 1
    assert "stopped after 0.2 s" in result.warnings[0]
    assert "1 of 2" in result.warnings[0]
    assert "unprobed" in bom_csv(result)


# ---------------------------------------------------------------- distributor


def test_propose_without_a_key_asks_no_distributor_and_stays_proposed():
    result = propose_sourcing(_model(_good_answer()), bom_rows(_board()),
                              probe=_fake_probe)
    assert {e.mpn_status for e in result.parts} == {"proposed", "none"}
    assert all(e.verify_error is None for e in result.parts)
    assert all(e.distributor is None for e in result.parts)
    assert result.confirmed == 0
    assert result.as_dict()["confirmed"] == 0


def test_propose_verifies_with_a_fake_and_stops_after_unavailable():
    from silkscreen.agents.distributor import Verdict

    asked: list[str] = []

    def verify(manufacturer, mpn):
        asked.append(mpn)
        if mpn == "AMS1117-3.3":
            return Verdict("verified", manufacturer="AMS", mpn=mpn,
                           sku="511-AMS1117", url="https://www.mouser.com/p/1")
        if mpn == "DRV8837DSGR":
            return Verdict("unlisted")
        return Verdict("unavailable", detail="Mouser answered HTTP 429")

    events: list[dict] = []
    result = propose_sourcing(
        _model(_good_answer()), bom_rows(_board()),
        probe=_fake_probe, verify=verify, on_event=events.append,
    )
    assert asked == ["AMS1117-3.3", "DRV8837DSGR", "CL31A226KAHNNNE"]
    by_ref = {e.ref: e for e in result.parts}
    assert by_ref["U1"].mpn_status == "verified"
    assert by_ref["U1"].distributor == "Mouser"
    assert by_ref["U1"].distributor_sku == "511-AMS1117"
    assert by_ref["U1"].manufacturer == "Advanced Monolithic Systems"
    assert by_ref["U2"].mpn_status == "proposed"
    assert by_ref["U2"].verify_error == "Mouser does not list DRV8837DSGR"
    assert by_ref["C1"].mpn_status == "proposed"
    assert by_ref["C1"].verify_error == "Mouser answered HTTP 429"
    assert by_ref["R1"].verify_error is None
    assert result.confirmed == 1 and result.proposed == 3 and result.unresolved == 2
    assert result.warnings == [
        "part numbers were not verified past C1: Mouser answered HTTP 429"
    ]
    parts = [e for e in events if e["event"] == "sourcing.part"]
    assert parts[0]["mpn_status"] == "verified"
    text = bom_csv(result)
    assert "AMS1117-3.3,verified," in text


def test_propose_reads_the_verifier_from_the_environment(monkeypatch):
    from silkscreen.agents import distributor

    monkeypatch.setenv("MOUSER_API_KEY", "k")
    calls: list[str] = []

    def answer(self, manufacturer, mpn):
        calls.append(mpn)
        return distributor.Verdict("unlisted")

    monkeypatch.setattr(distributor.MouserClient, "__call__", answer)
    result = propose_sourcing(_model(_good_answer()), bom_rows(_board()),
                              probe=_fake_probe)
    assert calls == ["AMS1117-3.3", "DRV8837DSGR", "CL31A226KAHNNNE"]
    assert all(
        e.verify_error and e.verify_error.startswith("Mouser does not list")
        for e in result.parts if e.mpn is not None
    )


# ---------------------------------------------------------------- grouped csv


def test_grouped_bom_collapses_identical_parts_with_a_quantity():
    from silkscreen.sourcing import GROUPED_CSV_HEADER, grouped_bom_csv

    rows = [
        SourcingEntry("R1", "10k", "resistor", "C_0603", manufacturer="Yageo",
                      mpn="RC0603FR-0710KL", mpn_status="proposed"),
        SourcingEntry("C1", "100nF", "capacitor", "C_0603"),
        SourcingEntry("R2", "10k", "resistor", "C_0603", manufacturer="Yageo",
                      mpn="RC0603FR-0710KL", mpn_status="verified",
                      distributor="Mouser", distributor_sku="603-RC0603FR-0710KL"),
        SourcingEntry("R3", "10k", "resistor", "C_0805"),
        SourcingEntry("C2", "100nF", "capacitor", "C_0603"),
        SourcingEntry("U1", "a, b", "device", "SOIC-8"),
    ]
    text = grouped_bom_csv(SourcingResult(rows))
    assert text == grouped_bom_csv(SourcingResult(rows))
    lines = text.split("\n")
    assert lines[0] == ",".join(GROUPED_CSV_HEADER) == (
        "designators,qty,value,package,manufacturer,mpn,mpn_status,distributor_sku,dnp"
    )
    # Board order of first appearance; a mixed group is only "proposed".
    assert lines[1] == (
        "R1 R2,2,10k,C_0603,Yageo,RC0603FR-0710KL,proposed,603-RC0603FR-0710KL,"
    )
    assert lines[2] == "C1 C2,2,100nF,C_0603,,,none,,"
    assert lines[3] == "R3,1,10k,C_0805,,,none,,"
    assert lines[4] == 'U1,1,"a, b",SOIC-8,,,none,,'
    assert lines[5] == ""
    assert "None" not in text and "\r" not in text


def test_grouped_bom_is_verified_only_when_every_member_is():
    from silkscreen.sourcing import grouped_bom_csv

    rows = [
        SourcingEntry("R1", "10k", "resistor", "C_0603", mpn="X",
                      mpn_status="verified", distributor_sku="1"),
        SourcingEntry("R2", "10k", "resistor", "C_0603", mpn="X",
                      mpn_status="verified", distributor_sku="1"),
    ]
    assert "R1 R2,2,10k,C_0603,,X,verified,1," in grouped_bom_csv(SourcingResult(rows))
    assert grouped_bom_csv(SourcingResult([])).count("\n") == 1

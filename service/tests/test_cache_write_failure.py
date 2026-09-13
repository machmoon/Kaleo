"""A cache that refuses the write must not throw away a finished run.

Both write sites sit on the far side of the expensive half: ``/generate``
stores the datasheet facts after ``generate_pcb`` has read datasheets, called
the model, solved, routed and reviewed; ``POST /steps`` stores them after the
read stage has already spent its calls. An unguarded ``store.put`` there turns
a Firestore hiccup into a 500 and a board nobody gets -- the caller pays twice
for the same design, and the second attempt hits the same cache.

The read half already had the rule (an unreadable entry is a miss, not a failed
request). These tests hold the write half to it, and to the repo's other rule:
the degraded path is named in ``warnings``, never silent.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from service import steps
from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import scripted, url

PART = "AMS1117-3.3"
DATASHEETS = {PART: "https://x/a.pdf"}


class BrokenStore(MemoryFactStore):
    """Reads like an empty cache; refuses every write, the way Firestore can.

    ``get`` still answers so the request takes the read path and produces the
    facts that are then written -- a store that failed on both sides would test
    a different bug.
    """

    def put(self, part_number, facts):
        raise RuntimeError("firestore unavailable: DEADLINE_EXCEEDED")


@pytest.fixture
def generate_server(offline_pdf_fetch):
    Handler.model_factory = staticmethod(scripted)
    Handler.store = BrokenStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None


@pytest.fixture
def steps_server(offline_pdf_fetch, tmp_path, monkeypatch):
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "missing-kicad-cli"))
    monkeypatch.setattr(steps, "PROBE", lambda url: "not_pdf")
    steps.reset_sessions()
    Handler.model_factory = staticmethod(scripted)
    Handler.store = BrokenStore()
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_generate_survives_a_failing_cache_write(generate_server):
    status, body = post(
        generate_server,
        "/generate",
        {"intent": "a regulator", "datasheets": DATASHEETS, "time_limit_s": 5},
    )
    assert status == 200, body
    # The whole point: the paid product is still in the response.
    assert body["kicad_pcb"].startswith("(kicad_pcb")
    assert body["parts"]
    assert [d["part"] for d in body["datasheets"]] == [PART]


def test_generate_names_the_failed_cache_write_in_warnings(generate_server):
    _, body = post(
        generate_server,
        "/generate",
        {"intent": "a regulator", "datasheets": DATASHEETS, "time_limit_s": 5},
    )
    warning = [w for w in body["warnings"] if "not cached" in w]
    assert len(warning) == 1, body["warnings"]
    # Which part, and why -- an unattributed "cache error" is not a warning
    # anyone can act on.
    assert PART in warning[0]
    assert "DEADLINE_EXCEEDED" in warning[0]


def test_a_working_cache_adds_no_warning(offline_pdf_fetch):
    """The guard must not make a healthy run look degraded."""
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, body = post(
            srv, "/generate",
            {"intent": "a regulator", "datasheets": DATASHEETS, "time_limit_s": 5},
        )
        assert not [w for w in body["warnings"] if "not cached" in w]
        assert Handler.store.get(PART) is not None
    finally:
        srv.shutdown()
        srv.server_close()
        Handler.store = None


def test_the_first_step_survives_a_failing_cache_write(steps_server):
    status, body = post(
        steps_server,
        "/steps",
        {"intent": "a regulator", "datasheets": DATASHEETS, "time_limit_s": 5},
    )
    assert status == 200, body
    # The read and the propose both happened; the session is live and the
    # schematic is on disk, which is what the step promised.
    assert body["stage"] == "proposed" and body["parts"] > 0
    assert body["session"] and body["files"]["schematic"]
    warning = [w for w in body.get("warnings", []) if "not cached" in w]
    assert len(warning) == 1, body.get("warnings")
    assert PART in warning[0] and "DEADLINE_EXCEEDED" in warning[0]


def test_a_healthy_first_step_reports_no_warnings(steps_server):
    Handler.store = MemoryFactStore()
    status, body = post(
        steps_server,
        "/steps",
        {"intent": "a regulator", "datasheets": DATASHEETS, "time_limit_s": 5},
    )
    assert status == 200, body
    assert not body.get("warnings")

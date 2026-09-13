"""POST /desk/resolve, driven over a real socket with a scripted model.

Mirrors test_transcribe_route.py: a ThreadingHTTPServer on an ephemeral
port with the Handler's desk factory swapped for a ScriptedModel, so the
whole route -- body parsing, validation, the agents call, the error
taxonomy -- runs offline with no key.
"""

from __future__ import annotations

import base64
import http.client
import json
import threading

import pytest
from silkscreen.agents.desk import DESK_MARKER
from silkscreen.agents.model import ScriptedModel

from service.app import DESK_MAX_BODY_BYTES, Handler, desk_request, make_server

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PNG_B64 = base64.b64encode(PNG).decode()

CAPTION = "That blocker is the missing decoupling on C1."

ANSWER = json.dumps(
    {
        "caption": f"  {CAPTION} \n",
        "abstain": False,
        "target": {
            "testid": "finding-card",
            "attrs": {"sev": "blocker"},
            "tab": "review",
        },
    }
)


def scripted():
    return ScriptedModel(by_marker={DESK_MARKER: ANSWER})


@pytest.fixture
def server():
    previous = Handler.__dict__["desk_model_factory"]
    Handler.desk_model_factory = staticmethod(scripted)
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.desk_model_factory = previous


def url(srv, path="/desk/resolve"):
    return f"http://127.0.0.1:{srv.server_port}{path}"


def post(srv, payload):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url(srv),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def body(**extra):
    return {
        "png_b64": PNG_B64,
        "utterance": "what's this",
        "cursor_x": 100.4,
        "cursor_y": 200.6,
        "width": 1512,
        "height": 982,
        "candidates": [
            {"testid": "finding-card", "attrs": {"sev": "blocker"}, "tab": "review"}
        ],
        **extra,
    }


# ------------------------------------------------------------- happy path


def test_desk_resolve_returns_caption_and_target(server):
    status, resp = post(server, body())
    assert status == 200
    assert resp["caption"] == CAPTION
    assert resp["abstain"] is False
    assert resp["target"] == {
        "testid": "finding-card",
        "attrs": {"sev": "blocker"},
        "tab": "review",
    }
    assert "model" in resp


def test_desk_resolve_uses_the_cheap_factory_and_is_paced(server):
    from service.app import DESK_QUOTA_RPM

    class RecordingPacer:
        def __init__(self):
            self.calls = []

        def wait(self, rpm, *, on_wait=None):
            del on_wait
            self.calls.append(rpm)
            return 0.0

    class NamedScripted(ScriptedModel):
        model = "gemini-desk-test"

        def __init__(self):
            super().__init__(by_marker={DESK_MARKER: ANSWER})

    cheap = NamedScripted()
    primary_calls = []

    def primary():
        primary_calls.append(1)
        return ScriptedModel()

    pacer = RecordingPacer()
    previous_pacer = Handler.request_pacer
    previous_cheap = Handler.__dict__["desk_model_factory"]
    previous_primary = Handler.model_factory
    Handler.request_pacer = pacer
    Handler.model_factory = staticmethod(primary)
    Handler.desk_model_factory = staticmethod(lambda: cheap)
    try:
        status, resp = post(server, body())
        paced_status, _ = post(server, {**body(), "quota_rpm": 6})
        bad_status, bad = post(server, {**body(), "quota_rpm": 20})
    finally:
        Handler.request_pacer = previous_pacer
        Handler.desk_model_factory = previous_cheap
        Handler.model_factory = previous_primary

    assert status == 200, resp
    assert resp["caption"] == CAPTION
    assert resp["model"] == "gemini-desk-test"
    assert len(cheap.calls) == 2
    assert primary_calls == []
    assert paced_status == 200
    assert pacer.calls == [DESK_QUOTA_RPM, 6]
    assert bad_status == 400 and "quota_rpm" in bad["error"]


def test_abstain_is_forwarded(server):
    previous = Handler.__dict__["desk_model_factory"]
    Handler.desk_model_factory = staticmethod(
        lambda: ScriptedModel(
            by_marker={
                DESK_MARKER: json.dumps(
                    {
                        "caption": "I cannot tell what you are pointing at.",
                        "abstain": True,
                        "target": None,
                    }
                )
            }
        )
    )
    try:
        status, resp = post(server, body())
    finally:
        Handler.desk_model_factory = previous
    assert status == 200
    assert resp["abstain"] is True
    assert resp["target"] is None


# ------------------------------------------------------------- validation


def test_missing_png_is_a_400_naming_the_field(server):
    payload = body()
    del payload["png_b64"]
    status, resp = post(server, payload)
    assert status == 400
    assert "png_b64" in resp["error"]


def test_undecodable_base64_is_a_400_naming_the_field(server):
    status, resp = post(server, body(png_b64="not base64!!!"))
    assert status == 400
    assert "png_b64" in resp["error"]


def test_non_png_bytes_are_a_400(server):
    status, resp = post(server, body(png_b64=base64.b64encode(b"RIFF").decode()))
    assert status == 400
    assert "PNG" in resp["error"]


def test_missing_utterance_is_a_400(server):
    payload = body()
    del payload["utterance"]
    status, resp = post(server, payload)
    assert status == 400
    assert "utterance" in resp["error"]


def test_missing_cursor_is_a_400(server):
    payload = body()
    del payload["cursor_x"]
    status, resp = post(server, payload)
    assert status == 400
    assert "cursor_x" in resp["error"]


def test_oversize_body_is_a_413(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        conn.request(
            "POST",
            "/desk/resolve",
            body=b"",
            headers={"Content-Length": str(DESK_MAX_BODY_BYTES + 1)},
        )
        resp = conn.getresponse()
        assert resp.status == 413
        assert json.loads(resp.read()) == {"error": "request body too large"}
    finally:
        conn.close()


def test_empty_png_is_refused_as_required():
    with pytest.raises(ValueError, match="png_b64"):
        desk_request({"png_b64": "", "utterance": "x", "cursor_x": 0, "cursor_y": 0})


def test_padding_only_base64_is_refused_as_invalid():
    with pytest.raises(ValueError, match="not valid base64"):
        desk_request(
            {
                "png_b64": "====",
                "utterance": "x",
                "cursor_x": 0,
                "cursor_y": 0,
            }
        )


# ---------------------------------------------------------- model failure


def test_model_failure_is_a_502(server):
    exhausted = ScriptedModel()
    previous = Handler.__dict__["desk_model_factory"]
    Handler.desk_model_factory = staticmethod(lambda: exhausted)
    try:
        status, resp = post(server, body())
    finally:
        Handler.desk_model_factory = previous
    assert status == 502
    assert "error" in resp


def test_bad_model_json_is_a_502(server):
    previous = Handler.__dict__["desk_model_factory"]
    Handler.desk_model_factory = staticmethod(
        lambda: ScriptedModel(by_marker={DESK_MARKER: "not json"})
    )
    try:
        status, resp = post(server, body())
    finally:
        Handler.desk_model_factory = previous
    assert status == 502
    assert "error" in resp

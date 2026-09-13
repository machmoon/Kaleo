"""POST /transcribe, driven over a real socket with a scripted model.

Mirrors test_app.py's harness: a ThreadingHTTPServer on an ephemeral port with
the Handler's model factory swapped for a ScriptedModel, so the whole route --
body parsing, validation, the agents call, the error taxonomy -- runs offline
with no key.
"""

import base64
import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest
from silkscreen.agents.model import ScriptedModel

from service.app import MAX_BODY_BYTES, Handler, make_server, transcribe_request

TRANSCRIPT = "a 3.3 volt LDO board with USB-C input"

#: The stable prompt marker transcribe.py sends; pinned in
#: engine/tests/test_transcribe.py as well.
MARKER = "transcribing a spoken request"


def scripted():
    return ScriptedModel(by_marker={MARKER: f"  {TRANSCRIPT}\n"})


@pytest.fixture
def server():
    # The route has its own factory (the cheap single-provider tier), not the
    # board pipeline's failover ladder; swapping model_factory would leave the
    # real one in place and every test below would answer 502 with no key.
    previous = Handler.__dict__["transcribe_model_factory"]
    Handler.transcribe_model_factory = staticmethod(scripted)
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.transcribe_model_factory = previous


def url(srv, path="/transcribe"):
    return f"http://127.0.0.1:{srv.server_port}{path}"


def post(srv, payload):
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


def body(mime_type="audio/wav", audio=b"RIFF fake audio", **extra):
    return {
        "audio_b64": base64.b64encode(audio).decode(),
        "mime_type": mime_type,
        **extra,
    }


# ------------------------------------------------------------- happy path


def test_transcribe_returns_the_stripped_transcript(server):
    status, resp = post(server, body())
    assert status == 200
    assert resp["text"] == TRANSCRIPT
    # A ScriptedModel has no model id to report; the key is still present so
    # the response shape does not depend on which model class answered.
    assert "model" in resp


def test_language_hint_is_optional_and_forwarded(server):
    status, resp = post(server, body(language="en"))
    assert status == 200
    assert resp["text"] == TRANSCRIPT


def test_codec_parameter_on_the_mime_type_is_accepted(server):
    """Chromium's MediaRecorder reports audio/webm;codecs=opus verbatim."""
    status, resp = post(server, body(mime_type="audio/webm;codecs=opus"))
    assert status == 200
    assert resp["text"] == TRANSCRIPT


# ------------------------------------------------------------- validation


def test_missing_audio_is_a_400_naming_the_field(server):
    status, resp = post(server, {"mime_type": "audio/wav"})
    assert status == 400
    assert "audio_b64" in resp["error"]


def test_undecodable_base64_is_a_400_naming_the_field(server):
    status, resp = post(
        server, {"audio_b64": "not base64!!!", "mime_type": "audio/wav"}
    )
    assert status == 400
    assert "audio_b64" in resp["error"]


def test_missing_mime_type_is_a_400_naming_the_field(server):
    status, resp = post(server, {"audio_b64": base64.b64encode(b"x").decode()})
    assert status == 400
    assert "mime_type" in resp["error"]


def test_non_audio_mime_type_is_a_400_listing_the_allowed_types(server):
    status, resp = post(server, body(mime_type="video/mp4"))
    assert status == 400
    assert "mime_type" in resp["error"]
    assert "audio/wav" in resp["error"]


def test_non_string_language_is_a_400(server):
    status, resp = post(server, body(language=7))
    assert status == 400
    assert "language" in resp["error"]


def test_unknown_purpose_is_a_400_naming_the_field(server):
    status, resp = post(server, body(purpose="spot"))
    assert status == 400
    assert "purpose" in resp["error"]


def test_wake_purpose_is_accepted(server):
    status, resp = post(server, body(purpose="wake"))
    assert status == 200
    assert resp["text"] == TRANSCRIPT


def test_oversize_body_is_a_413(server):
    """The shared body cap answers before a byte of audio is read."""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        conn.request(
            "POST",
            "/transcribe",
            body=b"",
            headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
        )
        resp = conn.getresponse()
        assert resp.status == 413
        assert json.loads(resp.read()) == {"error": "request body too large"}
    finally:
        conn.close()


def test_empty_audio_is_refused_as_required():
    """Unit-level: base64 of zero bytes is "", which fails the required check."""
    with pytest.raises(ValueError, match="audio_b64"):
        transcribe_request({"audio_b64": "", "mime_type": "audio/wav"})


def test_padding_only_base64_is_refused_as_invalid():
    with pytest.raises(ValueError, match="not valid base64"):
        transcribe_request({"audio_b64": "====", "mime_type": "audio/wav"})


# -------------------------------------------------------- the loudness peak


def test_a_peak_below_the_speech_threshold_is_refused_before_the_model_call(
    server,
):
    """Silence transcribes as the primed wake name, so it is not transcribed.

    The exhausted ScriptedModel raises on any call, so a 400 here (rather than
    the 502 a call would produce) is the proof that nothing was paid for.
    """
    exhausted = ScriptedModel()
    previous = Handler.__dict__["transcribe_model_factory"]
    Handler.transcribe_model_factory = staticmethod(lambda: exhausted)
    try:
        status, resp = post(server, body(peak=1.5, purpose="wake"))
    finally:
        Handler.transcribe_model_factory = previous
    assert status == 400
    assert "peak" in resp["error"]
    assert "no speech" in resp["error"]


def test_a_peak_over_the_threshold_is_transcribed(server):
    status, resp = post(server, body(peak=42))
    assert status == 200
    assert resp["text"] == TRANSCRIPT


def test_an_absent_peak_keeps_the_previous_behaviour(server):
    """A caller that cannot measure loudness is not refused on a number it
    never sent."""
    status, resp = post(server, body())
    assert status == 200
    assert resp["text"] == TRANSCRIPT
    assert transcribe_request(body())[3] is None


def test_a_non_numeric_peak_is_a_400_naming_the_field(server):
    status, resp = post(server, body(peak="loud"))
    assert status == 400
    assert "peak" in resp["error"]


def test_a_boolean_peak_is_a_400_rather_than_a_silent_one(server):
    status, resp = post(server, body(peak=True))
    assert status == 400
    assert "peak" in resp["error"]


def test_an_out_of_range_peak_is_a_400_naming_the_bounds(server):
    status, resp = post(server, body(peak=999))
    assert status == 400
    assert "peak" in resp["error"]
    assert "128" in resp["error"]


# ---------------------------------------------------------- model failure


def test_model_failure_is_a_502(server):
    exhausted = ScriptedModel()  # raises ModelError: ran out of responses
    previous = Handler.__dict__["transcribe_model_factory"]
    Handler.transcribe_model_factory = staticmethod(lambda: exhausted)
    try:
        status, resp = post(server, body())
    finally:
        Handler.transcribe_model_factory = previous
    assert status == 502
    assert "error" in resp
